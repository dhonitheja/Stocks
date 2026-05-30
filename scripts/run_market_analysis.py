#!/usr/bin/env python3
"""
Pre-Market Analysis Agent CLI

Usage:
  python scripts/run_market_analysis.py                 # analyze today
  python scripts/run_market_analysis.py 2026-05-29      # analyze specific date
  python scripts/run_market_analysis.py --save          # save to DB
  python scripts/run_market_analysis.py --output out.json
"""

import argparse
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

from agent.agents.trading import MarketAnalysisAgent

_console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Opus 4.7 pre-market analysis for StockSage")
    parser.add_argument("date", nargs="?", help="Analysis date YYYY-MM-DD (default: today)")
    parser.add_argument("--save", action="store_true", help="Persist outlook to database")
    parser.add_argument("--output", metavar="FILE", help="Save JSON output to file")
    args = parser.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")

    _console.print(f"\n[bold blue]StockSage — Pre-Market Analysis[/] | {date}\n")

    agent = MarketAnalysisAgent()
    outlook = agent.analyze(date=date)

    # ── Summary panel ──────────────────────────────────────────────────────
    sentiment_color = {
        "RISK_ON": "green", "RISK_OFF": "red", "NEUTRAL": "yellow"
    }.get(outlook.overall_sentiment, "white")

    _console.print(Panel(
        f"[bold]Sentiment:[/] [{sentiment_color}]{outlook.overall_sentiment}[/]   "
        f"[bold]Regime:[/] {outlook.market_regime}   "
        f"[bold]Confidence:[/] {outlook.confidence:.0%}\n\n"
        f"{outlook.analysis_summary}",
        title=f"[bold]Market Outlook — {date}[/]",
        border_style=sentiment_color,
    ))

    # ── Key themes ─────────────────────────────────────────────────────────
    if outlook.key_themes:
        _console.print("\n[bold]Key Themes:[/]")
        for theme in outlook.key_themes:
            _console.print(f"  • {theme}")

    # ── Sector table ───────────────────────────────────────────────────────
    if outlook.sector_outlooks:
        table = Table(title="Sector Outlooks", box=box.ROUNDED, show_lines=True)
        table.add_column("Sector", style="cyan")
        table.add_column("Outlook")
        table.add_column("Top Picks")
        table.add_column("Reasoning")
        for s in outlook.sector_outlooks:
            color = {"BULLISH": "green", "BEARISH": "red", "NEUTRAL": "yellow"}.get(s.outlook, "white")
            table.add_row(
                s.sector,
                f"[{color}]{s.outlook}[/]",
                ", ".join(s.top_picks),
                s.reasoning[:60] + "…" if len(s.reasoning) > 60 else s.reasoning,
            )
        _console.print()
        _console.print(table)

    # ── Stock recommendations ──────────────────────────────────────────────
    if outlook.top_recommendations:
        table = Table(title="Stock Recommendations", box=box.ROUNDED, show_lines=True)
        table.add_column("Symbol", style="cyan")
        table.add_column("Action")
        table.add_column("Key Catalyst")
        table.add_column("Risk Factor")
        for r in outlook.top_recommendations:
            color = {
                "BUY_CANDIDATE": "green", "AVOID": "red",
                "WATCH": "yellow", "HOLD": "blue"
            }.get(r.action, "white")
            table.add_row(
                r.symbol,
                f"[{color}]{r.action}[/]",
                r.key_catalyst[:50] + "…" if len(r.key_catalyst) > 50 else r.key_catalyst,
                r.risk_factor[:50] + "…" if len(r.risk_factor) > 50 else r.risk_factor,
            )
        _console.print()
        _console.print(table)

    if outlook.stocks_to_avoid:
        _console.print(f"\n[red bold]Avoid today:[/] {', '.join(outlook.stocks_to_avoid)}")

    if outlook.strategy_adjustments:
        _console.print("\n[bold]Strategy Adjustments:[/]")
        for adj in outlook.strategy_adjustments:
            _console.print(f"  ⚡ {adj}")

    if outlook.key_events_today:
        _console.print("\n[bold]Key Events:[/]")
        for ev in outlook.key_events_today:
            _console.print(f"  📅 {ev}")

    # ── Persist ────────────────────────────────────────────────────────────
    if args.save:
        try:
            from agent.db.repository import OutlookRepository
            OutlookRepository.save(
                date=outlook.date,
                sentiment=outlook.overall_sentiment,
                regime=outlook.market_regime,
                confidence=outlook.confidence,
                data=outlook.model_dump(),
            )
            _console.print("\n[dim]Outlook saved to database.[/]")
        except Exception as exc:
            _console.print(f"\n[yellow]DB save failed: {exc}[/]")

    if args.output:
        Path(args.output).write_text(outlook.model_dump_json(indent=2), encoding="utf-8")
        _console.print(f"\n[dim]Saved to {args.output}[/]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
