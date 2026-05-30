"""
Robinhood broker implementation via robin_stocks.

IMPORTANT: Robinhood does not have an official public API. robin_stocks
uses Robinhood's private web API. Use for personal/educational purposes only
and be aware this may violate Robinhood's Terms of Service. For regulated
production trading, prefer Alpaca which has an official API.

Install: pip install robin_stocks
"""
from __future__ import annotations

import os
from datetime import datetime

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import AccountInfo, BrokerClient, OrderResult, PositionInfo


class RobinhoodBroker(BrokerClient):
    """
    Robinhood broker via robin_stocks (private API).
    Supports fractional shares and commission-free trading.

    Required env vars:
        ROBINHOOD_USERNAME  — Robinhood account email
        ROBINHOOD_PASSWORD  — Robinhood account password
        ROBINHOOD_MFA_CODE  — (optional) TOTP code if 2FA is enabled
    """

    def __init__(self):
        self._rh = None
        logger.info("RobinhoodBroker initialized (credentials loaded on first use)")

    def _ensure_connected(self) -> None:
        if self._rh is not None:
            return
        try:
            import robin_stocks.robinhood as rh

            username = os.getenv("ROBINHOOD_USERNAME")
            password = os.getenv("ROBINHOOD_PASSWORD")
            mfa_code = os.getenv("ROBINHOOD_MFA_CODE")

            if not username or not password:
                raise ValueError("ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD must be set in .env")

            kwargs: dict = {
                "username": username,
                "password": password,
                "store_session": True,
                "pickle_name": "stocksage",
            }
            if mfa_code:
                kwargs["mfa_code"] = mfa_code

            login_result = rh.login(**kwargs)
            if not login_result:
                raise RuntimeError("Robinhood login failed — check credentials")

            self._rh = rh
            logger.info("Connected to Robinhood")
        except ImportError:
            logger.error("robin_stocks not installed. Run: pip install robin_stocks")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def buy(self, symbol: str, dollar_amount: float, reason: str = "") -> OrderResult:
        self._ensure_connected()
        logger.info(f"BUY | {symbol} | ${dollar_amount:.2f} | {reason}")
        try:
            result = self._rh.order_buy_fractional_by_price(
                symbol=symbol,
                amountInDollars=dollar_amount,
                timeInForce="gfd",
                extendedHours=False,
            )
            if not result or "id" not in result:
                raise RuntimeError(f"Unexpected Robinhood response: {result}")

            status = result.get("state", "pending")
            return OrderResult(
                order_id=result["id"],
                symbol=symbol,
                action="buy",
                quantity=float(result.get("quantity") or 0),
                notional=dollar_amount,
                status=status,
                filled_price=float(result.get("average_price") or 0),
                filled_at=datetime.now() if status == "filled" else None,
            )
        except Exception as e:
            logger.error(f"BUY failed | {symbol}: {e}")
            return OrderResult(
                order_id="", symbol=symbol, action="buy",
                quantity=0, notional=dollar_amount,
                status="error", filled_price=0.0, filled_at=None,
                error_msg=str(e),
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def sell(self, symbol: str, reason: str = "") -> OrderResult:
        self._ensure_connected()
        logger.info(f"SELL | {symbol} | {reason}")
        try:
            positions = self._rh.get_open_stock_positions()
            qty = next(
                (float(p["quantity"]) for p in positions
                 if self._rh.get_symbol_by_url(p["instrument"]) == symbol),
                0.0,
            )
            if qty == 0.0:
                return OrderResult(
                    order_id="", symbol=symbol, action="sell",
                    quantity=0, notional=0,
                    status="error", filled_price=0.0, filled_at=None,
                    error_msg=f"No open position found for {symbol}",
                )

            result = self._rh.order_sell_fractional_by_quantity(
                symbol=symbol,
                quantity=qty,
                timeInForce="gfd",
                extendedHours=False,
            )
            return OrderResult(
                order_id=result.get("id", ""),
                symbol=symbol,
                action="sell",
                quantity=qty,
                notional=0,
                status=result.get("state", "pending"),
                filled_price=float(result.get("average_price") or 0),
                filled_at=datetime.now(),
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
            portfolio = self._rh.load_portfolio_profile()
            account = self._rh.load_account_profile()
            equity = float(portfolio.get("equity") or 0)
            prev_equity = float(portfolio.get("adjusted_equity_previous_close") or equity)
            cash = float(account.get("cash") or 0)
            return AccountInfo(
                equity=equity,
                cash=cash,
                buying_power=float(account.get("buying_power") or cash),
                portfolio_value=equity,
                pnl_today=equity - prev_equity,
                status="active",
            )
        except Exception as e:
            logger.error(f"get_account failed: {e}")
            return AccountInfo(equity=0, cash=0, buying_power=0, portfolio_value=0, pnl_today=0, status="error")

    def get_positions(self) -> list[PositionInfo]:
        self._ensure_connected()
        try:
            raw = self._rh.get_open_stock_positions()
            results = []
            for p in raw:
                symbol = self._rh.get_symbol_by_url(p["instrument"])
                qty = float(p["quantity"])
                avg_entry = float(p["average_buy_price"])
                prices = self._rh.get_latest_price(symbol)
                current = float(prices[0]) if prices else avg_entry
                market_val = qty * current
                pnl = (current - avg_entry) * qty
                pnl_pct = ((current - avg_entry) / avg_entry * 100) if avg_entry else 0.0
                results.append(PositionInfo(
                    symbol=symbol,
                    qty=qty,
                    avg_entry=avg_entry,
                    current_price=current,
                    market_value=market_val,
                    unrealized_pnl=pnl,
                    unrealized_pnl_pct=pnl_pct,
                ))
            return results
        except Exception as e:
            logger.error(f"get_positions failed: {e}")
            return []

    def cancel_all_orders(self) -> None:
        self._ensure_connected()
        try:
            self._rh.cancel_all_open_orders()
            logger.warning("All Robinhood orders cancelled")
        except Exception as e:
            logger.error(f"cancel_all_orders failed: {e}")

    def liquidate_all(self) -> None:
        self._ensure_connected()
        logger.critical("LIQUIDATING ALL ROBINHOOD POSITIONS")
        for pos in self.get_positions():
            self.sell(pos.symbol, reason="emergency_liquidation")

    def is_market_open(self) -> bool:
        self._ensure_connected()
        try:
            hours = self._rh.get_market_hours(market="XNAS")
            return bool(hours.get("is_open", False))
        except Exception:
            import pytz
            et = pytz.timezone("America/New_York")
            now = datetime.now(et)
            if now.weekday() >= 5:
                return False
            return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16
