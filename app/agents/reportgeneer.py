"""
ReportGeneer — Agent Node #8 (Final)

Responsibility:
    Produce a structured JSON execution report summarising the entire pipeline run.
    This is the last node in the graph — it always runs, even on failure.

LangGraph contract:
    Input  : AgentState  (reads everything)
    Output : dict        (sets finished_at, status — state is already final)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.models.state import AgentState, PipelineStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)



def _compute_quality_score(state: AgentState) -> dict:
    """
    Compute a simple quality score for the pipeline run.

    Score breakdown (0-100):
      30 pts  — code was generated and written (files > 0)
      30 pts  — tests passed (returncode == 0)
      20 pts  — PR was created successfully
      10 pts  — diff was generated (actual code changed)
      10 pts  — no retries needed (first attempt succeeded)

    Returns dict with total score and breakdown.
    """
    score = 0
    breakdown = {}

    # Code generation
    change = state.code_change
    files_written = len(change.changed_files) if change and change.changed_files else 0
    if files_written > 0:
        score += 30
        breakdown["code_generated"] = {"points": 30, "detail": f"{files_written} files"}
    else:
        breakdown["code_generated"] = {"points": 0, "detail": "no files written"}

    # Tests
    test_result = state.test_result
    if test_result and test_result.status == "passed":
        score += 30
        breakdown["tests_passed"] = {"points": 30, "detail": "all tests green"}
    elif test_result:
        status_str = test_result.status.value if hasattr(test_result.status, "value") else str(test_result.status)
        breakdown["tests_passed"] = {"points": 0, "detail": f"tests {status_str}"}
    else:
        breakdown["tests_passed"] = {"points": 0, "detail": "not run"}

    # PR created
    if state.pull_request and state.pull_request.url:
        score += 20
        breakdown["pr_created"] = {"points": 20, "detail": state.pull_request.url}
    else:
        breakdown["pr_created"] = {"points": 0, "detail": "no PR"}

    # Diff generated
    if state.diff_preview:
        score += 10
        breakdown["diff_generated"] = {"points": 10, "detail": "diff available"}
    else:
        breakdown["diff_generated"] = {"points": 0, "detail": "no diff"}

    # No retries
    retry_count = test_result.retry_count if test_result else 0
    if retry_count == 0 and files_written > 0:
        score += 10
        breakdown["no_retries"] = {"points": 10, "detail": "succeeded on first attempt"}
    else:
        breakdown["no_retries"] = {"points": 0, "detail": f"{retry_count} retries"}

    # Code review bonus/penalty
    review = state.code_review
    if review:
        critical_count = sum(1 for i in review.issues if i.severity == "critical")
        warning_count  = sum(1 for i in review.issues if i.severity == "warning")
        if critical_count == 0 and warning_count == 0:
            score = min(score + 5, 100)
            breakdown["review_clean"] = {"points": 5, "detail": "no review issues"}
        else:
            penalty = critical_count * 10 + warning_count * 3
            score = max(score - penalty, 0)
            breakdown["review_issues"] = {
                "points": -penalty,
                "detail": f"{critical_count} critical, {warning_count} warnings",
            }

    grade = "A" if score >= 90 else "B" if score >= 70 else "C" if score >= 50 else "F"

    return {
        "total": score,
        "grade": grade,
        "breakdown": breakdown,
    }


def build_report(state: AgentState) -> dict[str, Any]:
    """Build a serialisable execution report dict from final AgentState."""
    task = state.parsed_task
    analysis = state.repo_analysis
    change = state.code_change
    test = state.test_result
    pr = state.pull_request

    return {
        "traceId": None,          # injected by the API layer
        "taskId": task.task_id if task else "unknown",
        "status": state.status.value,
        "startedAt": state.started_at,
        "finishedAt": state.finished_at,
        "error": state.error or None,
        "pipeline": {
            "steps": [
                {"step": log.step, "status": log.status,
                 "timestamp": log.timestamp, "detail": log.detail}
                for log in state.step_logs
            ],
        },
        "repository": {
            "url": task.repository_url if task else None,
            "baseBranch": task.base_branch if task else None,
            "featureBranch": state.feature_branch or None,
            "commitSha": state.commit_sha[:8] if state.commit_sha else None,
            "diffPreview": state.diff_preview or None,
        },
        "analysis": {
            "language": analysis.language if analysis else None,
            "framework": analysis.framework if analysis else None,
            "buildTool": analysis.build_tool if analysis else None,
            "testCommand": analysis.test_command if analysis else None,
            "relevantFiles": analysis.relevant_files if analysis else [],
        } if analysis else None,
        "codeChange": {
            "changedFiles": change.changed_files,
            "modelUsed": change.model_used,
            "promptTokens": change.prompt_tokens,
            "completionTokens": change.completion_tokens,
        } if change else None,
        "testResult": {
            "status": test.status.value,
            "command": test.command,
            "durationSeconds": test.duration_seconds,
            "retryCount": test.retry_count,
        } if test else None,
        "pullRequest": {
            "number": pr.number,
            "url": pr.url,
            "title": pr.title,
            "branch": pr.branch,
        } if pr else None,
        "tokenUsage": {
            agent: usage
            for agent, usage in (state.token_usage or {}).items()
        },
        "qualityScore": _compute_quality_score(state),
        "codeReview": {
            "passed":  state.code_review.passed,
            "summary": state.code_review.summary,
            "issues":  [
                {"category": i.category, "severity": i.severity,
                 "file": i.file, "description": i.description}
                for i in state.code_review.issues
            ],
        } if state.code_review else None,
        "criteriaVerification": {
            "allSatisfied":     state.criteria_result.all_satisfied,
            "unsatisfiedCount": state.criteria_result.unsatisfied_count,
            "retryCount":       state.criteria_retry_count,
            "results":          state.criteria_result.results,
        } if state.criteria_result else None,
        "rollback": {
            "performed": state.rollback_result.performed,
            "branch":    state.rollback_result.branch,
            "reason":    state.rollback_result.reason,
            "success":   state.rollback_result.success,
        } if state.rollback_result else None,
    }


def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node — always the last to execute."""
    finished_at = datetime.now(timezone.utc).isoformat()
    state.finished_at = finished_at
    state.log_step("report", "completed", detail=f"status={state.status.value}")

    report = build_report(state)
    logger.info("report.generated", status=state.status.value,
                task_id=report["taskId"], pr_url=report.get("pullRequest", {}) and
                report["pullRequest"].get("url") if report.get("pullRequest") else None)

    return {
        "finished_at": finished_at,
        "step_logs": state.step_logs,
    }