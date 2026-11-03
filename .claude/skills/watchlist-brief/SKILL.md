---
name: watchlist-brief
description: Daily pre-market or end-of-day digest of the StockSage watchlist: biggest movers, news, upcoming earnings, technical flags. Use for "morning brief" or "check my watchlist". Research only.
---

# Watchlist Brief

Research only. No buy/sell calls, no orders.

## Steps

1. Tickers: `python scripts/research/repo_signals.py --watchlist` (reads `market.watchlist` in `config/settings.yaml`, so there is one list to maintain). If the user names tickers, use those.
2. `python scripts/research/stock_data.py scan <tickers>`; on free keys scan in batches of 8-10 (see `stock-data` limits).
3. For tickers with |day change| above 3% or relative volume above 2, fetch `news T --days 2`.
4. `earnings T` for each; keep only entries in the `upcoming` list within 10 days. `reported_history` is past data. An empty `upcoming` means "none found in provider data" (coverage can be missing), and `upcoming: null` (Alpha Vantage) means unknown: say so and suggest checking the company's investor-relations page.
5. Optional: run `repo_signals.py T` on the 3 most notable names to show StockSage's own read.

## Output

1. Market tone in one line (only from data fetched; SPY and QQQ are on the watchlist and serve as the benchmark).
2. **Needs attention** (max 5): ticker, move %, why, key level.
3. **Technical flags**: RSI extremes (the repo uses 32/68 thresholds), SMA50/200 crosses, 20-day highs/lows, MACD flips.
4. **Earnings ahead**, soonest first.
5. **Quiet**: the rest in one line.

Under one screen. Not investment advice.
