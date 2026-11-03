"""
StockSage — Main Agent Entry Point
====================================
Orchestrates all layers: data feeds → analysis → risk → execution.

Run this to start the agent:
  python -m agent.main

Modes:
  paper  = Paper trading (simulated, safe to run anytime)
  live   = Real money (only after 30+ days of successful paper trading!)
"""
import argparse
import os
import sys
import time
import signal
import threading
from datetime import datetime
from typing import Dict

import pytz
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich import box

from agent.config import CONFIG, setup_logger
from agent.data.price_feed import PriceFeed
from agent.data.news_feed import NewsFeed
from agent.core.risk_manager import RiskManager
from agent.core.decision_engine import DecisionEngine, TradeDecision
from agent.core.execution_engine import ExecutionEngine
from agent.notifications import TelegramNotifier
from agent.db.repository import PositionRepository
from agent.api.app import record_trade, record_signal

# Rich console for beautiful terminal output
console = Console()


class StockSageAgent:
    """
    Main agent orchestrator.
    
    Lifecycle:
      1. start() — Initialize all feeds, load config
      2. run()   — Main loop: analyze → decide → execute
      3. stop()  — Graceful shutdown, close all feeds
    """

    def __init__(self):
        self.cfg = CONFIG
        self.mode = CONFIG["execution"]["mode"]
        self.tz = pytz.timezone(CONFIG["market"]["timezone"])
        self._running = False

        # ── Initialize all layers ─────────────────────────────
        logger.info("=" * 60)
        logger.info("🤖 StockSage AI Agent — Starting Up")
        logger.info(f"   Mode:     {self.mode.upper()}")
        logger.info(f"   Market:   {CONFIG['market']['exchange']} (US)")
        logger.info(f"   Fund:     ${CONFIG['risk']['total_fund']:.2f} USD")
        logger.info(f"   Watchlist:{len(CONFIG['market']['watchlist'])} stocks")
        logger.info("=" * 60)

        self.price_feed = PriceFeed()
        self.news_feed = NewsFeed()
        self.risk_manager = RiskManager()
        self.execution = ExecutionEngine()
        self.decision_engine = DecisionEngine(
            price_feed=self.price_feed,
            news_feed=self.news_feed,
            risk_manager=self.risk_manager,
        )
        self.notifier = TelegramNotifier()

        # Register for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Track today's decisions
        self._decisions: Dict[str, TradeDecision] = {}
        self._exit_alerted: set = set()
        self._exit_lock = threading.Lock()      # serializes every sell + close (monitor vs main loop)
        self._last_reconcile_sig = None
        self._monitor_tick = 0
        self._cycle_count = 0

    # ─── Startup ──────────────────────────────────────────────

    def start(self):
        """Start all data feeds and begin the agent loop."""
        console.print(Panel.fit(
            "[bold green]🚀 StockSage AI Agent Starting...[/bold green]\n"
            f"[cyan]Mode: {self.mode.upper()}[/cyan]  |  "
            f"[yellow]Fund: ${self.cfg['risk']['total_fund']:.2f}[/yellow]  |  "
            f"[magenta]Exchange: US (NASDAQ/NYSE)[/magenta]",
            border_style="green",
        ))

        # Restore open positions from last session
        self._recover_positions()
        self.risk_manager.prune_marks()   # forget saved marks of symbols that were not restored
        self._reconcile_with_broker()
        self._check_drawdown()   # evaluate before any trading, not after the first monitor tick
        if self.risk_manager.state_missing:
            self.notifier.error(
                "No risk state file found at startup (fresh peak, no drawdown halt, realized P&L "
                "starts at 0). Expected on a first run; otherwise logs/ may have been lost."
            )

        # Start data feeds
        self.price_feed.start()
        self.news_feed.start()

        # Notify Telegram on startup
        self.notifier.startup(
            fund=self.cfg["risk"]["total_fund"],
            watchlist=self.cfg["market"]["watchlist"],
        )

        # Give feeds time to populate initial data
        console.print("[dim]⏳ Warming up data feeds (15s)...[/dim]")
        time.sleep(15)

        # Start stop-loss monitor in background
        self._start_exit_monitor()

        # Main analysis loop
        self._running = True
        self.run()

    def run(self):
        """
        Main trading loop.
        Runs analysis every 5 minutes during market hours.
        Pre-market: news sentiment scan only.
        After hours: gracefully idle.
        """
        analysis_interval = 300  # 5 minutes

        while self._running:
            now = datetime.now(self.tz)
            market_open = self.price_feed.is_market_open()

            # ── Pre-market: news scan only ────────────────────
            if self._is_pre_market(now):
                logger.info("🌅 Pre-market: Running news sentiment scan...")
                self._pre_market_scan()
                self._sleep_until_open(now)
                continue

            # ── Market hours: full analysis + trading ─────────
            if market_open:
                self._cycle_count += 1
                logger.info(f"\n{'='*50}")
                logger.info(f"🔔 Analysis Cycle #{self._cycle_count} | {now.strftime('%H:%M:%S ET')}")

                # Run full analysis
                decisions = self.decision_engine.analyze_all()
                self._decisions.update(decisions)

                # Record signals to Prometheus
                for decision in decisions.values():
                    record_signal(decision.action)

                # Execute approved signals
                for symbol, decision in decisions.items():
                    self._execute_decision(decision)

                # Print terminal dashboard
                self._print_dashboard(decisions)

            # ── After hours ───────────────────────────────────
            else:
                if now.hour >= 16:
                    logger.info("🌙 Market closed. Agent sleeping until tomorrow.")
                    self._generate_eod_report()
                    self._sleep_until_tomorrow()
                    continue

            # Wait for next cycle
            time.sleep(analysis_interval)

    # ─── Decision Execution ───────────────────────────────────

    def _execute_decision(self, decision: TradeDecision):
        """Execute a BUY or SELL decision through the broker."""

        if decision.action == "BUY":
            # Evaluate drawdown now (not only on the monitor's 60s tick, which may not have run yet
            # after a restart), then belt and braces: can_buy vetoed earlier, but a halt can trip
            # between decision and execution.
            self._check_drawdown()
            if self.risk_manager.drawdown_halted or self.risk_manager.emergency_stop:
                logger.warning(f"BUY {decision.symbol} skipped: trading halted by risk layer")
                return

            # Human-like delay (can last seconds; other fills and the monitor may change state)
            self.decision_engine.apply_human_delay()

            if not self.risk_manager.trading_allowed():
                logger.warning(f"BUY {decision.symbol} cancelled: trading halted during order delay")
                return

            # analyze_all() ran can_buy for EVERY symbol against the same pre-trade state, so earlier
            # BUYs in this cycle are not reflected in those approvals. Re-run the full risk check
            # now (open-position count, sector, cash reserve, daily loss, blackout, already held)
            # and size from its answer.
            allowed, why, approved = self.risk_manager.can_buy(
                decision.symbol, decision.price, decision.confidence)
            if not allowed:
                logger.info(f"BUY {decision.symbol} vetoed at execution: {why}")
                return
            dollar_amount = approved
            # decision.quantity carries the (variance-adjusted) size; never exceed the fresh approval
            q, px = getattr(decision, "quantity", 0), getattr(decision, "price", 0)
            sized = q * px if isinstance(q, (int, float)) and isinstance(px, (int, float)) else 0
            if sized > 0:
                dollar_amount = min(dollar_amount, sized)

            if dollar_amount < 1.0:
                logger.debug(f"Insufficient capital for {decision.symbol} buy")
                return

            # Place order
            result = self.execution.buy(
                symbol=decision.symbol,
                dollar_amount=round(dollar_amount, 2),
                reason=f"AI Signal | Confidence: {decision.confidence:.0%}",
            )

            if result.status not in ("error", "rejected"):
                # Use actual fill data from broker; fall back to signal price if not yet filled
                fill_price = result.filled_price if result.filled_price > 0 else decision.price
                fill_qty = result.quantity if result.quantity > 0 else (dollar_amount / fill_price)
                self.risk_manager.open_position(
                    symbol=decision.symbol,
                    price=fill_price,
                    quantity=fill_qty,
                )
                self._persist_positions()
                console.print(
                    f"[bold green]✅ BOUGHT[/bold green] "
                    f"[cyan]{decision.symbol}[/cyan] "
                    f"${dollar_amount:.2f} @ ~${decision.price:.2f} "
                    f"| Confidence: {decision.confidence:.0%}"
                )
                self.notifier.buy(decision.symbol, dollar_amount, decision.price, decision.confidence)
                record_trade("BUY", decision.symbol)
            else:
                logger.error(f"Order failed: {result.error_msg}")

        elif decision.action == "SELL":
            allowed, reason = self.risk_manager.can_sell(decision.symbol)
            if not allowed:
                return

            self.decision_engine.apply_human_delay()   # outside the lock: it can last seconds
            with self._exit_lock:
                # The stop-loss monitor may have closed it during the delay.
                if decision.symbol not in self.risk_manager.positions:
                    return
                result = self.execution.sell(
                    symbol=decision.symbol,
                    reason="AI signal — sell",
                )
                if result.status in ("error", "rejected"):
                    logger.error(f"AI SELL failed for {decision.symbol}: {result.error_msg}")
                    return
                sell_price = self._booked_price(result, decision.price)
                pnl = self.risk_manager.close_position(decision.symbol, sell_price, "AI signal")
                self._persist_positions()
            emoji = "🟢" if pnl >= 0 else "🔴"
            console.print(
                f"[bold red]{emoji} SOLD[/bold red] "
                f"[cyan]{decision.symbol}[/cyan] "
                f"@ ${sell_price:.2f} | P&L: ${pnl:+.2f}"
            )
            self.notifier.sell(decision.symbol, sell_price, pnl)
            record_trade("SELL", decision.symbol)

    @staticmethod
    def _booked_price(result, fallback: float) -> float:
        """Use the broker's fill price when it reported one; else the trigger/signal price."""
        fp = getattr(result, "filled_price", 0) or 0
        return float(fp) if isinstance(fp, (int, float)) and fp > 0 else fallback

    # ─── Position Recovery ───────────────────────────────────────

    def _recover_positions(self) -> None:
        """Reload open positions from DB into risk manager on startup."""
        try:
            snapshots = PositionRepository.load_active()
            if not snapshots:
                logger.info("[Recovery] No prior positions found in DB")
                return
            for snap in snapshots:
                self.risk_manager.open_position(
                    symbol=snap.symbol,
                    price=snap.entry_price,
                    quantity=snap.quantity,
                    restored=True,          # keep its saved mark for cold-start booking
                )
                logger.info(
                    f"[Recovery] Restored {snap.symbol}: {snap.quantity} shares "
                    f"@ ${snap.entry_price:.2f}"
                )
            console.print(f"[cyan]♻️  Recovered {len(snapshots)} open position(s) from last session[/cyan]")
        except Exception as exc:
            logger.warning(f"[Recovery] Could not restore positions: {exc}")

    def _reconcile_with_broker(self, grace_seconds: float = 0.0) -> None:
        """Make the risk layer's positions match the broker (the source of truth)."""
        try:
            # get_account()/get_positions() swallow errors and return {}/[]; an empty account
            # means the broker was unreachable, and then positions must NOT be reconciled.
            if not self.execution.get_account():
                logger.warning("[Reconcile] Broker unreachable; skipping position reconciliation")
                return
            # Strict call: raises on failure instead of returning [] (which would look like "flat").
            broker_positions = self.execution.get_positions_strict()   # network: outside the lock
            with self._exit_lock:   # so it cannot interleave with a sell that is mid-flight
                result = self.risk_manager.reconcile_positions(
                    broker_positions, grace_seconds=grace_seconds)
            sig = (tuple(result["dropped"]), tuple(result["resized"]), tuple(result["untracked"]),
                   tuple(result["blocked_empty"]), tuple(result.get("negative_qty", [])),
                   tuple(result.get("adopted_excess", [])), tuple(result.get("unreadable_qty", [])))
            if any(sig) and sig != self._last_reconcile_sig:   # alert on change, not every cycle
                msg = (
                    f"Reconciled with broker | dropped={result['dropped']} "
                    f"resized={result['resized']} untracked(not adopted)={result['untracked']} "
                    f"NOT dropped (broker showed none; verify manually)={result['blocked_empty']} "
                    f"booked-at-cost(no mark, P&L NOT recorded)={result.get('unbooked', [])} "
                    f"booked-at-last-mark(age h)={result.get('booked_at_mark_age_h', {})} "
                    f"SHORT at broker={result.get('negative_qty', [])} "
                    f"extra shares adopted={result.get('adopted_excess', [])} "
                    f"UNREADABLE qty (left untouched; check broker)={result.get('unreadable_qty', [])}"
                )
                console.print(f"[yellow]⚠️  {msg}[/yellow]")
                logger.warning(msg)
                self.notifier.error(msg)
            self._last_reconcile_sig = sig
            if result["dropped"] or result["resized"]:
                self._persist_positions()
        except Exception as exc:
            logger.warning(f"[Reconcile] Could not reconcile with broker: {exc}")

    def _process_exit(self, symbol: str, price: float, reason: str) -> bool:
        """Sell at the broker, and record the close only if the broker accepted the order."""
        alerts: list = []          # notifier calls are network I/O: send them after the lock is released
        done = False
        booked = price
        pnl = 0.0
        with self._exit_lock:   # one sell/close at a time across the monitor and the main loop
            if symbol not in self.risk_manager.positions:
                return False  # already closed by another path (e.g. the AI SELL); nothing to do
            logger.warning(f"🛑 Auto-exit: {symbol} @ ${price:.2f} ({reason})")
            result = self.execution.sell(symbol, reason=reason)
            if result.status in ("error", "rejected"):
                err = (result.error_msg or "").lower()
                if ("position does not exist" in err or "40410000" in err) and self._absent_at_broker(symbol):
                    # Confirmed against the broker's position list (not just an error string).
                    logger.warning(f"{symbol} is not held at the broker; closing local record")
                    self.risk_manager.close_position(symbol, price, "closed_at_broker")
                    self._persist_positions()
                    self._exit_alerted.discard(symbol)
                    alerts.append(("error", (f"{symbol} was already closed at the broker; local record cleared",)))
                    done = True
                else:
                    logger.error(f"Exit sell failed for {symbol}: {result.error_msg}")
                    if symbol not in self._exit_alerted:  # alert once per symbol, not every minute
                        self._exit_alerted.add(symbol)
                        alerts.append(("error", (
                            f"Exit sell FAILED for {symbol}: {result.error_msg} (retrying each cycle)",)))
            else:
                self._exit_alerted.discard(symbol)
                booked = self._booked_price(result, price)
                pnl = self.risk_manager.close_position(symbol, booked, reason)
                self._persist_positions()
                alerts.append(("stop_loss", (symbol, booked, pnl)))
                done = True
        for name, args in alerts:
            getattr(self.notifier, name)(*args)
        return done

    def _absent_at_broker(self, symbol: str) -> bool:
        """True only if a successful positions call shows the symbol is not held."""
        try:
            held = {str(p["symbol"]).upper().strip() for p in self.execution.get_positions_strict()}
            return symbol.upper() not in held
        except Exception as exc:
            logger.warning(f"Could not confirm {symbol} absence at broker: {exc}")
            return False

    def _check_drawdown(self) -> None:
        """Evaluate the overall drawdown halt using latest prices; alert when it trips."""
        was_halted = self.risk_manager.drawdown_halted
        prices = {
            s: tick.ltp for s, tick in self.price_feed.get_all_ticks().items() if tick
        }
        if self.risk_manager.check_drawdown(prices) and not was_halted:
            msg = (
                f"MAX DRAWDOWN HALT: equity is {self.risk_manager.max_drawdown_pct:.0%}+ below "
                f"peak. New buys are blocked until an operator resets the halt."
            )
            if self.risk_manager.state_save_failed:
                msg += " WARNING: halt state could NOT be saved; a restart may clear it."
            console.print(f"[bold red]🔴 {msg}[/bold red]")
            self.notifier.error(msg)

    # ─── Stop-Loss Monitor ────────────────────────────────────

    def _start_exit_monitor(self):
        """Background thread: check stop-losses every 60 seconds."""
        def monitor():
            while self._running:
                try:
                    if self.price_feed.is_market_open():
                        self._check_drawdown()
                        self._monitor_tick += 1
                        if self._monitor_tick % 5 == 0:   # about every 5 minutes: catch untracked
                            self._reconcile_with_broker(grace_seconds=180)  # or externally closed
                        exits = self.decision_engine.check_exits()
                        for symbol, price, reason in exits:
                            self._process_exit(symbol, price, reason)
                except Exception as exc:  # a monitor crash must not silently stop protection
                    logger.exception(f"Exit monitor error: {exc}")
                    self.notifier.error(f"Exit monitor error: {exc}")
                time.sleep(60)

        t = threading.Thread(target=monitor, daemon=True, name="ExitMonitor")
        t.start()
        logger.info("🛡️  Stop-loss monitor running")

    # ─── Pre-Market ───────────────────────────────────────────

    def _pre_market_scan(self):
        """Run sentiment scan before market opens."""
        high_news = self.news_feed.get_high_importance(hours=12)
        if high_news:
            console.print(f"\n[yellow]📰 {len(high_news)} high-importance pre-market articles:[/yellow]")
            for article in high_news[:5]:
                console.print(f"  • [{article.source}] {article.title[:80]}")
        else:
            console.print("[dim]No high-importance pre-market news.[/dim]")

        # Check for news blackout events
        all_news = self.news_feed.get_recent(hours=12)
        blackout_events = self.cfg["risk"]["news_blackout_events"]
        for article in all_news:
            for event in blackout_events:
                if event.lower() in article.title.lower():
                    logger.warning(f"⚠️  Potential blackout event detected: '{event}' in '{article.title[:60]}'")
                    self.risk_manager.set_news_blackout(True, event)

    # ─── Terminal Dashboard ───────────────────────────────────

    def _print_dashboard(self, decisions: Dict[str, TradeDecision]):
        """Print a rich terminal table of current signals."""
        table = Table(
            title=f"🤖 StockSage | {datetime.now(self.tz).strftime('%Y-%m-%d %H:%M ET')}",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Symbol", style="bold")
        table.add_column("Price", justify="right")
        table.add_column("Signal", justify="center")
        table.add_column("Confidence", justify="right")
        table.add_column("Tech", justify="center")
        table.add_column("Sentiment", justify="center")
        table.add_column("Reasoning", max_width=60, no_wrap=True)

        for symbol, d in sorted(decisions.items()):
            action_color = {
                "BUY": "bold green",
                "SELL": "bold red",
                "HOLD": "yellow",
                "BLOCKED": "dim",
            }.get(d.action, "white")

            sent_color = {
                "positive": "green",
                "negative": "red",
                "neutral": "dim",
            }.get(d.sentiment_label, "white")

            table.add_row(
                symbol,
                f"${d.price:.2f}",
                f"[{action_color}]{d.action}[/{action_color}]",
                f"{d.confidence:.0%}",
                d.technical_action,
                f"[{sent_color}]{d.sentiment_label}[/{sent_color}]",
                d.reasoning.split("\n")[0][:60] if d.reasoning else "",
            )

        console.print(table)

        # Portfolio summary
        metrics = self.risk_manager.get_metrics()
        pnl_color = "green" if metrics.daily_pnl >= 0 else "red"
        console.print(
            f"[dim]Portfolio:[/dim] "
            f"Cash: [cyan]${metrics.cash_available:.2f}[/cyan]  |  "
            f"Invested: [yellow]${metrics.invested_amount:.2f}[/yellow]  |  "
            f"Day P&L: [{pnl_color}]${metrics.daily_pnl:+.2f} ({metrics.daily_pnl_pct:+.1f}%)[/{pnl_color}]  |  "
            f"Positions: {metrics.open_positions}/{self.risk_manager.max_open_positions}  |  "
            f"Risk: [{'red' if metrics.risk_level == 'HIGH' else 'green'}]{metrics.risk_level}[/]"
        )

    # ─── EOD Report ───────────────────────────────────────────

    def _generate_eod_report(self):
        """Generate end-of-day summary."""
        metrics = self.risk_manager.get_metrics()
        trades = self.risk_manager._today_trades
        today = datetime.now(self.tz).strftime("%Y-%m-%d")

        console.print("\n" + "="*60)
        console.print("[bold]📊 END OF DAY REPORT[/bold]")
        console.print(f"Date: {today}")
        console.print(f"Trades executed: {len(trades)}")
        console.print(f"Daily P&L: ${metrics.daily_pnl:+.2f} ({metrics.daily_pnl_pct:+.1f}%)")
        console.print(f"Open positions: {metrics.open_positions}")
        console.print(f"Cash remaining: ${metrics.cash_available:.2f}")
        console.print("="*60 + "\n")

        self.notifier.daily_summary(
            date=today,
            pnl=metrics.daily_pnl,
            trades=len(trades),
            open_positions=metrics.open_positions,
            portfolio_value=metrics.cash_available + metrics.invested_amount,
        )

    # ─── Time Helpers ─────────────────────────────────────────

    def _is_pre_market(self, now) -> bool:
        pre_time = self.cfg["market"]["trading_hours"]["pre_market_analysis"]
        open_time = self.cfg["market"]["trading_hours"]["start"]
        pre_h, pre_m = map(int, pre_time.split(":"))
        open_h, open_m = map(int, open_time.split(":"))
        return (
            (now.hour > pre_h or (now.hour == pre_h and now.minute >= pre_m))
            and (now.hour < open_h or (now.hour == open_h and now.minute < open_m))
        )

    def _sleep_until_open(self, now):
        open_h, open_m = map(int, self.cfg["market"]["trading_hours"]["start"].split(":"))
        target = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        secs = max(0, (target - now).total_seconds())
        logger.info(f"⏰ Sleeping {secs/60:.0f} minutes until market open...")
        time.sleep(secs)

    def _sleep_until_tomorrow(self):
        """Sleep until pre-market tomorrow."""
        time.sleep(60 * 60 * 14)  # ~14 hours (overnight)

    # ─── Shutdown ─────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown on CTRL+C."""
        console.print("\n[yellow]⚠️  Shutdown signal received. Stopping agent...[/yellow]")
        self._running = False
        self.price_feed.stop()
        self.news_feed.stop()
        self._generate_eod_report()
        self._persist_positions()
        console.print("[green]✅ Agent stopped safely. Positions remain open in broker.[/green]")
        sys.exit(0)

    def _persist_positions(self) -> None:
        """Save open positions to DB for recovery on next startup."""
        try:
            positions = [
                {
                    "symbol": sym,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss,
                    "target_price": pos.target_price,
                    "highest_price": pos.highest_price,
                    "entry_time": pos.entry_time,
                }
                for sym, pos in self.risk_manager.positions.items()
            ]
            # Save even when empty: save_all deactivates old snapshots, so closing the last
            # position must be persisted or it would be "recovered" on the next start.
            PositionRepository.save_all(positions)
            logger.info(f"[Persist] Saved {len(positions)} position(s) to DB")
        except Exception as exc:
            logger.warning(f"[Persist] Could not save positions: {exc}")


# ── Entry Point ────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StockSage AI Trading Agent")
    parser.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        dest="risk_acknowledged",
        help="Required flag to enable live trading (must be passed intentionally by a human).",
    )
    return parser.parse_args()


def _enforce_live_gate(args: argparse.Namespace) -> None:
    """Hard gate: live trading requires both TRADING_MODE=live AND --i-understand-the-risk."""
    mode = os.getenv("TRADING_MODE", CONFIG["execution"]["mode"])
    if mode != "live":
        return  # paper is always fine

    if not args.risk_acknowledged:
        print(
            "\n[SAFETY GATE] TRADING_MODE=live detected.\n"
            "You must pass --i-understand-the-risk to proceed with live trading.\n"
            "This flag must be typed by a human — it may not be scripted.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Override config so the agent uses live mode
    CONFIG["execution"]["mode"] = "live"
    print(
        "\n⚠️  LIVE TRADING MODE ACTIVE — real money at risk.\n"
        "   Kill-switch: POST /control/emergency-stop or CTRL+C\n"
        "   Max drawdown halt: configured in settings.yaml (max_daily_loss_pct)\n",
        file=sys.stderr,
    )


if __name__ == "__main__":
    _args = _parse_args()
    _enforce_live_gate(_args)
    agent = StockSageAgent()
    agent.start()
