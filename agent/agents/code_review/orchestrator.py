"""
Multi-agent code review orchestrator.

Runs 4 specialist subagents in parallel per file, then synthesizes findings
into a unified CodeReviewReport.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic

from agent.config import logger
from agent.agents.base_agent import MODEL, THINKING, MAX_TOKENS
from agent.agents.schemas import CodeReviewReport, FileReviewSummary, ReviewFinding
from .specialists import (
    SecurityReviewAgent,
    FinancialSafetyAgent,
    BugDetectionAgent,
    PerformanceReviewAgent,
)

_SYNTHESIS_SYSTEM = """\
You are a senior engineering lead reviewing a consolidated code audit for StockSage,
a live algorithmic trading bot. You will receive structured findings from 4 specialist
reviewers (Security, Financial Safety, Bug Detection, Performance).

Produce a concise executive summary, go-live recommendation, and the 3-5 highest
priority fixes. Focus on financial and production risk.

Respond as raw JSON (no markdown fences):
{
  "executive_summary": "<2-3 sentence paragraph>",
  "go_live_recommendation": "SAFE|UNSAFE|CONDITIONAL",
  "top_priority_fixes": ["<fix 1>", "<fix 2>", "<fix 3>"]
}
"""

_SPECIALIST_CLASSES = [
    SecurityReviewAgent,
    FinancialSafetyAgent,
    BugDetectionAgent,
    PerformanceReviewAgent,
]


def _count_by_severity(findings: list[ReviewFinding]) -> tuple[int, int, int, int]:
    c = h = m = l = 0
    for f in findings:
        if f.severity == "CRITICAL":
            c += 1
        elif f.severity == "HIGH":
            h += 1
        elif f.severity == "MEDIUM":
            m += 1
        else:
            l += 1
    return c, h, m, l


def _overall_assessment(findings: list[ReviewFinding], file_path: str) -> str:
    c, h, m, l = _count_by_severity(findings)
    if c > 0:
        return f"{file_path}: {c} CRITICAL issue(s) found — unsafe for live trading."
    if h > 0:
        return f"{file_path}: {h} HIGH issue(s) — conditional, fix before going live."
    if m + l > 0:
        return f"{file_path}: {m} medium / {l} low issues — generally safe, improvements recommended."
    return f"{file_path}: No issues found across all review domains."


class MultiAgentCodeReviewer:
    """
    Orchestrates 4 specialist agents per file in parallel, then synthesizes results.
    """

    def __init__(self, max_workers: int = 8) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_workers = max_workers
        self._project_root = Path(__file__).parent.parent.parent.parent.resolve()

    def review(self, targets: list[str]) -> CodeReviewReport:
        resolved = []
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = self._project_root / p
            resolved.append(str(p.resolve()))

        logger.info(f"[MultiAgentReviewer] {len(resolved)} file(s) × 4 specialists")

        # Build (file_path, SpecialistClass) tasks
        tasks = [(fp, cls) for fp in resolved for cls in _SPECIALIST_CLASSES]

        # Collect findings per file
        findings_by_file: dict[str, list[ReviewFinding]] = {fp: [] for fp in resolved}

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_task = {
                pool.submit(self._run_specialist, fp, cls): (fp, cls.__name__)
                for fp, cls in tasks
            }
            for future in as_completed(future_to_task):
                fp, label = future_to_task[future]
                try:
                    results = future.result()
                    findings_by_file[fp].extend(results)
                    logger.info(f"[{label}] {fp}: {len(results)} finding(s)")
                except Exception as exc:
                    logger.error(f"[{label}] {fp}: failed — {exc}")

        # Build per-file summaries
        file_summaries: list[FileReviewSummary] = []
        for fp in resolved:
            findings = findings_by_file[fp]
            c, h, m, l = _count_by_severity(findings)
            rel_path = str(Path(fp).relative_to(self._project_root))
            lines = self._count_lines(fp)
            file_summaries.append(FileReviewSummary(
                file_path=rel_path,
                lines_reviewed=lines,
                findings=findings,
                critical_count=c,
                high_count=h,
                medium_count=m,
                low_count=l,
                overall_assessment=_overall_assessment(findings, rel_path),
                is_safe_for_live_trading=(c == 0),
            ))

        total_c = sum(s.critical_count for s in file_summaries)
        total_h = sum(s.high_count for s in file_summaries)
        total_m = sum(s.medium_count for s in file_summaries)
        total_l = sum(s.low_count for s in file_summaries)

        summary, recommendation, top_fixes = self._synthesize(file_summaries)

        return CodeReviewReport(
            reviewed_at=datetime.now(),
            model=f"{MODEL} (multi-agent)",
            files_reviewed=file_summaries,
            total_critical=total_c,
            total_high=total_h,
            total_medium=total_m,
            total_low=total_l,
            go_live_recommendation=recommendation,
            executive_summary=summary,
            top_priority_fixes=top_fixes,
        )

    def _run_specialist(self, file_path: str, specialist_cls) -> list[ReviewFinding]:
        agent = specialist_cls()
        return agent.review_file(file_path)

    def _count_lines(self, file_path: str) -> int:
        try:
            return len(Path(file_path).read_text(encoding="utf-8").splitlines())
        except Exception:
            return 0

    def _synthesize(
        self, summaries: list[FileReviewSummary]
    ) -> tuple[str, str, list[str]]:
        """Single Claude call to produce executive summary + recommendation."""
        import json

        findings_dump = []
        for s in summaries:
            for f in s.findings:
                findings_dump.append({
                    "file": s.file_path,
                    "severity": f.severity,
                    "category": f.category,
                    "title": f.title,
                    "description": f.description,
                    "financial_impact": f.financial_impact,
                })

        total_c = sum(s.critical_count for s in summaries)
        total_h = sum(s.high_count for s in summaries)

        user_content = (
            f"Files reviewed: {len(summaries)}\n"
            f"Total findings: CRITICAL={total_c} HIGH={total_h}\n\n"
            f"All findings:\n{json.dumps(findings_dump, indent=2)}"
        )

        logger.info("[MultiAgentReviewer] running synthesis pass")
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=2048,
            thinking=THINKING,
            system=[{
                "type": "text",
                "text": _SYNTHESIS_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )

        raw = next(
            (b.text for b in response.content if b.type == "text"), "{}"
        )

        import re
        match = re.search(r"\{[\s\S]+\}", raw)
        if match:
            try:
                data = json.loads(match.group())
                return (
                    data.get("executive_summary", "See findings for details."),
                    data.get("go_live_recommendation", "CONDITIONAL"),
                    data.get("top_priority_fixes", []),
                )
            except Exception:
                pass

        # Fallback
        rec = "UNSAFE" if total_c > 0 else ("CONDITIONAL" if total_h > 0 else "SAFE")
        return "Multi-agent review complete. See per-file findings.", rec, []
