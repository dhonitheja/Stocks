"""
StockSage REST API — integration surface for dashboards and external tools.

Endpoints:
  GET  /health               — liveness probe
  GET  /portfolio            — current portfolio state
  GET  /positions            — open positions
  GET  /account              — broker account info
  GET  /trades               — trade history (query: days=30)
  GET  /signals              — recent signals (query: hours=24)
  GET  /snapshots            — daily P&L history (query: days=30)
  GET  /outlook/latest       — latest market analysis
  POST /control/emergency-stop   — halt all trading
  POST /control/resume           — deactivate emergency stop
  WS   /ws/live              — real-time portfolio updates (JSON stream)

Start with:
  python scripts/start_api.py
  uvicorn agent.api.app:create_app --factory --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ── Prometheus metrics ────────────────────────────────────────────────────
_trade_counter = Counter("stocksage_trades_total", "Total trades executed", ["action", "symbol"])
_signal_counter = Counter("stocksage_signals_total", "Total signals generated", ["action"])
_portfolio_value = Gauge("stocksage_portfolio_value_usd", "Current portfolio value in USD")
_daily_pnl = Gauge("stocksage_daily_pnl_usd", "Today's realised P&L in USD")
_open_positions = Gauge("stocksage_open_positions", "Number of open positions")
_request_latency = Histogram("stocksage_request_seconds", "API request latency", ["endpoint"])

from agent.db.repository import (
    OutlookRepository, SignalRepository, SnapshotRepository, TradeRepository,
)

# Global references injected by the trading system at runtime.
# Set these before calling create_app() to enable live broker queries.
_broker_client = None
_risk_manager = None
_trading_active = False


def set_runtime(broker=None, risk_manager=None) -> None:
    """Wire up live references from the trading engine."""
    global _broker_client, _risk_manager
    _broker_client = broker
    _risk_manager = risk_manager


def record_trade(action: str, symbol: str) -> None:
    _trade_counter.labels(action=action, symbol=symbol).inc()


def record_signal(action: str) -> None:
    _signal_counter.labels(action=action).inc()


def create_app() -> FastAPI:
    app = FastAPI(
        title="StockSage API",
        description="AI-powered US equity trading agent — REST integration surface",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health ────────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "timestamp": datetime.utcnow().isoformat(),
            "trading_active": _trading_active,
            "broker_connected": _broker_client is not None,
        }

    @app.get("/metrics")
    def metrics():
        """Prometheus scrape endpoint."""
        if _risk_manager is not None:
            try:
                m = _risk_manager.get_metrics()
                _portfolio_value.set(m.total_fund)
                _daily_pnl.set(m.daily_pnl)
                _open_positions.set(m.open_positions)
            except Exception:
                pass
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ── Portfolio & Positions ─────────────────────────────────────────────

    @app.get("/portfolio")
    def portfolio():
        if _risk_manager is None:
            # Fall back to latest DB snapshot
            snap = SnapshotRepository.history(days=1)
            if snap:
                return snap[0].positions_json or {}
            raise HTTPException(503, "Trading engine not running and no snapshots found")

        metrics = _risk_manager.get_metrics()
        return {
            "total_fund": metrics.total_fund,
            "cash": metrics.cash_available,
            "invested": metrics.invested_amount,
            "daily_pnl": metrics.daily_pnl,
            "daily_pnl_pct": metrics.daily_pnl_pct,
            "open_positions": metrics.open_positions,
            "risk_level": metrics.risk_level,
            "can_trade": metrics.can_trade,
            "block_reason": metrics.block_reason,
        }

    @app.get("/positions")
    def positions():
        if _broker_client is not None:
            try:
                return [p.__dict__ for p in _broker_client.get_positions()]
            except Exception as exc:
                raise HTTPException(502, f"Broker error: {exc}")
        if _risk_manager is not None:
            return [
                {
                    "symbol": sym,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "stop_loss": pos.stop_loss,
                    "target_price": pos.target_price,
                    "sector": pos.sector,
                }
                for sym, pos in _risk_manager.positions.items()
            ]
        raise HTTPException(503, "Trading engine not running")

    @app.get("/account")
    def account():
        if _broker_client is None:
            raise HTTPException(503, "Broker not connected")
        try:
            info = _broker_client.get_account()
            return info.__dict__
        except Exception as exc:
            raise HTTPException(502, f"Broker error: {exc}")

    # ── History ───────────────────────────────────────────────────────────

    @app.get("/trades")
    def trades(days: int = Query(30, ge=1, le=365)):
        records = TradeRepository.recent(days)
        return [
            {
                "id": r.id,
                "order_id": r.order_id,
                "symbol": r.symbol,
                "action": r.action,
                "quantity": r.quantity,
                "notional": r.notional,
                "entry_price": r.entry_price,
                "exit_price": r.exit_price,
                "pnl": r.pnl,
                "pnl_pct": r.pnl_pct,
                "status": r.status,
                "reason": r.reason,
                "broker": r.broker,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
            }
            for r in records
        ]

    @app.get("/signals")
    def signals(hours: int = Query(24, ge=1, le=168)):
        records = SignalRepository.recent(hours)
        return [
            {
                "id": r.id,
                "symbol": r.symbol,
                "action": r.action,
                "confidence": r.confidence,
                "technical_score": r.technical_score,
                "sentiment_score": r.sentiment_score,
                "reasoning": r.reasoning,
                "acted_on": r.acted_on,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            }
            for r in records
        ]

    @app.get("/snapshots")
    def snapshots(days: int = Query(30, ge=1, le=365)):
        records = SnapshotRepository.history(days)
        return [
            {
                "date": r.date,
                "portfolio_value": r.portfolio_value,
                "daily_pnl": r.daily_pnl,
                "daily_pnl_pct": r.daily_pnl_pct,
                "total_return_pct": r.total_return_pct,
                "trades_today": r.trades_today,
                "open_positions": r.open_positions,
            }
            for r in records
        ]

    @app.get("/outlook/latest")
    def outlook_latest():
        record = OutlookRepository.latest()
        if not record:
            raise HTTPException(404, "No market outlook found — run market analysis first")
        return record.analysis_json

    # ── Control ───────────────────────────────────────────────────────────

    @app.post("/control/emergency-stop")
    def emergency_stop(reason: str = "API request"):
        if _risk_manager is None:
            raise HTTPException(503, "Trading engine not running")
        _risk_manager.activate_emergency_stop(reason)
        logger.critical(f"Emergency stop triggered via API | Reason: {reason}")
        return {"status": "EMERGENCY_STOP_ACTIVE", "reason": reason}

    @app.post("/control/resume")
    def resume():
        if _risk_manager is None:
            raise HTTPException(503, "Trading engine not running")
        _risk_manager.deactivate_emergency_stop()
        return {"status": "TRADING_RESUMED"}

    @app.post("/control/news-blackout")
    def news_blackout(active: bool, event: str = ""):
        if _risk_manager is None:
            raise HTTPException(503, "Trading engine not running")
        _risk_manager.set_news_blackout(active, event)
        return {"status": "NEWS_BLACKOUT_ON" if active else "NEWS_BLACKOUT_OFF", "event": event}

    # ── WebSocket live stream ─────────────────────────────────────────────

    @app.websocket("/ws/live")
    async def live_feed(ws: WebSocket):
        """Streams portfolio state JSON every 5 seconds."""
        await ws.accept()
        logger.info(f"WebSocket client connected: {ws.client}")
        try:
            while True:
                payload: dict[str, Any] = {"timestamp": datetime.utcnow().isoformat()}
                if _risk_manager:
                    m = _risk_manager.get_metrics()
                    payload["portfolio"] = {
                        "cash": m.cash_available,
                        "invested": m.invested_amount,
                        "daily_pnl": m.daily_pnl,
                        "risk_level": m.risk_level,
                        "can_trade": m.can_trade,
                    }
                if _broker_client:
                    try:
                        payload["account"] = _broker_client.get_account().__dict__
                    except Exception:
                        pass
                await ws.send_text(json.dumps(payload))
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")

    return app
