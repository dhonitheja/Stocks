#!/usr/bin/env python3
"""
QA Testing Agent CLI

Usage:
  python scripts/run_qa_agent.py agent/core/risk_manager.py
  python scripts/run_qa_agent.py --priority-modules
  python scripts/run_qa_agent.py --full-suite
  python scripts/run_qa_agent.py --priority-modules --run-tests
  python scripts/run_qa_agent.py --full-suite --output-dir tests/generated
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

from agent.agents import QATestingAgent

_console = Console()

_PRIORITY_MODULES = [
    "agent/core/risk_manager.py",
    "agent/core/decision_engine.py",
    "agent/models/technical_indicators.py",
    "agent/core/execution_engine.py",
]

_FULL_SUITE = _PRIORITY_MODULES + [
    "agent/data/price_feed.py",
    "agent/models/sentiment_model.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude-powered QA test generator for StockSage")
    parser.add_argument("files", nargs="*", help="Modules to generate tests for")
    parser.add_argument("--priority-modules", action="store_true", help="Generate tests for highest-risk modules")
    parser.add_argument("--full-suite", action="store_true", help="Generate full test suite")
    parser.add_argument("--run-tests", action="store_true", help="Run pytest after generation")
    parser.add_argument("--output-dir", default="tests", metavar="DIR", help="Output directory (default: tests/)")
    args = parser.parse_args()

    targets = list(args.files)
    if args.full_suite:
        targets = _FULL_SUITE
    elif args.priority_modules:
        targets = _PRIORITY_MODULES

    if not targets:
        parser.print_help()
        return 1

    os.environ.setdefault("QA_OUTPUT_DIR", args.output_dir)

    agent = QATestingAgent()
    report = agent.generate(targets)

    # Summary table
    table = Table(title="QA Generation Summary", show_lines=True)
    table.add_column("Test File", style="cyan")
    table.add_column("Module Under Test")
    table.add_column("Tests", justify="right")
    table.add_column("Coverage Areas")

    for tf in report.test_files:
        table.add_row(
            tf.file_name,
            tf.module_under_test,
            str(tf.test_count),
            ", ".join(tf.coverage_areas[:3]),
        )

    _console.print()
    _console.print(table)
    _console.print(f"\n[bold]Total tests generated:[/] {report.total_test_count}")
    _console.print(f"[bold]Estimated coverage:[/] {report.estimated_coverage_pct:.0f}%")
    _console.print(f"[bold]conftest.py generated:[/] {report.conftest_generated}")

    if report.missing_coverage:
        _console.print("\n[yellow]Missing coverage:[/]")
        for area in report.missing_coverage:
            _console.print(f"  - {area}")

    if args.run_tests:
        _console.print(f"\n[bold]Running pytest {args.output_dir}/...[/]")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", args.output_dir, "-v", "--tb=short"],
            cwd=str(Path(__file__).parent.parent),
        )
        return result.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
