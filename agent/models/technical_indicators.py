"""
Technical Indicators Engine
============================
Computes all technical analysis signals from OHLCV data.
Emulates how a 30-year veteran reads charts.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from loguru import logger

from agent.config import CONFIG


@dataclass
class TechnicalSignal:
    """Output of the technical analysis engine for a single stock."""
    symbol: str
    action: str           # BUY | SELL | HOLD
    confidence: float     # 0.0 – 1.0
    
    # Individual indicator signals
    trend: str            # UPTREND | DOWNTREND | SIDEWAYS
    rsi_signal: str       # OVERSOLD | OVERBOUGHT | NEUTRAL
    macd_signal: str      # BULLISH | BEARISH | NEUTRAL
    vwap_signal: str      # ABOVE | BELOW | AT
    volume_signal: str    # SURGE | NORMAL | LOW
    
    # Key values
    rsi: float = 0.0
    macd: float = 0.0
    macd_hist: float = 0.0
    ema9: float = 0.0
    ema21: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    vwap: float = 0.0
    support: float = 0.0
    resistance: float = 0.0
    
    # Pattern detected
    pattern: str = "none"   # doji | hammer | engulfing | none
    reasoning: str = ""     # Plain English explanation


class TechnicalAnalyzer:
    """
    Full technical analysis engine.
    Computes indicators and produces a unified BUY/SELL/HOLD signal.
    """

    def __init__(self):
        self.cfg = CONFIG["technical"]
        self.ema_periods = self.cfg["ema_periods"]        # [9, 21, 50, 200]
        self.rsi_period = self.cfg["rsi_period"]          # 14
        self.rsi_oversold = self.cfg["rsi_oversold"]      # 30
        self.rsi_overbought = self.cfg["rsi_overbought"]  # 70
        self.macd_fast = self.cfg["macd_fast"]            # 12
        self.macd_slow = self.cfg["macd_slow"]            # 26
        self.macd_signal = self.cfg["macd_signal"]        # 9
        self.bb_period = self.cfg["bb_period"]            # 20
        self.bb_std = self.cfg["bb_std"]                  # 2.0

    def analyze(self, symbol: str, df: pd.DataFrame) -> Optional[TechnicalSignal]:
        """
        Full technical analysis on OHLCV DataFrame.
        
        Args:
            symbol: Stock symbol
            df: DataFrame with columns Open, High, Low, Close, Volume
                (datetime index, at least 200 rows recommended)
        
        Returns:
            TechnicalSignal with consolidated BUY/SELL/HOLD + reasoning
        """
        if df is None or len(df) < 50:
            logger.warning(f"Insufficient data for {symbol}: {len(df) if df is not None else 0} rows")
            return None

        try:
            # yfinance ≥0.2.50 returns MultiIndex columns for single tickers — flatten
            if isinstance(df.columns, pd.MultiIndex):
                df = df.copy()
                df.columns = [col[0] for col in df.columns]

            close = df["Close"].astype(float)
            high = df["High"].astype(float)
            low = df["Low"].astype(float)
            volume = df["Volume"].astype(float)

            # ── Compute all indicators ───────────────────────
            emas = self._compute_emas(close)
            rsi = self._compute_rsi(close)
            macd, macd_sig, macd_hist = self._compute_macd(close)
            bb_upper, bb_mid, bb_lower = self._compute_bollinger(close)
            vwap = self._compute_vwap(high, low, close, volume)
            support, resistance = self._compute_sr_levels(high, low, close)
            avg_volume = volume.rolling(20).mean()
            pattern = self._detect_candlestick_pattern(df)

            # Latest values
            last_close = float(close.iat[-1])
            last_rsi = float(rsi.dropna().iat[-1]) if not rsi.dropna().empty else 50.0
            last_macd = float(macd.iat[-1])
            last_macd_hist = float(macd_hist.iat[-1])
            last_vwap = float(vwap.iat[-1]) if len(vwap) > 0 else last_close
            last_volume = float(volume.iat[-1])
            last_avg_vol = float(avg_volume.iat[-1]) if not pd.isna(avg_volume.iat[-1]) else last_volume

            ema_vals = {p: float(emas[p].iat[-1]) for p in self.ema_periods}

            # ── Individual signal classifications ────────────
            trend = self._classify_trend(last_close, ema_vals)
            rsi_sig = self._classify_rsi(last_rsi)
            macd_sig_str = self._classify_macd(last_macd_hist, macd_hist)
            vwap_sig = "ABOVE" if last_close > last_vwap else "BELOW"
            vol_sig = "SURGE" if last_volume > last_avg_vol * 2 else (
                "LOW" if last_volume < last_avg_vol * 0.5 else "NORMAL"
            )

            # ── Score each signal (positive = bullish) ────────
            scores = []
            reasons = []

            # Trend score
            if trend == "UPTREND":
                scores.append(0.25)
                reasons.append("stock is in a clear uptrend (EMA alignment)")
            elif trend == "DOWNTREND":
                scores.append(-0.25)
                reasons.append("stock is in a downtrend")
            else:
                scores.append(0.0)
                reasons.append("price is sideways/consolidating")

            # RSI score
            if rsi_sig == "OVERSOLD":
                scores.append(0.20)
                reasons.append(f"RSI={last_rsi:.1f} — oversold, potential reversal")
            elif rsi_sig == "OVERBOUGHT":
                scores.append(-0.20)
                reasons.append(f"RSI={last_rsi:.1f} — overbought, caution")
            else:
                scores.append(0.05 if last_rsi > 50 else -0.05)
                reasons.append(f"RSI={last_rsi:.1f} — neutral")

            # MACD score
            if macd_sig_str == "BULLISH":
                scores.append(0.20)
                reasons.append("MACD histogram turning positive — bullish momentum")
            elif macd_sig_str == "BEARISH":
                scores.append(-0.20)
                reasons.append("MACD histogram turning negative — bearish momentum")
            else:
                scores.append(0.0)
                reasons.append("MACD is flat/neutral")

            # VWAP score
            if vwap_sig == "ABOVE":
                scores.append(0.15)
                reasons.append(f"price ${last_close:.1f} above VWAP ${last_vwap:.1f}")
            else:
                scores.append(-0.15)
                reasons.append(f"price ${last_close:.1f} below VWAP ${last_vwap:.1f}")

            # Volume score
            if vol_sig == "SURGE":
                multiplier = 1.5 if sum(scores) > 0 else 0.5
                vol_score = 0.10 * multiplier * (1 if sum(scores) > 0 else -1)
                scores.append(vol_score)
                reasons.append(f"volume is {last_volume/last_avg_vol:.1f}x average — confirming move")
            elif vol_sig == "LOW":
                reasons.append("volume is low — weak conviction, be cautious")

            # Candlestick pattern
            if pattern == "hammer":
                scores.append(0.10)
                reasons.append("hammer candle detected — reversal signal")
            elif pattern == "engulfing_bull":
                scores.append(0.15)
                reasons.append("bullish engulfing pattern — strong reversal signal")
            elif pattern == "engulfing_bear":
                scores.append(-0.15)
                reasons.append("bearish engulfing pattern — sell signal")
            elif pattern == "doji":
                scores.append(0.0)
                reasons.append("doji candle — market indecision, wait")

            # ── Final decision ────────────────────────────────
            total_score = sum(scores)
            confidence = min(abs(total_score), 1.0)

            if total_score >= 0.35:
                action = "BUY"
            elif total_score <= -0.35:
                action = "SELL"
            else:
                action = "HOLD"

            reasoning = f"[{action} @ {confidence:.0%} confidence] " + "; ".join(reasons)

            return TechnicalSignal(
                symbol=symbol,
                action=action,
                confidence=round(confidence, 3),
                trend=trend,
                rsi_signal=rsi_sig,
                macd_signal=macd_sig_str,
                vwap_signal=vwap_sig,
                volume_signal=vol_sig,
                rsi=round(last_rsi, 2),
                macd=round(last_macd, 4),
                macd_hist=round(last_macd_hist, 4),
                ema9=round(ema_vals.get(9, 0), 2),
                ema21=round(ema_vals.get(21, 0), 2),
                ema50=round(ema_vals.get(50, 0), 2),
                ema200=round(ema_vals.get(200, 0), 2),
                vwap=round(last_vwap, 2),
                support=round(float(support), 2),
                resistance=round(float(resistance), 2),
                pattern=pattern,
                reasoning=reasoning,
            )

        except Exception as e:
            logger.error(f"Technical analysis failed for {symbol}: {e}", exc_info=True)
            return None

    # ─── Indicator Computations ───────────────────────────────

    def _compute_emas(self, close: pd.Series) -> Dict[int, pd.Series]:
        return {p: close.ewm(span=p, adjust=False).mean() for p in self.ema_periods}

    def _compute_rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(span=self.rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _compute_macd(self, close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=self.macd_signal, adjust=False).mean()
        histogram = macd - signal
        return macd, signal, histogram

    def _compute_bollinger(self, close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        mid = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        upper = mid + (self.bb_std * std)
        lower = mid - (self.bb_std * std)
        return upper, mid, lower

    def _compute_vwap(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        volume: pd.Series,
        lookback: int = 20,
    ) -> pd.Series:
        # Rolling VWAP over `lookback` bars (session-agnostic but avoids multi-month drift)
        typical = (high + low + close) / 3
        tp_vol = typical * volume
        rolling_tp_vol = tp_vol.rolling(lookback).sum()
        rolling_vol = volume.rolling(lookback).sum()
        return rolling_tp_vol / rolling_vol.replace(0, np.nan)

    def _compute_sr_levels(
        self,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        lookback: int = 20,
    ) -> Tuple[float, float]:
        """Compute dynamic support/resistance from recent highs/lows."""
        recent_high = high.tail(lookback).max()
        recent_low = low.tail(lookback).min()
        return recent_low, recent_high

    # ─── Signal Classifiers ───────────────────────────────────

    def _classify_trend(self, price: float, emas: Dict[int, float]) -> str:
        """EMA alignment: 9 > 21 > 50 > 200 = perfect uptrend."""
        aligned_bull = (
            emas.get(9, 0) > emas.get(21, 0) > emas.get(50, 0)
            and price > emas.get(50, 0)
        )
        aligned_bear = (
            emas.get(9, 0) < emas.get(21, 0) < emas.get(50, 0)
            and price < emas.get(50, 0)
        )
        if aligned_bull:
            return "UPTREND"
        elif aligned_bear:
            return "DOWNTREND"
        return "SIDEWAYS"

    def _classify_rsi(self, rsi: float) -> str:
        if rsi <= self.rsi_oversold:
            return "OVERSOLD"
        elif rsi >= self.rsi_overbought:
            return "OVERBOUGHT"
        return "NEUTRAL"

    def _classify_macd(self, hist_now: float, hist_series: pd.Series) -> str:
        """MACD crossover detection."""
        if len(hist_series) < 3:
            return "NEUTRAL"
        prev = float(hist_series.iloc[-2])
        if prev < 0 and hist_now > 0:
            return "BULLISH"  # Positive crossover
        elif prev > 0 and hist_now < 0:
            return "BEARISH"  # Negative crossover
        elif hist_now > 0 and hist_now > prev:
            return "BULLISH"  # Growing positive
        elif hist_now < 0 and hist_now < prev:
            return "BEARISH"  # Growing negative
        return "NEUTRAL"

    def _detect_candlestick_pattern(self, df: pd.DataFrame) -> str:
        """Detect common single/two-candle reversal patterns."""
        if len(df) < 2:
            return "none"

        o = float(df["Open"].iat[-1])
        h = float(df["High"].iat[-1])
        l = float(df["Low"].iat[-1])
        c = float(df["Close"].iat[-1])
        body = abs(c - o)
        total_range = h - l

        if total_range == 0:
            return "none"

        # Doji: tiny body
        if body / total_range < 0.1:
            return "doji"

        # Hammer: small body at top, long lower wick
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        if lower_wick > 2 * body and upper_wick < 0.3 * body:
            return "hammer"

        # Engulfing
        prev_o = float(df["Open"].iat[-2])
        prev_c = float(df["Close"].iat[-2])
        if c > prev_o and o < prev_c and c > o:
            return "engulfing_bull"
        elif c < prev_o and o > prev_c and c < o:
            return "engulfing_bear"

        return "none"
