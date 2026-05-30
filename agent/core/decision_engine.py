"""
Decision Engine — The Master Brain
====================================
Fuses technical analysis + sentiment + risk management
into final BUY/SELL/HOLD decisions.

This is the core of StockSage — it acts like a 30-year veteran:
- Never trades on emotion
- Requires multiple confirming signals
- Always checks risk before acting
- Explains every decision in plain English
"""
import random
import time
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass, field
from loguru import logger

from agent.config import CONFIG
from agent.models.technical_indicators import TechnicalAnalyzer, TechnicalSignal
from agent.models.sentiment_model import SentimentModel
from agent.models.llm_client import LLMClient
from agent.models.fundamental_scorer import FundamentalScorer
from agent.core.risk_manager import RiskManager
from agent.data.price_feed import PriceFeed
from agent.data.news_feed import NewsFeed


@dataclass
class TradeDecision:
    """Final output of the decision engine for one stock."""
    symbol: str
    action: str                # BUY | SELL | HOLD | BLOCKED
    price: float
    quantity: float            # 0 for HOLD/BLOCKED; fractional shares supported
    confidence: float          # 0.0 – 1.0
    timestamp: datetime

    # Component scores
    technical_score: float = 0.0
    sentiment_score: float = 0.0
    fundamental_score: float = 0.0
    final_score: float = 0.0

    # Signal breakdown
    technical_action: str = "HOLD"
    sentiment_label: str = "neutral"

    # The agent's reasoning — plain English
    reasoning: str = ""
    block_reason: str = ""


class DecisionEngine:
    """
    The master decision-making brain.
    
    For each stock in the watchlist, every analysis cycle:
    1. Gets the latest price
    2. Runs technical analysis
    3. Gets news sentiment
    4. Fuses signals with weighted scoring
    5. Passes through risk management
    6. Returns a final BUY/SELL/HOLD decision
    """

    def __init__(
        self,
        price_feed: PriceFeed,
        news_feed: NewsFeed,
        risk_manager: RiskManager,
    ):
        self.price_feed = price_feed
        self.news_feed = news_feed
        self.risk_manager = risk_manager
        self.technical = TechnicalAnalyzer()
        self.sentiment = SentimentModel()
        self.llm = LLMClient()
        self.fundamental = FundamentalScorer()

        self.cfg = CONFIG
        self.weights = CONFIG["decision_weights"]
        self.behavior = CONFIG["behavior"]
        self.watchlist = CONFIG["market"]["watchlist"]

        # Decision history (most recent per symbol)
        self.last_decisions: Dict[str, TradeDecision] = {}

        llm_status = f"active ({self.llm.provider_name})" if self.llm.is_available() else "not configured (no API key in .env)"
        logger.info(f"DecisionEngine initialized | LLM: {llm_status}")

    # ─── Main Analysis Cycle ──────────────────────────────────

    def analyze_all(self) -> Dict[str, TradeDecision]:
        """
        Run full analysis for all symbols in watchlist.
        Called every analysis cycle (e.g., every 5 minutes intraday).
        
        Returns:
            Dict of symbol → TradeDecision
        """
        logger.info(f"🔍 Starting analysis cycle for {len(self.watchlist)} stocks...")
        decisions = {}

        for symbol in self.watchlist:
            try:
                decision = self.analyze_symbol(symbol)
                if decision:
                    decisions[symbol] = decision
                    self.last_decisions[symbol] = decision

                    if decision.action in ("BUY", "SELL"):
                        logger.info(
                            f"📌 {decision.action} signal | {symbol} @ ₹{decision.price:.2f} "
                            f"| Confidence: {decision.confidence:.0%} | Qty: {decision.quantity}"
                        )
            except Exception as e:
                logger.error(f"Analysis failed for {symbol}: {e}", exc_info=True)

        # Log summary
        actions = [d.action for d in decisions.values()]
        buys = actions.count("BUY")
        sells = actions.count("SELL")
        holds = actions.count("HOLD")
        logger.info(f"✅ Analysis cycle complete | BUY: {buys} | SELL: {sells} | HOLD: {holds}")

        return decisions

    def analyze_symbol(self, symbol: str) -> Optional[TradeDecision]:
        """Full analysis for a single symbol."""

        # ── 1. Get current price ──────────────────────────────
        tick = self.price_feed.get_latest_tick(symbol)
        if not tick:
            logger.debug(f"No tick data for {symbol} — skipping")
            return None
        current_price = tick.ltp

        # ── 2. Technical analysis ─────────────────────────────
        df = self.price_feed.get_intraday(symbol, interval="15m")
        if df is None or df.empty:
            df = self.price_feed.get_historical(symbol, period="1mo", interval="1d")

        tech_signal: Optional[TechnicalSignal] = None
        tech_score = 0.0
        tech_action = "HOLD"

        if df is not None and not df.empty:
            tech_signal = self.technical.analyze(symbol, df)
            if tech_signal:
                tech_action = tech_signal.action
                # Map BUY/SELL/HOLD to score
                if tech_signal.action == "BUY":
                    tech_score = tech_signal.confidence
                elif tech_signal.action == "SELL":
                    tech_score = -tech_signal.confidence
                else:
                    tech_score = 0.0

        # ── 3. News sentiment ─────────────────────────────────
        articles = self.news_feed.get_for_symbol(symbol, hours=4)
        sentiment_data = self.sentiment.score_symbol_news(symbol, articles)
        sent_score = sentiment_data["avg_score"]
        sent_label = sentiment_data["label"]

        # ── 4. Fundamental score (yfinance: P/E, P/B, growth, margins) ──
        fundamental_score = self.fundamental.score(symbol)

        # ── 5. Weighted fusion ────────────────────────────────
        w_tech = self.weights["technical"]      # 0.45
        w_sent = self.weights["sentiment"]      # 0.35
        w_fund = self.weights["fundamental"]    # 0.20

        # Fundamental score: map 0-1 → -0.5 to +0.5
        fund_contribution = (fundamental_score - 0.5)

        final_score = (
            tech_score * w_tech
            + sent_score * w_sent
            + fund_contribution * w_fund
        )

        confidence = min(abs(final_score) / 0.7, 1.0)  # Normalize

        # ── 6. Decision threshold ─────────────────────────────
        # Symmetric gates: both directions require score threshold AND indicator agreement
        if final_score >= 0.25 and tech_action == "BUY":
            action = "BUY"
        elif final_score <= -0.25 and tech_action == "SELL":
            action = "SELL"
        else:
            action = "HOLD"

        # ── 6b. LLM tie-break for borderline signals ──────────
        # When signals are mixed (-0.25 < score < +0.25), ask the LLM
        # for a final confident decision like an experienced trader would.
        llm_reasoning = ""
        if action == "HOLD" and self.llm.is_available() and abs(final_score) > 0.10:
            try:
                key_headlines = [a.title for a in articles[:3]]
                prompt = self.llm.build_trading_prompt(
                    symbol=symbol,
                    price=current_price,
                    tech_signal=tech_action,
                    tech_confidence=tech_signal.confidence if tech_signal else 0.0,
                    sentiment_score=sent_score,
                    sentiment_label=sent_label,
                    key_news=key_headlines,
                    rsi=tech_signal.rsi if tech_signal else 50.0,
                    trend=tech_signal.trend if tech_signal else "SIDEWAYS",
                )
                llm_response = self.llm.reason(prompt, max_tokens=80)
                llm_reasoning = f"LLM ({self.llm.provider}): {llm_response}"

                # Parse the LLM's recommendation
                resp_upper = llm_response.upper()
                if resp_upper.startswith("BUY") or " BUY " in resp_upper[:20]:
                    action = "BUY"
                    confidence = min(confidence + 0.10, 0.95)
                elif resp_upper.startswith("SELL") or " SELL " in resp_upper[:20]:
                    action = "SELL"
                    confidence = min(confidence + 0.10, 0.95)
                # else stays HOLD

                logger.debug(f"LLM tie-break for {symbol}: {llm_response[:60]}")
            except Exception as e:
                logger.debug(f"LLM tie-break skipped for {symbol}: {e}")

        # ── 7. Risk management check ──────────────────────────
        quantity = 0
        block_reason = ""

        if action == "BUY":
            allowed, reason, dollar_amount = self.risk_manager.can_buy(
                symbol, current_price, confidence
            )
            if not allowed:
                action = "BLOCKED"
                block_reason = reason
                quantity = 0
            else:
                # Apply human-like size variance to the dollar amount
                variance = self.behavior["order_size_variance_pct"]
                dollar_amount *= 1 + random.uniform(-variance, variance)
                # Fractional share count for display (actual order uses dollar_amount)
                quantity = round(dollar_amount / current_price, 6) if current_price > 0 else 1.0

        elif action == "SELL":
            allowed, reason = self.risk_manager.can_sell(symbol)
            if not allowed:
                action = "HOLD"
                block_reason = reason

        # ── 8. Build reasoning text ───────────────────────────
        reasoning_parts = []

        if tech_signal:
            reasoning_parts.append(f"Technical: {tech_signal.reasoning}")
        reasoning_parts.append(f"News: {sentiment_data['summary']}")
        if llm_reasoning:
            reasoning_parts.append(llm_reasoning)
        if action == "BUY":
            reasoning_parts.append(
                f"Risk OK -- buying {quantity} shares @ ${current_price:.2f}"
            )
        elif action == "BLOCKED":
            reasoning_parts.append(f"Blocked: {block_reason}")
        elif action == "SELL":
            reasoning_parts.append(f"Selling position in {symbol}")
        else:
            reasoning_parts.append("Signals are mixed -- holding and watching")

        reasoning = "\n".join(reasoning_parts)

        return TradeDecision(
            symbol=symbol,
            action=action,
            price=current_price,
            quantity=quantity,
            confidence=round(confidence, 3),
            timestamp=datetime.now(),
            technical_score=round(tech_score, 4),
            sentiment_score=round(sent_score, 4),
            fundamental_score=round(fundamental_score, 4),
            final_score=round(final_score, 4),
            technical_action=tech_action,
            sentiment_label=sent_label,
            reasoning=reasoning,
            block_reason=block_reason,
        )

    # ─── Stop-Loss Monitor ────────────────────────────────────

    def check_exits(self) -> list:
        """
        Check all open positions for stop-loss / target hits.
        Should be called every minute (or on every tick in live mode).
        
        Returns list of (symbol, price, reason) tuples to exit.
        """
        current_prices = {
            s: tick.ltp
            for s, tick in self.price_feed.get_all_ticks().items()
            if tick
        }
        return self.risk_manager.check_stop_losses(current_prices)

    # ─── Human-Like Order Delay ───────────────────────────────

    def apply_human_delay(self):
        """
        Add a randomized delay before placing an order.
        Mimics natural human behavior — avoids pattern detection.
        """
        delay = random.uniform(
            self.behavior["order_delay_min_seconds"],
            self.behavior["order_delay_max_seconds"],
        )
        logger.debug(f"⏱️  Human delay: {delay:.1f}s before order")
        time.sleep(delay)
