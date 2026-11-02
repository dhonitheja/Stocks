---
name: stock-data
description: Provider-agnostic US stock data (Finnhub, Polygon, Alpha Vantage) for research: quotes, candles, news, earnings, and locally computed indicators. Use whenever a research skill needs market data. Read-only; never places orders.
---

# Stock Data (research, read-only)

Wraps `scripts/research/stock_data.py` (JSON output). Keys come from the environment; the script also copies only the provider key variables from the project `.env` (nothing else in that file is loaded by it). Never print or open `.env`. Tickers must be plain symbols and error text has key values redacted.

```bash
python scripts/research/stock_data.py quote NVDA
python scripts/research/stock_data.py indicators NVDA      # SMA20/50/200, RSI, MACD, Bollinger, ATR, 52w, rel volume
python scripts/research/stock_data.py candles NVDA --days 120
python scripts/research/stock_data.py news NVDA --days 7
python scripts/research/stock_data.py earnings NVDA
python scripts/research/stock_data.py scan AAPL MSFT NVDA
```

Keys: `FINNHUB_API_KEY`, `POLYGON_API_KEY`, and `ALPHA_VANTAGE_KEY` (already in `.env.example`; `ALPHAVANTAGE_API_KEY` also works). With several set, the script tries providers best-first per task and falls back on 403/429/empty results. Force one with `--provider`.

## Free-tier limits (checked Nov 2026; recheck before relying on them)

| Provider | Free limit | Catches |
|---|---|---|
| Finnhub | 60 calls/min | Candle endpoint reported paid-only by some sources, so the script falls back to Polygon |
| Polygon (Massive) | 5 calls/min, end-of-day, 2y history | Snapshot/real-time is paid |
| Alpha Vantage | 25 calls/day | Errors arrive as HTTP 200 with an `Information` message; `scan` uses about 2 calls per ticker |

Keep scans under about 10 tickers on free keys. Polygon calls are paced at about 12.5s (set `POLYGON_MIN_INTERVAL=0` on a paid plan). Polygon prices are split-adjusted; Alpha Vantage daily prices are not, so indicators can differ by provider after a split. Report the exact error when every provider fails; never fill gaps from memory.

Not investment advice.
