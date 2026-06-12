"""
Task API — POST /api/tasks and GET /api/tasks/{traceId}/report
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.utils.logger import bind_trace, get_logger

router = APIRouter(tags=["tasks"])
logger = get_logger(__name__)

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "./reports"))
REPORTS_DIR.mkdir(exist_ok=True)

_reports: dict[str, dict[str, Any]] = {}
_running_tasks: set[str] = set()
_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    taskId: str
    title: str
    description: str

    @field_validator("taskId", "title", "description")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must not be empty")
        return v.strip()


class TaskAcceptedResponse(BaseModel):
    traceId: str
    taskId: str
    status: str = "accepted"
    message: str = "Task received. Pipeline started."


# ─────────────────────────────────────────────────────────────────────────────
# Report persistence
# ─────────────────────────────────────────────────────────────────────────────

async def _save_report(trace_id: str, report: dict[str, Any]) -> None:
    async with _lock:
        _reports[trace_id] = report
    try:
        path = REPORTS_DIR / f"{trace_id}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
    except Exception as exc:
        logger.warning("report.disk_write_failed", trace_id=trace_id, error=str(exc))


def _load_report(trace_id: str) -> dict[str, Any] | None:
    if trace_id in _reports:
        return _reports[trace_id]
    path = REPORTS_DIR / f"{trace_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _reports[trace_id] = data
            return data
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate branch pre-check (sync, runs before background task)
# ─────────────────────────────────────────────────────────────────────────────

def _would_create_duplicate_branch(task_id: str, title: str, description: str) -> str | None:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return None
    safe_title = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower())
    safe_title = re.sub(r"-{2,}", "-", safe_title).strip("-")[:50]
    safe_task  = re.sub(r"[^a-zA-Z0-9\-]", "-", task_id)
    branch     = f"ai-agent/{safe_task}-{safe_title}"
    url_match  = re.search(r"https?://github\.com/[\w\-]+/[\w\-\.]+", description)
    if not url_match:
        return None
    repo_url   = url_match.group(0).rstrip("/")
    slug_match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    if not slug_match:
        return None
    try:
        from github import Auth, Github
        gh   = Github(auth=Auth.Token(token))
        repo = gh.get_repo(slug_match.group(1))
        repo.get_branch(branch)
        return branch
    except Exception as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Background pipeline runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline_bg(trace_id: str, payload: dict[str, Any]) -> None:
    bind_trace(trace_id)
    task_id = payload.get("taskId", "unknown")
    logger.info("pipeline.background_started", task_id=task_id)
    try:
        from app.graph.pipeline import run_pipeline
        report = run_pipeline(raw_task=payload, trace_id=trace_id)
        await _save_report(trace_id, report)
        logger.info("pipeline.background_finished",
                    task_id=task_id, status=report.get("status"))
    except Exception as exc:
        logger.error("pipeline.background_error", error=str(exc))
        await _save_report(trace_id, {
            "traceId": trace_id,
            "taskId": task_id,
            "status": "failed",
            "error": str(exc),
        })
    finally:
        _running_tasks.discard(task_id)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskAcceptedResponse, status_code=202)
async def create_task(
    payload: TaskRequest,
    background_tasks: BackgroundTasks,
    dry_run: bool = False,
    require_approval: bool = False,
) -> TaskAcceptedResponse:
    """
    Accept a task and start the agent pipeline asynchronously.

    Query params:
      - **dry_run** (bool): Run everything except git push and PR creation.
      - **require_approval** (bool): Pause before push, wait for /approve.
    """
    trace_id = str(uuid.uuid4())
    bind_trace(trace_id)

    # Sanitize all user-controlled fields
    from app.security.sanitizer import sanitize_user_input
    safe_task_id     = sanitize_user_input(payload.taskId)
    safe_title       = sanitize_user_input(payload.title)
    safe_description = sanitize_user_input(payload.description)

    # Duplicate task check (concurrent)
    if safe_task_id in _running_tasks:
        logger.warning("task.already_running", task_id=safe_task_id)
        raise HTTPException(
            status_code=409,
            detail=f"Task '{safe_task_id}' is already running. "
                   f"Wait for it to finish or use a different taskId.",
        )

    # Duplicate branch check (already processed)
    duplicate_branch = _would_create_duplicate_branch(
        safe_task_id, safe_title, safe_description
    )
    if duplicate_branch:
        logger.warning("task.duplicate_rejected",
                       task_id=safe_task_id, branch=duplicate_branch)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_task",
                "message": (
                    f"Branch '{duplicate_branch}' already exists on the remote. "
                    f"This task was likely already processed. "
                    f"Please use a different taskId or modify the title."
                ),
                "branch": duplicate_branch,
            },
        )

    sanitized_payload = {
        "taskId":           safe_task_id,
        "title":            safe_title,
        "description":      safe_description,
        "dry_run":          dry_run,
        "require_approval": require_approval,
    }

    logger.info("task.received",
                task_id=safe_task_id,
                trace_id=trace_id,
                dry_run=dry_run,
                require_approval=require_approval)

    _running_tasks.add(safe_task_id)
    background_tasks.add_task(
        _run_pipeline_bg,
        trace_id=trace_id,
        payload=sanitized_payload,
    )
    return TaskAcceptedResponse(traceId=trace_id, taskId=safe_task_id)


@router.post("/tasks/{trace_id}/approve", tags=["tasks"])
async def approve_task(trace_id: str) -> JSONResponse:
    """Approve a paused pipeline (require_approval=true) to continue with push + PR."""
    report = _load_report(trace_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    if report.get("status") not in ("partial",):
        return JSONResponse(
            status_code=400,
            content={"error": "Pipeline is not awaiting approval",
                     "status": report.get("status")},
        )
    saved_payload = report.get("_raw_payload", {})
    if saved_payload:
        saved_payload["approved"] = True
        asyncio.create_task(_run_pipeline_bg(trace_id + "-approved", saved_payload))
    logger.info("task.approved", trace_id=trace_id)
    return JSONResponse(
        status_code=202,
        content={"status": "approved", "traceId": trace_id,
                 "message": "Pipeline approved — push and PR will proceed"},
    )


@router.get("/tasks/{trace_id}/report", tags=["tasks"])
async def get_report(trace_id: str) -> JSONResponse:
    """Poll for the execution report. Returns 202 while running, 200 when done."""
    report = _load_report(trace_id)
    if report is None:
        return JSONResponse(
            status_code=202,
            content={"traceId": trace_id, "status": "running",
                     "message": "Pipeline is still executing"},
        )
    return JSONResponse(status_code=200, content=report)