#!/usr/bin/env python3
"""
Backtest CLI

Usage:
  python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31
  python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --symbols AAPL MSFT NVDA
  python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --all --output results.json
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from agent.config import CONFIG
from backtesting import Backtester

_DEFAULT_SYMBOLS = CONFIG["market"]["watchlist"]


def main() -> int:
    parser = argparse.ArgumentParser(description="StockSage historical backtester")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Symbols to test (default: full watchlist)")
    parser.add_argument("--all", dest="all_symbols", action="store_true",
                        help="Use full watchlist")
    parser.add_argument("--cash", type=float, default=100.0, help="Initial cash (default: $100)")
    parser.add_argument("--output", metavar="FILE", help="Save JSON report to file")
    args = parser.parse_args()

    symbols = args.symbols or _DEFAULT_SYMBOLS

    bt = Backtester(start=args.start, end=args.end, initial_cash=args.cash)
    report = bt.run(symbols)
    report.print_summary()

    if args.output:
        report.to_json(args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
