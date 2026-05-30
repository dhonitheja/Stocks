"""
Broker abstraction layer.
All broker implementations must satisfy this interface so the trading
engine is broker-agnostic — swap Alpaca for Robinhood by changing one
config value.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    action: str            # buy | sell
    quantity: float        # shares (fractional OK)
    notional: float        # dollar amount placed
    status: str            # filled | pending | rejected | error
    filled_price: float
    filled_at: Optional[datetime]
    error_msg: str = ""

    @property
    def success(self) -> bool:
        return self.status not in ("error", "rejected")


@dataclass
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    pnl_today: float
    status: str


@dataclass
class PositionInfo:
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


class BrokerClient(ABC):
    """Abstract broker interface. All methods are synchronous."""

    @abstractmethod
    def buy(self, symbol: str, dollar_amount: float, reason: str = "") -> OrderResult:
        """Place a fractional BUY order for a dollar amount."""

    @abstractmethod
    def sell(self, symbol: str, reason: str = "") -> OrderResult:
        """Close the full position in `symbol`."""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """Return account balance and P&L."""

    @abstractmethod
    def get_positions(self) -> list[PositionInfo]:
        """Return all open positions."""

    @abstractmethod
    def cancel_all_orders(self) -> None:
        """Cancel all pending/open orders."""

    @abstractmethod
    def liquidate_all(self) -> None:
        """Emergency: immediately close every open position."""

    @abstractmethod
    def is_market_open(self) -> bool:
        """Return True if the exchange is currently open for trading."""
