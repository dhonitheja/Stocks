"""
Production-level code review agent powered by Claude Opus 4.7.

Analyzes Python source files for bugs, security vulnerabilities, performance
issues, and code quality problems. Tuned for financial trading systems where
bugs carry real monetary risk.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from agent.config import logger
from .base_agent import BaseAgent, LIST_DIRECTORY_TOOL, MODEL, READ_FILE_TOOL
from .schemas import CodeReviewReport, FileReviewSummary, ReviewFinding

_SYSTEM_PROMPT = """\
You are an elite Python security auditor and code quality engineer with 15 years of
experience in financial systems. You are reviewing StockSage — a live algorithmic
trading bot that executes real buy/sell orders on Alpaca Markets with real money.

## Severity Levels
- CRITICAL: Can cause financial loss, data breach, or system failure
  (hardcoded credentials, wrong stop-loss math, uncaught exceptions in order paths)
- HIGH: Will likely cause incorrect behavior or serious performance issues
  (wrong calculation, missing input validation, exception swallowing)
- MEDIUM: May fail in edge cases, hurts maintainability
  (overly broad except clauses, magic numbers, missing type hints)
- LOW: Style and documentation
  (naming, docstrings, unused imports)

## Review Categories
For every file check ALL of:
- SECURITY: credential exposure, path traversal, unsafe eval/exec, injection
- BUG: logic errors, off-by-one, wrong comparisons, incorrect math
- FINANCIAL_SAFETY: stop-loss correctness, position sizing, risk limit enforcement
- EXCEPTION_HANDLING: bare except, swallowed exceptions, missing error paths
- CONCURRENCY: race conditions (price_feed / news_feed use background threads)
- PERFORMANCE: blocking calls in hot paths, missing caching, N+1 patterns
- TYPE_SAFETY: wrong return type vs docstring, missing annotations
- CODE_QUALITY: SOLID violations, coupling, testability, magic numbers

## Codebase Context
- Python 3.10+, loguru logging (`from agent.config import CONFIG, logger`)
- Alpaca broker for order execution — ALPACA_PAPER env var controls paper vs live
- Yahoo Finance for price data; FinBERT for sentiment
- Risk is managed in `agent/core/risk_manager.py`
- Orders are placed in `agent/core/execution_engine.py`

## Output Format
Respond with a single JSON object matching this exact schema.
Do NOT wrap in markdown fences. Raw JSON only.

{
  "reviewed_at": "<ISO 8601 datetime>",
  "model": "<model name>",
  "files_reviewed": [
    {
      "file_path": "<relative path>",
      "lines_reviewed": <int>,
      "findings": [
        {
          "severity": "CRITICAL|HIGH|MEDIUM|LOW",
          "category": "SECURITY|BUG|FINANCIAL_SAFETY|EXCEPTION_HANDLING|CONCURRENCY|PERFORMANCE|TYPE_SAFETY|CODE_QUALITY",
          "line_start": <int>,
          "line_end": <int>,
          "title": "<short title>",
          "description": "<detailed explanation>",
          "recommendation": "<exact fix or actionable step>",
          "financial_impact": "<monetary/risk impact if triggered in live trading, or null>"
        }
      ],
      "critical_count": <int>,
      "high_count": <int>,
      "medium_count": <int>,
      "low_count": <int>,
      "overall_assessment": "<paragraph>",
      "is_safe_for_live_trading": <bool>
    }
  ],
  "total_critical": <int>,
  "total_high": <int>,
  "total_medium": <int>,
  "total_low": <int>,
  "go_live_recommendation": "SAFE|UNSAFE|CONDITIONAL",
  "executive_summary": "<paragraph>",
  "top_priority_fixes": ["<fix 1>", "<fix 2>", "..."]
}
"""

_TOOLS = [READ_FILE_TOOL, LIST_DIRECTORY_TOOL]


class CodeReviewAgent(BaseAgent):
    def review(self, targets: list[str]) -> CodeReviewReport:
        """
        Review one or more Python source files.

        Args:
            targets: List of file paths (absolute or relative to project root)

        Returns:
            CodeReviewReport with structured findings per file
        """
        resolved = []
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = self.project_root / p
            resolved.append(str(p.resolve()))

        user_msg = (
            "Review the following Python source files for the StockSage trading bot.\n"
            "Use the read_file tool to load each file, then produce the full JSON report.\n\n"
            "Files to review:\n"
            + "\n".join(f"- {p}" for p in resolved)
        )

        messages: list[dict] = [
            {"role": "user", "content": [self._make_text_block(user_msg)]}
        ]

        logger.info(f"[CodeReviewAgent] reviewing {len(resolved)} file(s)")
        raw = self._run_agentic_loop(
            system=self._make_system_block(_SYSTEM_PROMPT),
            messages=messages,
            tools=_TOOLS,
            title="Code Review",
        )

        return self._parse_report(raw)

    def _parse_report(self, raw: str) -> CodeReviewReport:
        # Try direct parse first
        try:
            return CodeReviewReport.model_validate_json(raw.strip())
        except Exception:
            pass

        # Extract JSON block from mixed text
        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                data.setdefault("reviewed_at", datetime.now().isoformat())
                data.setdefault("model", MODEL)
                return CodeReviewReport.model_validate(data)
            except Exception as exc:
                logger.error(f"[CodeReviewAgent] parse error: {exc}")

        # Fallback: empty report
        logger.warning("[CodeReviewAgent] could not parse structured report — returning empty")
        return CodeReviewReport(
            reviewed_at=datetime.now(),
            model=MODEL,
            files_reviewed=[],
            total_critical=0,
            total_high=0,
            total_medium=0,
            total_low=0,
            go_live_recommendation="CONDITIONAL",
            executive_summary="Report parsing failed — check raw output.",
            top_priority_fixes=[],
        )
