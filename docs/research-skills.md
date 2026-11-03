# Research skills (read-only)

Claude skills and a research agent for US stock research, kept separate from the trading loop.
They fetch data and write briefs. They never place orders: `scripts/research/` imports no broker or
execution code, and `tests/test_research_skills.py` checks this by importing the bridge in a subprocess and asserting no broker or Alpaca module loads.

Note: the bridge imports `agent.config`, which calls `load_dotenv()` and so puts the whole `.env` in that process's environment (nothing prints it, and provider keys are redacted from output). `stock_data.py` on its own copies only the provider keys. The `stock-analyst` agent is limited to `scripts/research`, agent-reach CLIs, read and web tools; that restriction relies on Claude Code honoring the `tools` field, which I did not verify.

| Piece | Where | Purpose |
|---|---|---|
| `stock-data` | `.claude/skills/stock-data` | Quotes, candles, news, earnings, indicators from Finnhub / Polygon / Alpha Vantage with automatic fallback |
| `ticker-research` | `.claude/skills/ticker-research` | One-page brief on a ticker, including StockSage's own analyzer read |
| `watchlist-brief` | `.claude/skills/watchlist-brief` | Daily digest of `market.watchlist` in `config/settings.yaml` |
| `sentiment-scan` | `.claude/skills/sentiment-scan` | Reddit / X / web / news sentiment via agent-reach |
| `idea-screener` | `.claude/skills/idea-screener` | Shortlist: oversold bounce, breakout, volume surge, pre-earnings |
| `stock-analyst` | `.claude/agents/stock-analyst.md` | Read-only orchestrator |

## How it reuses the existing agent

`scripts/research/repo_signals.py` calls `TechnicalAnalyzer` (and optionally `FundamentalScorer`) and reads
the watchlist from `config/settings.yaml`, so the research view and the trading view use the same logic and
the same ticker list.

## Setup

Add `FINNHUB_API_KEY` and/or `POLYGON_API_KEY` to `.env` (`ALPHA_VANTAGE_KEY` is already supported). Then ask
Claude: "research NVDA", "morning brief", "find setups".

Free-tier limits differ a lot (Polygon 5 calls/min and end-of-day only, Alpha Vantage 25 calls/day, Finnhub
60 calls/min but candles may be paid). See `.claude/skills/stock-data/SKILL.md`.

## Scheduling a morning brief

Weekdays about 8:15 AM Eastern, before `scripts/run_market_analysis.py` at 08:45:
"Run the watchlist-brief skill with the stock-analyst agent. Lead with tickers needing attention, list
earnings within 10 days, one screen, end with the research-only disclaimer."

Research only, not investment advice.
