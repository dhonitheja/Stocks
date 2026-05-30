"""
Production-level QA testing agent powered by Claude Opus 4.7.

Generates comprehensive pytest test suites for StockSage modules.
Tests are isolated (no real network/broker calls), deterministic,
and financially aware (covers edge cases that matter for trading systems).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic

from agent.config import logger
from .base_agent import BaseAgent, LIST_DIRECTORY_TOOL, MODEL, READ_FILE_TOOL
from .schemas import QAReport, TestFile

_SYSTEM_PROMPT = """\
You are a senior Python test engineer specializing in financial trading systems.
Generate production-quality pytest test suites for StockSage modules.

## Standards
- pytest with fixtures (function/module/session scope as appropriate)
- unittest.mock.patch and MagicMock for ALL external dependencies
- pytest.mark.parametrize for multi-case coverage
- pytest.raises for exception testing
- conftest.py for shared fixtures
- Full type annotations on all test functions
- Arrange/Act/Assert structure with comments

## Always Mock These (never let them make real calls)
- yfinance.Ticker, yfinance.download
- alpaca.trading.client.TradingClient and all Alpaca SDK types
- transformers.pipeline (FinBERT)
- feedparser.parse, requests.get, httpx.get
- os.getenv for API keys (ALPACA_API_KEY, ALPACA_SECRET_KEY, etc.)
- threading.Thread.start (prevent background threads in tests)

## Test Categories
- UNIT: each public method in isolation with mocked dependencies
- BOUNDARY: zero/negative/max values, empty collections, NaN/inf
- FINANCIAL: stop-loss triggers, position limits, circuit breaker logic, P&L math
- CONCURRENCY: basic thread-safety checks where applicable
- ERROR: broker API failures, data feed failures, model failures

## Naming Convention
test_<method_name>_<scenario_description>

## conftest.py Must Include
- mock_config: stripped-down CONFIG dict with all required keys
- sample_ohlcv_df: 250-row synthetic OHLCV DataFrame (realistic price data)
- mock_alpaca_client: MagicMock configured with expected return shapes
- mock_finbert: MagicMock pipeline returning deterministic sentiment results

## File Writing
Use the write_test_file tool to write each generated test file.
Write conftest.py first, then one test file per module.

## Report Format
After writing all files, output a JSON summary (raw, no markdown fences):
{
  "generated_at": "<ISO 8601>",
  "model": "<model>",
  "modules_tested": ["<module path>"],
  "test_files": [
    {
      "file_name": "<tests/test_xxx.py>",
      "module_under_test": "<agent/core/xxx.py>",
      "test_count": <int>,
      "test_cases": [
        {
          "name": "<test function name>",
          "category": "UNIT|BOUNDARY|FINANCIAL|CONCURRENCY|ERROR",
          "description": "<what behavior is tested>",
          "mocks_required": ["<mock1>"],
          "assertions": ["<assertion description>"]
        }
      ],
      "coverage_areas": ["<area1>"]
    }
  ],
  "total_test_count": <int>,
  "conftest_generated": <bool>,
  "estimated_coverage_pct": <float>,
  "missing_coverage": ["<area>"],
  "known_limitations": ["<limitation>"]
}
"""

_WRITE_FILE_TOOL: dict[str, Any] = {
    "name": "write_test_file",
    "description": "Write a generated pytest test file into the tests/ directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": "File name relative to tests/ directory, e.g. 'test_risk_manager.py' or 'conftest.py'",
            },
            "content": {
                "type": "string",
                "description": "Complete Python file content",
            },
        },
        "required": ["file_name", "content"],
    },
}

_TOOLS = [READ_FILE_TOOL, LIST_DIRECTORY_TOOL, _WRITE_FILE_TOOL]


class QATestingAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__()
        self.output_dir = self.project_root / os.getenv("QA_OUTPUT_DIR", "tests")
        self.output_dir.mkdir(exist_ok=True)

    def _tool_write_test_file(self, file_name: str, content: str) -> str:
        # Only allow writing inside output_dir
        safe_name = Path(file_name).name  # strip any path components
        dest = (self.output_dir / safe_name).resolve()
        if not str(dest).startswith(str(self.output_dir.resolve())):
            return f"ERROR: path traversal denied — {file_name}"
        try:
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(dest)
            logger.info(f"[QAAgent] wrote {dest.relative_to(self.project_root)}")
            return f"OK: wrote {dest}"
        except Exception as exc:
            return f"ERROR: {exc}"

    def _execute_tool(self, name: str, tool_input: dict) -> str:
        if name == "write_test_file":
            return self._tool_write_test_file(
                tool_input["file_name"], tool_input["content"]
            )
        return super()._execute_tool(name, tool_input)

    def generate(self, module_paths: list[str]) -> QAReport:
        """
        Generate pytest test suite for one or more modules.

        Args:
            module_paths: List of source file paths to test

        Returns:
            QAReport with metadata about generated tests
        """
        resolved = []
        for p in module_paths:
            path = Path(p)
            if not path.is_absolute():
                path = self.project_root / path
            resolved.append(str(path.resolve()))

        user_msg = (
            "Generate a production-quality pytest test suite for the following StockSage modules.\n"
            "Start by listing the agent/ directory to understand the full codebase structure.\n"
            "Then write conftest.py first, followed by one test file per module.\n"
            "Use write_test_file to save each file.\n\n"
            "Modules to test:\n"
            + "\n".join(f"- {p}" for p in resolved)
            + f"\n\nOutput directory: {self.output_dir}"
        )

        messages: list[dict] = [
            {"role": "user", "content": [self._make_text_block(user_msg)]}
        ]

        logger.info(f"[QAAgent] generating tests for {len(resolved)} module(s)")
        raw = self._run_agentic_loop(
            system=self._make_system_block(_SYSTEM_PROMPT),
            messages=messages,
            tools=_TOOLS,
            title="QA Test Generation",
        )

        return self._parse_report(raw)

    def _parse_report(self, raw: str) -> QAReport:
        try:
            return QAReport.model_validate_json(raw.strip())
        except Exception:
            pass

        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                data.setdefault("generated_at", datetime.now().isoformat())
                data.setdefault("model", MODEL)
                return QAReport.model_validate(data)
            except Exception as exc:
                logger.error(f"[QAAgent] parse error: {exc}")

        logger.warning("[QAAgent] could not parse structured report — returning empty")
        return QAReport(
            generated_at=datetime.now(),
            model=MODEL,
            modules_tested=[],
            test_files=[],
            total_test_count=0,
            conftest_generated=(self.output_dir / "conftest.py").exists(),
            estimated_coverage_pct=0.0,
            missing_coverage=["report parsing failed"],
            known_limitations=["check raw output above"],
        )
