"""
Price Feed Module
=================
Real-time and historical OHLCV price data.
Supports both Zerodha Kite (live) and Yahoo Finance (fallback/paper mode).
"""
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

import yfinance as yf
import pandas as pd
from loguru import logger

from agent.config import CONFIG


@dataclass
class Tick:
    """Represents a single price tick."""
    symbol: str
    ltp: float          # Last Traded Price
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime
    exchange: str = "NSE"
    change_pct: float = 0.0


@dataclass
class OHLCV:
    """Historical OHLCV bar."""
    symbol: str
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceFeed:
    """
    Unified price feed that works in both paper and live modes.

    Paper mode: Uses Yahoo Finance for real market data (no broker needed)
    Live mode:  Uses Alpaca WebSocket for actual live ticks
    """

    def __init__(self):
        self.config = CONFIG
        self.mode = CONFIG["execution"]["mode"]
        self.exchange = CONFIG["market"]["exchange"]
        self.watchlist = CONFIG["market"]["watchlist"]

        self._tick_cache: Dict[str, Tick] = {}
        self._subscribers: List[Callable[[Tick], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Alpaca live stream (live mode only)
        self._alpaca_stream = None

        logger.info(f"PriceFeed initialized | Mode: {self.mode} | Symbols: {len(self.watchlist)}")

    # ─── Subscription API ─────────────────────────────────────

    def subscribe(self, callback: Callable[[Tick], None]):
        """Register a callback to receive every new tick."""
        self._subscribers.append(callback)
        logger.debug(f"New tick subscriber registered. Total: {len(self._subscribers)}")

    def _notify(self, tick: Tick):
        """Push tick to all subscribers."""
        self._tick_cache[tick.symbol] = tick
        for cb in self._subscribers:
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"Subscriber callback error: {e}")

    # ─── Tick Access ──────────────────────────────────────────

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        """Get the most recent tick for a symbol."""
        return self._tick_cache.get(symbol)

    def get_all_ticks(self) -> Dict[str, Tick]:
        """Get latest ticks for all symbols."""
        return dict(self._tick_cache)

    # ─── Historical Data ──────────────────────────────────────

    def get_historical(
        self,
        symbol: str,
        period: str = "3mo",
        interval: str = "1d"
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data via Yahoo Finance.
        
        Args:
            symbol: Stock symbol (e.g., "RELIANCE")
            period: Data period — 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y
            interval: Bar interval — 1m, 5m, 15m, 1h, 1d, 1wk

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
        """
        ticker_sym = self._to_yahoo_symbol(symbol)
        try:
            df = yf.download(
                ticker_sym,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                logger.warning(f"No historical data for {symbol}")
                return pd.DataFrame()

            df.index = pd.to_datetime(df.index)
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            logger.debug(f"Fetched {len(df)} bars for {symbol} ({interval})")
            return df

        except Exception as e:
            logger.error(f"Historical fetch failed for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday(self, symbol: str, interval: str = "5m") -> pd.DataFrame:
        """Fetch today's intraday OHLCV data."""
        return self.get_historical(symbol, period="1d", interval=interval)

    # ─── Live Feed (Paper Mode — Yahoo Finance polling) ───────

    def start(self):
        """Start the price feed."""
        if self._running:
            return
        self._running = True

        if self.mode == "paper":
            self._thread = threading.Thread(
                target=self._paper_feed_loop,
                daemon=True,
                name="PriceFeedPaper"
            )
            self._thread.start()
            logger.info("📡 Paper mode price feed started (Yahoo Finance polling)")

        elif self.mode == "live":
            self._start_live_alpaca_feed()

    def stop(self):
        """Stop the price feed."""
        self._running = False
        if self._alpaca_stream:
            try:
                import asyncio
                asyncio.get_event_loop().run_until_complete(self._alpaca_stream.stop())
            except Exception:
                pass
        logger.info("Price feed stopped")

    def _paper_feed_loop(self):
        """
        Paper mode: Poll Yahoo Finance every 15 seconds during market hours.
        Simulates a live tick stream for testing without real broker API.
        """
        import random

        while self._running:
            for symbol in self.watchlist:
                try:
                    ticker = yf.Ticker(self._to_yahoo_symbol(symbol))
                    info = ticker.fast_info

                    ltp = getattr(info, 'last_price', None) or getattr(info, 'regularMarketPrice', 0)
                    prev_close = getattr(info, 'previous_close', ltp) or ltp

                    if ltp and ltp > 0:
                        change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
                        tick = Tick(
                            symbol=symbol,
                            ltp=float(ltp),
                            open=getattr(info, 'open', ltp) or ltp,
                            high=getattr(info, 'day_high', ltp) or ltp,
                            low=getattr(info, 'day_low', ltp) or ltp,
                            close=float(ltp),
                            volume=int(getattr(info, 'three_month_average_volume', 0) or 0),
                            timestamp=datetime.now(),
                            change_pct=round(change_pct, 2),
                        )
                        self._notify(tick)

                except Exception as e:
                    logger.debug(f"Paper feed error for {symbol}: {e}")

                time.sleep(0.5)  # Small delay between symbols

            # Wait before next poll cycle
            time.sleep(15)

    # ─── Live Feed (Alpaca WebSocket — Live Mode) ─────────────

    def _start_live_alpaca_feed(self):
        """Connect to Alpaca WebSocket for real live ticks (US markets)."""
        try:
            from alpaca.data.live import StockDataStream

            api_key = os.getenv("ALPACA_API_KEY")
            secret_key = os.getenv("ALPACA_SECRET_KEY")

            if not api_key or not secret_key:
                raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")

            self._alpaca_stream = StockDataStream(api_key, secret_key)

            async def on_bar(bar):
                """Handle incoming real-time bar data."""
                tick = Tick(
                    symbol=bar.symbol,
                    ltp=float(bar.close),
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=int(bar.volume),
                    timestamp=bar.timestamp,
                    exchange="NASDAQ",
                    change_pct=0.0,
                )
                self._notify(tick)

            self._alpaca_stream.subscribe_bars(on_bar, *self.watchlist)

            # Run the stream in a background thread
            import threading
            t = threading.Thread(
                target=self._alpaca_stream.run,
                daemon=True,
                name="AlpacaLiveStream"
            )
            t.start()
            logger.info(f"Alpaca live WebSocket stream started for {len(self.watchlist)} symbols")

        except ImportError:
            logger.error("alpaca-py not installed. Run: pip install alpaca-py")
        except Exception as e:
            logger.error(f"Failed to start Alpaca live feed: {e}")

    # ─── Helpers ──────────────────────────────────────────────

    def _to_yahoo_symbol(self, symbol: str) -> str:
        """
        Convert ticker to Yahoo Finance format.
        US stocks (NASDAQ/NYSE) need NO suffix — just the raw symbol.
        e.g., AAPL → AAPL, MSFT → MSFT
        """
        exchange = self.config["market"]["exchange"]
        if exchange in ("NSE",):
            return f"{symbol}.NS"
        elif exchange in ("BSE",):
            return f"{symbol}.BO"
        # NASDAQ, NYSE, US markets — no suffix needed
        return symbol

    def is_market_open(self) -> bool:
        """Check if the US stock market (NYSE/NASDAQ) is currently open (ET)."""
        import pytz
        tz = pytz.timezone("America/New_York")
        now = datetime.now(tz)

        # Weekends
        if now.weekday() >= 5:
            return False

        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

        return market_open <= now <= market_close
