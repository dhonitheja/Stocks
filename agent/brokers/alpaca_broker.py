"""
Alpaca Markets broker implementation.
Supports paper trading (https://paper-api.alpaca.markets) and live trading.
Alpaca supports fractional shares, commission-free trades, and $0 minimum.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.config import CONFIG
from .base import AccountInfo, BrokerClient, OrderResult, PositionInfo


class AlpacaBroker(BrokerClient):
    """Alpaca Markets broker — paper and live US equity trading."""

    def __init__(self):
        mode = CONFIG["execution"]["mode"]
        self.is_paper = mode == "paper" or os.getenv("ALPACA_PAPER", "true").lower() == "true"
        self._trading_client = None
        self._data_client = None

        mode_label = "PAPER" if self.is_paper else "LIVE"
        logger.info(f"AlpacaBroker initialized — {mode_label} mode")

    def _ensure_connected(self) -> None:
        if self._trading_client:
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
            self._data_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
            logger.info("Connected to Alpaca API")
        except ImportError:
            logger.error("alpaca-py not installed. Run: pip install alpaca-py")
            raise

    def buy(self, symbol: str, dollar_amount: float, reason: str = "") -> OrderResult:
        self._ensure_connected()
        client_order_id = f"ss-buy-{symbol}-{uuid.uuid4().hex[:12]}"
        logger.info(f"BUY | {symbol} | ${dollar_amount:.2f} | {reason}")
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            order = self._trading_client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    notional=round(dollar_amount, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id,
                )
            )
            logger.info(f"BUY placed | {symbol} | ID: {order.id} | Status: {order.status}")
            # filled_avg_price is None until settled; fall back to 0.0 (caller handles)
            return OrderResult(
                order_id=str(order.id),
                symbol=symbol,
                action="buy",
                quantity=float(order.qty or 0),
                notional=dollar_amount,
                status=str(order.status),
                filled_price=float(order.filled_avg_price or 0),
                filled_at=order.filled_at,
            )
        except Exception as e:
            logger.error(f"BUY failed | {symbol}: {e}")
            return OrderResult(
                order_id="", symbol=symbol, action="buy",
                quantity=0, notional=dollar_amount,
                status="error", filled_price=0.0, filled_at=None,
                error_msg=str(e),
            )

    def sell(self, symbol: str, reason: str = "") -> OrderResult:
        self._ensure_connected()
        logger.info(f"SELL | {symbol} | {reason}")
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            position = self._trading_client.get_open_position(symbol)
            qty = float(position.qty)

            order = self._trading_client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            logger.info(f"SELL placed | {symbol} | {qty} shares | ID: {order.id}")
            return OrderResult(
                order_id=str(order.id),
                symbol=symbol,
                action="sell",
                quantity=qty,
                notional=0,
                status=str(order.status),
                filled_price=float(order.filled_avg_price or 0),
                filled_at=order.filled_at,
            )
        except Exception as e:
            logger.error(f"SELL failed | {symbol}: {e}")
            return OrderResult(
                order_id="", symbol=symbol, action="sell",
                quantity=0, notional=0,
                status="error", filled_price=0.0, filled_at=None,
                error_msg=str(e),
            )

    def get_account(self) -> AccountInfo:
        self._ensure_connected()
        try:
            acc = self._trading_client.get_account()
            return AccountInfo(
                equity=float(acc.equity),
                cash=float(acc.cash),
                buying_power=float(acc.buying_power),
                portfolio_value=float(acc.portfolio_value),
                pnl_today=float(acc.equity) - float(acc.last_equity),
                status=str(acc.status),
            )
        except Exception as e:
            logger.error(f"get_account failed: {e}")
            return AccountInfo(equity=0, cash=0, buying_power=0, portfolio_value=0, pnl_today=0, status="error")

    def get_positions(self) -> list[PositionInfo]:
        self._ensure_connected()
        try:
            return [
                PositionInfo(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                    market_value=float(p.market_value),
                    unrealized_pnl=float(p.unrealized_pl),
                    unrealized_pnl_pct=float(p.unrealized_plpc) * 100,
                )
                for p in self._trading_client.get_all_positions()
            ]
        except Exception as e:
            logger.error(f"get_positions failed: {e}")
            return []

    def cancel_all_orders(self) -> None:
        self._ensure_connected()
        try:
            self._trading_client.cancel_orders()
            logger.warning("All pending Alpaca orders cancelled")
        except Exception as e:
            logger.error(f"cancel_all_orders failed: {e}")

    def liquidate_all(self) -> None:
        self._ensure_connected()
        logger.critical("LIQUIDATING ALL ALPACA POSITIONS")
        try:
            self._trading_client.close_all_positions(cancel_orders=True)
        except Exception as e:
            logger.error(f"Liquidation failed: {e}")

    def is_market_open(self) -> bool:
        self._ensure_connected()
        try:
            return self._trading_client.get_clock().is_open
        except Exception:
            import pytz
            et = pytz.timezone("America/New_York")
            now = datetime.now(et)
            if now.weekday() >= 5:
                return False
            return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16
