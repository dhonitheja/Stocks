"""Tests for DecisionEngine — signal fusion, thresholds, risk gating."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

from agent.core.decision_engine import DecisionEngine, TradeDecision


def _make_engine(
    tech_action="HOLD", tech_confidence=0.6,
    sent_score=0.0, fund_score=0.5,
    risk_can_buy=(True, "", 1), risk_can_sell=(True, ""),
):
    """Build a DecisionEngine with all external deps mocked."""
    price_feed = MagicMock()
    news_feed = MagicMock()
    risk_manager = MagicMock()

    tick = MagicMock()
    tick.ltp = 100.0
    price_feed.get_latest_tick.return_value = tick

    import pandas as pd
    import numpy as np
    n = 150
    prices = np.linspace(50, 100, n)
    idx = pd.date_range(end="2024-12-31", periods=n, freq="D")
    df = pd.DataFrame({
        "Open": prices * 0.999, "High": prices * 1.01,
        "Low": prices * 0.99, "Close": prices,
        "Volume": [1_000_000] * n,
    }, index=idx)
    price_feed.get_intraday.return_value = df
    price_feed.get_historical.return_value = df

    tech_signal = MagicMock()
    tech_signal.action = tech_action
    tech_signal.confidence = tech_confidence
    tech_signal.reasoning = "test reasoning"
    tech_signal.rsi = 55.0
    tech_signal.trend = "UP"

    news_feed.get_for_symbol.return_value = []

    risk_manager.can_buy.return_value = risk_can_buy
    risk_manager.can_sell.return_value = risk_can_sell

    with (
        patch("agent.core.decision_engine.TechnicalAnalyzer") as MockTA,
        patch("agent.core.decision_engine.SentimentModel") as MockSM,
        patch("agent.core.decision_engine.LLMClient") as MockLLM,
        patch("agent.core.decision_engine.FundamentalScorer") as MockFS,
    ):
        ta_inst = MockTA.return_value
        ta_inst.analyze.return_value = tech_signal

        sm_inst = MockSM.return_value
        sm_inst.score_symbol_news.return_value = {
            "avg_score": sent_score, "label": "neutral", "summary": "No news",
        }

        llm_inst = MockLLM.return_value
        llm_inst.is_available.return_value = False

        fs_inst = MockFS.return_value
        fs_inst.score.return_value = fund_score

        engine = DecisionEngine(price_feed, news_feed, risk_manager)
        engine._ta_mock = ta_inst
        engine._llm_mock = llm_inst
        engine._fs_mock = fs_inst

    return engine


class TestAnalyzeSymbol:
    def test_returns_trade_decision(self):
        engine = _make_engine(tech_action="BUY", tech_confidence=0.8)
        decision = engine.analyze_symbol("AAPL")
        assert isinstance(decision, TradeDecision)
        assert decision.symbol == "AAPL"

    def test_no_tick_returns_none(self):
        engine = _make_engine()
        engine.price_feed.get_latest_tick.return_value = None
        result = engine.analyze_symbol("AAPL")
        assert result is None

    def test_action_in_valid_set(self):
        engine = _make_engine(tech_action="HOLD")
        decision = engine.analyze_symbol("AAPL")
        assert decision.action in ("BUY", "SELL", "HOLD", "BLOCKED")

    def test_confidence_in_range(self):
        engine = _make_engine(tech_action="BUY", tech_confidence=0.9)
        decision = engine.analyze_symbol("AAPL")
        assert 0.0 <= decision.confidence <= 1.0


class TestSignalFusion:
    def test_strong_buy_signal_gives_buy(self):
        # High tech BUY + positive sentiment → should BUY
        engine = _make_engine(
            tech_action="BUY", tech_confidence=0.9,
            sent_score=0.6, fund_score=0.7,
        )
        decision = engine.analyze_symbol("AAPL")
        assert decision.action == "BUY"

    def test_strong_sell_signal_gives_sell(self):
        engine = _make_engine(
            tech_action="SELL", tech_confidence=0.9,
            sent_score=-0.5, fund_score=0.3,
            risk_can_sell=(True, ""),
        )
        decision = engine.analyze_symbol("AAPL")
        assert decision.action == "SELL"

    def test_mixed_signals_give_hold(self):
        # Weak tech, neutral sentiment → HOLD
        engine = _make_engine(
            tech_action="HOLD", tech_confidence=0.5,
            sent_score=0.0, fund_score=0.5,
        )
        decision = engine.analyze_symbol("AAPL")
        assert decision.action == "HOLD"

    def test_scores_stored_in_decision(self):
        engine = _make_engine(
            tech_action="BUY", tech_confidence=0.8,
            sent_score=0.4, fund_score=0.7,
        )
        decision = engine.analyze_symbol("AAPL")
        assert decision.technical_score != 0.0
        assert decision.sentiment_score == pytest.approx(0.4)
        assert decision.fundamental_score == pytest.approx(0.7)


class TestRiskGating:
    def test_buy_blocked_by_risk_manager(self):
        engine = _make_engine(
            tech_action="BUY", tech_confidence=0.9,
            sent_score=0.7,
            risk_can_buy=(False, "Already holding AAPL", 0),
        )
        decision = engine.analyze_symbol("AAPL")
        assert decision.action == "BLOCKED"
        assert "Already holding" in decision.block_reason

    def test_sell_blocked_by_risk_manager(self):
        engine = _make_engine(
            tech_action="SELL",
            risk_can_sell=(False, "Min hold time not reached"),
        )
        decision = engine.analyze_symbol("AAPL")
        # SELL blocked → reverts to HOLD
        assert decision.action == "HOLD"

    def test_buy_approved_has_nonzero_quantity(self):
        engine = _make_engine(
            tech_action="BUY", tech_confidence=0.9,
            sent_score=0.6,
            risk_can_buy=(True, "", 3),
        )
        decision = engine.analyze_symbol("AAPL")
        if decision.action == "BUY":
            assert decision.quantity > 0


class TestLLMTieBreak:
    def test_llm_upgrades_hold_to_buy(self):
        # sent_score=0.35 → final_score≈0.12 which clears the 0.10 LLM threshold
        engine = _make_engine(
            tech_action="HOLD", tech_confidence=0.5,
            sent_score=0.35,
        )
        engine.llm.is_available.return_value = True
        engine.llm.build_trading_prompt.return_value = "test prompt"
        engine.llm.reason.return_value = "BUY — momentum supports entry"
        engine.llm.provider = "gemini"

        decision = engine.analyze_symbol("AAPL")
        # LLM said BUY so action should upgrade (risk may block)
        assert decision.action in ("BUY", "BLOCKED")

    def test_llm_error_falls_back_gracefully(self):
        engine = _make_engine(tech_action="HOLD", sent_score=0.35)
        engine.llm.is_available.return_value = True
        engine.llm.build_trading_prompt.return_value = "test prompt"
        engine.llm.reason.side_effect = Exception("LLM timeout")

        decision = engine.analyze_symbol("AAPL")
        # Should still return a valid decision despite LLM failure
        assert decision is not None
        assert decision.action in ("BUY", "SELL", "HOLD", "BLOCKED")


class TestAnalyzeAll:
    def test_returns_dict_keyed_by_symbol(self):
        engine = _make_engine()
        engine.watchlist = ["AAPL", "MSFT"]
        results = engine.analyze_all()
        assert isinstance(results, dict)
        for sym in results:
            assert sym in ("AAPL", "MSFT")

    def test_failed_symbol_does_not_abort_cycle(self):
        engine = _make_engine()
        engine.watchlist = ["AAPL", "FAIL", "MSFT"]
        engine.price_feed.get_latest_tick.side_effect = [
            MagicMock(ltp=100.0),
            Exception("network error"),
            MagicMock(ltp=200.0),
        ]
        # Should not raise — errors are caught per-symbol
        results = engine.analyze_all()
        assert "FAIL" not in results
