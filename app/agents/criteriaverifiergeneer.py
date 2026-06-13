"""
Acceptance Criteria Verifier — Node 4.8 (runs after code_reviewer).

Implements the Evaluator-Optimizer pattern from Anthropic's agent guide:
  - One LLM generates code (code_writer)
  - Another LLM evaluates whether criteria are met (criteria_verifier)
  - If criteria are NOT met, code_writer gets another chance (max MAX_RETRIES)

This catches the most common failure mode: LLM changes the API contract
(e.g. returns 422 instead of 400) while thinking it satisfied the requirement.

Retry flow:
    code_writer → criteria_verifier
        ↑               ↓ criteria not met (retry < MAX_RETRIES)
        └───────────────┘
                        ↓ criteria met OR retries exhausted
                   test_runner
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.models.state import AgentState, CriteriaResult, PipelineStatus
from app.prompts import load_prompt
from app.utils.logger import get_logger

logger = get_logger(__name__)

MODEL_NAME     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_RETRIES    = int(os.getenv("CRITERIA_MAX_RETRIES", "2"))
_SYSTEM_PROMPT = load_prompt("criteria_verifier")

_llm: ChatGroq | None = None


def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model=MODEL_NAME, temperature=0.1, max_tokens=2048)
    return _llm


def _build_verify_prompt(state: AgentState) -> str:
    task      = state.parsed_task
    change    = state.code_change
    workspace = Path(state.workspace_path) if state.workspace_path else None

    lines = []
    if task and task.acceptance_criteria:
        lines.append("ACCEPTANCE CRITERIA TO VERIFY:")
        for i, c in enumerate(task.acceptance_criteria, 1):
            lines.append(f"  {i}. {c}")
        lines.append("")

    if change and change.changed_files and workspace:
        lines.append("CHANGED FILES:")
        total = 0
        for rel_path in change.changed_files:
            abs_path = workspace / rel_path
            if abs_path.exists():
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                if total + len(content) > 10_000:
                    lines.append(f"\n--- {rel_path} (truncated) ---")
                    lines.append(content[:10_000 - total])
                    break
                lines.append(f"\n--- {rel_path} ---")
                lines.append(content)
                total += len(content)

    return "\n".join(lines)


def _parse_verification(raw: str) -> list[dict]:
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
        return ast.literal_eval(cleaned)
    except Exception:
        return []


def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node — verify acceptance criteria."""
    task = state.parsed_task
    retry_count = getattr(state, "criteria_retry_count", 0)

    logger.info("criteria_verifier.started",
                criteria_count=len(task.acceptance_criteria) if task else 0,
                retry=retry_count)

    # Skip if no criteria defined
    if not task or not task.acceptance_criteria:
        logger.info("criteria_verifier.skipped", reason="no acceptance criteria")
        state.log_step("criteria_verifier", "skipped", detail="no criteria defined")
        return {
            "criteria_result": CriteriaResult(
                results=[], all_satisfied=True,
                unsatisfied_count=0, retry_needed=False,
            ),
            "step_logs": state.step_logs,
        }

    try:
        response = _get_llm().invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_build_verify_prompt(state)),
        ])
        raw     = response.content
        results = _parse_verification(raw)

        unsatisfied = [r for r in results if not r.get("satisfied", True)]
        all_ok      = len(unsatisfied) == 0
        retry_needed = (not all_ok) and (retry_count < MAX_RETRIES)

        logger.info(
            "criteria_verifier.completed",
            total=len(results),
            satisfied=len(results) - len(unsatisfied),
            unsatisfied=len(unsatisfied),
            retry_needed=retry_needed,
            retry_count=retry_count,
        )

        if unsatisfied:
            for u in unsatisfied:
                logger.warning(
                    "criteria_verifier.unmet_criterion",
                    criterion=u.get("criterion", "?")[:80],
                    reason=u.get("reason", "?")[:120],
                )

        state.log_step(
            "criteria_verifier",
            "completed" if all_ok else ("retrying" if retry_needed else "failed_criteria"),
            detail=(
                f"{len(results) - len(unsatisfied)}/{len(results)} criteria met"
                + (f" — retrying ({retry_count + 1}/{MAX_RETRIES})" if retry_needed else "")
            ),
        )

        return {
            "criteria_result": CriteriaResult(
                results=results,
                all_satisfied=all_ok,
                unsatisfied_count=len(unsatisfied),
                retry_needed=retry_needed,
            ),
            "criteria_retry_count": retry_count + (1 if retry_needed else 0),
            "step_logs": state.step_logs,
        }

    except Exception as exc:
        logger.error("criteria_verifier.error", error=str(exc),
                     hint="Verification failed — pipeline continues to test_runner")
        state.log_step("criteria_verifier", "skipped", detail=f"error: {exc}")
        return {
            "criteria_result": CriteriaResult(
                results=[], all_satisfied=True,
                unsatisfied_count=0, retry_needed=False,
            ),
            "step_logs": state.step_logs,
        }