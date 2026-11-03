"""
Risk Management Engine
======================
Enforces all capital protection rules.
A 30-year veteran never trades without stop-losses and position limits.
"""
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional, List
import pytz
from loguru import logger

from agent.config import CONFIG, LOGS_DIR

# Exit reasons that may bypass the minimum-holding-time rule. Matched exactly (not by substring)
# so an arbitrary reason string can never skip the rule.
EXEMPT_EXIT_REASONS = frozenset({"stop_loss", "trailing_stop_loss", "emergency_stop"})
DEFAULT_MAX_DRAWDOWN_PCT = 0.15


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

    def __init__(self, state_path: Optional[Path] = None):
        cfg = CONFIG["risk"]
        # All limits come from config/settings.yaml [risk]; nothing here is hard-coded.
        self.total_fund = cfg["total_fund"]
        self.max_position_pct = cfg["max_position_pct"]
        self.max_sector_pct = cfg["max_sector_pct"]
        self.min_cash_reserve_pct = cfg["min_cash_reserve_pct"]
        self.max_daily_loss_pct = cfg["max_daily_loss_pct"]
        self.max_trade_loss_pct = cfg["max_trade_loss_pct"]
        self.trailing_stop_pct = cfg["trailing_stop_pct"]
        self.min_confidence = cfg["min_confidence_score"]
        self.max_open_positions = cfg["max_open_positions"]
        self.min_holding_minutes = cfg["min_holding_minutes"]
        self.circuit_breaker_enabled = cfg["circuit_breaker_enabled"]
        self.blackout_events = cfg["news_blackout_events"]
        self.max_drawdown_pct = cfg.get("max_drawdown_pct", DEFAULT_MAX_DRAWDOWN_PCT)
        self._tz = pytz.timezone(CONFIG["market"]["timezone"])

        # Overall (peak-to-trough) drawdown halt. Persisted so a restart cannot clear it.
        self._state_path = Path(state_path) if state_path else Path(LOGS_DIR) / "risk_state.json"
        self.peak_equity = self.total_fund
        self.drawdown_halted = False
        self.realized_pnl = 0.0            # cumulative across sessions; persisted
        self.state_save_failed = False
        self.state_missing = False   # True when no state file existed at startup
        self._state_lock = threading.Lock()
        self._last_marks: Dict[str, float] = {}
        self._saved_marks: Dict[str, float] = {}
        self._mark_warned: set = set()
        self._blocked_logged: tuple = ()
        self._now = lambda: datetime.now(self._tz)   # injectable clock for tests

        self.positions: Dict[str, Position] = {}
        self.daily_pnl = 0.0
        self.cash = self.total_fund
        self.emergency_stop = False
        self.news_blackout = False
        self._today_trades: List[dict] = []
        self._last_reset_date = self._today()
        self._lock = threading.Lock()
        self._load_state()
        self.cash = self.total_fund + self.realized_pnl  # realized P&L from earlier sessions counts
        if self.state_missing:
            self._save_state()  # create it now so only the first start reports it missing

        logger.info(f"RiskManager initialized | Fund: ${self.total_fund:,.0f}")

    # ─── Daily Reset ─────────────────────────────────────────

    def _today(self) -> date:
        """Current date in the market timezone (US/Eastern), not the server's local time."""
        return self._now().date()

    def _check_daily_reset(self):
        """Reset daily P&L counter at start of new trading day."""
        today = self._today()
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

        # Overall drawdown halt (cleared only by a human via reset_drawdown_halt)
        if self.drawdown_halted:
            return False, (
                f"🔴 Max drawdown halt ({self.max_drawdown_pct:.0%} from peak) is active"
            ), 0

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
        if held_minutes < self.min_holding_minutes and reason not in EXEMPT_EXIT_REASONS:
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
            self._mark_warned.discard(symbol)
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
            self._last_marks.pop(symbol, None)
            pnl = pos.current_pnl(price)
            self.realized_pnl += pnl
            self.daily_pnl += pnl
            self.cash += price * pos.quantity
            self._today_trades.append({
                "action": "SELL", "symbol": symbol, "price": price,
                "qty": pos.quantity, "pnl": pnl, "reason": reason,
            })

        self._save_state()
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

    # ─── Drawdown halt ────────────────────────────────────────

    def equity(self, prices: Optional[Dict[str, float]] = None) -> float:
        """
        Cash plus open positions marked at `prices`. A missing price falls back to the last known
        mark, then to entry price (with a warning), so a feed outage cannot silently hide a loss.
        """
        prices = prices or {}
        with self._lock:
            total = self.cash
            for sym, p in self.positions.items():
                px = prices.get(sym)
                if px and px > 0:
                    self._last_marks[sym] = px
                else:
                    px = self._last_marks.get(sym) or p.entry_price
                    if sym not in self._mark_warned:     # once per symbol, not every minute
                        self._mark_warned.add(sym)
                        logger.error(f"[Equity] No live price for {sym}; marking at ${px:.2f}. "
                                     "Losses on it are invisible until prices return.")
                total += p.quantity * px
            return total

    def check_drawdown(self, prices: Optional[Dict[str, float]] = None) -> bool:
        """
        Update peak equity and halt new buys when equity falls `max_drawdown_pct` below the peak.

        Returns True if the halt is active. A halt survives restarts and is cleared only by
        `reset_drawdown_halt()`, which is a deliberate human action.
        """
        eq = self.equity(prices)
        changed = False
        with self._lock:
            if eq > self.peak_equity:
                self.peak_equity = eq
                changed = True
            drawdown = (self.peak_equity - eq) / self.peak_equity if self.peak_equity > 0 else 0.0
            if drawdown >= self.max_drawdown_pct and not self.drawdown_halted:
                self.drawdown_halted = True
                changed = True
                logger.critical(
                    f"🔴 MAX DRAWDOWN HALT | equity ${eq:,.2f} is {drawdown:.1%} below peak "
                    f"${self.peak_equity:,.2f} (limit {self.max_drawdown_pct:.0%}). New buys blocked."
                )
        with self._lock:
            marks_moved = any(
                abs(self._last_marks.get(k, 0) - self._saved_marks.get(k, 0)) >= 0.01
                for k in self._last_marks
            ) or set(self._saved_marks) - set(self._last_marks) != set()
        if changed or marks_moved:
            self._save_state()
        return self.drawdown_halted

    def reset_drawdown_halt(self, new_peak: Optional[float] = None) -> None:
        """Human-only: clear the drawdown halt and restart peak tracking from `new_peak`."""
        if new_peak is not None and not (math.isfinite(new_peak) and new_peak > 0):
            raise ValueError("new_peak must be a finite number greater than 0")
        with self._lock:
            self.drawdown_halted = False
            self.peak_equity = new_peak if new_peak is not None else self.total_fund + self.realized_pnl
        self._save_state()
        logger.warning(f"Drawdown halt reset by operator | peak set to ${self.peak_equity:,.2f}")

    def _save_state(self) -> bool:
        """Atomically persist halt state. Serialized so concurrent threads cannot interleave."""
        with self._state_lock:
            with self._lock:
                snapshot = {
                    "peak_equity": self.peak_equity,
                    "drawdown_halted": self.drawdown_halted,
                    "realized_pnl": self.realized_pnl,
                    # marks of open positions only, so a cold start can book down-time losses and
                    # a re-bought symbol never inherits an old trade's mark
                    "marks": {k: v for k, v in self._last_marks.items() if k in self.positions},
                }
                self._saved_marks = dict(snapshot["marks"])
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot), encoding="utf-8")
                os.replace(tmp, self._state_path)
                self.state_save_failed = False
                return True
            except Exception as exc:  # OSError, or a non-serializable value
                self.state_save_failed = True
                logger.critical(
                    f"RISK STATE NOT SAVED ({exc}); a restart may lose the drawdown halt / realized P&L"
                )
                return False

    def _load_state(self) -> None:
        if not self._state_path.exists():
            self.state_missing = True
            logger.warning(
                f"No risk state at {self._state_path}; starting with a fresh peak and no halt. "
                "If this is not a first run (e.g. ephemeral disk), a prior halt may have been lost."
            )
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state is not an object")
            peak = float(data["peak_equity"])
            realized = float(data.get("realized_pnl", 0.0))
            halted = bool(data.get("drawdown_halted", False))
            if not (math.isfinite(peak) and peak > 0 and math.isfinite(realized)):
                raise ValueError("non-finite or non-positive value")
            self.peak_equity, self.realized_pnl, self.drawdown_halted = peak, realized, halted
            marks = data.get("marks", {})
            if isinstance(marks, dict):
                self._last_marks = {
                    str(k): float(v) for k, v in marks.items()
                    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                }
                self._saved_marks = dict(self._last_marks)
            if halted:
                logger.critical("🔴 Max drawdown halt restored from previous session")
        except Exception as exc:  # any unreadable state fails safe: halt rather than trade unguarded
            self.drawdown_halted = True
            aside = self._state_path.with_name(f"{self._state_path.name}.corrupt-{int(time.time())}")
            try:  # keep the evidence (peak, realized P&L) instead of overwriting it on the next save
                self._state_path.rename(aside)
                kept = f"original kept as {aside.name}"
            except OSError:
                kept = "could not move the original aside"
            logger.critical(
                f"Risk state unreadable ({exc}); {kept}. Halting new buys until an operator runs "
                "scripts/reset_drawdown_halt.py --new-peak <current equity>. Realized P&L restarts at 0; "
                "recover it from the preserved file if needed."
            )
            self._save_state()   # persist the halt now, or a restart would see "no file" and un-halt

    # ─── Broker reconciliation ────────────────────────────────

    def reconcile_positions(self, broker_positions: List[dict], grace_seconds: float = 0.0) -> dict:
        """
        Make tracked positions match the broker, which is the source of truth.

        - tracked but not at the broker -> dropped; the loss/gain is booked at the last known mark
          (cost basis, i.e. zero P&L, only when no mark exists -> listed under `unbooked`)
        - quantity differs              -> broker quantity wins (the difference is booked likewise)
        - at the broker but untracked   -> NOT adopted (may not belong to this agent); reported
        - positions opened less than `grace_seconds` ago are left alone (a just-accepted order may
          not show at the broker yet); use ~180 for periodic runs, 0 at startup.
        Callers must pass a list from a call that verifiably succeeded (not one that swallowed an
        error). An empty list while positions are tracked is refused and reported as `blocked_empty`.
        """
        held = {str(p["symbol"]).upper().strip(): p for p in broker_positions}
        dropped, resized, unbooked, young = [], [], [], []
        now = datetime.now()
        with self._lock:
            tracked = {k.upper().strip(): k for k in self.positions}
            def is_young(pos):
                return bool(grace_seconds) and (now - pos.entry_time).total_seconds() < grace_seconds
            young = [k for k in self.positions if is_young(self.positions[k])]
            old_keys = [k for k in self.positions if k not in young]
            if not held and old_keys:
                # An empty broker list while we track (non-new) positions is far more likely a failed
                # or partial call than a real flat book. Never wipe tracking on that evidence.
                blocked = sorted(old_keys)
                if tuple(blocked) != self._blocked_logged:        # log on change, not every cycle
                    self._blocked_logged = tuple(blocked)
                    logger.critical(
                        f"[Reconcile] Broker reports NO positions but {blocked} are tracked; "
                        "refusing to drop them automatically. Verify at the broker."
                    )
                return {"dropped": [], "resized": [], "untracked": [], "blocked_empty": blocked,
                        "unbooked": [], "skipped_new": sorted(young)}
            self._blocked_logged = ()
            for norm, key in list(tracked.items()):
                pos = self.positions[key]
                if key in young:
                    continue
                mark = self._last_marks.get(norm) or self._last_marks.get(key)
                bp = held.get(norm, {}).get("current_price")
                if isinstance(bp, (int, float)) and bp > 0:
                    mark = float(bp)
                price = mark if mark else pos.entry_price
                if norm not in held:
                    pnl = (price - pos.entry_price) * pos.quantity
                    self.realized_pnl += pnl
                    self.daily_pnl += pnl
                    self.cash += price * pos.quantity
                    del self.positions[key]
                    self._last_marks.pop(key, None)
                    self._last_marks.pop(norm, None)
                    dropped.append(key)
                    if not mark:
                        unbooked.append(key)
                    continue
                broker_qty = float(held[norm]["qty"])
                if abs(broker_qty - pos.quantity) > 1e-6:
                    delta = pos.quantity - broker_qty
                    if delta > 0:                 # shares left without us: book at the mark
                        pnl = (price - pos.entry_price) * delta
                        self.realized_pnl += pnl
                        self.daily_pnl += pnl
                    elif broker_qty > 0:          # extra shares: weighted-average the entry price
                        pos.entry_price = (pos.entry_price * pos.quantity + price * -delta) / broker_qty
                    self.cash += delta * price
                    pos.quantity = broker_qty
                    resized.append(key)
                    if not mark:
                        unbooked.append(key)
            for norm, bp in held.items():  # broker marks, so a feed outage right after startup
                px = bp.get("current_price")  # cannot hide a loss behind entry-price marking
                if norm in tracked and isinstance(px, (int, float)) and px > 0:
                    self._last_marks[norm] = float(px)
            untracked = sorted(s for s in held if s not in tracked)
        for sym in dropped:
            logger.warning(f"[Reconcile] {sym} tracked locally but not held at broker; dropped and booked")
        for sym in resized:
            logger.warning(f"[Reconcile] {sym} quantity adjusted to broker value")
        if unbooked:
            logger.warning(f"[Reconcile] No mark for {unbooked}; booked at cost (P&L unknown, check broker)")
        if untracked:
            logger.warning(f"[Reconcile] Broker holds untracked positions (not adopted): {untracked}")
        if dropped or resized:
            self._save_state()
        return {"dropped": dropped, "resized": resized, "untracked": untracked,
                "blocked_empty": [], "unbooked": unbooked, "skipped_new": sorted(young)}

    def trading_allowed(self) -> bool:
        """True when no halt is active. Re-check immediately before placing an order."""
        return not (self.emergency_stop or self.drawdown_halted)

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
            and not self.drawdown_halted
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
                "Max drawdown halt" if self.drawdown_halted else
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
