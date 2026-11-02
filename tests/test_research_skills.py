"""Offline tests for the read-only research skills (scripts/research).

No network: provider HTTP is monkeypatched. Includes a guard that the research
scripts can never reach the broker or execution path.
"""
import ast
import importlib.util
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
RESEARCH = ROOT / "scripts" / "research"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, RESEARCH / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(RESEARCH))
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sd(monkeypatch):
    monkeypatch.setenv("STOCK_DATA_NO_DOTENV", "1")  # tests must not depend on a developer's .env
    monkeypatch.setenv("POLYGON_MIN_INTERVAL", "0")
    mod = _load("stock_data")
    for var in mod.ENV.values():
        monkeypatch.delenv(var, raising=False)
    return mod


def _candles(n=260, seed=1):
    random.seed(seed)
    p, out = 100.0, []
    for i in range(n):
        p *= 1 + random.uniform(-0.02, 0.021)
        day = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
        out.append({"date": day, "o": p, "h": p * 1.01, "l": p * 0.99, "c": p,
                    "v": 1e6 + random.random() * 1e5})
    return out


def test_indicators_are_sane(sd):
    r = sd.indicators(_candles())
    assert 0 < r["rsi14"] < 100
    assert r["bollinger_upper"] > r["bollinger_lower"]
    assert r["sma200"] is not None
    assert r["high_52w"] >= r["low_52w"]


def test_rsi_monotonic_rise_is_100(sd):
    assert sd.rsi([float(i) for i in range(1, 40)]) == 100.0


def test_indicators_need_enough_data(sd):
    assert "error" in sd.indicators(_candles(10))


def test_no_keys_exits_with_clear_message(sd):
    with pytest.raises(SystemExit) as e:
        sd.run("quote", lambda p: {})
    assert "no API key" in str(e.value)


def test_falls_back_when_first_provider_is_paid_only(sd, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setenv("POLYGON_API_KEY", "y")

    def fake(url, params):
        if "finnhub" in url:
            raise RuntimeError("403: endpoint needs a paid plan on this provider")
        return {"results": [{"t": 1_700_000_000_000 + i * 86_400_000, "o": 1, "h": 2,
                             "l": 1, "c": 1.5, "v": 10} for i in range(3)]}

    monkeypatch.setattr(sd, "http_json", fake)
    provider, res = sd.run("candles", lambda p: sd.candles(p, "AAPL", 3))
    assert provider == "polygon" and len(res) == 3


def test_finnhub_unknown_symbol_triggers_fallback(sd, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setenv("POLYGON_API_KEY", "y")

    def fake(url, params):
        if "snapshot" in url:
            raise RuntimeError("403: paid")
        if "aggs" in url:
            return {"results": [{"t": 1_700_000_000_000 + i * 86_400_000, "o": 10 + i,
                                 "h": 11 + i, "l": 9 + i, "c": 10 + i, "v": 1} for i in range(5)]}
        return {"c": 0}  # Finnhub's answer for an unknown symbol

    monkeypatch.setattr(sd, "http_json", fake)
    provider, q = sd.run("quote", lambda p: sd.quote(p, "ZZZZ"))
    assert provider == "polygon"
    assert "end-of-day" in q["note"]


def test_alpha_vantage_alias_env_var(monkeypatch):
    import os
    monkeypatch.setenv("STOCK_DATA_NO_DOTENV", "1")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "tmp")
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")  # registers cleanup so the module's write cannot leak
    monkeypatch.setenv("ALPHA_VANTAGE_KEY", "abc")
    mod = _load("stock_data")
    assert os.environ["ALPHAVANTAGE_API_KEY"] == "abc"
    assert mod.ENV["alphavantage"] == "ALPHAVANTAGE_API_KEY"
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")


def test_research_scripts_cannot_reach_trading_path():
    """Import the bridge in a clean interpreter and assert no broker/execution module loads."""
    import subprocess
    code = (
        "import sys, importlib.util\n"
        f"sys.path[:0] = [{str(RESEARCH)!r}, {str(ROOT)!r}]\n"
        "import repo_signals, stock_data\n"
        "from agent.models.technical_indicators import TechnicalAnalyzer\n"
        "bad = [m for m in sys.modules if m.startswith(('agent.brokers', 'agent.core.execution_engine',"
        " 'alpaca', 'robin_stocks'))]\n"
        "print('BAD:' + ','.join(bad)) if bad else print('CLEAN')\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         env={"STOCK_DATA_NO_DOTENV": "1", "PATH": "/usr/bin:/bin"}, cwd=ROOT)
    if "No module named" in out.stderr:
        pytest.skip("optional dependency missing: " + out.stderr.strip().splitlines()[-1])
    assert out.stdout.strip() == "CLEAN", out.stdout + out.stderr


def test_repo_signals_bridge_end_to_end_with_stubbed_data(monkeypatch):
    pytest.importorskip("loguru")
    monkeypatch.setenv("STOCK_DATA_NO_DOTENV", "1")
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    rs = _load("repo_signals")
    df = rs.to_frame(_candles())
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    monkeypatch.setattr(rs.stock_data, "candles", lambda p, t, d: _candles())
    provider, sig = rs.repo_technical("TEST", forced="finnhub")
    assert provider == "finnhub"
    assert sig["symbol"] == "TEST" and 0.0 <= sig["confidence"] <= 1.0


def test_watchlist_comes_from_settings():
    pytest.importorskip("yaml")
    rs = _load("repo_signals")
    wl = rs.watchlist()
    assert "AAPL" in wl and "SPY" in wl


# ---- regressions from code review -------------------------------------------------

def test_invalid_ticker_is_rejected(sd):
    for bad in ["AA PL", "AAPL\nX", "../x", "", "A" * 20, "aapl;rm"]:
        with pytest.raises(SystemExit):
            sd.valid_ticker(bad)
    assert sd.valid_ticker("brk.b") == "BRK.B"


def test_api_keys_are_redacted_from_errors(sd, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "SECRETKEY123")
    with pytest.raises(SystemExit) as e:
        sd.run("candles", lambda p: (_ for _ in ()).throw(RuntimeError("bad url ...apiKey=SECRETKEY123")))
    assert "SECRETKEY123" not in str(e.value)
    assert "***" in str(e.value)


def test_http_errors_never_echo_the_url(sd, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "SECRETKEY123")

    def boom(*a, **k):
        raise ValueError("URL can't contain control characters: apiKey=SECRETKEY123")

    monkeypatch.setattr(sd.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as e:
        sd.http_json("https://api.polygon.io/v2/x", {"apiKey": "SECRETKEY123"})
    assert "SECRETKEY123" not in str(e.value)


def test_empty_quotes_are_failures_not_data(sd, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "k")
    monkeypatch.setenv("POLYGON_API_KEY", "k2")
    monkeypatch.setattr(sd, "http_json", lambda url, params: {"Global Quote": {}, "ticker": {}})
    assert sd.quote("alphavantage", "AAPL") is None
    assert sd.quote("polygon", "AAPL") is None


def test_earnings_empty_upcoming_does_not_fall_back_to_history(sd, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "y")

    def fake(url, params):
        if "finnhub" in url:
            return {"earningsCalendar": []}
        return {"quarterlyEarnings": [{"reportedDate": "2020-01-01"}]}

    monkeypatch.setattr(sd, "http_json", fake)
    provider, res = sd.run("earnings", lambda p: sd.earnings(p, "AAPL"))
    assert provider == "finnhub" and res["upcoming"] == []


def test_alpha_vantage_earnings_are_labeled_historical(sd, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "y")
    monkeypatch.setattr(sd, "http_json", lambda u, p: {"quarterlyEarnings": [{"reportedDate": "2020-01-01"}]})
    res = sd.earnings("alphavantage", "AAPL")
    assert res["upcoming"] is None and "HISTORICAL" in res["note"]


def test_polygon_earnings_raises(sd):
    with pytest.raises(RuntimeError):
        sd.earnings("polygon", "AAPL")


def test_alpha_vantage_information_message_is_an_error(sd, monkeypatch):
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return b'{"Information": "premium endpoint"}'

    monkeypatch.setattr(sd.urllib.request, "urlopen", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="alphavantage"):
        sd.http_json("https://www.alphavantage.co/query", {})


def test_indicator_values_against_known_numbers(sd):
    # constant price: no gains or losses -> RSI 100 by convention here, zero ATR range with flat bars
    flat = [{"date": str(i), "o": 10, "h": 10, "l": 10, "c": 10, "v": 100} for i in range(60)]
    r = sd.indicators(flat)
    assert r["sma20"] == 10 and r["atr14"] == 0 and r["macd"] == 0
    # known RSI: 14 gains of +1 then one -1 -> avg gain ~ (14-0)/14 smoothed
    vals = [float(i) for i in range(15)] + [13.0]
    assert 80 < sd.rsi(vals) < 95
    r2 = sd.indicators(_candles())
    assert "macd_hist_prev" in r2
    # rel_volume excludes the current bar from its own average
    cs = _candles()
    cs[-1]["v"] = 1e9
    assert sd.indicators(cs)["rel_volume"] > 100
