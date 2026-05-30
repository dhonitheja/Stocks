"""
Repository layer — all DB reads and writes go through here.
Keeps SQL out of the business logic.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

from .models import (
    DailySnapshot, MarketOutlookRecord, PositionSnapshot,
    SignalRecord, TradeRecord,
)
from .session import get_session


class TradeRepository:
    @staticmethod
    def save(
        order_id: str, symbol: str, action: str, quantity: float,
        notional: float, entry_price: float, status: str,
        reason: str = "", broker: str = "alpaca",
    ) -> TradeRecord:
        record = TradeRecord(
            order_id=order_id or f"manual-{datetime.utcnow().timestamp()}",
            symbol=symbol, action=action, quantity=quantity,
            notional=notional, entry_price=entry_price,
            status=status, reason=reason, broker=broker,
        )
        with get_session() as session:
            session.add(record)
        logger.debug(f"TradeRecord saved: {action} {symbol}")
        return record

    @staticmethod
    def close(order_id: str, exit_price: float, pnl: float, pnl_pct: float) -> None:
        with get_session() as session:
            record = session.query(TradeRecord).filter_by(order_id=order_id).first()
            if record:
                record.exit_price = exit_price
                record.pnl = pnl
                record.pnl_pct = pnl_pct

    @staticmethod
    def recent(days: int = 30) -> list[TradeRecord]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_session() as session:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.created_at >= cutoff)
                .order_by(TradeRecord.created_at.desc())
                .all()
            )

    @staticmethod
    def today() -> list[TradeRecord]:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0)
        with get_session() as session:
            return (
                session.query(TradeRecord)
                .filter(TradeRecord.created_at >= today_start)
                .order_by(TradeRecord.created_at.asc())
                .all()
            )


class PositionRepository:
    @staticmethod
    def save_all(positions: list[dict]) -> None:
        """Persist current open positions (called on shutdown)."""
        with get_session() as session:
            # Deactivate old snapshots
            session.query(PositionSnapshot).filter_by(is_active=True).update(
                {"is_active": False}
            )
            for p in positions:
                session.add(PositionSnapshot(
                    symbol=p["symbol"],
                    entry_price=p["entry_price"],
                    quantity=p["quantity"],
                    stop_loss=p.get("stop_loss"),
                    target_price=p.get("target_price"),
                    trailing_stop_pct=p.get("trailing_stop_pct"),
                    sector=p.get("sector"),
                    highest_price=p.get("highest_price"),
                    entry_time=p.get("entry_time"),
                    is_active=True,
                ))
        logger.info(f"Persisted {len(positions)} open positions to DB")

    @staticmethod
    def load_active() -> list[PositionSnapshot]:
        """Recover open positions after restart."""
        with get_session() as session:
            return session.query(PositionSnapshot).filter_by(is_active=True).all()


class SignalRepository:
    @staticmethod
    def save(
        symbol: str, action: str, confidence: float,
        technical_score: float, sentiment_score: float,
        fundamental_score: float, reasoning: str,
    ) -> SignalRecord:
        record = SignalRecord(
            symbol=symbol, action=action, confidence=confidence,
            technical_score=technical_score, sentiment_score=sentiment_score,
            fundamental_score=fundamental_score, reasoning=reasoning,
        )
        with get_session() as session:
            session.add(record)
        return record

    @staticmethod
    def mark_acted(signal_id: int) -> None:
        with get_session() as session:
            session.query(SignalRecord).filter_by(id=signal_id).update({"acted_on": True})

    @staticmethod
    def recent(hours: int = 24) -> list[SignalRecord]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with get_session() as session:
            return (
                session.query(SignalRecord)
                .filter(SignalRecord.generated_at >= cutoff)
                .order_by(SignalRecord.generated_at.desc())
                .all()
            )


class SnapshotRepository:
    @staticmethod
    def save_daily(
        portfolio_value: float, cash: float, invested: float,
        daily_pnl: float, daily_pnl_pct: float, total_return_pct: float,
        open_positions: int, trades_today: int, wins: int, losses: int,
        positions_json: list, broker: str = "alpaca",
    ) -> DailySnapshot:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        snap = DailySnapshot(
            date=today, portfolio_value=portfolio_value, cash=cash,
            invested=invested, daily_pnl=daily_pnl, daily_pnl_pct=daily_pnl_pct,
            total_return_pct=total_return_pct, open_positions=open_positions,
            trades_today=trades_today, wins=wins, losses=losses,
            positions_json=positions_json, broker=broker,
        )
        with get_session() as session:
            existing = session.query(DailySnapshot).filter_by(date=today).first()
            if existing:
                for k, v in snap.__dict__.items():
                    if not k.startswith("_"):
                        setattr(existing, k, v)
            else:
                session.add(snap)
        logger.info(f"Daily snapshot saved: {today} | P&L ${daily_pnl:+.2f}")
        return snap

    @staticmethod
    def history(days: int = 30) -> list[DailySnapshot]:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_session() as session:
            return (
                session.query(DailySnapshot)
                .filter(DailySnapshot.date >= cutoff)
                .order_by(DailySnapshot.date.desc())
                .all()
            )


class OutlookRepository:
    @staticmethod
    def save(date: str, sentiment: str, regime: str, confidence: float, data: dict) -> None:
        with get_session() as session:
            existing = session.query(MarketOutlookRecord).filter_by(date=date).first()
            if existing:
                existing.overall_sentiment = sentiment
                existing.market_regime = regime
                existing.confidence = confidence
                existing.analysis_json = data
            else:
                session.add(MarketOutlookRecord(
                    date=date, overall_sentiment=sentiment,
                    market_regime=regime, confidence=confidence,
                    analysis_json=data,
                ))

    @staticmethod
    def latest() -> Optional[MarketOutlookRecord]:
        with get_session() as session:
            return (
                session.query(MarketOutlookRecord)
                .order_by(MarketOutlookRecord.date.desc())
                .first()
            )
