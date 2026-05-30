"""
Specialist subagents — each focused on one review domain.

Each agent reads a single file and returns JSON findings for its category only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent.config import logger
from agent.agents.base_agent import BaseAgent, READ_FILE_TOOL, MODEL
from agent.agents.schemas import ReviewFinding

_FILE_CONTEXT = """\
## Codebase Context
- Python 3.10+, loguru logging (`from agent.config import CONFIG, logger`)
- Alpaca broker for order execution
- Yahoo Finance for price data; FinBERT for sentiment
- Risk managed in `agent/core/risk_manager.py`
- Orders placed in `agent/core/execution_engine.py`
"""

_OUTPUT_FORMAT = """\
## Output Format
Return a JSON array (raw, no markdown fences). Each element:
{
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "category": "<your assigned category>",
  "line_start": <int>,
  "line_end": <int>,
  "title": "<short title>",
  "description": "<detailed explanation>",
  "recommendation": "<exact actionable fix>",
  "financial_impact": "<monetary/risk impact or null>"
}
Return [] if no issues found.
"""

_SECURITY_SYSTEM = f"""\
You are a Python security auditor specializing in financial trading systems.
Review the given file ONLY for SECURITY vulnerabilities.

## Your Scope: SECURITY only
- Hardcoded credentials, API keys, tokens
- Path traversal in file operations
- Unsafe eval/exec/subprocess calls
- SQL/command injection
- Insecure deserialization
- Exposed secrets in logs or error messages
- Unsafe use of environment variables

Severity:
- CRITICAL: Credential exposure, live system compromise possible
- HIGH: Exploitable vulnerability but requires specific conditions
- MEDIUM: Security smell, indirect risk
- LOW: Minor security hygiene

{_FILE_CONTEXT}
{_OUTPUT_FORMAT}
"""

_FINANCIAL_SYSTEM = f"""\
You are a quantitative finance engineer auditing algorithmic trading code.
Review the given file ONLY for FINANCIAL_SAFETY issues.

## Your Scope: FINANCIAL_SAFETY only
- Stop-loss math correctness (off-by-one, wrong comparison operators)
- Position sizing calculations
- Risk limit enforcement (daily loss cap, max position size)
- Order quantity rounding (fractional shares)
- Price feed staleness checks
- Duplicate order prevention
- Circuit breaker logic
- P&L calculation accuracy

Severity:
- CRITICAL: Can directly cause monetary loss in live trading
- HIGH: Likely incorrect behavior that affects order execution
- MEDIUM: Edge case that may misfire in volatile markets
- LOW: Minor inconsistency in financial logic

{_FILE_CONTEXT}
{_OUTPUT_FORMAT}
"""

_BUGS_SYSTEM = f"""\
You are a Python expert specializing in bug detection and exception handling.
Review the given file ONLY for BUG and EXCEPTION_HANDLING issues.

## Your Scope: BUG + EXCEPTION_HANDLING only
Bugs:
- Logic errors, wrong comparison operators
- Off-by-one errors in loops/slices
- Incorrect variable usage (typos, wrong scope)
- Wrong math operations
- Null/None dereference without guards
- Incorrect type assumptions

Exception Handling:
- Bare `except:` or `except Exception:` that swallow errors
- Missing error paths in critical flows
- Exceptions logged but not re-raised when they should be
- Missing finally/cleanup blocks
- Incorrect exception hierarchy

Severity:
- CRITICAL: Will crash or corrupt state in production
- HIGH: Will cause wrong results or silent failures
- MEDIUM: Fails only in edge cases
- LOW: Minor robustness issue

{_FILE_CONTEXT}
{_OUTPUT_FORMAT}
"""

_PERFORMANCE_SYSTEM = f"""\
You are a Python performance and concurrency engineer.
Review the given file for PERFORMANCE, CONCURRENCY, TYPE_SAFETY, and CODE_QUALITY issues.

## Your Scope: PERFORMANCE + CONCURRENCY + TYPE_SAFETY + CODE_QUALITY
Performance:
- Blocking I/O calls in hot trading loops
- Missing caching for repeated expensive calls
- N+1 query/request patterns
- Unnecessary object creation in tight loops

Concurrency:
- Race conditions (price_feed / news_feed use background threads)
- Missing locks around shared mutable state
- Thread-unsafe data structures used across threads
- Missing volatile/memory-fence equivalents

Type Safety:
- Wrong return type vs docstring/annotation
- Missing type annotations on public methods
- Incorrect type assumptions passed to downstream functions

Code Quality:
- SOLID violations (God class, tight coupling)
- Magic numbers without named constants
- Deeply nested code (> 4 levels)
- Functions doing too many things
- Poor testability (untestable side effects)

Severity:
- CRITICAL: Race condition causes wrong trades or system freeze
- HIGH: Significant performance degradation in live trading
- MEDIUM: Technical debt, medium impact
- LOW: Style/maintainability

{_FILE_CONTEXT}
{_OUTPUT_FORMAT}
"""


class SpecialistAgent(BaseAgent):
    """Base for focused single-domain review agents."""

    system_prompt: str = ""
    agent_label: str = "Specialist"

    def review_file(self, file_path: str) -> list[ReviewFinding]:
        resolved = str(Path(file_path).resolve())
        user_msg = (
            f"Review this file and return findings as a JSON array.\n\nFile: {resolved}"
        )
        messages: list[dict] = [
            {"role": "user", "content": [self._make_text_block(user_msg)]}
        ]
        logger.debug(f"[{self.agent_label}] reviewing {file_path}")
        raw = self._run_agentic_loop(
            system=self._make_system_block(self.system_prompt),
            messages=messages,
            tools=[READ_FILE_TOOL],
            title=self.agent_label,
        )
        return self._parse_findings(raw)

    def _parse_findings(self, raw: str) -> list[ReviewFinding]:
        # Try direct array parse
        try:
            data = json.loads(raw.strip())
            if isinstance(data, list):
                return [ReviewFinding.model_validate(f) for f in data]
        except Exception:
            pass

        # Extract JSON array from mixed text
        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                data = json.loads(match.group())
                return [ReviewFinding.model_validate(f) for f in data]
            except Exception as exc:
                logger.warning(f"[{self.agent_label}] parse error: {exc}")

        logger.warning(f"[{self.agent_label}] no findings parsed")
        return []


class SecurityReviewAgent(SpecialistAgent):
    system_prompt = _SECURITY_SYSTEM
    agent_label = "Security"


class FinancialSafetyAgent(SpecialistAgent):
    system_prompt = _FINANCIAL_SYSTEM
    agent_label = "FinancialSafety"


class BugDetectionAgent(SpecialistAgent):
    system_prompt = _BUGS_SYSTEM
    agent_label = "BugDetection"


class PerformanceReviewAgent(SpecialistAgent):
    system_prompt = _PERFORMANCE_SYSTEM
    agent_label = "Performance"
