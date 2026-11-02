---
name: idea-screener
description: Build a shortlist of US stock setups to research (oversold bounce, breakout, volume surge, pre-earnings run), by default from the StockSage watchlist. Use for "find setups" or "what's breaking out". Output is a research list, not recommendations.
---

# Idea Screener

## Universe

1. User-supplied list or sector, else
2. The StockSage watchlist (`repo_signals.py --watchlist`), else
3. Trending names via agent-reach (web search for unusual volume, Reddit "stocks to watch"); verify each is a US listing.
Cap at 25 tickers. Polygon's free plan allows 5 calls/min and the script paces itself (about 12s per call, so a 25-ticker scan takes many minutes there); prefer Finnhub for quotes and keep free-key scans to 8-10 tickers.

## Steps

1. `python scripts/research/stock_data.py scan <universe>`.
2. Apply one preset to the `ind` fields:
   - **Oversold bounce**: rsi14 < 35, above_sma200, macd_hist > macd_hist_prev.
   - **Breakout**: last_close within 1% of resistance_20d_high, rel_volume > 1.5, above_sma50.
   - **Volume surge**: rel_volume > 2.5 and |change_pct| > 3.
   - **Pre-earnings run**: above_sma50 and an upcoming earnings date within 14 days. `scan` does not include earnings, so run `earnings T` only on tickers that already pass the other filters. Only the `upcoming` list counts; `reported_history` is past data.
3. For the top 5 hits run `python scripts/research/repo_signals.py T` and note whether StockSage's own analyzer leans the same way (bullish / bearish / neutral, with its confidence versus `min_confidence_score` in `config/settings.yaml`). Never quote its BUY/SELL string. Disagreement is useful information; report it.
4. Add one catalyst line per hit from `news T --days 3`.

## Output

Table: ticker, price, day %, RSI, rel volume, ATR%, setup, repo-analyzer view, catalyst, next earnings. State the preset and universe size. A short list is fine; never loosen thresholds to pad it. Suggest `ticker-research` for any hit.

Research shortlist only: no buy/sell calls and no orders. Not investment advice.
