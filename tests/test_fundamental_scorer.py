"""Tests for FundamentalScorer — yfinance-based stock health scoring."""
import pytest
from unittest.mock import patch, MagicMock

from agent.models.fundamental_scorer import FundamentalScorer


@pytest.fixture
def scorer():
    return FundamentalScorer()


def _mock_info(**kwargs):
    defaults = {
        "trailingPE": None, "forwardPE": None,
        "priceToBook": None, "revenueGrowth": None,
        "profitMargins": None, "debtToEquity": None,
        "returnOnEquity": None,
    }
    defaults.update(kwargs)
    ticker = MagicMock()
    ticker.info = defaults
    return ticker


class TestScoreRange:
    def test_score_is_between_0_and_1(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(trailingPE=20.0)):
            score = scorer.score("AAPL")
        assert 0.0 <= score <= 1.0

    def test_no_data_returns_neutral(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info()):
            score = scorer.score("AAPL")
        assert score == 0.5

    def test_error_returns_neutral(self, scorer):
        with patch("yfinance.Ticker", side_effect=Exception("network error")):
            score = scorer.score("AAPL")
        assert score == 0.5


class TestPERatio:
    def test_low_pe_scores_high(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(trailingPE=8.0)):
            score = scorer._compute("AAPL")
        assert score > 0.7

    def test_high_pe_scores_low(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(trailingPE=80.0)):
            score = scorer._compute("AAPL")
        assert score < 0.3

    def test_fair_pe_scores_near_neutral(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(trailingPE=22.0)):
            score = scorer._compute("AAPL")
        assert 0.4 <= score <= 0.7


class TestDebtRatio:
    def test_low_debt_scores_high(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(debtToEquity=10.0)):
            score = scorer._compute("AAPL")
        assert score > 0.7

    def test_high_debt_scores_low(self, scorer):
        with patch("yfinance.Ticker", return_value=_mock_info(debtToEquity=400.0)):
            score = scorer._compute("AAPL")
        assert score < 0.3


class TestCaching:
    def test_score_cached_after_first_call(self, scorer):
        call_count = 0

        def mock_ticker(sym):
            nonlocal call_count
            call_count += 1
            return _mock_info(trailingPE=20.0)

        with patch("yfinance.Ticker", side_effect=mock_ticker):
            scorer.score("AAPL")
            scorer.score("AAPL")  # should hit cache
        assert call_count == 1

    def test_different_symbols_not_shared(self, scorer):
        with patch("yfinance.Ticker", side_effect=[
            _mock_info(trailingPE=8.0),   # AAPL — cheap
            _mock_info(trailingPE=80.0),  # TSLA — expensive
        ]):
            score_aapl = scorer.score("AAPL")
            score_tsla = scorer.score("TSLA")
        assert score_aapl > score_tsla
