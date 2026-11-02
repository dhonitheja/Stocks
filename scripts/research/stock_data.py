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
if not os.environ.get("STOCK_DATA_NO_DOTENV"):
    try:  # copy ONLY the provider keys from the project's .env; never load the rest of the file
        from dotenv import dotenv_values
        for _k, _v in dotenv_values().items():
            if _k in _PROVIDER_VARS and _v and not os.environ.get(_k):
                os.environ[_k] = _v
    except ImportError:
        pass

# StockSage's .env.example already names the Alpha Vantage key ALPHA_VANTAGE_KEY; accept both spellings.
if os.environ.get("ALPHA_VANTAGE_KEY") and not os.environ.get("ALPHAVANTAGE_API_KEY"):
    os.environ["ALPHAVANTAGE_API_KEY"] = os.environ["ALPHA_VANTAGE_KEY"]

ENV = {"finnhub": "FINNHUB_API_KEY", "polygon": "POLYGON_API_KEY",
       "alphavantage": "ALPHAVANTAGE_API_KEY"}


def pick_provider(requested=None):
    if requested:
        if not os.environ.get(ENV[requested]):
            sys.exit(f"error: {ENV[requested]} is not set")
        return requested
    for name, var in ENV.items():
        if os.environ.get(var):
            return name
    sys.exit("error: no API key found. Set FINNHUB_API_KEY, POLYGON_API_KEY or ALPHAVANTAGE_API_KEY")


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


_last_call = {}


def _throttle(url):
    # Polygon free plan allows 5 calls/min. Set POLYGON_MIN_INTERVAL=0 on a paid plan.
    if "polygon.io" in url:
        gap = float(os.environ.get("POLYGON_MIN_INTERVAL", "12.5"))
        wait = _last_call.get("polygon", 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call["polygon"] = time.time()


def http_json(url, params, _retry=True):
    _throttle(url)
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "stock-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError("403: endpoint needs a paid plan on this provider") from None
        if e.code == 429:
            if _retry:
                time.sleep(12)
                return http_json(url, params, _retry=False)
            raise RuntimeError("429: rate limited") from None
        raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}") from None
    except Exception as e:  # never let a URL (which carries the key) leak through an exception
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
    end = date.today()
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
            d = http_json(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{urllib.parse.quote(t, safe='')}",
                          {"apiKey": key(p)}).get("ticker", {})
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
    start = (date.today() - timedelta(days=days)).isoformat()
    if p == "finnhub":
        d = http_json("https://finnhub.io/api/v1/company-news",
                      {"symbol": t, "from": start, "to": date.today().isoformat(), "token": key(p)})
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
                      {"symbol": t, "from": date.today().isoformat(),
                       "to": (date.today() + timedelta(days=120)).isoformat(), "token": key(p)})
        return {"upcoming": d.get("earningsCalendar", []), "window_days": 120}
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
    avail = [forced] if forced else [x for x in ORDER[task] if os.environ.get(ENV[x])]
    if not avail:
        sys.exit("error: no API key found. Set FINNHUB_API_KEY, POLYGON_API_KEY or ALPHAVANTAGE_API_KEY")
    errors = []
    for p in avail:
        if not os.environ.get(ENV[p]):
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
    tickers = [valid_ticker(t) for t in a.tickers]
    if a.cmd != "scan" and len(tickers) > 1:
        sys.exit("error: this command takes one ticker; use scan for several")
    t, f = tickers[0], a.provider
    if a.cmd == "quote":
        p, res = run("quote", lambda p: quote(p, t), f)
    elif a.cmd == "candles":
        p, res = run("candles", lambda p: candles(p, t, a.days or 120), f)
    elif a.cmd == "news":
        p, res = run("news", lambda p: news(p, t, a.days or 7), f)
    elif a.cmd == "earnings":
        p, res = run("earnings", lambda p: earnings(p, t), f)
    elif a.cmd == "indicators":
        p, res = run("candles", lambda p: candles(p, t, a.days or 250), f)
        res = indicators(res)
    else:
        p, res = "mixed", []
        for tk in tickers:
            try:
                qp, q = run("quote", lambda p: quote(p, tk), f)
                cp, c = run("candles", lambda p: candles(p, tk, 250), f)
                res.append({"ticker": tk, "quote_provider": qp, "candle_provider": cp,
                            **q, "ind": indicators(c)})
            except (SystemExit, Exception) as e:  # keep scanning if one ticker fails
                res.append({"ticker": tk, "error": redact(e)})
    print(json.dumps({"provider": p, "data": res}, indent=2))


if __name__ == "__main__":
    main()
