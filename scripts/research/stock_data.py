#!/usr/bin/env python3
"""Provider-agnostic US stock data CLI (stdlib only).

Providers (set the matching env var; the first one found is used, or pass --provider):
  finnhub       FINNHUB_API_KEY
  polygon       POLYGON_API_KEY
  alphavantage  ALPHAVANTAGE_API_KEY (or ALPHA_VANTAGE_KEY, as in .env.example)

Commands (all print JSON to stdout):
  quote TICKER
  candles TICKER [--days 120]
  news TICKER [--days 7]
  earnings TICKER
  indicators TICKER [--days 250]     # computed locally from candles
  scan TICKER [TICKER ...]           # one-line snapshot per ticker

Research data only. Read-only: nothing here places orders. Not investment advice.
"""
import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import re
import time

_PROVIDER_VARS = ("FINNHUB_API_KEY", "POLYGON_API_KEY", "ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_KEY")


def today_et():
    """Today's date in US/Eastern (falls back to local time if tz data is unavailable)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return date.today()


def real_key(v):
    """False for empty values and the placeholders shipped in .env.example (your_..._here)."""
    v = (v or "").strip()
    return bool(v) and not (v.lower().startswith("your_") or v.lower().endswith("_here"))


if not os.environ.get("STOCK_DATA_NO_DOTENV"):
    try:  # copy ONLY the provider keys from the project's .env; never load the rest of the file
        from dotenv import dotenv_values
        for _k, _v in dotenv_values().items():
            if _k in _PROVIDER_VARS and real_key(_v) and not real_key(os.environ.get(_k)):
                os.environ[_k] = _v
    except ImportError:
        pass

# StockSage's .env.example already names the Alpha Vantage key ALPHA_VANTAGE_KEY; accept both spellings.
if real_key(os.environ.get("ALPHA_VANTAGE_KEY")) and not real_key(os.environ.get("ALPHAVANTAGE_API_KEY")):
    os.environ["ALPHAVANTAGE_API_KEY"] = os.environ["ALPHA_VANTAGE_KEY"]

ENV = {"finnhub": "FINNHUB_API_KEY", "polygon": "POLYGON_API_KEY",
       "alphavantage": "ALPHAVANTAGE_API_KEY"}


TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def valid_ticker(t):
    """Tickers may come from web/Reddit text; reject anything that is not a plain symbol."""
    t = t.upper()
    if not TICKER_RE.fullmatch(t):
        sys.exit(f"error: invalid ticker {t[:12]!r}")
    return t


def redact(msg):
    """Remove every configured API key value from a message before it is printed or stored."""
    msg = str(msg)
    for var in _PROVIDER_VARS + tuple(ENV.values()):
        v = os.environ.get(var)
        if v and len(v) >= 4:
            msg = msg.replace(v, "***")
    return msg


import hashlib
import stat

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "stock-agent")   # per-user, mode 0700
_STATE_FILE = os.path.join(_STATE_DIR, "stock_data_state.json")
_MAX_BYTES = 5_000_000  # refuse absurdly large responses
_MAX_SLEEP = 65.0       # never sleep longer than this, whatever a state file says


_LOCAL_STATE: dict = {}      # in-process fallback when the shared file is unusable
_WARNED_STATE = False


def _warn_state(reason):
    global _WARNED_STATE
    if not _WARNED_STATE:
        _WARNED_STATE = True
        print(f"stock_data: shared state unavailable ({reason}); pacing and 403 memory are "
              "per-process only", file=sys.stderr)


def _check_state_dir(path):
    """The state directory must be a real directory (not a symlink) that we own, private (0700).
    A directory we own with loose permissions is tightened; anything else is refused."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError("state directory is not a real directory (symlink?)")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise OSError("state directory is owned by another user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def _open_state():
    """Open the shared state file safely, or return None (callers then use the in-process fallback).
    Refuses symlinks (file or directory), non-regular files, hardlinked files, files not owned by us
    or writable by others, and directories owned by others or left group/world accessible."""
    try:
        d = os.path.dirname(_STATE_FILE)
        os.makedirs(d, mode=0o700, exist_ok=True)
        _check_state_dir(d)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(_STATE_FILE, flags, 0o600)
    except OSError as e:
        _warn_state(str(e) or type(e).__name__)
        return None
    try:
        st = os.fstat(fd)
        uid_ok = not hasattr(os, "getuid") or st.st_uid == os.getuid()
        if not (stat.S_ISREG(st.st_mode) and uid_ok and not (st.st_mode & 0o022) and st.st_nlink == 1):
            os.close(fd)
            _warn_state("state file is not a private regular file")
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _with_state(fn):
    """Run fn(state_dict) under an inter-process lock; state is saved back. Shared by all CLI runs
    (the agent fetches in parallel), so pacing and 403 memory hold across processes. If the shared
    file cannot be used, a module-level dict keeps pacing working within this process."""
    try:
        import fcntl
    except ImportError:  # Windows: best effort without a lock
        fcntl = None
    fd = _open_state()
    if fd is None:
        return fn(_LOCAL_STATE)
    try:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as e:               # e.g. ENOLCK on network filesystems
                _warn_state(f"cannot lock state file: {e}")
                return fn(_LOCAL_STATE)
        raw = b""
        while len(raw) < 1_000_000:           # an oversized file is treated as corrupt (reset below)
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        try:
            state = json.loads(raw or b"{}")
            if not isinstance(state, dict):
                state = {}
        except ValueError:
            state = {}
        result = fn(state)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(state).encode())   # tiny; a short write would just reset the state
        return result
    finally:
        os.close(fd)  # closing releases the flock


def _num(v, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


class _TooLarge(Exception):
    pass


def _polygon_wait():
    """Reserve the next Polygon slot (free plan: 5 calls/min). POLYGON_MIN_INTERVAL=0 on paid plans.
    The wait is clamped, so a poisoned or stale state file can never hang the CLI."""
    gap = min(max(_num(os.environ.get("POLYGON_MIN_INTERVAL", "12.5"), 12.5), 0.0), 60.0)
    if gap <= 0:
        return 0.0

    def reserve(state):
        now = time.time()
        nxt = _num(state.get("polygon_next"))
        if nxt > now + 6 * gap + 1:              # impossible via normal use (max is now + 6*gap): corrupt
            nxt = 0.0
        slot = max(now, nxt)
        if slot - now > 5 * gap:                 # 5 calls already queued: do not stack a 6th on top
            return None
        state["polygon_next"] = slot + gap
        return min(slot - now, _MAX_SLEEP)

    wait = _with_state(reserve)
    if wait is None:
        raise RuntimeError("polygon: rate-limit queue full (5 calls already waiting); try later "
                           "or use another provider")
    return wait


def _key_id(p="polygon"):
    return hashlib.sha256(os.environ.get(ENV[p], "").encode()).hexdigest()[:12]


def snapshot_forbidden():
    """True if Polygon's snapshot endpoint answered 403 recently FOR THIS API KEY (free plan)."""
    kid = _key_id()

    def check(st):
        d = st.get("snapshot_403", {})
        return isinstance(d, dict) and time.time() < _num(d.get(kid))

    return _with_state(check)


def note_snapshot_forbidden():
    kid = _key_id()

    def mark(st):
        d = st.get("snapshot_403")
        now = time.time()
        d = d if isinstance(d, dict) else {}
        st["snapshot_403"] = {k: v for k, v in d.items() if _num(v) > now}   # drop expired entries
        st["snapshot_403"][kid] = now + 6 * 3600     # a different key/plan is not affected

    _with_state(mark)


def _throttle(url):
    if "polygon.io" in url:
        wait = _polygon_wait()
        if wait > 0:
            time.sleep(wait)


def _retry_after(e):
    try:
        return min(max(float(e.headers.get("Retry-After", "")), 1.0), 65.0)
    except (TypeError, ValueError, AttributeError):
        return 12.0


def http_json(url, params, _retry=True):
    _throttle(url)
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "stock-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise _TooLarge()
            d = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError("403: endpoint needs a paid plan on this provider") from None
        if e.code == 429:
            if _retry:
                time.sleep(_retry_after(e))
                return http_json(url, params, _retry=False)
            raise RuntimeError("429: rate limited") from None
        raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}") from None
    except Exception as e:  # never let a URL (which carries the key) leak through an exception
        if isinstance(e, _TooLarge):
            raise RuntimeError(f"response too large from {urllib.parse.urlparse(url).netloc}") from None
        raise RuntimeError(f"{type(e).__name__} calling {urllib.parse.urlparse(url).netloc}") from None
    # Alpha Vantage returns HTTP 200 with these keys when limited or premium-only
    if isinstance(d, dict):
        for k in ("Information", "Note", "Error Message"):
            if k in d:
                raise RuntimeError(f"alphavantage: {d[k][:160]}")
    return d


def key(p):
    return os.environ[ENV[p]]


# ---------- candles (normalized: list of {date, o, h, l, c, v}, oldest first) ----------
def candles(p, t, days):
    end = today_et()
    start = end - timedelta(days=int(days * 1.6) + 5)  # calendar days to cover trading days
    out = []
    if p == "finnhub":
        d = http_json("https://finnhub.io/api/v1/stock/candle", {
            "symbol": t, "resolution": "D",
            "from": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
            "to": int(datetime.combine(end, datetime.min.time(), timezone.utc).timestamp()),
            "token": key(p)})
        if d.get("s") == "ok":
            for i, ts in enumerate(d["t"]):
                out.append({"date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
                            "o": d["o"][i], "h": d["h"][i], "l": d["l"][i], "c": d["c"][i], "v": d["v"][i]})
    elif p == "polygon":
        d = http_json(f"https://api.polygon.io/v2/aggs/ticker/{urllib.parse.quote(t, safe='')}/range/1/day/{start}/{end}",
                      {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": key(p)})
        for b in d.get("results", []):
            out.append({"date": datetime.fromtimestamp(b["t"] / 1000, timezone.utc).date().isoformat(),
                        "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    else:
        d = http_json("https://www.alphavantage.co/query", {
            "function": "TIME_SERIES_DAILY", "symbol": t, "outputsize": "full", "apikey": key(p)})
        for k, v in sorted(d.get("Time Series (Daily)", {}).items()):
            if k >= start.isoformat():
                out.append({"date": k, "o": float(v["1. open"]), "h": float(v["2. high"]),
                            "l": float(v["3. low"]), "c": float(v["4. close"]), "v": float(v["5. volume"])})
    return out[-days:]


def quote(p, t):
    if p == "finnhub":
        d = http_json("https://finnhub.io/api/v1/quote", {"symbol": t, "token": key(p)})
        if not d.get("c"):  # Finnhub returns zeros/nulls for unknown symbols
            return None
        return {"ticker": t, "price": d.get("c"), "change": d.get("d"), "change_pct": d.get("dp"),
                "high": d.get("h"), "low": d.get("l"), "open": d.get("o"), "prev_close": d.get("pc")}
    if p == "polygon":
        try:
            if snapshot_forbidden():
                raise RuntimeError("403: snapshot known to need a paid plan")
            try:
                d = http_json(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{urllib.parse.quote(t, safe='')}",
                              {"apiKey": key(p)}).get("ticker", {})
            except RuntimeError as e:
                if str(e).startswith("403"):
                    note_snapshot_forbidden()
                raise
        except RuntimeError:  # snapshot is paid; free plan is end-of-day, so use last daily bars
            bars = candles(p, t, 5)
            if len(bars) < 2:
                raise
            l, pv = bars[-1], bars[-2]
            return {"ticker": t, "price": l["c"], "change": l["c"] - pv["c"],
                    "change_pct": (l["c"] / pv["c"] - 1) * 100, "high": l["h"], "low": l["l"],
                    "open": l["o"], "prev_close": pv["c"], "note": f"end-of-day bar {l['date']}"}
        if not d:
            return None
        day, prev = d.get("day", {}), d.get("prevDay", {})
        return {"ticker": t, "price": day.get("c") or d.get("lastTrade", {}).get("p"),
                "change": d.get("todaysChange"), "change_pct": d.get("todaysChangePerc"),
                "high": day.get("h"), "low": day.get("l"), "open": day.get("o"),
                "prev_close": prev.get("c")}
    d = http_json("https://www.alphavantage.co/query",
                  {"function": "GLOBAL_QUOTE", "symbol": t, "apikey": key(p)}).get("Global Quote", {})
    if not d.get("05. price"):
        return None
    return {"ticker": t, "price": float(d.get("05. price", 0) or 0),
            "change": float(d.get("09. change", 0) or 0),
            "change_pct": float((d.get("10. change percent", "0%") or "0%").rstrip("%")),
            "high": float(d.get("03. high", 0) or 0), "low": float(d.get("04. low", 0) or 0),
            "open": float(d.get("02. open", 0) or 0), "prev_close": float(d.get("08. previous close", 0) or 0)}


def news(p, t, days):
    start = (today_et() - timedelta(days=days)).isoformat()
    if p == "finnhub":
        d = http_json("https://finnhub.io/api/v1/company-news",
                      {"symbol": t, "from": start, "to": today_et().isoformat(), "token": key(p)})
        return [{"time": datetime.fromtimestamp(x["datetime"], timezone.utc).isoformat(),
                 "headline": x["headline"], "source": x["source"], "url": x["url"]} for x in d[:25]]
    if p == "polygon":
        d = http_json("https://api.polygon.io/v2/reference/news",
                      {"ticker": t, "published_utc.gte": start, "limit": 25, "apiKey": key(p)})
        return [{"time": x["published_utc"], "headline": x["title"],
                 "source": x.get("publisher", {}).get("name"), "url": x["article_url"]}
                for x in d.get("results", [])]
    d = http_json("https://www.alphavantage.co/query",
                  {"function": "NEWS_SENTIMENT", "tickers": t, "limit": 25, "apikey": key(p)})
    return [{"time": x["time_published"], "headline": x["title"], "source": x["source"],
             "url": x["url"], "sentiment": x.get("overall_sentiment_label")} for x in d.get("feed", [])]


def earnings(p, t):
    if p == "finnhub":
        d = http_json("https://finnhub.io/api/v1/calendar/earnings",
                      {"symbol": t, "from": today_et().isoformat(),
                       "to": (today_et() + timedelta(days=120)).isoformat(), "token": key(p)})
        return {"upcoming": d.get("earningsCalendar", []), "window_days": 120,
                "note": "An empty list means none found in this provider's data, not a guarantee."}
    if p == "alphavantage":
        d = http_json("https://www.alphavantage.co/query", {"function": "EARNINGS", "symbol": t, "apikey": key(p)})
        return {"upcoming": None, "reported_history": d.get("quarterlyEarnings", [])[:4],
                "note": "HISTORICAL only; Alpha Vantage here gives no upcoming date. Do not treat as upcoming."}
    raise RuntimeError("polygon has no earnings calendar; use FINNHUB_API_KEY")


# ---------- indicators (local math, provider independent) ----------
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n=14):
    if len(vals) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = vals[i] - vals[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        ch = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(c, n=14):
    if len(c) <= n:
        return None
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
           for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def indicators(c):
    if len(c) < 30:
        return {"error": "not enough candles", "count": len(c)}
    closes = [x["c"] for x in c]
    vols = [x["v"] for x in c]
    macd_line = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    signal = ema_series(macd_line, 9)
    last = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    sd = math.sqrt(sum((x - s20) ** 2 for x in closes[-20:]) / 20)
    hi52 = max(x["h"] for x in c[-252:])
    lo52 = min(x["l"] for x in c[-252:])
    avgv = sma(vols[:-1], 20)  # prior 20 bars, so today's volume is not in its own average
    a = atr(c)
    r = lambda v, d=2: None if v is None else round(v, d)
    return {
        "last_close": r(last), "sma20": r(s20), "sma50": r(s50), "sma200": r(s200),
        "above_sma50": None if s50 is None else last > s50,
        "above_sma200": None if s200 is None else last > s200,
        "rsi14": r(rsi(closes)), "macd": r(macd_line[-1], 3), "macd_signal": r(signal[-1], 3),
        "macd_hist": r(macd_line[-1] - signal[-1], 3),
        "macd_hist_prev": r(macd_line[-2] - signal[-2], 3),
        "bollinger_upper": r(s20 + 2 * sd), "bollinger_lower": r(s20 - 2 * sd),
        "atr14": r(a), "atr_pct": r(a / last * 100) if a else None,
        "high_52w": r(hi52), "low_52w": r(lo52), "pct_from_52w_high": r((last / hi52 - 1) * 100),
        "avg_vol_20d": r(avgv, 0), "rel_volume": r(vols[-1] / avgv) if avgv else None,
        "support_20d_low": r(min(x["l"] for x in c[-20:])),
        "resistance_20d_high": r(max(x["h"] for x in c[-20:])),
        "candles_used": len(c),
    }


# Best-first provider order per task, based on free-tier realities:
# Finnhub free: 60 calls/min but candles are paid; Polygon free: 5/min, end-of-day, 2y history;
# Alpha Vantage free: 25 calls/day total, so it goes last.
ORDER = {"quote": ["finnhub", "polygon", "alphavantage"],
         "candles": ["polygon", "finnhub", "alphavantage"],
         "news": ["finnhub", "polygon", "alphavantage"],
         "earnings": ["finnhub", "alphavantage", "polygon"]}


def run(task, fn, forced=None):
    """Try configured providers in order; return (provider, result). Errors are collected, not hidden."""
    avail = [forced] if forced else [x for x in ORDER[task] if real_key(os.environ.get(ENV[x]))]
    if not avail:
        sys.exit("error: no API key found. Set FINNHUB_API_KEY, POLYGON_API_KEY or ALPHAVANTAGE_API_KEY")
    errors = []
    for p in avail:
        if not real_key(os.environ.get(ENV[p])):
            sys.exit(f"error: {ENV[p]} is not set")
        try:
            res = fn(p)
            if res not in (None, [], {}):
                return p, res
            errors.append(f"{p}: empty result")
        except Exception as e:
            errors.append(f"{p}: {redact(e)}")
    sys.exit(redact("error: all providers failed -> " + "; ".join(errors)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["quote", "candles", "news", "earnings", "indicators", "scan"])
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--provider", choices=list(ENV))
    ap.add_argument("--days", type=int)
    a = ap.parse_args()
    days = a.days
    if days is not None and not 1 <= days <= 5000:
        sys.exit("error: --days must be between 1 and 5000")
    tickers = [valid_ticker(t) for t in a.tickers]
    if a.cmd != "scan" and len(tickers) > 1:
        sys.exit("error: this command takes one ticker; use scan for several")
    t, f = tickers[0], a.provider
    if a.cmd == "quote":
        p, res = run("quote", lambda p: quote(p, t), f)
    elif a.cmd == "candles":
        p, res = run("candles", lambda p: candles(p, t, days or 120), f)
    elif a.cmd == "news":
        p, res = run("news", lambda p: news(p, t, days or 7), f)
    elif a.cmd == "earnings":
        p, res = run("earnings", lambda p: earnings(p, t), f)
    elif a.cmd == "indicators":
        p, res = run("candles", lambda p: candles(p, t, days or 250), f)
        res = indicators(res)
    else:
        p, res = "mixed", []
        for tk in tickers:
            try:
                qp, q = run("quote", lambda p: quote(p, tk), f)
                cp, c = run("candles", lambda p: candles(p, tk, 250), f)
                res.append({"ticker": tk, "quote_provider": qp, "candle_provider": cp,
                            **q, "ind": indicators(c)})
            except (SystemExit, Exception) as e:  # keep scanning if one ticker fails (run() exits on failure)
                res.append({"ticker": tk, "error": redact(e)})
    print(json.dumps({"provider": p, "data": res}, indent=2))


if __name__ == "__main__":
    main()
