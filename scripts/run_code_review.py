#!/usr/bin/env python3
"""
Code Review Agent CLI

Usage:
  python scripts/run_code_review.py agent/core/risk_manager.py
  python scripts/run_code_review.py --all-core
  python scripts/run_code_review.py --full-project
  python scripts/run_code_review.py agent/core/risk_manager.py --output report.json
  python scripts/run_code_review.py --all-core --strict   # exits 1 if CRITICAL found
"""

import argparse
import json
import sys
from pathlib import Path

# Run from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

from agent.agents import CodeReviewAgent, MultiAgentCodeReviewer

_console = Console()

_CORE_MODULES = [
    "agent/core/risk_manager.py",
    "agent/core/decision_engine.py",
    "agent/core/execution_engine.py",
]

_ALL_MODULES = _CORE_MODULES + [
    "agent/data/price_feed.py",
    "agent/data/news_feed.py",
    "agent/models/technical_indicators.py",
    "agent/models/sentiment_model.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude-powered code review for StockSage")
    parser.add_argument("files", nargs="*", help="Files to review")
    parser.add_argument("--all-core", action="store_true", help="Review all core modules")
    parser.add_argument("--full-project", action="store_true", help="Review all modules")
    parser.add_argument("--output", metavar="FILE", help="Save JSON report to file")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any CRITICAL found")
    parser.add_argument("--multi-agent", action="store_true",
                        help="Use 4 parallel specialist subagents (Security/Financial/Bugs/Performance)")
    args = parser.parse_args()

    targets = list(args.files)
    if args.full_project:
        targets = _ALL_MODULES
    elif args.all_core:
        targets = _CORE_MODULES

    if not targets:
        parser.print_help()
        return 1

    if args.multi_agent:
        _console.print("[bold cyan]Mode:[/] Multi-agent (Security + Financial + Bugs + Performance in parallel)")
        reviewer = MultiAgentCodeReviewer()
        report = reviewer.review(targets)
    else:
        reviewer = CodeReviewAgent()
        report = reviewer.review(targets)

    # Summary table
    table = Table(title="Code Review Summary", show_lines=True)
    table.add_column("File", style="cyan")
    table.add_column("CRITICAL", style="red")
    table.add_column("HIGH", style="yellow")
    table.add_column("MEDIUM", style="blue")
    table.add_column("LOW", style="dim")
    table.add_column("Live Safe?")

    for f in report.files_reviewed:
        safe = "[green]YES[/]" if f.is_safe_for_live_trading else "[red]NO[/]"
        table.add_row(
            f.file_path,
            str(f.critical_count),
            str(f.high_count),
            str(f.medium_count),
            str(f.low_count),
            safe,
        )

    _console.print()
    _console.print(table)
    _console.print(f"\n[bold]Recommendation:[/] {report.go_live_recommendation}")
    _console.print(f"[bold]Summary:[/] {report.executive_summary}\n")

    if report.top_priority_fixes:
        _console.print("[bold]Top Priority Fixes:[/]")
        for i, fix in enumerate(report.top_priority_fixes, 1):
            _console.print(f"  {i}. {fix}")

    if args.output:
        Path(args.output).write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        _console.print(f"\n[dim]Report saved to {args.output}[/]")

    if args.strict and report.total_critical > 0:
        _console.print(f"\n[red]STRICT MODE: {report.total_critical} CRITICAL issue(s) found — exiting 1[/]")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
