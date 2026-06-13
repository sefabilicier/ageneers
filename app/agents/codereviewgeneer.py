"""
Code Review Agent — Node 4.5 in the pipeline (runs after code_writer).

Performs a lightweight LLM-based code review on the changes before they
are tested and merged. Does NOT block the pipeline — issues are recorded
in the execution report and quality score, but the pipeline continues.

Why non-blocking:
  - Blocking on review would require human judgement for edge cases
  - LLM reviewers can produce false positives
  - The test runner is a stronger signal than review comments
  - Critical issues still lower the quality score significantly

What it checks:
  - Security: hardcoded secrets, injection risks, unvalidated input
  - Correctness: logic errors, wrong status codes, missing edge cases
  - Quality: dead code, missing error handling
  - Tests: missing coverage, weak assertions
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.models.state import AgentState, CodeReview, PipelineStatus, ReviewIssue
from app.prompts import load_prompt
from app.utils.logger import get_logger

logger = get_logger(__name__)

MODEL_NAME        = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_DIFF_CHARS    = 8_000   # keep review context focused
_SYSTEM_PROMPT    = load_prompt("code_reviewer")

_llm: ChatGroq | None = None


def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model=MODEL_NAME, temperature=0.1, max_tokens=2048)
    return _llm


def _build_review_prompt(state: AgentState) -> str:
    """Build the human message for the review LLM."""
    task       = state.parsed_task
    change     = state.code_change
    workspace  = Path(state.workspace_path) if state.workspace_path else None

    lines = []

    # Requirement and criteria
    if task:
        lines.append(f"REQUIREMENT:\n{task.requirement}\n")
        if task.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA:")
            for i, c in enumerate(task.acceptance_criteria, 1):
                lines.append(f"  {i}. {c}")
            lines.append("")

    # Changed files content
    if change and change.changed_files and workspace:
        lines.append("CHANGED FILES:")
        total_chars = 0
        for rel_path in change.changed_files:
            abs_path = workspace / rel_path
            if abs_path.exists():
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                if total_chars + len(content) > MAX_DIFF_CHARS:
                    lines.append(f"\n--- {rel_path} (truncated) ---")
                    remaining = MAX_DIFF_CHARS - total_chars
                    lines.append(content[:remaining] + "\n[... truncated ...]")
                    break
                lines.append(f"\n--- {rel_path} ---")
                lines.append(content)
                total_chars += len(content)

    # Diff if available
    if state.diff_preview:
        diff_excerpt = state.diff_preview[:2000]
        lines.append(f"\nDIFF (excerpt):\n{diff_excerpt}")

    return "\n".join(lines)


def _parse_review_output(raw: str) -> list[dict]:
    """Parse LLM JSON output, with fallback."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    try:
        data = ast.literal_eval(cleaned)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node — code review after code generation."""
    logger.info("code_reviewer.started",
                files=state.code_change.changed_files if state.code_change else [])

    # Skip if no code was generated
    if not state.code_change or not state.code_change.changed_files:
        logger.warning("code_reviewer.skipped", reason="no code changes to review")
        state.log_step("code_reviewer", "skipped", detail="no changes")
        return {
            "code_review": CodeReview(issues=[], passed=True, summary="No changes to review"),
            "step_logs":   state.step_logs,
        }

    prompt_text = _build_review_prompt(state)

    try:
        response = _get_llm().invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=prompt_text),
        ])
        raw = response.content
        issues_raw = _parse_review_output(raw)

        issues = []
        critical_count = 0
        for item in issues_raw:
            if not isinstance(item, dict):
                continue
            severity = item.get("severity", "info")
            issue = ReviewIssue(
                category=item.get("category", "quality"),
                severity=severity,
                file=item.get("file", "unknown"),
                description=item.get("description", ""),
            )
            issues.append(issue)
            if severity == "critical":
                critical_count += 1

        passed  = critical_count == 0
        summary = (
            f"{len(issues)} issue(s) found"
            f" ({critical_count} critical)" if issues
            else "No issues found"
        )

        logger.info(
            "code_reviewer.completed",
            issues=len(issues),
            critical=critical_count,
            passed=passed,
            summary=summary,
        )
        state.log_step("code_reviewer", "completed",
                       detail=f"{len(issues)} issues, {critical_count} critical")

        review = CodeReview(issues=issues, passed=passed, summary=summary)

    except Exception as exc:
        logger.error("code_reviewer.error", error=str(exc),
                     hint="Review failed — pipeline continues without review")
        state.log_step("code_reviewer", "skipped", detail=f"error: {exc}")
        review = CodeReview(issues=[], passed=True, summary=f"Review error: {exc}")

    return {
        "code_review": review,
        "step_logs":   state.step_logs,
    }