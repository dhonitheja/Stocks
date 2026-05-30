"""
Pydantic schema for settings.yaml — validates config at startup.
Any missing required field or type mismatch raises on import, before any trade runs.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class TradingHours(BaseModel):
    start: str
    end: str
    pre_market_analysis: str


class MarketConfig(BaseModel):
    exchange: str
    currency: str
    timezone: str
    trading_hours: TradingHours
    watchlist: list[str] = Field(min_length=1)
    blacklisted_sectors: list[str] = []


class RiskConfig(BaseModel):
    total_fund: float = Field(gt=0)
    max_position_pct: float = Field(gt=0, le=1)
    max_sector_pct: float = Field(gt=0, le=1)
    min_cash_reserve_pct: float = Field(ge=0, lt=1)
    max_daily_loss_pct: float = Field(gt=0, le=1)
    max_trade_loss_pct: float = Field(gt=0, le=1)
    trailing_stop_pct: float = Field(gt=0, le=1)
    min_confidence_score: float = Field(ge=0, le=1)
    max_open_positions: int = Field(gt=0)
    min_holding_minutes: int = Field(ge=0)
    circuit_breaker_enabled: bool
    news_blackout_events: list[str] = []


class TechnicalConfig(BaseModel):
    ema_periods: list[int]
    rsi_period: int = Field(gt=0)
    rsi_oversold: float = Field(ge=0, le=100)
    rsi_overbought: float = Field(ge=0, le=100)
    macd_fast: int = Field(gt=0)
    macd_slow: int = Field(gt=0)
    macd_signal: int = Field(gt=0)
    bb_period: int = Field(gt=0)
    bb_std: float = Field(gt=0)
    vwap_enabled: bool = True
    volume_surge_threshold: float = Field(gt=1)

    @field_validator("rsi_overbought")
    @classmethod
    def overbought_above_oversold(cls, v: float, info) -> float:
        oversold = info.data.get("rsi_oversold", 0)
        if v <= oversold:
            raise ValueError("rsi_overbought must be > rsi_oversold")
        return v

    @field_validator("macd_slow")
    @classmethod
    def slow_above_fast(cls, v: int, info) -> int:
        fast = info.data.get("macd_fast", 0)
        if v <= fast:
            raise ValueError("macd_slow must be > macd_fast")
        return v


class DecisionWeights(BaseModel):
    technical: float = Field(ge=0, le=1)
    sentiment: float = Field(ge=0, le=1)
    fundamental: float = Field(ge=0, le=1)

    @field_validator("fundamental")
    @classmethod
    def weights_sum_to_one(cls, v: float, info) -> float:
        tech = info.data.get("technical", 0)
        sent = info.data.get("sentiment", 0)
        total = tech + sent + v
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"decision_weights must sum to 1.0 (got {total:.3f})")
        return v


class CostModel(BaseModel):
    commission_pct: float = Field(ge=0, default=0.0)
    slippage_bps: float = Field(ge=0, default=5)
    spread_bps: float = Field(ge=0, default=3)
    market_impact_bps: float = Field(ge=0, default=2)


class ExecutionConfig(BaseModel):
    mode: str

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"execution.mode must be 'paper' or 'live', got '{v}'")
        return v


class BehaviorConfig(BaseModel):
    order_delay_min_seconds: float = Field(ge=0)
    order_delay_max_seconds: float = Field(ge=0)
    order_size_variance_pct: float = Field(ge=0, le=1)
    skip_obvious_pump: bool = True


class AppConfig(BaseModel):
    market: MarketConfig
    risk: RiskConfig
    technical: TechnicalConfig
    decision_weights: DecisionWeights
    execution: ExecutionConfig
    behavior: BehaviorConfig
    cost_model: CostModel = Field(default_factory=CostModel)


def validate_config(raw: dict) -> AppConfig:
    """Parse and validate raw YAML dict. Raises ValidationError on bad config."""
    return AppConfig.model_validate(raw)
