"""
Database session factory.
Uses DATABASE_URL env var (PostgreSQL) or falls back to SQLite for local dev.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        db_url = os.getenv("DATABASE_URL", "sqlite:///stocksage.db")
        if db_url.startswith("postgres://"):
            # SQLAlchemy 2.x requires postgresql://
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        kwargs = {}
        if db_url.startswith("postgresql"):
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10
            kwargs["pool_pre_ping"] = True

        _engine = create_engine(db_url, **kwargs)
        Base.metadata.create_all(_engine)
        logger.info(f"Database connected: {db_url.split('@')[-1]}")  # hide credentials
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager — auto-commits on success, rolls back on exception."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
