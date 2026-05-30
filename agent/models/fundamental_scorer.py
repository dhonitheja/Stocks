"""
Fundamental Scorer — yfinance-based stock health scoring (0.0 – 1.0).

Metrics used:
  - P/E ratio        (lower = cheaper = better)
  - P/B ratio        (lower = more value)
  - Revenue growth   (higher = better)
  - Profit margin    (higher = better)
  - Debt/equity      (lower = safer)
  - Return on equity (higher = better)

Score of 0.5 = neutral, >0.5 = fundamentally positive, <0.5 = negative.
"""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Optional

import yfinance as yf
from loguru import logger


class FundamentalScorer:
    """Scores a stock's fundamental health using yfinance data."""

    _CACHE_TTL = 4 * 3600

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, float]] = {}  # symbol → (score, timestamp)

    def score(self, symbol: str) -> float:
        """
        Return fundamental score in [0, 1].
        0.5 = neutral/unknown, >0.5 = fundamentally positive.
        """
        # Check cache
        if symbol in self._cache:
            cached_score, ts = self._cache[symbol]
            if time.time() - ts < self._CACHE_TTL:
                return cached_score

        try:
            score = self._compute(symbol)
        except Exception as exc:
            logger.warning(f"[FundamentalScorer] {symbol}: fetch failed ({exc}), using neutral")
            score = 0.5

        self._cache[symbol] = (score, time.time())
        logger.debug(f"[FundamentalScorer] {symbol}: score={score:.3f}")
        return score

    def _compute(self, symbol: str) -> float:
        info = yf.Ticker(symbol).info
        scores: list[float] = []

        # 1. P/E ratio — ideal range 10–25, above 50 is stretched
        pe = info.get("trailingPE") or info.get("forwardPE")
        if pe and pe > 0:
            if pe < 10:
                scores.append(0.85)   # very cheap
            elif pe < 20:
                scores.append(0.70)   # good value
            elif pe < 30:
                scores.append(0.55)   # fair
            elif pe < 50:
                scores.append(0.35)   # expensive
            else:
                scores.append(0.15)   # very stretched

        # 2. P/B ratio — below 1 = deep value, above 5 = premium
        pb = info.get("priceToBook")
        if pb and pb > 0:
            if pb < 1:
                scores.append(0.85)
            elif pb < 2:
                scores.append(0.70)
            elif pb < 4:
                scores.append(0.50)
            elif pb < 8:
                scores.append(0.30)
            else:
                scores.append(0.15)

        # 3. Revenue growth (YoY) — scale linearly
        rev_growth = info.get("revenueGrowth")
        if rev_growth is not None:
            # -0.3 → 0.0 score, 0.0 → 0.5, +0.3 → 1.0
            scores.append(max(0.0, min(1.0, 0.5 + rev_growth / 0.6)))

        # 4. Profit margin — above 20% is excellent
        margin = info.get("profitMargins")
        if margin is not None:
            scores.append(max(0.0, min(1.0, 0.5 + margin * 2.5)))

        # 5. Debt/equity — lower is safer (D/E > 2 is risky)
        de = info.get("debtToEquity")
        if de is not None:
            de_ratio = de / 100  # yfinance returns as percentage
            if de_ratio < 0.3:
                scores.append(0.90)
            elif de_ratio < 0.8:
                scores.append(0.65)
            elif de_ratio < 1.5:
                scores.append(0.45)
            elif de_ratio < 3.0:
                scores.append(0.25)
            else:
                scores.append(0.10)

        # 6. Return on equity — above 15% is good
        roe = info.get("returnOnEquity")
        if roe is not None:
            scores.append(max(0.0, min(1.0, 0.5 + roe * 2.5)))

        if not scores:
            return 0.5  # no data — neutral

        return round(sum(scores) / len(scores), 4)
