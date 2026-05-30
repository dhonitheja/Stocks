"""
Shared pytest fixtures for StockSage tests.

All external dependencies are mocked:
  - yfinance (no real network calls)
  - alpaca-py (no real broker calls)
  - transformers/FinBERT (no GPU required)
  - feedparser (no RSS fetching)
  - anthropic (no Claude API calls)
"""
import sys
from pathlib import Path
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Price bar helpers ────────────────────────────────────────────

def make_ohlcv(n: int = 100, start_price: float = 100.0, trend: float = 0.001) -> pd.DataFrame:
    """Generate synthetic OHLCV data for indicator tests."""
    prices = [start_price]
    for _ in range(n - 1):
        change = np.random.normal(trend, 0.015)
        prices.append(prices[-1] * (1 + change))

    prices = np.array(prices)
    high = prices * np.random.uniform(1.001, 1.02, n)
    low = prices * np.random.uniform(0.98, 0.999, n)
    volume = np.random.randint(500_000, 5_000_000, n)

    idx = pd.date_range(end=datetime.now(), periods=n, freq="D")
    return pd.DataFrame({
        "Open": prices * np.random.uniform(0.999, 1.001, n),
        "High": high,
        "Low": low,
        "Close": prices,
        "Volume": volume,
    }, index=idx)


@pytest.fixture
def bullish_bars():
    """100 bars with mild uptrend (great for BUY signal tests)."""
    return make_ohlcv(n=100, trend=0.003)


@pytest.fixture
def bearish_bars():
    """100 bars with downtrend (great for SELL signal tests)."""
    return make_ohlcv(n=100, trend=-0.003)


@pytest.fixture
def flat_bars():
    """100 bars with no trend (great for HOLD signal tests)."""
    return make_ohlcv(n=100, trend=0.0)


# ── RiskManager fixture ──────────────────────────────────────────

@pytest.fixture
def risk_manager():
    from agent.core.risk_manager import RiskManager
    return RiskManager()


# ── Mock yfinance ────────────────────────────────────────────────

@pytest.fixture
def mock_yfinance(bullish_bars):
    ticker_mock = MagicMock()
    ticker_mock.history.return_value = bullish_bars
    ticker_mock.info = {
        "trailingPE": 18.0,
        "priceToBook": 2.5,
        "revenueGrowth": 0.15,
        "profitMargins": 0.20,
        "debtToEquity": 50.0,
        "returnOnEquity": 0.22,
    }
    with patch("yfinance.Ticker", return_value=ticker_mock):
        with patch("yfinance.download", return_value=bullish_bars):
            yield ticker_mock


# ── Mock Alpaca ──────────────────────────────────────────────────

@pytest.fixture
def mock_alpaca():
    with patch("alpaca.trading.client.TradingClient") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        client.get_account.return_value = MagicMock(
            cash="85.00", portfolio_value="100.00",
            buying_power="85.00", status="ACTIVE",
        )
        client.submit_order.return_value = MagicMock(
            id="test-order-id", status="filled",
            filled_qty="1", filled_avg_price="100.0",
        )
        yield client


# ── Mock Anthropic ───────────────────────────────────────────────

@pytest.fixture
def mock_anthropic():
    with patch("anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"action": "HOLD", "reasoning": "Test mock"}'
        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        response.usage = MagicMock(
            input_tokens=100, output_tokens=50,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        client.messages.create.return_value = response
        yield client


# ── Mock feedparser ──────────────────────────────────────────────

@pytest.fixture
def mock_feedparser():
    entry = MagicMock()
    entry.title = "Fed holds rates steady amid economic uncertainty"
    entry.summary = "The Federal Reserve decided to keep interest rates unchanged."
    entry.link = "https://example.com/article/1"
    entry.published = "Mon, 01 Jan 2024 12:00:00 +0000"
    entry.get = lambda k, d=None: getattr(entry, k, d)

    feed = MagicMock()
    feed.entries = [entry]
    feed.bozo = False

    with patch("feedparser.parse", return_value=feed):
        yield feed
