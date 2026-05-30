"""Tests for RiskManager — position sizing, stop-loss, circuit breaker."""
import pytest
from datetime import datetime
from unittest.mock import patch


class TestCanBuy:
    def test_buy_approved_for_valid_trade(self, risk_manager):
        # returns dollar_amount now — any price is valid (fractional shares)
        allowed, reason, dollar_amount = risk_manager.can_buy("AAPL", price=500.0, confidence=0.80)
        assert allowed is True
        assert dollar_amount > 0

    def test_blocked_below_confidence_threshold(self, risk_manager):
        allowed, reason, qty = risk_manager.can_buy("AAPL", price=100.0, confidence=0.50)
        assert allowed is False
        assert "Confidence" in reason

    def test_blocked_when_already_holding(self, risk_manager):
        risk_manager.open_position("AAPL", price=100.0, quantity=1)
        allowed, reason, qty = risk_manager.can_buy("AAPL", price=100.0, confidence=0.90)
        assert allowed is False
        assert "Already holding" in reason

    def test_blocked_when_max_positions_reached(self, risk_manager):
        for i, sym in enumerate(["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]):
            risk_manager.open_position(sym, price=10.0, quantity=1)
            if i < 4:
                risk_manager.cash -= 10.0
        # Drain to max_open_positions
        risk_manager.max_open_positions = 5
        allowed, reason, qty = risk_manager.can_buy("TSLA", price=10.0, confidence=0.90)
        assert allowed is False

    def test_blocked_when_emergency_stop(self, risk_manager):
        risk_manager.emergency_stop = True
        allowed, reason, qty = risk_manager.can_buy("AAPL", price=100.0, confidence=0.90)
        assert allowed is False
        assert "EMERGENCY" in reason.upper()

    def test_blocked_when_news_blackout(self, risk_manager):
        risk_manager.news_blackout = True
        allowed, reason, qty = risk_manager.can_buy("AAPL", price=100.0, confidence=0.90)
        assert allowed is False

    def test_blocked_when_circuit_breaker_triggered(self, risk_manager):
        risk_manager.circuit_breaker_enabled = True
        risk_manager.daily_pnl = -10.0  # large loss
        allowed, reason, qty = risk_manager.can_buy("AAPL", price=1.0, confidence=0.90)
        assert allowed is False


class TestCanSell:
    def test_sell_approved_for_existing_position(self, risk_manager):
        risk_manager.open_position("AAPL", price=100.0, quantity=1)
        pos = risk_manager.positions["AAPL"]
        # Bypass minimum hold time
        from datetime import timedelta
        pos.entry_time = datetime.now() - timedelta(minutes=10)
        allowed, reason = risk_manager.can_sell("AAPL")
        assert allowed is True

    def test_sell_blocked_for_missing_position(self, risk_manager):
        allowed, reason = risk_manager.can_sell("AAPL")
        assert allowed is False

    def test_sell_blocked_below_min_hold_time(self, risk_manager):
        risk_manager.open_position("AAPL", price=100.0, quantity=1)
        # Entry time is right now, min_holding_minutes > 0
        allowed, reason = risk_manager.can_sell("AAPL", reason="signal")
        assert allowed is False


class TestOpenClosePosition:
    def test_open_position_deducts_cash(self, risk_manager):
        initial_cash = risk_manager.cash
        risk_manager.open_position("AAPL", price=50.0, quantity=2)
        assert risk_manager.cash == initial_cash - 100.0
        assert "AAPL" in risk_manager.positions

    def test_close_position_returns_cash(self, risk_manager):
        risk_manager.open_position("AAPL", price=50.0, quantity=2)
        pnl = risk_manager.close_position("AAPL", price=60.0, reason="test")
        assert pnl == pytest.approx(20.0)  # (60-50)*2
        assert "AAPL" not in risk_manager.positions

    def test_close_nonexistent_position_returns_zero(self, risk_manager):
        pnl = risk_manager.close_position("TSLA", price=100.0, reason="test")
        assert pnl == 0.0

    def test_stop_loss_set_correctly_on_open(self, risk_manager):
        risk_manager.open_position("AAPL", price=100.0, quantity=1)
        pos = risk_manager.positions["AAPL"]
        expected_stop = 100.0 * (1 - risk_manager.max_trade_loss_pct)
        assert pos.stop_loss == pytest.approx(expected_stop)


class TestRiskMetrics:
    def test_metrics_reflect_open_positions(self, risk_manager):
        risk_manager.open_position("AAPL", price=50.0, quantity=1)
        metrics = risk_manager.get_metrics()
        assert metrics.open_positions == 1

    def test_metrics_update_daily_pnl(self, risk_manager):
        risk_manager.open_position("AAPL", price=50.0, quantity=1)
        risk_manager.close_position("AAPL", price=60.0, reason="test")
        metrics = risk_manager.get_metrics()
        assert metrics.daily_pnl == pytest.approx(10.0)
