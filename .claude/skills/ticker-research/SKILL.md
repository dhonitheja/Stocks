---
name: ticker-research
description: Research one US stock for a swing/short-term setup: quote, technicals, StockSage's own signal, news, earnings date, sentiment, then a one-page brief. Use for "research NVDA" or "what's going on with AAPL". Research only; no buy/sell calls.
---

# Ticker Research

Research only. Never give buy/sell/hold calls or targets, and never place or suggest placing orders. End with the disclaimer.

## Steps

1. Normalize the ticker (uppercase, US listing). Ask once if ambiguous.
2. Fetch in parallel with the `stock-data` skill: `quote`, `indicators`, `news --days 7`, `earnings`.
3. Get the repo's own read, so research matches what the agent sees:
   `python scripts/research/repo_signals.py T --fundamentals`
   This returns StockSage's `TechnicalAnalyzer` output and the `FundamentalScorer` 0-1 score. Report its trend, RSI/MACD state, EMAs, support/resistance, pattern and confidence. Do NOT quote its `action` field (BUY/SELL/HOLD): that is a trading-loop input, not research output. If asked, call it the model's "lean" (bullish / bearish / neutral). If `yfinance` or other deps are missing, skip it and say so.
4. Run the `sentiment-scan` skill.
5. Catalysts: `mcporter call exa.web_search_exa query="T 8-K guidance downgrade this week" numResults=5` (agent-reach), if available.

## Brief (one screen)

- **Snapshot**: price, day %, relative volume, distance from 52w high.
- **Trend**: price vs SMA20/50/200 and EMAs, MACD direction, RSI zone.
- **Agent's view**: the repo analyzer's trend, lean and confidence, noting where it agrees or conflicts with the indicators above.
- **Levels**: support, resistance, ATR% for volatility.
- **Catalysts**: next earnings date (flag if within 7 days), top 3 news items with source and date.
- **Sentiment**: one line with sample size.
- **Bull case / bear case**: two bullets each, grounded in fetched data.
- **What would change the picture**: 2 levels or events.

Cite provider and timestamp for every number. Not investment advice.
