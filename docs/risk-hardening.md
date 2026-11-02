# Risk-layer hardening

## Overall drawdown halt
`risk.max_drawdown_pct` in `config/settings.yaml` (default 0.15). `RiskManager.check_drawdown()` tracks peak
equity (cash plus open positions at latest prices). When equity falls that far below the peak, **new buys are
blocked**. Open positions keep their stops; nothing is liquidated automatically.

- Checked every minute by the exit monitor while the market is open; a Telegram alert fires when it trips.
- State is saved to `logs/risk_state.json`, so a restart cannot clear it. An unreadable state file halts buys
  (fail safe).
- `/control/resume` clears the emergency stop but **not** this halt.
- To clear it, a human calls `RiskManager.reset_drawdown_halt(new_peak=...)`, or deletes `logs/risk_state.json`
  (peak restarts at `total_fund`). Decide deliberately: the halt exists to make you stop and look.

This is separate from `max_daily_loss_pct`, which pauses trading for the day.

## Broker reconciliation (startup)
After restoring positions from the DB, the agent compares them with the broker, which is the source of truth:
positions the broker does not hold are dropped, quantities differ -> broker wins, positions held at the broker
but not tracked are **reported, not adopted** (they may not belong to this agent). It uses a strict positions call
that raises on failure, skips reconciliation if the broker is unreachable, and refuses to drop everything when the
broker shows no positions while some are tracked (alerts instead). Positions are also saved after every open and close (previously only on clean shutdown, and closing
the last position was never saved).

## Exits
A broker sell that errors or is rejected is no longer recorded as closed; the next cycle retries and an alert
is sent. The minimum-hold exemption now applies only to the exact reasons `stop_loss`, `trailing_stop_loss` and
`emergency_stop`. The daily reset uses the market timezone (US/Eastern).

## Safety hook
`.claude/hooks/guard.py` (registered in `.claude/settings.json`; the launcher uses `python3`, else `python`, and blocks if neither exists) replaces
`guard-bash.sh` and `guard-edit.sh`, which were never registered and read the wrong payload key, so they could
not have blocked anything. `protect-env.ps1` stays as a second, Windows-only layer for `.env`. The guard blocks
reads/edits of `.env`, `.env.*` (not `.env.example`), `*.pem`, `*.key` and secret config files, scripted
`TRADING_MODE=live`, `--i-understand-the-risk`, force-push (not `--force-with-lease`) and destructive shell/SQL.
It fails closed on its own errors. It is regex heuristics and can be bypassed by obfuscation; treat it as a
guard rail, and use Claude Code's permission deny rules if you need enforcement. Known false positive:
`grep "\.env"` is blocked.

## Operator reset
`python scripts/reset_drawdown_halt.py` shows the state and clears the halt only after you type `RESET` in a
terminal. It refuses to run non-interactively.

## Restarts and realized losses
`logs/risk_state.json` also stores cumulative realized P&L, so cash and equity after a restart include earlier
losses and gains; a run of stop-outs across days still counts toward the halt. If the file is missing the agent
starts with a fresh peak and no halt, logs a warning and sends a Telegram alert once (the file is then recreated). A tripped halt is lost with the file, so keep `logs/` on durable storage. A
corrupt or non-finite file halts buys until an operator resets. If saving fails when the halt trips, a critical
log and Telegram alert say so.

## Limits
A position whose sell fails with "position does not exist" is cleared locally only after a successful positions call confirms the broker no longer holds it. After startup, broker prices seed the equity marks so a feed outage cannot hide a loss behind entry-price marking.

The halt blocks new buys only (no liquidation, stops stay in place) and is evaluated while the market is open.
Reconciling a position the broker no longer holds credits cash back at cost, which overstates cash if it was
really sold at a loss.

## Not done
Money is still `float` (CLAUDE.md asks for `Decimal`/integer cents); stops are polled, not broker-side orders;
backtest stop fills assume the stop price (no gap slippage); no live-broker test of reconciliation.
