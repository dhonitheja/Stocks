"""Tests for StockSage REST API — FastAPI endpoints."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import agent.api.app as api_module
from agent.api.app import create_app


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests."""
    api_module._broker_client = None
    api_module._risk_manager = None
    api_module._trading_active = False
    yield
    api_module._broker_client = None
    api_module._risk_manager = None
    api_module._trading_active = False


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def mock_risk_manager():
    rm = MagicMock()
    metrics = MagicMock()
    metrics.total_fund = 100.0
    metrics.cash_available = 85.0
    metrics.invested_amount = 15.0
    metrics.daily_pnl = 2.5
    metrics.daily_pnl_pct = 2.5
    metrics.open_positions = 1
    metrics.risk_level = "LOW"
    metrics.can_trade = True
    metrics.block_reason = ""
    rm.get_metrics.return_value = metrics
    rm.positions = {}
    return rm


@pytest.fixture
def mock_broker():
    from agent.brokers.base import AccountInfo, PositionInfo
    broker = MagicMock()
    broker.get_account.return_value = AccountInfo(
        equity=100.0, cash=85.0, buying_power=85.0,
        portfolio_value=100.0, pnl_today=2.5, status="ACTIVE",
    )
    broker.get_positions.return_value = [
        PositionInfo(
            symbol="AAPL", qty=0.5, avg_entry=150.0,
            current_price=160.0, market_value=80.0,
            unrealized_pnl=5.0, unrealized_pnl_pct=6.67,
        )
    ]
    return broker


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_has_timestamp(self, client):
        resp = client.get("/health")
        assert "timestamp" in resp.json()

    def test_health_broker_connected_false_when_no_broker(self, client):
        resp = client.get("/health")
        assert resp.json()["broker_connected"] is False


class TestPortfolio:
    def test_portfolio_503_without_engine(self, client):
        with patch("agent.api.app.SnapshotRepository") as mock_snap:
            mock_snap.history.return_value = []
            resp = client.get("/portfolio")
        assert resp.status_code == 503

    def test_portfolio_returns_metrics_when_engine_wired(self, client, mock_risk_manager):
        api_module._risk_manager = mock_risk_manager
        resp = client.get("/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert "cash" in data
        assert "daily_pnl" in data
        assert data["cash"] == pytest.approx(85.0)


class TestPositions:
    def test_positions_503_without_engine(self, client):
        resp = client.get("/positions")
        assert resp.status_code == 503

    def test_positions_from_broker(self, client, mock_broker):
        api_module._broker_client = mock_broker
        resp = client.get("/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "AAPL"

    def test_positions_from_risk_manager_when_no_broker(self, client, mock_risk_manager):
        from agent.core.risk_manager import Position
        from datetime import datetime
        pos = MagicMock()
        pos.entry_price = 150.0
        pos.quantity = 1
        pos.stop_loss = 147.0
        pos.target_price = 165.0
        pos.sector = "Technology"
        mock_risk_manager.positions = {"AAPL": pos}
        api_module._risk_manager = mock_risk_manager
        resp = client.get("/positions")
        assert resp.status_code == 200


class TestAccount:
    def test_account_503_without_broker(self, client):
        resp = client.get("/account")
        assert resp.status_code == 503

    def test_account_returns_data(self, client, mock_broker):
        api_module._broker_client = mock_broker
        resp = client.get("/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash"] == pytest.approx(85.0)
        assert data["status"] == "ACTIVE"


class TestTrades:
    def test_trades_returns_list(self, client):
        with patch("agent.api.app.TradeRepository") as mock_repo:
            mock_repo.recent.return_value = []
            resp = client.get("/trades")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_trades_default_days_30(self, client):
        with patch("agent.api.app.TradeRepository") as mock_repo:
            mock_repo.recent.return_value = []
            client.get("/trades")
            mock_repo.recent.assert_called_once_with(30)

    def test_trades_custom_days(self, client):
        with patch("agent.api.app.TradeRepository") as mock_repo:
            mock_repo.recent.return_value = []
            client.get("/trades?days=7")
            mock_repo.recent.assert_called_once_with(7)


class TestSignals:
    def test_signals_returns_list(self, client):
        with patch("agent.api.app.SignalRepository") as mock_repo:
            mock_repo.recent.return_value = []
            resp = client.get("/signals")
        assert resp.status_code == 200

    def test_signals_default_hours_24(self, client):
        with patch("agent.api.app.SignalRepository") as mock_repo:
            mock_repo.recent.return_value = []
            client.get("/signals")
            mock_repo.recent.assert_called_once_with(24)


class TestOutlook:
    def test_outlook_404_when_empty(self, client):
        with patch("agent.api.app.OutlookRepository") as mock_repo:
            mock_repo.latest.return_value = None
            resp = client.get("/outlook/latest")
        assert resp.status_code == 404

    def test_outlook_returns_json_when_present(self, client):
        mock_record = MagicMock()
        mock_record.analysis_json = {"regime": "BULL", "picks": []}
        with patch("agent.api.app.OutlookRepository") as mock_repo:
            mock_repo.latest.return_value = mock_record
            resp = client.get("/outlook/latest")
        assert resp.status_code == 200
        assert resp.json()["regime"] == "BULL"


class TestControl:
    def test_emergency_stop_503_without_engine(self, client):
        resp = client.post("/control/emergency-stop")
        assert resp.status_code == 503

    def test_emergency_stop_activates(self, client, mock_risk_manager):
        api_module._risk_manager = mock_risk_manager
        resp = client.post("/control/emergency-stop?reason=test")
        assert resp.status_code == 200
        mock_risk_manager.activate_emergency_stop.assert_called_once()

    def test_resume_503_without_engine(self, client):
        resp = client.post("/control/resume")
        assert resp.status_code == 503

    def test_resume_deactivates(self, client, mock_risk_manager):
        api_module._risk_manager = mock_risk_manager
        resp = client.post("/control/resume")
        assert resp.status_code == 200
        mock_risk_manager.deactivate_emergency_stop.assert_called_once()
