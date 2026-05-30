"""Tests for AlpacaBroker — buy, sell, account, positions."""
import pytest
from unittest.mock import MagicMock, patch

from agent.brokers.base import OrderResult, AccountInfo, PositionInfo
from agent.brokers.alpaca_broker import AlpacaBroker


@pytest.fixture
def broker(mock_alpaca):
    """AlpacaBroker with Alpaca client pre-connected via mock."""
    with patch("agent.brokers.alpaca_broker.os.getenv", side_effect=lambda k, d=None: {
        "ALPACA_API_KEY": "test_key",
        "ALPACA_SECRET_KEY": "test_secret",
        "ALPACA_PAPER": "true",
    }.get(k, d)):
        b = AlpacaBroker()
        b._trading_client = mock_alpaca
        return b


class TestBuy:
    def test_buy_returns_order_result(self, broker):
        result = broker.buy("AAPL", dollar_amount=50.0, reason="test")
        assert isinstance(result, OrderResult)

    def test_buy_success_has_order_id(self, broker):
        result = broker.buy("AAPL", dollar_amount=50.0)
        assert result.order_id == "test-order-id"

    def test_buy_status_filled(self, broker):
        result = broker.buy("AAPL", dollar_amount=50.0)
        assert result.status == "filled"

    def test_buy_network_error_returns_error_result(self, broker):
        broker._trading_client.submit_order.side_effect = Exception("connection timeout")
        result = broker.buy("AAPL", dollar_amount=50.0)
        assert result.status == "error"
        assert "connection timeout" in result.error_msg

    def test_buy_error_result_not_success(self, broker):
        broker._trading_client.submit_order.side_effect = Exception("rejected")
        result = broker.buy("AAPL", dollar_amount=50.0)
        assert result.success is False


class TestSell:
    def test_sell_returns_order_result(self, broker):
        broker._trading_client.get_open_position.return_value = MagicMock(qty="1.0")
        result = broker.sell("AAPL", reason="test")
        assert isinstance(result, OrderResult)

    def test_sell_network_error_returns_error_result(self, broker):
        broker._trading_client.get_open_position.side_effect = Exception("no position")
        result = broker.sell("AAPL")
        assert result.status == "error"

    def test_sell_error_not_success(self, broker):
        broker._trading_client.get_open_position.side_effect = Exception("fail")
        result = broker.sell("AAPL")
        assert result.success is False


class TestAccount:
    def test_get_account_returns_account_info(self, broker):
        broker._trading_client.get_account.return_value = MagicMock(
            equity="100.00", cash="85.00",
            buying_power="85.00", portfolio_value="100.00",
            last_equity="98.00", status="ACTIVE",
        )
        info = broker.get_account()
        assert isinstance(info, AccountInfo)

    def test_account_values_parsed(self, broker):
        broker._trading_client.get_account.return_value = MagicMock(
            equity="150.00", cash="50.00",
            buying_power="50.00", portfolio_value="150.00",
            last_equity="140.00", status="ACTIVE",
        )
        info = broker.get_account()
        assert info.equity == pytest.approx(150.0)
        assert info.cash == pytest.approx(50.0)
        assert info.pnl_today == pytest.approx(10.0)

    def test_account_error_returns_zero_values(self, broker):
        broker._trading_client.get_account.side_effect = Exception("API error")
        info = broker.get_account()
        assert info.equity == 0
        assert info.status == "error"


class TestPositions:
    def test_get_positions_returns_list(self, broker):
        mock_pos = MagicMock(
            symbol="AAPL", qty="2.0", avg_entry_price="150.0",
            current_price="160.0", market_value="320.0",
            unrealized_pl="20.0", unrealized_plpc="0.0667",
        )
        broker._trading_client.get_all_positions.return_value = [mock_pos]
        positions = broker.get_positions()
        assert isinstance(positions, list)
        assert len(positions) == 1

    def test_position_fields_parsed(self, broker):
        mock_pos = MagicMock(
            symbol="MSFT", qty="1.5", avg_entry_price="200.0",
            current_price="220.0", market_value="330.0",
            unrealized_pl="30.0", unrealized_plpc="0.10",
        )
        broker._trading_client.get_all_positions.return_value = [mock_pos]
        pos = broker.get_positions()[0]
        assert isinstance(pos, PositionInfo)
        assert pos.symbol == "MSFT"
        assert pos.qty == pytest.approx(1.5)
        assert pos.unrealized_pnl == pytest.approx(30.0)

    def test_positions_error_returns_empty_list(self, broker):
        broker._trading_client.get_all_positions.side_effect = Exception("error")
        positions = broker.get_positions()
        assert positions == []


class TestOrderResultSuccess:
    def test_filled_is_success(self):
        r = OrderResult("id", "AAPL", "buy", 1.0, 100.0, "filled", 100.0, None)
        assert r.success is True

    def test_error_is_not_success(self):
        r = OrderResult("", "AAPL", "buy", 0, 100.0, "error", 0.0, None)
        assert r.success is False

    def test_rejected_is_not_success(self):
        r = OrderResult("", "AAPL", "buy", 0, 100.0, "rejected", 0.0, None)
        assert r.success is False
