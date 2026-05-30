"""
PortfolioAgent — Claude Opus 4.7 powered portfolio review and rebalancing.

Call at end-of-day (16:30 ET) or on-demand to:
- Review current positions vs optimal allocation
- Assess P&L performance and drawdown
- Generate rebalancing recommendations
- Produce daily trading report
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from loguru import logger

from agent.agents.base_agent import BaseAgent, MODEL, READ_FILE_TOOL
from agent.agents.schemas.trading_schemas import PortfolioReview
from agent.config import CONFIG

_SYSTEM_PROMPT = """\
You are a senior portfolio manager and risk officer with 25+ years managing US equity portfolios.
You are reviewing an automated trading system's $100 USD portfolio of US stocks.

## Your Review Task
Analyze the portfolio state provided and output actionable recommendations.

## Framework
1. **Performance**: Daily P&L, total return, winner/loser breakdown
2. **Risk**: Concentration risk, sector exposure, drawdown from peak
3. **Position Quality**: Each position's momentum, stop-loss headroom, target proximity
4. **Rebalancing**: Which positions to trim, add to, or close
5. **Strategy**: Is the current approach working? What to change tomorrow?

## Risk Constraints (built-in system rules)
- Max 15% portfolio per position
- Min 20% cash reserve
- 5% daily loss = circuit breaker
- 2% per-trade stop-loss (static)
- 1% trailing stop

## Portfolio Health Scoring
- EXCELLENT: Daily P&L > 0, all positions > stop-loss with buffer, diversified
- GOOD: Flat to slightly positive, manageable risk
- FAIR: Small losses, concentration risk, or thin stop-loss buffer
- POOR: Near circuit-breaker, multiple positions near stop-loss, concentration issues

## Output Format
Output ONLY this raw JSON after analysis (no markdown fences):
{
  "generated_at": "<ISO 8601>",
  "model": "<model string>",
  "portfolio_health": "EXCELLENT|GOOD|FAIR|POOR",
  "total_return_pct": <float>,
  "daily_pnl_usd": <float>,
  "sharpe_assessment": "<qualitative risk-adjusted return commentary>",
  "key_insights": ["<up to 5 most important observations>"],
  "position_reviews": [
    {
      "symbol": "AAPL",
      "action": "HOLD|ADD|REDUCE|CLOSE",
      "reasoning": "<specific reason>",
      "current_weight_pct": <float>,
      "target_weight_pct": <float>
    }
  ],
  "rebalancing_suggestions": ["<specific actionable suggestion>"],
  "risk_warnings": ["<specific risk that needs attention>"],
  "next_actions": ["<prioritized actions for tomorrow>"],
  "eod_recommendation": "HOLD_ALL|REBALANCE|REDUCE_RISK|INCREASE_EXPOSURE"
}
"""

_GET_POSITIONS_TOOL: dict[str, Any] = {
    "name": "get_portfolio_state",
    "description": "Return the current portfolio state: positions, cash, P&L, and risk metrics.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_TOOLS = [READ_FILE_TOOL, _GET_POSITIONS_TOOL]


class PortfolioAgent(BaseAgent):
    """End-of-day portfolio review and rebalancing agent."""

    def __init__(self, portfolio_state_fn=None) -> None:
        """
        Args:
            portfolio_state_fn: Callable that returns current portfolio state as dict.
                                If None, uses the state passed directly to review().
        """
        super().__init__()
        self._portfolio_state_fn = portfolio_state_fn

    def _tool_get_portfolio_state(self) -> str:
        if self._portfolio_state_fn:
            try:
                state = self._portfolio_state_fn()
                return json.dumps(state, default=str, indent=2)
            except Exception as exc:
                return json.dumps({"error": str(exc)})
        return json.dumps({"error": "No portfolio state provider configured"})

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        if name == "get_portfolio_state":
            return self._tool_get_portfolio_state()
        return super()._execute_tool(name, tool_input)

    def review(self, portfolio_state: dict) -> PortfolioReview:
        """
        Review the portfolio and generate recommendations.

        Args:
            portfolio_state: Dict with keys: cash, invested, daily_pnl, daily_pnl_pct,
                             total_return_pct, positions (list of dicts), trades_today (list),
                             risk_level, circuit_breaker_triggered.

        Returns:
            PortfolioReview with health score and actionable recommendations.
        """
        total_fund = CONFIG["risk"]["total_fund"]
        state_json = json.dumps(portfolio_state, default=str, indent=2)

        user_msg = (
            f"Review the following portfolio state for {datetime.now().strftime('%Y-%m-%d')} "
            f"(end of trading day).\n\n"
            f"Total Fund: ${total_fund:.2f} USD\n\n"
            f"Portfolio State:\n{state_json}\n\n"
            "Use get_portfolio_state tool if you need a fresh state snapshot.\n"
            "Provide honest, direct assessment — no sugar-coating.\n"
            "Output the PortfolioReview JSON when done."
        )

        messages = [{"role": "user", "content": [self._make_text_block(user_msg, cache=True)]}]

        logger.info("[PortfolioAgent] starting portfolio review")
        raw = self._run_agentic_loop(
            system=self._make_system_block(_SYSTEM_PROMPT),
            messages=messages,
            tools=_TOOLS,
            title="Portfolio Review",
        )
        return self._parse_report(raw, portfolio_state)

    def _parse_report(self, raw: str, state: dict) -> PortfolioReview:
        try:
            return PortfolioReview.model_validate_json(raw.strip())
        except Exception:
            pass

        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                data.setdefault("generated_at", datetime.now().isoformat())
                data.setdefault("model", MODEL)
                return PortfolioReview.model_validate(data)
            except Exception as exc:
                logger.error(f"[PortfolioAgent] parse error: {exc}")

        logger.warning("[PortfolioAgent] could not parse report — returning fallback")
        return PortfolioReview(
            generated_at=datetime.now().isoformat(),
            model=MODEL,
            portfolio_health="FAIR",
            total_return_pct=state.get("total_return_pct", 0.0),
            daily_pnl_usd=state.get("daily_pnl", 0.0),
            sharpe_assessment="Unknown — parsing failed",
            key_insights=["report parsing failed — check raw output"],
            position_reviews=[],
            rebalancing_suggestions=[],
            risk_warnings=["review output manually"],
            next_actions=["re-run portfolio review"],
            eod_recommendation="HOLD_ALL",
        )
