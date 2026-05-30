"""Pydantic schemas for trading AI agents (MarketAnalysisAgent, PortfolioAgent)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ─── Market Analysis ──────────────────────────────────────────────────────────

class SectorOutlook(BaseModel):
    sector: str
    outlook: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    reasoning: str
    top_picks: list[str] = Field(default_factory=list)


class StockRecommendation(BaseModel):
    symbol: str
    action: Literal["BUY_CANDIDATE", "WATCH", "AVOID", "HOLD"]
    reasoning: str
    key_catalyst: str
    risk_factor: str


class MarketOutlook(BaseModel):
    date: str                              # YYYY-MM-DD
    generated_at: str                      # ISO 8601
    model: str
    overall_sentiment: Literal["RISK_ON", "RISK_OFF", "NEUTRAL"]
    market_regime: Literal["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE"]
    vix_assessment: str
    key_themes: list[str] = Field(default_factory=list)
    sector_outlooks: list[SectorOutlook] = Field(default_factory=list)
    top_recommendations: list[StockRecommendation] = Field(default_factory=list)
    stocks_to_avoid: list[str] = Field(default_factory=list)
    key_events_today: list[str] = Field(default_factory=list)
    strategy_adjustments: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    analysis_summary: str


# ─── Portfolio Review ─────────────────────────────────────────────────────────

class PositionReview(BaseModel):
    symbol: str
    action: Literal["HOLD", "ADD", "REDUCE", "CLOSE"]
    reasoning: str
    current_weight_pct: float
    target_weight_pct: float


class PortfolioReview(BaseModel):
    generated_at: str                      # ISO 8601
    model: str
    portfolio_health: Literal["EXCELLENT", "GOOD", "FAIR", "POOR"]
    total_return_pct: float
    daily_pnl_usd: float
    sharpe_assessment: str
    key_insights: list[str] = Field(default_factory=list)
    position_reviews: list[PositionReview] = Field(default_factory=list)
    rebalancing_suggestions: list[str] = Field(default_factory=list)
    risk_warnings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    eod_recommendation: Literal["HOLD_ALL", "REBALANCE", "REDUCE_RISK", "INCREASE_EXPOSURE"]
