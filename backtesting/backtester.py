"""
StockSage Backtester — historical simulation using yfinance OHLCV data.

Reuses the same TechnicalAnalyzer and signal fusion logic as live trading
so backtest results reflect real agent behavior.

Usage:
  bt = Backtester(start="2024-01-01", end="2024-12-31", initial_cash=100.0)
  report = bt.run(["AAPL", "MSFT", "NVDA"])
  report.print_summary()
  report.to_json("backtest_results.json")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

from agent.config import CONFIG
from agent.models.technical_indicators import TechnicalAnalyzer


@dataclass
class SimTrade:
    symbol: str
    action: str          # BUY | SELL
    date: date
    price: float
    shares: float
    dollar_value: float
    pnl: float = 0.0     # filled on SELL


@dataclass
class SimPosition:
    symbol: str
    shares: float
    entry_price: float
    entry_date: date
    stop_loss: float
    cost_basis: float


class Backtester:
    """
    Event-driven backtester driven by daily OHLCV bars.

    Signal source: TechnicalAnalyzer (same as live)
    Risk rules: stop_loss_pct, max_position_pct, max_open_positions from config
    """

    def __init__(
        self,
        start: str,
        end: str,
        initial_cash: float = 100.0,
        commission_pct: float | None = None,
        slippage_bps: float | None = None,
        spread_bps: float | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.cash = initial_cash
        self.initial_cash = initial_cash

        cost_cfg = CONFIG.get("cost_model", {})
        # Caller can override; otherwise pull from config (honest defaults)
        self.commission_pct: float = commission_pct if commission_pct is not None else cost_cfg.get("commission_pct", 0.0)
        self.slippage_bps: float = slippage_bps if slippage_bps is not None else cost_cfg.get("slippage_bps", 5)
        self.spread_bps: float = spread_bps if spread_bps is not None else cost_cfg.get("spread_bps", 3)
        # Total one-way cost as a fraction (slippage + half-spread + commission)
        self._one_way_cost: float = self.commission_pct + (self.slippage_bps + self.spread_bps) / 10_000

        cfg = CONFIG["risk"]
        self.max_position_pct: float = cfg["max_position_pct"]
        self.stop_loss_pct: float = cfg["max_trade_loss_pct"]
        self.max_open_positions: int = cfg["max_open_positions"]
        self.min_confidence: float = cfg["min_confidence_score"]

        self._analyzer = TechnicalAnalyzer()
        self._positions: dict[str, SimPosition] = {}
        self._trades: list[SimTrade] = []

    # ── Public ──────────────────────────────────────────────────

    def run(self, symbols: list[str]) -> "BacktestReport":
        from .report import BacktestReport

        logger.info(f"[Backtest] {self.start} → {self.end} | {len(symbols)} symbols | ${self.initial_cash:.2f}")

        # Fetch all historical data upfront
        bars: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                df = yf.download(sym, start=self.start, end=self.end, progress=False, auto_adjust=True)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [col[0] for col in df.columns]
                if len(df) > 50:
                    bars[sym] = df
                    logger.debug(f"[Backtest] {sym}: {len(df)} bars")
                else:
                    logger.warning(f"[Backtest] {sym}: insufficient data ({len(df)} bars), skipping")
            except Exception as exc:
                logger.warning(f"[Backtest] {sym}: fetch error — {exc}")

        if not bars:
            logger.error("[Backtest] No data fetched — aborting")
            return BacktestReport(
                start=self.start, end=self.end,
                initial_cash=self.initial_cash, final_cash=self.initial_cash,
                trades=[], symbols=symbols,
            )

        # Find common trading dates
        all_dates = sorted(set.intersection(*[set(df.index.date) for df in bars.values()]))

        # Simulate day by day
        for sim_date in all_dates:
            self._check_stop_losses(bars, sim_date)
            self._generate_signals(bars, sim_date, symbols)

        # Close all remaining positions at last date
        if all_dates:
            self._close_all(bars, all_dates[-1])

        last_date = all_dates[-1] if all_dates else None
        open_value = 0.0
        for pos in self._positions.values():
            if pos.symbol not in bars:
                continue
            price = self._get_price(bars[pos.symbol], last_date) if last_date else None
            if price:
                open_value += price * pos.shares
        final_value = self.cash + open_value

        return BacktestReport(
            start=self.start, end=self.end,
            initial_cash=self.initial_cash, final_cash=final_value,
            trades=self._trades, symbols=list(bars.keys()),
        )

    # ── Internal helpers ─────────────────────────────────────────

    def _get_price(self, df: pd.DataFrame, sim_date: date) -> Optional[float]:
        rows = df[df.index.date == sim_date]
        if rows.empty:
            return None
        return float(rows["Close"].iloc[-1])

    def _check_stop_losses(self, bars: dict[str, pd.DataFrame], sim_date: date) -> None:
        to_close: list[str] = []
        for sym, pos in self._positions.items():
            if sym not in bars:
                continue
            price = self._get_price(bars[sym], sim_date)
            if price is None:
                continue
            low = bars[sym][bars[sym].index.date == sim_date]["Low"].iloc[-1] if not bars[sym][bars[sym].index.date == sim_date].empty else price
            if float(low) <= pos.stop_loss:
                to_close.append((sym, pos.stop_loss))  # execute at stop price

        for sym, stop_price in to_close:
            self._sell(sym, stop_price, sim_date, reason="stop_loss")

    def _generate_signals(
        self, bars: dict[str, pd.DataFrame], sim_date: date, symbols: list[str]
    ) -> None:
        for sym in symbols:
            if sym not in bars:
                continue

            # Build history up to (but not including) current date for signal
            hist = bars[sym][bars[sym].index.date < sim_date].tail(200)
            if len(hist) < 50:
                continue

            signal = self._analyzer.analyze(sym, hist)
            if signal is None:
                continue

            price = self._get_price(bars[sym], sim_date)
            if price is None:
                continue

            confidence = signal.confidence

            if signal.action == "BUY" and confidence >= self.min_confidence:
                if sym not in self._positions and len(self._positions) < self.max_open_positions:
                    self._buy(sym, price, sim_date)

            elif signal.action == "SELL" and sym in self._positions:
                self._sell(sym, price, sim_date, reason="signal")

    def _buy(self, symbol: str, price: float, sim_date: date) -> None:
        max_dollars = min(
            self.initial_cash * self.max_position_pct,
            self.cash * 0.95,
        )
        if max_dollars < 1.0:
            return
        # Fill price includes slippage (we pay more on the buy)
        fill_price = price * (1 + self._one_way_cost)
        shares = max_dollars / fill_price
        cost = shares * fill_price  # what actually leaves the account

        self.cash -= cost
        stop = fill_price * (1 - self.stop_loss_pct)
        self._positions[symbol] = SimPosition(
            symbol=symbol, shares=shares, entry_price=fill_price,
            entry_date=sim_date, stop_loss=stop, cost_basis=cost,
        )
        self._trades.append(SimTrade(
            symbol=symbol, action="BUY", date=sim_date,
            price=fill_price, shares=shares, dollar_value=cost,
        ))
        logger.debug(
            f"[Backtest] BUY {symbol} @ ${fill_price:.4f} (mid=${price:.2f}, "
            f"cost={self._one_way_cost*10000:.1f}bps) {shares:.4f}sh on {sim_date}"
        )

    def _sell(self, symbol: str, price: float, sim_date: date, reason: str = "signal") -> None:
        if symbol not in self._positions:
            return
        pos = self._positions.pop(symbol)
        # Fill price includes slippage (we receive less on the sell)
        fill_price = price * (1 - self._one_way_cost)
        proceeds = pos.shares * fill_price
        pnl = proceeds - pos.cost_basis
        self.cash += proceeds
        self._trades.append(SimTrade(
            symbol=symbol, action="SELL", date=sim_date,
            price=fill_price, shares=pos.shares, dollar_value=proceeds, pnl=pnl,
        ))
        logger.debug(
            f"[Backtest] SELL {symbol} @ ${fill_price:.4f} (mid=${price:.2f}) "
            f"| P&L ${pnl:+.2f} [{reason}] on {sim_date}"
        )

    def _close_all(self, bars: dict[str, pd.DataFrame], last_date: date) -> None:
        for sym in list(self._positions.keys()):
            if sym in bars:
                price = self._get_price(bars[sym], last_date)
                if price:
                    self._sell(sym, price, last_date, reason="end_of_backtest")
