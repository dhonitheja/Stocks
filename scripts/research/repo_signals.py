#!/usr/bin/env python3
"""Bridge between the research skills and StockSage's own analytics. Read-only.

Reuses code the agent already has so research and trading views agree:
  - agent.models.technical_indicators.TechnicalAnalyzer  (repo's own BUY/SELL/HOLD read)
  - agent.models.fundamental_scorer.FundamentalScorer     (yfinance 0-1 score, optional)
  - config/settings.yaml market.watchlist                 (single source of truth for tickers)

Usage:
  python scripts/research/repo_signals.py --watchlist
  python scripts/research/repo_signals.py AAPL [--fundamentals]

This script never imports brokers or the execution engine and never places orders.
The signal it prints is context for a human, not an instruction to trade.
"""
import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import stock_data  # noqa: E402


def watchlist():
    import yaml
    with open(ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["market"]["watchlist"]


def to_frame(candles):
    import pandas as pd
    df = pd.DataFrame(candles).rename(
        columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df.index = pd.to_datetime(df.pop("date"))
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _scrub_env_after(importer):
    """agent.config calls load_dotenv(), which would put the whole .env (broker, Telegram, DB
    secrets) into this process's environment. Afterwards remove the names that the .env file
    contributed, except the data-provider keys. This protects child processes and env dumps only:
    agent.config has already read those values into its own module-level variables."""
    try:
        from dotenv import dotenv_values
        dotenv_names = set(dotenv_values().keys())
    except Exception:
        dotenv_names = None          # cannot tell which names came from .env: fail closed below
    before = set(os.environ)
    result = importer()
    for k in set(os.environ) - before:
        if k in stock_data._PROVIDER_VARS:
            continue
        if dotenv_names is None or k in dotenv_names:
            del os.environ[k]
    return result


def repo_technical(ticker, days=250, forced=None):
    def _imp():
        from agent.models.technical_indicators import TechnicalAnalyzer
        return TechnicalAnalyzer
    TechnicalAnalyzer = _scrub_env_after(_imp)
    provider, candles = stock_data.run("candles", lambda p: stock_data.candles(p, ticker, days), forced)
    sig = TechnicalAnalyzer().analyze(ticker, to_frame(candles))
    return provider, (dataclasses.asdict(sig) if sig else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--watchlist", action="store_true", help="print tickers from config/settings.yaml")
    ap.add_argument("--fundamentals", action="store_true", help="add FundamentalScorer score (needs yfinance)")
    ap.add_argument("--provider", choices=list(stock_data.ENV))
    a = ap.parse_args()
    if a.watchlist:
        print(json.dumps({"watchlist": watchlist()}))
        return
    if not a.ticker:
        ap.error("ticker required (or use --watchlist)")
    t = stock_data.valid_ticker(a.ticker)
    provider, tech = repo_technical(t, forced=a.provider)
    out = {"ticker": t, "candle_provider": provider, "repo_technical": tech,
           "note": "Research context only. Not an order and not investment advice."}
    if a.fundamentals:
        def _imp2():
            from agent.models.fundamental_scorer import FundamentalScorer
            return FundamentalScorer
        out["fundamental_score_0_to_1"] = _scrub_env_after(_imp2)().score(t)
        out["fundamental_note"] = "0.5 can mean neutral OR unknown (data missing)."
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
