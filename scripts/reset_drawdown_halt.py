#!/usr/bin/env python3
"""Operator tool: clear the max-drawdown halt after you have looked at why it tripped.

Requires typing RESET interactively; it refuses to run non-interactively so it cannot be scripted
by an agent. Peak equity restarts at total_fund + cumulative realized P&L (or --new-peak).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.core.risk_manager import RiskManager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--new-peak", type=float, help="peak equity to restart from")
    args = ap.parse_args()
    if args.new_peak is not None and not (args.new_peak == args.new_peak and 0 < args.new_peak < float("inf")):
        ap.error("--new-peak must be a finite number greater than 0")
    rm = RiskManager()
    print(f"Halt active:      {rm.drawdown_halted}")
    print(f"Peak equity:      ${rm.peak_equity:,.2f}")
    print(f"Realized P&L:     ${rm.realized_pnl:,.2f}")
    print(f"Limit:            {rm.max_drawdown_pct:.0%} below peak")
    if not rm.drawdown_halted:
        print("Nothing to reset.")
        return 0
    if not sys.stdin.isatty():
        print("Refusing to run non-interactively.", file=sys.stderr)
        return 2
    peak = args.new_peak if args.new_peak is not None else rm.total_fund + rm.realized_pnl
    print(f"Peak equity will restart at ${peak:,.2f}.")
    if input("Type RESET to clear the halt: ").strip() != "RESET":
        print("Cancelled.")
        return 1
    rm.reset_drawdown_halt(new_peak=args.new_peak)
    print("Halt cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
