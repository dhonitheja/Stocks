from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


class ReviewFinding(BaseModel):
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    category: Literal[
        "SECURITY", "BUG", "FINANCIAL_SAFETY", "EXCEPTION_HANDLING",
        "CONCURRENCY", "PERFORMANCE", "TYPE_SAFETY", "CODE_QUALITY"
    ]
    line_start: int
    line_end: int
    title: str
    description: str
    recommendation: str
    financial_impact: Optional[str] = None


class FileReviewSummary(BaseModel):
    file_path: str
    lines_reviewed: int
    findings: list[ReviewFinding]
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    overall_assessment: str
    is_safe_for_live_trading: bool


class CodeReviewReport(BaseModel):
    reviewed_at: datetime
    model: str
    files_reviewed: list[FileReviewSummary]
    total_critical: int
    total_high: int
    total_medium: int
    total_low: int
    go_live_recommendation: Literal["SAFE", "UNSAFE", "CONDITIONAL"]
    executive_summary: str
    top_priority_fixes: list[str] = Field(
        description="Ordered list of the 3-5 most important fixes before going live"
    )
