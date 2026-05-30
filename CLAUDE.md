# CLAUDE.md — Automated US Equity Trading Agent

> Project memory for Claude Code. Read this before doing anything. These rules are
> non-negotiable unless the human explicitly overrides them in the current session.

---

## 1. What this project is

A research-and-execution system that trains ML models on US equities, generates
high-confidence signals, and (eventually) places real orders through a broker API.

**The real objective (do not let this drift):** build a fully automated, well-monitored
system that beats a buy-and-hold S&P 500 benchmark **on a risk-adjusted (Sharpe) basis,
net of all costs**, over a sustained out-of-sample period. If it can't, the correct
answer is to index. We are **not** chasing a fixed return target — no real strategy
guarantees one, and sizing a system around an aspirational number (e.g. "50%") is how
accounts blow up. Optimize for *not ruining the account first, edge second*.

**Status:** pre-production. Default mode is **paper trading / backtest**. Live trading
is gated and off by default (see §6).

---

## 2. Tech stack

- **Language (research/ML):** Python 3.11+, managed with `uv` (or `poetry`). Type-hinted.
- **Language (execution/OMS, optional):** can be Python; a JVM (Spring Boot) service is
  acceptable for the order-management service if reliability demands it. Keep the
  research/live boundary clean either way.
- **ML:** scikit-learn, XGBoost/LightGBM, PyTorch (only if a tree model is genuinely
  beaten out-of-sample). Experiment tracking: MLflow.
- **Backtesting:** VectorBT (research sweeps) → Backtrader or LEAN (event-driven validation).
- **Data:** Polygon.io / Databento (real-time), Alpaca data (IEX free / SIP paid).
  Store as Parquet on S3 + a Postgres/TimescaleDB metadata + feature layer.
- **Broker:** Alpaca (default, Python-first, paper account) → IBKR when scale/margin matters.
- **Infra:** AWS (ECS/Fargate or a single hardened EC2 for the live loop, RDS/Timescale, S3,
  Step Functions/Airflow for batch retrain, CloudWatch). Monitoring: Prometheus + Grafana.
- **Dashboard:** Next.js/React (read-only views of P&L, exposure, model drift).

---

## 3. Repository layout

```
algo-trader/
  data/            # ingestion adapters, point-in-time feature store, schemas
  features/        # feature engineering — MUST be point-in-time correct
  models/          # training, evaluation, serving; versioned artifacts
  signals/         # model output -> target positions, confidence gating
  risk/            # position sizing, limits, kill-switch  (NON-BYPASSABLE)
  oms/             # order lifecycle, idempotency, broker reconciliation
  execution/       # broker adapters: SimulatedBroker + LiveBroker (one interface)
  backtest/        # harness, walk-forward, cost model, validation (CPCV, DSR)
  monitoring/      # metrics, alerting, drift detection
  dashboard/       # Next.js UI
  config/          # YAML configs; NO secrets here
  tests/           # unit + integration + backtest-regression
  .claude/         # agents, hooks, settings (this tooling)
```

**Hard rule:** `execution/` exposes a single `Broker` interface with two implementations
(`SimulatedBroker`, `LiveBroker`). Backtest and live share the exact same signal→risk→OMS
path. If you find yourself writing logic that only runs in backtest or only in live, stop
and reconsider — that divergence is where strategies silently die.

---

## 4. Engineering rules (the non-negotiables)

These exist because the failure modes here are specific and lethal. Enforce them in code
and in review.

1. **No lookahead bias.** Every decision uses only data available at decision time.
   Never use a bar's close to trade that same bar. Fundamentals must be point-in-time.
   Universe selection must include delisted names (no survivorship bias).
2. **Always model costs.** Commissions, bid-ask spread, slippage, and market impact are
   modeled in every backtest. A backtest without a cost model is a lie. Default slippage
   assumption lives in `config/` and must be cited in any result.
3. **The risk layer is non-bypassable.** `risk/` sits between signals and the OMS and can
   veto any order. No code path may place an order without passing through it.
4. **Kill-switch first.** A hard max-drawdown halt (default 15–20%) and a manual kill-switch
   must exist and be tested *before* any live capital. Connectivity loss, anomalous orders,
   and duplicate fills must trigger alerts.
5. **Sizing is conservative by default.** Fractional Kelly (¼–½) or a flat ≤1% risk-per-trade
   cap. No full Kelly. No leverage in early phases. Sizing changes require explicit human sign-off.
6. **Validation over complexity.** A simple XGBoost ranker with airtight validation beats a
   fancy net that's secretly overfit. Use purged/embargoed CV (CPCV) and report the Deflated
   Sharpe Ratio with the number of trials. Treat every backtest as overfit until proven otherwise.
7. **Paper before money.** New strategies run in paper trading with live data and must
   reconcile against backtest expectations before any real-capital proposal.
8. **Determinism & reproducibility.** Seed everything. Pin dependencies. A result that can't
   be reproduced from a commit hash + config doesn't count.

---

## 5. Coding conventions

- Python: `ruff` (lint) + `black` (format) + `mypy` (types). 100-char lines. Google-style docstrings.
- Pure functions for feature/transform logic; no hidden global state.
- All financial math in integer cents/paise or `Decimal` — **never** float for money.
- Config via YAML + env vars; validated with `pydantic` at startup. Fail fast on bad config.
- Logging is structured (JSON). Every order, fill, and risk decision is logged with a correlation id.
- No network calls or live broker calls in unit tests — use `SimulatedBroker` and fixtures.

---

## 6. Live-trading safety gate

- Live trading requires the env var `TRADING_MODE=live` **and** `--i-understand-the-risk`
  passed to the entrypoint. Default is `TRADING_MODE=paper`.
- Never commit code that hardcodes `TRADING_MODE=live`.
- Never place a live order from a test, a script, or an agent action. Live runs are launched
  by a human, intentionally.
- Hooks in this repo block edits that flip the default to live and block obviously destructive
  shell commands (see `.claude/settings.json`). Do not disable them.

---

## 7. Secrets

- Secrets (broker keys, data API keys) live in `.env` (gitignored) or AWS Secrets Manager.
- **Never** read, print, log, echo, or commit `.env`, `*.pem`, or any `*secret*`/`*key*` file.
  The hooks block edits to these; do not work around them.

---

## 8. Testing requirements

- Unit tests for all `features/`, `signals/`, `risk/`, `oms/` logic. Coverage target ≥ 80%
  on those four packages (they are where money is lost).
- A **backtest regression test**: a fixed dataset + strategy must produce a known metric set
  within tolerance. If a refactor changes backtest output, that must be explained, not ignored.
- Integration test of the full signal→risk→OMS→SimulatedBroker path on every PR.
- Run `make test` (unit), `make backtest-regression`, and `make lint` before proposing a merge.

---

## 9. Git & workflow

- Branch per feature: `feat/`, `fix/`, `chore/`. Small, reviewable PRs.
- Conventional Commits. No commits straight to `main`.
- Before merge, the **code-reviewer** subagent reviews the diff and the **qa-engineer**
  subagent confirms tests + backtest regression pass (see §10).
- Production deploys go through the **deployment-engineer** subagent's checklist (§10).
- Never `git push --force` to `main`. Never commit secrets, data dumps, or model binaries
  (use S3 + a pointer/version).

---

## 10. Subagents (delegation)

Three project subagents live in `.claude/agents/`. Use them, don't reinvent their work inline:

- **`code-reviewer`** — invoke after writing or changing code, before merge. Focuses on the
  §4 non-negotiables (lookahead bias, cost modeling, risk-layer bypass), security, and correctness.
- **`qa-engineer`** — invoke to design/run tests and verify the backtest-regression and the
  full simulated execution path. Owns "is this actually correct and covered?"
- **`deployment-engineer`** — invoke for production deploys. Owns the pre-deploy checklist,
  paper→live gating, rollback plan, and monitoring/kill-switch verification. It must refuse to
  green-light a live deploy if the kill-switch, drawdown halt, or alerting aren't verified.

Explicit invocation example: *"Use the code-reviewer subagent on the risk/ changes."*

---

## 11. Honest framing (keep this in view)

The best track record in history (Medallion) netted ~39%/yr with ~200 PhDs and is capped/closed
because the edge doesn't scale. Top 2024 multi-strats returned ~11–20% and many underperformed
the index. Under 1% of day traders show persistent skill; most quit within two years. None of
that means *don't build this* — it's a great engineering project — it means **measure honestly,
size small, and be willing to shut it down.** A pre-committed shutdown rule (a drawdown /
underperformance threshold at which we stop and index) is a feature, not a defeat.

*This project handles real money. Nothing here is financial, legal, or tax advice; a
trader-tax CPA and the relevant broker/FINRA rules govern the real-world specifics.*

---

## Current implementation (StockSage — existing codebase)

The working implementation lives in `agent/`. It uses the architecture above as a target;
gaps between it and the ideal layout are known tech debt.

### Running

```bash
# Deps
pip install -r requirements.txt
cp .env.example .env   # fill in API keys

# Main trading loop (paper by default)
PYTHONUTF8=1 python -m agent.main

# Pre-market analysis (08:45 ET)
python scripts/run_market_analysis.py

# Portfolio review (16:30 ET EOD)
python scripts/run_portfolio_agent.py --mock

# REST API
python scripts/start_api.py

# Backtest
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31

# Tests
python -m pytest
```

### Environment variables

| Variable | Purpose | Required |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini LLM (decision engine) | Yes |
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Alpaca broker | Yes |
| `ANTHROPIC_API_KEY` | Claude sub-agents | For agents only |
| `BROKER` | `alpaca` (default) or `robinhood` | No |
| `DATABASE_URL` | PostgreSQL URL (falls back to SQLite) | No |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Trade notifications | No |

### Data flow

```
PriceFeed (Yahoo Finance / Alpaca WebSocket)
    → TechnicalAnalyzer (EMA/RSI/MACD/VWAP)
    → SentimentModel (FinBERT on RSS news)
    → FundamentalScorer (yfinance P/E, P/B, ROE …)
    → DecisionEngine (45% tech + 35% sentiment + 20% fundamental; Gemini tie-break)
    → RiskManager (position limits, stop-loss, circuit breaker, threading.Lock)
    → BrokerClient (Alpaca fractional notional orders)
    → DB (SQLite / Postgres) + REST API + Telegram
```
