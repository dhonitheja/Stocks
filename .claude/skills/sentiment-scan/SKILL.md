---
name: sentiment-scan
description: Measure retail and news sentiment on a US stock from provider news, the web, Reddit and X (via agent-reach). Use for "buzz on TSLA" or as a step of ticker-research. Read-only.
---

# Sentiment Scan

Depends on the `agent-reach` skill for web, Reddit and X. Announce "using agent-reach, platform X via backend Y" before each source, and run `agent-reach doctor --json` first for login-backed platforms. Read-only: never post, comment, or log in for the user.

## Steps

1. Provider news: `python scripts/research/stock_data.py news T --days 7`.
2. Web: `mcporter call exa.web_search_exa query="T stock news analysis" numResults=8`.
3. Reddit: `rdt search "T" --limit 15` (or `opencli reddit search "T" -f yaml`); r/wallstreetbets, r/stocks, r/investing, r/options.
4. X: `twitter search '$NVDA' -n 15` (single quotes, with the real ticker in place of NVDA, so the shell does not expand the cashtag to an empty string) if configured; skip if doctor shows no backend.
5. Label each item bullish, bearish or neutral from its text and count them.

## Output

- Per source: n items and bull/bear/neutral counts.
- Overall lean with sample size, e.g. "leans bullish, 9 of 22 items, low confidence".
- Two representative items per side, paraphrased, with links.
- Themes (earnings, product, squeeze, regulation).

Small or single-source samples are "low confidence". Flag copy-pasted promotion. Sentiment is context, not a prediction. The agent's own FinBERT model (`agent/models/sentiment_model.py`) is heavy; do not load it for a quick scan. Not investment advice.
