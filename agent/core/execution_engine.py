"""
Execution Engine — Alpaca Broker
=================================
Places real (or paper) orders via Alpaca's REST API.
Alpaca supports:
  - Fractional shares (crucial for $100 fund — buy $5 of NVDA!)
  - Commission-free trading
  - Free paper trading API
  - $0 account minimum

Paper mode:  https://paper-api.alpaca.markets
Live mode:   https://api.alpaca.markets
"""
import os
import random
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass
from loguru import logger

from agent.config import CONFIG


@dataclass
class OrderResult:
    """Result of a placed order."""
    order_id: str
    symbol: str
    action: str           # buy | sell
    quantity: float       # fractional OK
    notional: float       # dollar amount (for fractional orders)
    status: str           # filled | pending | rejected | error
    filled_price: float
    filled_at: Optional[datetime]
    error_msg: str = ""


class ExecutionEngine:
    """
    Order execution via Alpaca Markets API.
    Handles both paper and live trading modes.
    Supports fractional shares for small funds like $100.
    """

    def __init__(self):
        self.cfg = CONFIG
        self.is_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        mode = CONFIG["execution"]["mode"]

        # Determine base URL
        if mode == "paper" or self.is_paper:
            self.base_url = CONFIG["broker"]["paper_base_url"]
            logger.info("🧪 ExecutionEngine: PAPER TRADING mode (no real money)")
        else:
            self.base_url = CONFIG["broker"]["live_base_url"]
            logger.warning("💰 ExecutionEngine: LIVE TRADING mode — real money at risk!")

        self._client = None
        self._trading_client = None

    def _ensure_connected(self):
        """Lazy-connect to Alpaca on first use."""
        if self._client:
            return
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient

            api_key = os.getenv("ALPACA_API_KEY")
            secret_key = os.getenv("ALPACA_SECRET_KEY")

            if not api_key or not secret_key:
                raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")

            self._trading_client = TradingClient(
                api_key=api_key,
                secret_key=secret_key,
                paper=self.is_paper,
            )
            self._data_client = StockHistoricalDataClient(
                api_key=api_key,
                secret_key=secret_key,
            )
            logger.info("✅ Connected to Alpaca API")

        except ImportError:
            logger.error("alpaca-py not installed. Run: pip install alpaca-py")
            raise

    # ─── Buy Order ────────────────────────────────────────────

    def buy(
        self,
        symbol: str,
        dollar_amount: float,
        reason: str = "",
    ) -> OrderResult:
        """
        Place a fractional BUY order for a dollar amount.
        
        For a $100 fund, use dollar amounts (e.g., $15 of AAPL)
        instead of share counts — Alpaca handles fractional shares.
        
        Args:
            symbol: Stock ticker (e.g., "AAPL")
            dollar_amount: Dollar amount to invest (e.g., 15.00)
            reason: Human-readable reason for logging
        """
        self._ensure_connected()
        logger.info(f"📤 Placing BUY | {symbol} | ${dollar_amount:.2f} | Reason: {reason}")

        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            # Use notional (dollar amount) for fractional shares
            order_data = MarketOrderRequest(
                symbol=symbol,
                notional=round(dollar_amount, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )

            order = self._trading_client.submit_order(order_data)

            result = OrderResult(
                order_id=str(order.id),
                symbol=symbol,
                action="buy",
                quantity=float(order.qty) if order.qty else 0,
                notional=dollar_amount,
                status=str(order.status),
                filled_price=float(order.filled_avg_price) if order.filled_avg_price else 0.0,
                filled_at=order.filled_at,
            )
            logger.info(f"✅ BUY order placed | {symbol} | ID: {order.id} | Status: {order.status}")
            return result

        except Exception as e:
            logger.error(f"BUY order failed for {symbol}: {e}")
            return OrderResult(
                order_id="", symbol=symbol, action="buy",
                quantity=0, notional=dollar_amount,
                status="error", filled_price=0.0, filled_at=None,
                error_msg=str(e),
            )

    # ─── Sell Order ───────────────────────────────────────────

    def sell(
        self,
        symbol: str,
        reason: str = "",
    ) -> OrderResult:
        """
        Sell ALL shares of a symbol (close full position).
        
        Args:
            symbol: Stock ticker
            reason: Human-readable reason (stop_loss, target, signal, etc.)
        """
        self._ensure_connected()
        logger.info(f"📤 Placing SELL | {symbol} | Reason: {reason}")

        try:
            # Get current position quantity
            position = self._trading_client.get_open_position(symbol)
            qty = float(position.qty)

            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )

            order = self._trading_client.submit_order(order_data)
            logger.info(f"✅ SELL order placed | {symbol} | {qty} shares | ID: {order.id}")

            return OrderResult(
                order_id=str(order.id),
                symbol=symbol,
                action="sell",
                quantity=qty,
                notional=0,
                status=str(order.status),
                filled_price=float(order.filled_avg_price) if order.filled_avg_price else 0.0,
                filled_at=order.filled_at,
            )

        except Exception as e:
            logger.error(f"SELL order failed for {symbol}: {e}")
            return OrderResult(
                order_id="", symbol=symbol, action="sell",
                quantity=0, notional=0,
                status="error", filled_price=0.0, filled_at=None,
                error_msg=str(e),
            )

    # ─── Portfolio Status ─────────────────────────────────────

    def get_account(self) -> Dict:
        """Get Alpaca account info (balance, buying power, etc.)."""
        self._ensure_connected()
        try:
            account = self._trading_client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "pnl_today": float(account.equity) - float(account.last_equity),
                "status": str(account.status),
                "pattern_day_trader": account.pattern_day_trader,
            }
        except Exception as e:
            logger.error(f"Failed to fetch account: {e}")
            return {}

    def get_positions_strict(self) -> List[Dict]:
        """Like get_positions() but raises on failure, so callers can tell "none" from "error"."""
        self._ensure_connected()
        positions = self._trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pnl": float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
            }
            for p in positions
        ]

    def get_positions(self) -> List[Dict]:
        """Get all open positions (returns [] on error; use get_positions_strict to detect it)."""
        try:
            return self.get_positions_strict()
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def cancel_all_orders(self):
        """Cancel all pending orders (emergency use)."""
        self._ensure_connected()
        try:
            self._trading_client.cancel_orders()
            logger.warning("🚨 All pending orders cancelled")
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")

    def liquidate_all(self):
        """🔴 Emergency: Close ALL positions immediately."""
        self._ensure_connected()
        logger.critical("🔴 LIQUIDATING ALL POSITIONS")
        try:
            self._trading_client.close_all_positions(cancel_orders=True)
        except Exception as e:
            logger.error(f"Liquidation failed: {e}")

    # ─── Market Hours ─────────────────────────────────────────

    def is_market_open(self) -> bool:
        """Check if US market is currently open via Alpaca."""
        self._ensure_connected()
        try:
            clock = self._trading_client.get_clock()
            return clock.is_open
        except Exception:
            # Fallback: time-based check
            import pytz
            from datetime import datetime
            et = pytz.timezone("America/New_York")
            now = datetime.now(et)
            if now.weekday() >= 5:
                return False
            return (
                now.hour > 9 or (now.hour == 9 and now.minute >= 30)
            ) and now.hour < 16
