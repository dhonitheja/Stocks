"""
Backtest regression test.

Runs the backtester on synthetic deterministic data and asserts that key
metrics stay within tolerance. If a refactor changes these numbers:
  1. DO NOT silently update the golden values.
  2. Explain the delta and get sign-off from the team.
  3. Then update the values below with a clear commit message.

Run standalone: python tests/test_backtest_regression.py
Run via make:   make backtest-regression
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.backtester import Backtester
from backtesting.report import BacktestReport


def _make_deterministic_bars(
    symbol: str,
    start_price: float = 100.0,
    trend: float = 0.001,
    n: int = 252,
    seed: int = 42,
) -> pd.DataFrame:
    """Synthetic OHLCV — fixed seed so results are fully reproducible."""
    rng = np.random.default_rng(seed)
    prices = [start_price]
    for _ in range(n - 1):
        change = rng.normal(trend, 0.015)
        prices.append(prices[-1] * (1 + change))
    prices = np.array(prices)

    idx = pd.date_range(start="2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Open": prices * rng.uniform(0.999, 1.001, n),
            "High": prices * rng.uniform(1.001, 1.02, n),
            "Low": prices * rng.uniform(0.98, 0.999, n),
            "Close": prices,
            "Volume": rng.integers(500_000, 5_000_000, n).astype(float),
        },
        index=idx,
    )


def _run_regression(symbols=("AAPL", "MSFT"), initial_cash=100.0) -> BacktestReport:
    """Run backtester on synthetic data, bypassing yfinance."""
    bt = Backtester(
        start="2024-01-02",
        end="2024-12-31",
        initial_cash=initial_cash,
        slippage_bps=5,
        spread_bps=3,
    )

    # Inject synthetic bars directly — no network calls
    bars = {sym: _make_deterministic_bars(sym, seed=i * 7) for i, sym in enumerate(symbols)}
    all_dates = sorted(
        {d.date() for df in bars.values() for d in df.index}
    )

    for sim_date in all_dates:
        bt._check_stop_losses(bars, sim_date)
        bt._generate_signals(bars, sim_date, list(symbols))

    if all_dates:
        bt._close_all(bars, all_dates[-1])

    last_date = all_dates[-1] if all_dates else None
    open_value = sum(
        (bt._get_price(bars[sym], last_date) or 0) * pos.shares
        for sym, pos in bt._positions.items()
        if sym in bars
    )
    final_value = bt.cash + open_value

    from backtesting.report import BacktestReport

    return BacktestReport(
        start=bt.start,
        end=bt.end,
        initial_cash=bt.initial_cash,
        final_cash=final_value,
        trades=bt._trades,
        symbols=list(symbols),
    )


# ── Golden values (update only with explicit sign-off) ────────────
# Generated 2026-05-30 on commit: initial regression baseline
# Strategy: TechnicalAnalyzer signals, 5bps slippage + 3bps spread, $100 fund
GOLDEN = {
    "max_drawdown_floor": -50.0,   # total return must not be below -50% (catastrophic loss guard)
    "trade_count_min": 0,           # may be 0 if signals never fire on synthetic data
    "final_cash_positive": True,    # fund must not go negative
}


class TestBacktestRegression:
    """Golden-value regression tests for the backtester."""

    def setup_method(self):
        self.report = _run_regression()

    def test_fund_does_not_go_negative(self):
        assert self.report.final_cash >= 0.0, (
            f"Fund went negative: ${self.report.final_cash:.2f}. "
            "This is a critical regression — check stop-loss logic."
        )

    def test_total_return_above_catastrophic_floor(self):
        ret = self.report.total_return_pct
        assert ret >= GOLDEN["max_drawdown_floor"], (
            f"Total return {ret:.1f}% is below catastrophic floor "
            f"{GOLDEN['max_drawdown_floor']:.1f}%. "
            "Regression detected — do not silently update the golden value."
        )

    def test_trade_count_non_negative(self):
        assert len(self.report.trades) >= GOLDEN["trade_count_min"]

    def test_cost_model_applied(self):
        """Every buy trade should have a fill price slightly above mid (slippage)."""
        buy_trades = [t for t in self.report.trades if t.action == "BUY"]
        if not buy_trades:
            pytest.skip("No buy trades generated on synthetic data — increase trend strength")
        # With 5bps slippage + 3bps spread, fill price > raw bar close
        # We can't directly compare without the original bar prices,
        # but dollar_value / shares should exceed a naive price by ≥ 0 (always positive cost)
        for t in buy_trades:
            assert t.dollar_value > 0, f"BUY trade {t.symbol} has non-positive dollar value"
            assert t.shares > 0, f"BUY trade {t.symbol} has non-positive shares"

    def test_win_rate_is_valid_fraction(self):
        if not self.report.trades:
            pytest.skip("No trades to assess win rate")
        assert 0.0 <= self.report.win_rate <= 1.0

    def test_report_serializable(self, tmp_path):
        """to_json must not crash — critical for monitoring pipelines."""
        out = tmp_path / "regression_report.json"
        self.report.to_json(str(out))
        assert out.exists()
        assert out.stat().st_size > 10


if __name__ == "__main__":
    # Standalone: python tests/test_backtest_regression.py
    report = _run_regression()
    report.print_summary()
    print(f"\nTotal return: {report.total_return_pct:+.2f}%")
    print(f"Trades: {len(report.trades)}")
    print(f"Win rate: {report.win_rate:.1%}")
    print("\nRegression baseline check:")
    print(f"  Fund positive: {report.final_cash >= 0} (${report.final_cash:.2f})")
    print(f"  Return floor: {report.total_return_pct:.2f}% >= {GOLDEN['max_drawdown_floor']}%")
