"""
MarketAnalysisAgent — Claude Opus 4.7 powered pre-market intelligence.

Run each trading day at 08:45 ET before the market opens.
Analyzes macro conditions, sector rotation, news, and the 14-stock watchlist,
then outputs a structured MarketOutlook to guide the day's trading strategy.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf
from loguru import logger

from agent.config import CONFIG
from agent.agents.base_agent import BaseAgent, MODEL, LIST_DIRECTORY_TOOL, READ_FILE_TOOL
from agent.agents.schemas.trading_schemas import MarketOutlook

_SYSTEM_PROMPT = """\
You are a senior quantitative analyst and portfolio manager with 20+ years of Wall Street experience,
specializing in US equity markets (NASDAQ, NYSE). You help an automated trading system
managing a $100 USD portfolio of 14 US stocks.

## Your Daily Pre-Market Task
Analyze current market conditions at 08:45 ET and provide actionable intelligence.

## Watchlist
AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, AMD, INTC, NFLX, JPM, BAC, SPY, QQQ

## Analysis Framework
1. **Market Health**: SPY and QQQ as proxies — trend direction, recent momentum
2. **Volatility**: VIX proxy from SPY options or recent swing size — fear gauge
3. **Sector Rotation**: Tech (AAPL/MSFT/NVDA/AMD/INTC), Mega-cap (GOOGL/AMZN/META),
   Consumer (TSLA/NFLX), Finance (JPM/BAC), ETFs (SPY/QQQ)
4. **News Catalysts**: Earnings, Fed speakers, economic data (CPI, jobs, PMI)
5. **Technical Context**: Each stock's trend, RSI zone, support/resistance
6. **Risk Assessment**: Position size guidance based on market regime

## Tools
Use `fetch_price_data(symbol, period)` for OHLCV data.
Available periods: 1d, 5d, 1mo, 3mo. Use "5d" for recent momentum, "1mo" for trend.
Start with SPY and QQQ to establish market baseline, then dive into individual stocks.

## Output Format
After completing analysis, output ONLY this raw JSON (no markdown fences):
{
  "date": "YYYY-MM-DD",
  "generated_at": "<ISO 8601>",
  "model": "<model string>",
  "overall_sentiment": "RISK_ON|RISK_OFF|NEUTRAL",
  "market_regime": "TRENDING_UP|TRENDING_DOWN|RANGING|VOLATILE",
  "vix_assessment": "<VIX level description and implication>",
  "key_themes": ["<up to 5 themes driving markets today>"],
  "sector_outlooks": [
    {
      "sector": "Technology",
      "outlook": "BULLISH|BEARISH|NEUTRAL",
      "reasoning": "<specific price/news observation>",
      "top_picks": ["NVDA", "MSFT"]
    }
  ],
  "top_recommendations": [
    {
      "symbol": "AAPL",
      "action": "BUY_CANDIDATE|WATCH|AVOID|HOLD",
      "reasoning": "<why this symbol today>",
      "key_catalyst": "<specific trigger to monitor>",
      "risk_factor": "<main downside risk>"
    }
  ],
  "stocks_to_avoid": ["TSLA"],
  "key_events_today": ["<time ET — event description>"],
  "strategy_adjustments": ["<specific guidance like reduce sizes if volatile>"],
  "confidence": 0.75,
  "analysis_summary": "<2-3 sentence executive summary for the trading system>"
}
"""

_FETCH_PRICE_TOOL: dict[str, Any] = {
    "name": "fetch_price_data",
    "description": (
        "Fetch OHLCV price data for a US stock ticker via Yahoo Finance. "
        "Returns a JSON summary with recent closes, % change, 5-day momentum, "
        "52-week range, average volume, and RSI(14)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "US stock ticker, e.g. 'AAPL', 'SPY'",
            },
            "period": {
                "type": "string",
                "description": "Data window: '1d', '5d', '1mo', '3mo'. Default '5d'.",
                "default": "5d",
            },
        },
        "required": ["symbol"],
    },
}

_TOOLS = [READ_FILE_TOOL, LIST_DIRECTORY_TOOL, _FETCH_PRICE_TOOL]


class MarketAnalysisAgent(BaseAgent):
    """Pre-market intelligence agent for daily US equity trading."""

    def __init__(self) -> None:
        super().__init__()
        self.watchlist: list[str] = CONFIG["market"]["watchlist"]

    # ── Tool: fetch price data (yfinance, no broker) ──────────────────────

    def _tool_fetch_price_data(self, symbol: str, period: str = "5d") -> str:
        try:
            ticker = yf.Ticker(symbol)
            df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=True)
            if df.empty:
                return json.dumps({"error": f"No data for {symbol}"})

            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            closes = df["Close"].dropna()
            if len(closes) < 2:
                return json.dumps({"error": f"Insufficient bars for {symbol}"})

            # RSI-14
            delta = closes.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, 1e-9)
            rsi = float((100 - 100 / (1 + rs)).iloc[-1])

            # Key stats
            current = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            start = float(closes.iloc[0])
            chg_pct = (current - prev) / prev * 100
            period_chg_pct = (current - start) / start * 100
            avg_vol = int(df["Volume"].mean())
            today_vol = int(df["Volume"].iloc[-1]) if not df.empty else 0

            info = {}
            try:
                fast = ticker.fast_info
                info = {
                    "52w_high": getattr(fast, "year_high", None),
                    "52w_low": getattr(fast, "year_low", None),
                    "market_cap_b": round(getattr(fast, "market_cap", 0) / 1e9, 1),
                }
            except Exception:
                pass

            recent_closes = [round(float(c), 2) for c in closes.tail(5).tolist()]

            return json.dumps({
                "symbol": symbol,
                "period": period,
                "current_price": round(current, 2),
                "prev_close": round(prev, 2),
                "change_pct_today": round(chg_pct, 2),
                f"change_pct_{period}": round(period_chg_pct, 2),
                "rsi_14": round(rsi, 1),
                "avg_volume": avg_vol,
                "today_volume": today_vol,
                "volume_vs_avg": round(today_vol / avg_vol, 2) if avg_vol else None,
                "recent_closes": recent_closes,
                **info,
            })
        except Exception as exc:
            logger.debug(f"fetch_price_data error for {symbol}: {exc}")
            return json.dumps({"error": str(exc), "symbol": symbol})

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        if name == "fetch_price_data":
            return self._tool_fetch_price_data(
                tool_input["symbol"],
                tool_input.get("period", "5d"),
            )
        return super()._execute_tool(name, tool_input)

    # ── Main entry point ──────────────────────────────────────────────────

    def analyze(self, date: str | None = None, context: str = "") -> MarketOutlook:
        """
        Run pre-market analysis for the given date.

        Args:
            date: Analysis date in YYYY-MM-DD format. Defaults to today.
            context: Optional extra context string (e.g., current portfolio state).

        Returns:
            MarketOutlook with sector recommendations and stock calls.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        user_msg_parts = [
            f"Run pre-market analysis for {date}.",
            f"Today is {date}. Market opens at 09:30 ET.",
            f"Watchlist: {', '.join(self.watchlist)}",
            "",
            "Start by fetching SPY and QQQ data to establish market baseline,",
            "then analyze each sector. Use 5d period for momentum, 1mo for trend.",
            "Output the MarketOutlook JSON when analysis is complete.",
        ]
        if context:
            user_msg_parts += ["", "Additional context:", context]

        user_msg = "\n".join(user_msg_parts)
        messages = [{"role": "user", "content": [self._make_text_block(user_msg)]}]

        logger.info(f"[MarketAnalysisAgent] starting pre-market analysis for {date}")
        raw = self._run_agentic_loop(
            system=self._make_system_block(_SYSTEM_PROMPT),
            messages=messages,
            tools=_TOOLS,
            title=f"Market Analysis — {date}",
        )
        return self._parse_report(raw, date)

    def _parse_report(self, raw: str, date: str) -> MarketOutlook:
        try:
            return MarketOutlook.model_validate_json(raw.strip())
        except Exception:
            pass

        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                data.setdefault("date", date)
                data.setdefault("generated_at", datetime.now().isoformat())
                data.setdefault("model", MODEL)
                return MarketOutlook.model_validate(data)
            except Exception as exc:
                logger.error(f"[MarketAnalysisAgent] parse error: {exc}")

        logger.warning("[MarketAnalysisAgent] could not parse report — returning fallback")
        return MarketOutlook(
            date=date,
            generated_at=datetime.now().isoformat(),
            model=MODEL,
            overall_sentiment="NEUTRAL",
            market_regime="RANGING",
            vix_assessment="Unknown — analysis parsing failed",
            key_themes=["parse error"],
            sector_outlooks=[],
            top_recommendations=[],
            stocks_to_avoid=[],
            key_events_today=[],
            strategy_adjustments=["check raw output above"],
            confidence=0.0,
            analysis_summary="Analysis could not be parsed. Check logs.",
        )
