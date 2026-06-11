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