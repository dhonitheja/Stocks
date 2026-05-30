"""
Risk Management Engine
======================
Enforces all capital protection rules.
A 30-year veteran never trades without stop-losses and position limits.
"""
import threading
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, Optional, List
from loguru import logger

from agent.config import CONFIG


@dataclass
class Position:
    """An open position in the portfolio."""
    symbol: str
    entry_price: float
    quantity: float  # fractional shares supported
    entry_time: datetime
    stop_loss: float
    target_price: float
    trailing_stop_pct: float
    sector: str = "General"
    highest_price: float = 0.0  # For trailing stop tracking

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity

    def current_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity

    def current_pnl_pct(self, current_price: float) -> float:
        return (current_price - self.entry_price) / self.entry_price

    def should_stop_loss(self, current_price: float) -> bool:
        """Check if static stop-loss is triggered."""
        return current_price <= self.stop_loss

    def update_trailing_stop(self, current_price: float) -> bool:
        """
        Update trailing stop. Returns True if trailing stop is triggered.
        As price rises, stop-loss rises with it, locking in profit.
        """
        if current_price > self.highest_price:
            self.highest_price = current_price
            # Move stop-loss up
            self.stop_loss = max(
                self.stop_loss,
                current_price * (1 - self.trailing_stop_pct),
            )
        return current_price <= self.stop_loss


@dataclass
class RiskMetrics:
    """Current real-time risk snapshot."""
    total_fund: float
    cash_available: float
    invested_amount: float
    open_positions: int
    daily_pnl: float
    daily_pnl_pct: float
    max_new_position_size: float
    risk_level: str           # LOW | MODERATE | HIGH | CRITICAL
    can_trade: bool
    block_reason: str = ""    # Why trading is blocked (if any)


class RiskManager:
    """
    Portfolio risk manager.
    All trade decisions must pass through this before execution.
    """

    def __init__(self):
        cfg = CONFIG["risk"]
        self.total_fund = cfg["total_fund"]
        self.max_position_pct = cfg["max_position_pct"]          # 0.02
        self.max_sector_pct = cfg["max_sector_pct"]              # 0.20
        self.min_cash_reserve_pct = cfg["min_cash_reserve_pct"]  # 0.30
        self.max_daily_loss_pct = cfg["max_daily_loss_pct"]      # 0.03
        self.max_trade_loss_pct = cfg["max_trade_loss_pct"]      # 0.015
        self.trailing_stop_pct = cfg["trailing_stop_pct"]        # 0.008
        self.min_confidence = cfg["min_confidence_score"]        # 0.72
        self.max_open_positions = cfg["max_open_positions"]      # 10
        self.min_holding_minutes = cfg["min_holding_minutes"]    # 5
        self.circuit_breaker_enabled = cfg["circuit_breaker_enabled"]
        self.blackout_events = cfg["news_blackout_events"]

        self.positions: Dict[str, Position] = {}
        self.daily_pnl = 0.0
        self.cash = self.total_fund
        self.emergency_stop = False
        self.news_blackout = False
        self._today_trades: List[dict] = []
        self._last_reset_date = date.today()
        self._lock = threading.Lock()

        logger.info(f"RiskManager initialized | Fund: ${self.total_fund:,.0f}")

    # ─── Daily Reset ─────────────────────────────────────────

    def _check_daily_reset(self):
        """Reset daily P&L counter at start of new trading day."""
        today = date.today()
        if today != self._last_reset_date:
            logger.info(f"📅 New trading day — resetting daily P&L (was ${self.daily_pnl:,.0f})")
            self.daily_pnl = 0.0
            self._today_trades = []
            self._last_reset_date = today

    # ─── Trade Approval ───────────────────────────────────────

    def can_buy(
        self,
        symbol: str,
        price: float,
        confidence: float,
        sector: str = "General",
    ) -> tuple[bool, str, float]:
        """
        Check if a BUY is allowed and compute approved dollar amount.

        Returns:
            (allowed: bool, reason: str, dollar_amount: float)
            dollar_amount is the notional value to invest (supports fractional shares).
        """
        self._check_daily_reset()

        # Emergency stop
        if self.emergency_stop:
            return False, "🔴 EMERGENCY STOP is active", 0

        # News blackout
        if self.news_blackout:
            return False, "📰 News blackout mode active — waiting for clarity", 0

        # Confidence threshold
        if confidence < self.min_confidence:
            return False, f"Confidence {confidence:.0%} < minimum {self.min_confidence:.0%}", 0

        # Already holding this stock
        if symbol in self.positions:
            return False, f"Already holding {symbol}", 0

        # Max open positions
        if len(self.positions) >= self.max_open_positions:
            return False, f"Max open positions ({self.max_open_positions}) reached", 0

        # Daily loss circuit breaker
        max_daily_loss = self.total_fund * self.max_daily_loss_pct
        if self.daily_pnl <= -max_daily_loss and self.circuit_breaker_enabled:
            return False, f"🟡 Daily loss limit hit (${self.daily_pnl:,.0f}). Trading paused.", 0

        # Cash reserve check
        min_cash = self.total_fund * self.min_cash_reserve_pct
        if self.cash <= min_cash:
            return False, f"Cash below reserve minimum (${min_cash:,.0f})", 0

        # Sector concentration check
        sector_invested = sum(
            p.cost_basis for p in self.positions.values()
            if p.sector == sector
        )
        max_sector = self.total_fund * self.max_sector_pct
        if sector_invested >= max_sector:
            return False, f"Sector '{sector}' already at max ({self.max_sector_pct:.0%})", 0

        # Compute position size (dollar-based; supports fractional shares)
        max_per_trade = self.total_fund * self.max_position_pct
        dollar_amount = min(self.cash - min_cash, max_per_trade)

        if dollar_amount < 1.0:
            return False, f"Available capital ${dollar_amount:.2f} < $1 minimum", 0.0

        return True, f"✅ BUY approved — ${dollar_amount:.2f} @ ${price:.2f}", dollar_amount

    def can_sell(self, symbol: str, reason: str = "") -> tuple[bool, str]:
        """Check if SELL is allowed for a position."""
        if symbol not in self.positions:
            return False, f"{symbol} not in portfolio"

        pos = self.positions[symbol]

        # Minimum holding time
        held_minutes = (datetime.now() - pos.entry_time).total_seconds() / 60
        if held_minutes < self.min_holding_minutes and "stop_loss" not in reason:
            return False, f"Minimum hold time not reached ({held_minutes:.1f} min < {self.min_holding_minutes} min)"

        return True, f"✅ SELL approved for {symbol}"

    # ─── Position Management ──────────────────────────────────

    def open_position(self, symbol: str, price: float, quantity: float, sector: str = "General"):
        """Record a new position after successful buy order."""
        stop_loss_price = price * (1 - self.max_trade_loss_pct)
        target_price = price * (1 + self.max_trade_loss_pct * 2)  # 2:1 reward/risk

        pos = Position(
            symbol=symbol,
            entry_price=price,
            quantity=quantity,
            entry_time=datetime.now(),
            stop_loss=round(stop_loss_price, 4),
            target_price=round(target_price, 4),
            trailing_stop_pct=self.trailing_stop_pct,
            sector=sector,
            highest_price=price,
        )
        with self._lock:
            self.positions[symbol] = pos
            self.cash -= price * quantity

        logger.info(
            f"TRADE | BOUGHT {quantity:.4f}x {symbol} @ ${price:.2f} "
            f"| SL: ${pos.stop_loss:.2f} | Target: ${pos.target_price:.2f} "
            f"| Cash remaining: ${self.cash:,.2f}"
        )
        with self._lock:
            self._today_trades.append({"action": "BUY", "symbol": symbol, "price": price, "qty": quantity})

    def close_position(self, symbol: str, price: float, reason: str = "") -> float:
        """Record position close and compute P&L."""
        with self._lock:
            if symbol not in self.positions:
                return 0.0
            pos = self.positions.pop(symbol)
            pnl = pos.current_pnl(price)
            self.daily_pnl += pnl
            self.cash += price * pos.quantity
            self._today_trades.append({
                "action": "SELL", "symbol": symbol, "price": price,
                "qty": pos.quantity, "pnl": pnl, "reason": reason,
            })

        pnl_pct = pos.current_pnl_pct(price) * 100
        emoji = "🟢" if pnl >= 0 else "🔴"
        logger.info(
            f"TRADE | {emoji} SOLD {pos.quantity:.4f}x {symbol} @ ${price:.2f} "
            f"| P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%) "
            f"| Reason: {reason} | Cash: ${self.cash:,.2f}"
        )
        return pnl

    # ─── Stop-Loss Monitor ────────────────────────────────────

    def check_stop_losses(self, prices: Dict[str, float]) -> List[tuple[str, float, str]]:
        """
        Check all open positions against current prices.
        Returns list of (symbol, price, reason) tuples for positions that hit stop-loss or target.
        """
        exits = []
        for symbol, pos in list(self.positions.items()):
            price = prices.get(symbol)
            if not price:
                continue

            # Update trailing stop
            if pos.update_trailing_stop(price):
                logger.warning(f"🛑 TRAILING STOP hit for {symbol} @ ${price:.2f}")
                exits.append((symbol, price, "trailing_stop_loss"))
                continue

            # Static stop-loss
            if pos.should_stop_loss(price):
                logger.warning(f"🛑 STOP LOSS hit for {symbol} @ ${price:.2f} (SL: ${pos.stop_loss:.2f})")
                exits.append((symbol, price, "stop_loss"))
                continue

            # Target hit
            if price >= pos.target_price:
                logger.info(f"🎯 TARGET hit for {symbol} @ ${price:.2f}")
                exits.append((symbol, price, "target_reached"))

        return exits

    # ─── Kill Switches ────────────────────────────────────────

    def activate_emergency_stop(self, reason: str = "Manual"):
        """🔴 Halt ALL trading immediately."""
        self.emergency_stop = True
        logger.critical(f"🔴 EMERGENCY STOP ACTIVATED | Reason: {reason}")

    def deactivate_emergency_stop(self):
        self.emergency_stop = False
        logger.info("Emergency stop deactivated")

    def set_news_blackout(self, active: bool, event: str = ""):
        """Pause trading during major uncertain events."""
        self.news_blackout = active
        if active:
            logger.warning(f"📰 News blackout mode ON — Event: {event}")
        else:
            logger.info("News blackout mode OFF — Resuming normal trading")

    # ─── Portfolio Snapshot ───────────────────────────────────

    def get_metrics(self, current_prices: Dict[str, float] = None) -> RiskMetrics:
        """Get current risk metrics snapshot."""
        self._check_daily_reset()
        current_prices = current_prices or {}

        invested = sum(p.cost_basis for p in self.positions.values())
        unrealized_pnl = sum(
            p.current_pnl(current_prices.get(s, p.entry_price))
            for s, p in self.positions.items()
        )
        total_pnl = self.daily_pnl + unrealized_pnl
        total_pnl_pct = total_pnl / self.total_fund

        # Risk level assessment
        max_daily_loss = self.total_fund * self.max_daily_loss_pct
        if self.emergency_stop:
            risk_level = "CRITICAL"
        elif total_pnl <= -max_daily_loss * 0.8:
            risk_level = "HIGH"
        elif len(self.positions) >= self.max_open_positions * 0.8:
            risk_level = "MODERATE"
        else:
            risk_level = "LOW"

        can_trade = (
            not self.emergency_stop
            and not self.news_blackout
            and total_pnl > -max_daily_loss
            and len(self.positions) < self.max_open_positions
        )

        return RiskMetrics(
            total_fund=self.total_fund,
            cash_available=self.cash,
            invested_amount=invested,
            open_positions=len(self.positions),
            daily_pnl=round(total_pnl, 2),
            daily_pnl_pct=round(total_pnl_pct * 100, 2),
            max_new_position_size=round(min(
                self.cash - self.total_fund * self.min_cash_reserve_pct,
                self.total_fund * self.max_position_pct
            ), 2),
            risk_level=risk_level,
            can_trade=can_trade,
            block_reason="" if can_trade else (
                "Emergency stop" if self.emergency_stop else
                "News blackout" if self.news_blackout else
                "Daily loss limit hit"
            ),
        )

    def portfolio_summary(self, current_prices: Dict[str, float] = None) -> str:
        """Human-readable portfolio summary."""
        metrics = self.get_metrics(current_prices)
        lines = [
            f"📊 PORTFOLIO SUMMARY",
            f"Total Fund:      ${metrics.total_fund:>12,.0f}",
            f"Cash Available:  ${metrics.cash_available:>12,.0f}",
            f"Invested:        ${metrics.invested_amount:>12,.0f}",
            f"Daily P&L:       ${metrics.daily_pnl:>+12,.0f} ({metrics.daily_pnl_pct:+.2f}%)",
            f"Open Positions:  {metrics.open_positions}/{self.max_open_positions}",
            f"Risk Level:      {metrics.risk_level}",
            f"Can Trade:       {'✅ YES' if metrics.can_trade else '❌ NO — ' + metrics.block_reason}",
        ]
        return "\n".join(lines)
