"""
SQLAlchemy ORM models for StockSage persistence.
Positions, trades, signals, and daily snapshots survive restarts.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Integer, String, Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class TradeRecord(Base):
    """Every executed buy/sell order."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, index=True, nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    action = Column(String(8), nullable=False)        # buy | sell
    quantity = Column(Float, nullable=False)
    notional = Column(Float)                          # dollar amount
    entry_price = Column(Float)
    exit_price = Column(Float)
    status = Column(String(16))                       # filled | pending | error
    reason = Column(String(256))
    pnl = Column(Float)
    pnl_pct = Column(Float)
    broker = Column(String(32), default="alpaca")
    executed_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class PositionSnapshot(Base):
    """Open positions persisted on shutdown for recovery on restart."""
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    stop_loss = Column(Float)
    target_price = Column(Float)
    trailing_stop_pct = Column(Float)
    sector = Column(String(64))
    highest_price = Column(Float)
    entry_time = Column(DateTime)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SignalRecord(Base):
    """Every generated trade signal (whether acted on or not)."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)
    action = Column(String(8))                        # BUY | SELL | HOLD
    confidence = Column(Float)
    technical_score = Column(Float)
    sentiment_score = Column(Float)
    fundamental_score = Column(Float)
    reasoning = Column(Text)
    acted_on = Column(Boolean, default=False)
    generated_at = Column(DateTime, default=datetime.utcnow, index=True)


class DailySnapshot(Base):
    """End-of-day portfolio snapshot for performance tracking."""
    __tablename__ = "daily_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), unique=True, index=True, nullable=False)  # YYYY-MM-DD
    portfolio_value = Column(Float)
    cash = Column(Float)
    invested = Column(Float)
    daily_pnl = Column(Float)
    daily_pnl_pct = Column(Float)
    total_return_pct = Column(Float)
    open_positions = Column(Integer)
    trades_today = Column(Integer)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    positions_json = Column(JSON)       # list of open positions at EOD
    broker = Column(String(32))
    created_at = Column(DateTime, default=datetime.utcnow)


class MarketOutlookRecord(Base):
    """Persisted pre-market analysis outputs for review and backtesting."""
    __tablename__ = "market_outlooks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), index=True, nullable=False)
    overall_sentiment = Column(String(16))
    market_regime = Column(String(32))
    confidence = Column(Float)
    analysis_json = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
