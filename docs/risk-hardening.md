# Risk-layer hardening

## Overall drawdown halt
`risk.max_drawdown_pct` in `config/settings.yaml` (default 0.15). `RiskManager.check_drawdown()` tracks peak
equity (cash plus open positions at latest prices). When equity falls that far below the peak, **new buys are
blocked**. Open positions keep their stops; nothing is liquidated automatically.

- Checked every minute by the exit monitor while the market is open; a Telegram alert fires when it trips.
- State is saved to `logs/risk_state.json`, so a restart cannot clear it. An unreadable state file halts buys
  (fail safe).
- `/control/resume` clears the emergency stop but **not** this halt.
- To clear it, **stop the agent**, then a human runs `python scripts/reset_drawdown_halt.py --new-peak <current equity>`
  (see below). Deleting `logs/risk_state.json` is not a supported reset, and the safety hook blocks it for agents.
  Decide deliberately: the halt exists to make you stop and look.

This is separate from `max_daily_loss_pct`, which pauses trading for the day.

## Broker reconciliation (startup)
After restoring positions from the DB, the agent compares them with the broker, which is the source of truth:
positions the broker does not hold are dropped, quantities differ -> broker wins, positions held at the broker
but not tracked are **reported, not adopted** (they may not belong to this agent). It uses a strict positions call
that raises on failure, skips reconciliation if the broker is unreachable, and refuses to drop everything when the
broker shows no positions while some are tracked (alerts instead). Positions are also saved after every open and close (previously only on clean shutdown, and closing
the last position was never saved).

## Buys are re-checked at execution
`analyze_all()` approves every BUY against the same pre-trade state, so earlier fills in a cycle are not reflected in
the later approvals. `_execute_decision` therefore re-runs the full `can_buy` (open-position count, sector, cash
reserve, daily loss, blackout, already held) right before each order, after the human-like delay, and sizes from that
fresh answer, capped at the decision's own size.

## Reconciliation details
A position the broker no longer holds is dropped and its loss/gain is **booked at the last known mark** (or the
broker's price), so a stop that fired while the agent was down still counts toward the drawdown. Marks of open
positions (price and timestamp) are saved in `logs/risk_state.json` about once a minute, so this also works after a
restart. The booked price is the last mark before shutdown and the alert states its age, because it can be hours
old. Marks are only kept for restored positions (`open_position(..., restored=True)` from DB recovery); a fresh buy
never inherits an old mark, and the operator reset script keeps all saved marks. With no mark at all it is booked at cost and listed as
`unbooked`: that loss is NOT recorded, so check the broker. Extra shares at the broker (for example a manual buy) are adopted at the broker's average cost with stop and target
re-derived from it, and reported, because the agent's exit sells the whole position. A broker quantity of 0 or a short
(negative) drops the local record and is reported loudly. An unreadable quantity (missing, NaN) leaves the local record
untouched, so its stop keeps covering a position that may still exist, and is reported as `unreadable_qty`. Adopting
extra shares never loosens a stop that has already trailed up. The empty-broker safeguard ignores positions still inside the 3-minute grace window. Reconciliation also runs about every 5 minutes while the market is open,
skipping positions opened in the last 3 minutes (a just-accepted order may not show at the broker yet). Alerts fire
only when the findings change.

## Exits
Sells go through one lock, so the stop-loss monitor and the AI SELL path cannot both sell the same symbol, and closes
are booked at the broker's fill price when it reports one (else the trigger price). A market order that is accepted
but not yet filled is still treated as closed, which is the existing behavior.

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
guard rail, and use Claude Code's permission deny rules if you need enforcement. The hook also protects itself: edits to `.claude/hooks/*` and `.claude/settings*.json` (through the Edit/Write tools or
shell writes) are blocked, so changing the hook is a human edit. It blocks force-push in every spelling (`-fu`, `git -C`,
`+refspec`; `--force-with-lease` is allowed), recursive searches over the project root without a `.env` exclusion
(`grep -r X .`, `rg --hidden`), more `.env` readers (`tac`, `dd if=`, `curl -d @.env`, `git show HEAD:.env`), and
environment-variable exposure (`printenv NAME_KEY`, `echo $X_SECRET`, `export`). `echo`/`printf` are not treated as
message text, because they are how files get written. Commands are split on unquoted
`&&`, `||`, `|`, `|&`, `&`, `;` and newlines (honoring quotes and escapes) and each segment is checked. Message text
(commit/PR bodies, `echo`) is ignored only when the shell would not run it: a `$(cat <<'EOF' ... EOF)` body is
skipped only for a quoted delimiter, up to the first delimiter line, with nothing but `)` after it; anything piped,
unquoted-heredoc, or containing `$(`/backticks is scanned in full. It also protects
`logs/risk_state.json` (no edits, rm, mv or redirects) and blocks calls to `reset_drawdown_halt(`; the launcher fails
closed if the guard crashes or no working Python is found. Known false positive:
`grep "\.env"` is blocked.

## Operator reset
`python scripts/reset_drawdown_halt.py --new-peak <equity>` shows the state and clears the halt only after you
type `RESET` in a terminal. It refuses to run non-interactively. `--new-peak` is required (a default would usually
re-trip the halt within a minute): use your current equity. **Stop the agent first**: a running agent keeps the
halt in memory and writes it back.

## Restarts and realized losses
`logs/risk_state.json` also stores cumulative realized P&L, so cash and equity after a restart include earlier
losses and gains; a run of stop-outs across days still counts toward the halt. If the file is missing the agent
starts with a fresh peak and no halt, logs a warning and sends a Telegram alert once (the file is then recreated). A tripped halt is lost with the file, so keep `logs/` on durable storage. A
corrupt or non-finite file halts buys until an operator resets, and is first moved aside as
`risk_state.json.corrupt-<time>` so its peak and realized P&L are not overwritten. If saving fails when the halt trips, a critical
log and Telegram alert say so.

## Limits
- The reset script cannot detect a running agent; "stop the agent first" is advisory.
- `execution.sell` runs under the sell lock; make sure the Alpaca client has a request timeout, or a hung call would
  block the stop-loss monitor.
- `drawdown_halted` in the state file is read with `bool()`, so any non-empty string (even `"false"`) means halted.
  That fails safe.
- The hook launcher is POSIX `sh`; Windows needs a bash-compatible shell (Git Bash) on PATH or the hook blocks.
A position whose sell fails with "position does not exist" is cleared locally only after a successful positions call confirms the broker no longer holds it. After startup, broker prices seed the equity marks so a feed outage cannot hide a loss behind entry-price marking.

The halt blocks new buys only (no liquidation, stops stay in place). It is evaluated every minute while the market is
open, and again at startup and immediately before every buy.


## Known hook limits
A heredoc body that writes a file (`cat > f <<'EOF'`) is scanned as commands, and any piped command is scanned in full,
so a few harmless commands that merely mention these words are blocked. Runtime string building
(`a=risk; rm logs/${a}_state.json`, `base64 -d | sh`) and `cd logs && rm r*` are not caught: that is inherent to pattern
matching, and why permission deny rules are recommended as the real boundary.

## Not done
Money is still `float` (CLAUDE.md asks for `Decimal`/integer cents); stops are polled, not broker-side orders;
backtest stop fills assume the stop price (no gap slippage); no live-broker test of reconciliation.
