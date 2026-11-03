#!/usr/bin/env python3
"""Operator tool: clear the max-drawdown halt after you have looked at why it tripped.

Requires typing RESET interactively; it refuses to run non-interactively so it cannot be scripted
by an agent. You must give --new-peak (your current account equity as the agent sees it, i.e.
total_fund + realized P&L + unrealized P&L); a default would usually re-trip the halt within a
minute. STOP THE AGENT FIRST: a running agent keeps the halt in memory and would write it back.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.core.risk_manager import RiskManager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--new-peak", type=float, required=True,
                    help="peak equity to restart from (current equity)")
    ap.add_argument("--state-path", type=Path, help=argparse.SUPPRESS)  # tests
    args = ap.parse_args()
    if not (args.new_peak == args.new_peak and 0 < args.new_peak < float("inf")):
        ap.error("--new-peak must be a finite number greater than 0")
    rm = RiskManager(state_path=args.state_path)
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
    peak = args.new_peak
    print(f"Peak equity will restart at ${peak:,.2f}. The agent must NOT be running.")
    if input("Type RESET to clear the halt: ").strip() != "RESET":
        print("Cancelled.")
        return 1
    rm.reset_drawdown_halt(new_peak=peak)
    print("Halt cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
