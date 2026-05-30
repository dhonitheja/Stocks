"""Tests for TechnicalAnalyzer — EMA, RSI, MACD, signals."""
import pytest
import numpy as np
import pandas as pd

from agent.models.technical_indicators import TechnicalAnalyzer


@pytest.fixture
def analyzer():
    return TechnicalAnalyzer()


class TestEMA:
    def test_ema_returns_all_periods(self, analyzer, bullish_bars):
        emas = analyzer._compute_emas(bullish_bars["Close"])
        for period in analyzer.ema_periods:
            assert period in emas

    def test_ema_length_matches_input(self, analyzer, bullish_bars):
        emas = analyzer._compute_emas(bullish_bars["Close"])
        for period, series in emas.items():
            assert len(series) == len(bullish_bars)

    def test_ema_is_finite(self, analyzer, bullish_bars):
        emas = analyzer._compute_emas(bullish_bars["Close"])
        for series in emas.values():
            assert np.all(np.isfinite(series.values))

    def test_fast_ema_more_responsive_than_slow(self, analyzer):
        # Strictly monotone uptrend — EMA-9 must exceed EMA-50 throughout
        prices = pd.Series(range(100, 300, 2), dtype=float)  # 100 deterministic points
        emas = analyzer._compute_emas(prices)
        # In a sustained uptrend fast EMA > slow EMA (fast tracks the rise more aggressively)
        assert emas[9].iloc[-1] > emas[50].iloc[-1]


def _trending_prices(start, end, n=100, seed=42):
    """Noisy but reliably trending price series."""
    rng = np.random.default_rng(seed)
    trend = np.linspace(start, end, n)
    noise = rng.normal(0, abs(end - start) * 0.01, n)  # 1% noise amplitude
    return pd.Series(np.maximum(trend + noise, 0.01))


class TestRSI:
    def test_rsi_in_valid_range(self, analyzer):
        rsi = analyzer._compute_rsi(_trending_prices(100, 120))
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_rsi_high_on_uptrend(self, analyzer):
        rsi = analyzer._compute_rsi(_trending_prices(50, 100))
        assert rsi.dropna().iloc[-1] > 60

    def test_rsi_low_on_downtrend(self, analyzer):
        rsi = analyzer._compute_rsi(_trending_prices(100, 50))
        assert rsi.dropna().iloc[-1] < 40


class TestMACD:
    def test_macd_returns_three_series(self, analyzer, bullish_bars):
        macd_line, signal, hist = analyzer._compute_macd(bullish_bars["Close"])
        assert len(macd_line) == len(bullish_bars)
        assert len(signal) == len(bullish_bars)
        assert len(hist) == len(bullish_bars)

    def test_macd_hist_is_difference(self, analyzer, bullish_bars):
        macd_line, signal, hist = analyzer._compute_macd(bullish_bars["Close"])
        diff = (macd_line - signal).dropna()
        hist_valid = hist.dropna()
        pd.testing.assert_series_equal(
            diff.round(8), hist_valid.round(8), check_names=False
        )


class TestAnalyze:
    def test_analyze_returns_signal_object(self, analyzer, bullish_bars):
        result = analyzer.analyze("AAPL", bullish_bars)
        assert result is not None
        assert result.action in ("BUY", "SELL", "HOLD")
        assert 0.0 <= result.confidence <= 1.0

    def test_analyze_returns_none_for_insufficient_data(self, analyzer):
        tiny = pd.DataFrame({
            "Open": [100.0], "High": [101.0], "Low": [99.0],
            "Close": [100.0], "Volume": [1000],
        })
        result = analyzer.analyze("AAPL", tiny)
        assert result is None

    def test_bullish_bars_tend_to_buy(self, analyzer):
        n = 150
        prices = np.linspace(50, 100, n)
        idx = pd.date_range(end="2024-12-31", periods=n, freq="D")
        bars = pd.DataFrame({
            "Open": prices * 0.999, "High": prices * 1.01,
            "Low": prices * 0.99, "Close": prices,
            "Volume": [1_000_000] * n,
        }, index=idx)
        result = analyzer.analyze("TEST", bars)
        assert result is not None
        assert result.action in ("BUY", "HOLD")

    def test_bearish_bars_tend_to_sell(self, analyzer):
        n = 150
        prices = np.linspace(100, 50, n)
        idx = pd.date_range(end="2024-12-31", periods=n, freq="D")
        bars = pd.DataFrame({
            "Open": prices * 1.001, "High": prices * 1.01,
            "Low": prices * 0.99, "Close": prices,
            "Volume": [1_000_000] * n,
        }, index=idx)
        result = analyzer.analyze("TEST", bars)
        assert result is not None
        assert result.action in ("SELL", "HOLD")
