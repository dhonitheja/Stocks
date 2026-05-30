#!/usr/bin/env python3
"""
Portfolio Review Agent CLI

Usage:
  python scripts/run_portfolio_agent.py                  # live portfolio (requires running engine)
  python scripts/run_portfolio_agent.py --mock           # use mock data for testing
  python scripts/run_portfolio_agent.py --output out.json
  python scripts/run_portfolio_agent.py --save           # persist to DB
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from agent.agents.trading import PortfolioAgent

_console = Console()

_MOCK_PORTFOLIO = {
    "cash": 62.50,
    "invested": 37.50,
    "daily_pnl": 1.25,
    "daily_pnl_pct": 1.25,
    "total_return_pct": 2.3,
    "risk_level": "LOW",
    "circuit_breaker_triggered": False,
    "positions": [
        {
            "symbol": "AAPL",
            "entry_price": 185.00,
            "current_price": 187.50,
            "quantity": 0.1,
            "market_value": 18.75,
            "unrealized_pnl": 0.25,
            "unrealized_pnl_pct": 1.35,
            "stop_loss": 181.30,
            "target_price": 188.70,
        },
        {
            "symbol": "NVDA",
            "entry_price": 875.00,
            "current_price": 895.00,
            "quantity": 0.021,
            "market_value": 18.79,
            "unrealized_pnl": 0.42,
            "unrealized_pnl_pct": 2.29,
            "stop_loss": 857.50,
            "target_price": 892.50,
        },
    ],
    "trades_today": [
        {"action": "BUY", "symbol": "AAPL", "price": 185.00, "notional": 18.50, "reason": "technical_signal"},
        {"action": "BUY", "symbol": "NVDA", "price": 875.00, "notional": 18.37, "reason": "momentum_breakout"},
    ],
}


def _load_live_portfolio() -> dict:
    """Try to load portfolio from broker and risk manager."""
    try:
        from agent.brokers import create_broker
        from agent.config import CONFIG
        from agent.core.risk_manager import RiskManager

        broker = create_broker()
        account = broker.get_account()
        broker_positions = broker.get_positions()

        return {
            "cash": account.cash,
            "invested": account.portfolio_value - account.cash,
            "daily_pnl": account.pnl_today,
            "daily_pnl_pct": account.pnl_today / account.portfolio_value * 100 if account.portfolio_value else 0,
            "total_return_pct": (account.equity - CONFIG["risk"]["total_fund"]) / CONFIG["risk"]["total_fund"] * 100,
            "risk_level": "UNKNOWN",
            "circuit_breaker_triggered": False,
            "positions": [p.__dict__ for p in broker_positions],
            "trades_today": [],
        }
    except Exception as exc:
        _console.print(f"[yellow]Could not load live portfolio ({exc}), use --mock for demo.[/]")
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Opus 4.7 portfolio review for StockSage")
    parser.add_argument("--mock", action="store_true", help="Use mock portfolio data (no broker needed)")
    parser.add_argument("--save", action="store_true", help="Persist review to database")
    parser.add_argument("--output", metavar="FILE", help="Save JSON output to file")
    args = parser.parse_args()

    _console.print("\n[bold blue]StockSage — Portfolio Review Agent[/]\n")

    portfolio_state = _MOCK_PORTFOLIO if args.mock else _load_live_portfolio()

    if args.mock:
        _console.print("[dim]Using mock portfolio data (--mock)[/]\n")

    agent = PortfolioAgent()
    review = agent.review(portfolio_state)

    # ── Health panel ───────────────────────────────────────────────────────
    health_color = {
        "EXCELLENT": "green", "GOOD": "blue", "FAIR": "yellow", "POOR": "red"
    }.get(review.portfolio_health, "white")

    eod_color = {
        "HOLD_ALL": "blue", "REBALANCE": "yellow",
        "REDUCE_RISK": "red", "INCREASE_EXPOSURE": "green"
    }.get(review.eod_recommendation, "white")

    _console.print(Panel(
        f"[bold]Health:[/] [{health_color}]{review.portfolio_health}[/]   "
        f"[bold]Daily P&L:[/] ${review.daily_pnl_usd:+.2f}   "
        f"[bold]Total Return:[/] {review.total_return_pct:+.2f}%\n\n"
        f"[bold]EOD Recommendation:[/] [{eod_color}]{review.eod_recommendation}[/]\n"
        f"[italic]{review.sharpe_assessment}[/]",
        title="[bold]Portfolio Review[/]",
        border_style=health_color,
    ))

    # ── Key insights ───────────────────────────────────────────────────────
    if review.key_insights:
        _console.print("\n[bold]Key Insights:[/]")
        for insight in review.key_insights:
            _console.print(f"  💡 {insight}")

    # ── Position reviews ───────────────────────────────────────────────────
    if review.position_reviews:
        table = Table(title="Position Reviews", box=box.ROUNDED, show_lines=True)
        table.add_column("Symbol", style="cyan")
        table.add_column("Action")
        table.add_column("Current %")
        table.add_column("Target %")
        table.add_column("Reasoning")
        for pos in review.position_reviews:
            color = {
                "HOLD": "blue", "ADD": "green", "REDUCE": "yellow", "CLOSE": "red"
            }.get(pos.action, "white")
            table.add_row(
                pos.symbol,
                f"[{color}]{pos.action}[/]",
                f"{pos.current_weight_pct:.1f}%",
                f"{pos.target_weight_pct:.1f}%",
                pos.reasoning[:55] + "…" if len(pos.reasoning) > 55 else pos.reasoning,
            )
        _console.print()
        _console.print(table)

    if review.risk_warnings:
        _console.print("\n[bold red]Risk Warnings:[/]")
        for w in review.risk_warnings:
            _console.print(f"  ⚠️  {w}")

    if review.rebalancing_suggestions:
        _console.print("\n[bold]Rebalancing:[/]")
        for s in review.rebalancing_suggestions:
            _console.print(f"  ⚖️  {s}")

    if review.next_actions:
        _console.print("\n[bold]Next Actions (Tomorrow):[/]")
        for i, action in enumerate(review.next_actions, 1):
            _console.print(f"  {i}. {action}")

    # ── Persist ────────────────────────────────────────────────────────────
    if args.save:
        try:
            from agent.db.repository import SnapshotRepository
            SnapshotRepository.save_daily(
                portfolio_value=portfolio_state.get("invested", 0) + portfolio_state.get("cash", 0),
                cash=portfolio_state.get("cash", 0),
                invested=portfolio_state.get("invested", 0),
                daily_pnl=review.daily_pnl_usd,
                daily_pnl_pct=portfolio_state.get("daily_pnl_pct", 0),
                total_return_pct=review.total_return_pct,
                open_positions=len(portfolio_state.get("positions", [])),
                trades_today=len(portfolio_state.get("trades_today", [])),
                wins=sum(1 for t in portfolio_state.get("trades_today", []) if t.get("pnl", 0) > 0),
                losses=sum(1 for t in portfolio_state.get("trades_today", []) if t.get("pnl", 0) < 0),
                positions_json=portfolio_state.get("positions", []),
            )
            _console.print("\n[dim]Review saved to database.[/]")
        except Exception as exc:
            _console.print(f"\n[yellow]DB save failed: {exc}[/]")

    if args.output:
        Path(args.output).write_text(review.model_dump_json(indent=2), encoding="utf-8")
        _console.print(f"\n[dim]Saved to {args.output}[/]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
