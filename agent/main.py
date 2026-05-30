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
            # Calculate dollar amount to invest
            dollar_amount = min(
                self.cfg["risk"]["total_fund"] * self.cfg["risk"]["max_position_pct"],
                self.risk_manager.cash - (self.cfg["risk"]["total_fund"] * self.cfg["risk"]["min_cash_reserve_pct"])
            )

            if dollar_amount < 1.0:
                logger.debug(f"Insufficient capital for {decision.symbol} buy")
                return

            # Human-like delay
            self.decision_engine.apply_human_delay()

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

            self.decision_engine.apply_human_delay()
            result = self.execution.sell(
                symbol=decision.symbol,
                reason="AI signal — sell",
            )

            if result.status not in ("error", "rejected"):
                pnl = self.risk_manager.close_position(
                    decision.symbol,
                    decision.price,
                    "AI signal",
                )
                emoji = "🟢" if pnl >= 0 else "🔴"
                console.print(
                    f"[bold red]{emoji} SOLD[/bold red] "
                    f"[cyan]{decision.symbol}[/cyan] "
                    f"@ ${decision.price:.2f} | P&L: ${pnl:+.2f}"
                )
                self.notifier.sell(decision.symbol, decision.price, pnl)
                record_trade("SELL", decision.symbol)

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
                )
                logger.info(
                    f"[Recovery] Restored {snap.symbol}: {snap.quantity} shares "
                    f"@ ${snap.entry_price:.2f}"
                )
            console.print(f"[cyan]♻️  Recovered {len(snapshots)} open position(s) from last session[/cyan]")
        except Exception as exc:
            logger.warning(f"[Recovery] Could not restore positions: {exc}")

    # ─── Stop-Loss Monitor ────────────────────────────────────

    def _start_exit_monitor(self):
        """Background thread: check stop-losses every 60 seconds."""
        def monitor():
            while self._running:
                if self.price_feed.is_market_open():
                    exits = self.decision_engine.check_exits()
                    for symbol, price, reason in exits:
                        logger.warning(f"🛑 Auto-exit: {symbol} @ ${price:.2f} ({reason})")
                        self.execution.sell(symbol, reason=reason)
                        pnl = self.risk_manager.close_position(symbol, price, reason)
                        self.notifier.stop_loss(symbol, price, pnl)
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
            if positions:
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
