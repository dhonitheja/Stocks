from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class TestCase(BaseModel):
    name: str
    category: Literal["UNIT", "BOUNDARY", "FINANCIAL", "CONCURRENCY", "ERROR"]
    description: str
    mocks_required: list[str]
    assertions: list[str]


class TestFile(BaseModel):
    file_name: str
    module_under_test: str
    test_count: int
    test_cases: list[TestCase]
    coverage_areas: list[str]


class QAReport(BaseModel):
    generated_at: datetime
    model: str
    modules_tested: list[str]
    test_files: list[TestFile]
    total_test_count: int
    conftest_generated: bool
    estimated_coverage_pct: float
    missing_coverage: list[str]
    known_limitations: list[str]
