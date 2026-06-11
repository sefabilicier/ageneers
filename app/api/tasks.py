"""
Task API — POST /api/tasks and GET /api/tasks/{traceId}/report

State persistence:
  - Primary:  in-memory dict (fast, lost on restart)
  - Fallback: JSON files in REPORTS_DIR (survives restart)

This is a dev-grade solution. Production should use Redis or PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.utils.logger import bind_trace, get_logger

# ── Duplicate branch pre-check (sync, runs before background task) ────────────

def _would_create_duplicate_branch(task_id: str, title: str, description: str) -> str | None:
    """
    Check if the branch that would be created for this task already exists.
    Returns the branch name if it's a duplicate, None if safe to proceed.
    Runs synchronously so the user gets an immediate 409 response.
    """
    import os, re
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return None

    # Mirror gitgeneer._make_branch_name
    safe_title = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower())
    safe_title = re.sub(r"-{2,}", "-", safe_title).strip("-")[:50]
    safe_task  = re.sub(r"[^a-zA-Z0-9\-]", "-", task_id)
    branch     = f"ai-agent/{safe_task}-{safe_title}"

    # Extract repo URL from description
    url_match = re.search(r"https?://github\.com/[\w\-]+/[\w\-\.]+", description)
    if not url_match:
        return None
    repo_url = url_match.group(0).rstrip("/")

    slug_match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    if not slug_match:
        return None

    try:
        from github import Auth, Github
        gh   = Github(auth=Auth.Token(token))
        repo = gh.get_repo(slug_match.group(1))
        repo.get_branch(branch)
        return branch   # branch exists → duplicate
    except Exception as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None  # branch doesn't exist → safe
        return None      # can't check → let pipeline handle it

router = APIRouter(tags=["tasks"])
logger = get_logger(__name__)

# ── Persistence config ────────────────────────────────────────────────────────
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "./reports"))
REPORTS_DIR.mkdir(exist_ok=True)

_reports: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()
_running_tasks: set[str] = set()   # taskIds currently being processed


async def _save_report(trace_id: str, report: dict[str, Any]) -> None:
    """Store report in memory AND on disk (survives server restart)."""
    async with _lock:
        _reports[trace_id] = report
    try:
        path = REPORTS_DIR / f"{trace_id}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
    except Exception as exc:
        logger.warning("report.disk_write_failed", trace_id=trace_id, error=str(exc))


def _load_report(trace_id: str) -> dict[str, Any] | None:
    """Check memory first, then disk."""
    if trace_id in _reports:
        return _reports[trace_id]
    path = REPORTS_DIR / f"{trace_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _reports[trace_id] = data   # warm the cache
            return data
        except Exception:
            pass
    return None


# ── Schemas ───────────────────────────────────────────────────────────────────

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


# ── Background runner ─────────────────────────────────────────────────────────

async def _run_pipeline_bg(trace_id: str, payload: dict[str, Any]) -> None:
    bind_trace(trace_id)
    task_id = payload.get("taskId", "")
    logger.info("pipeline.background_started", task_id=task_id)
    try:
        from app.graph.pipeline import run_pipeline
        report = run_pipeline(raw_task=payload, trace_id=trace_id)
        await _save_report(trace_id, report)
        logger.info("pipeline.background_finished",
                    task_id=task_id, status=report.get("status"))
    except Exception as exc:
        logger.error("pipeline.background_error", error=str(exc))
        error_report = {
            "traceId": trace_id,
            "taskId": task_id,
            "status": "failed",
            "error": str(exc),
        }
        await _save_report(trace_id, error_report)
    finally:
        # Always release the lock so the taskId can be re-submitted later
        _running_tasks.discard(task_id)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskAcceptedResponse, status_code=202)
async def create_task(
    payload: TaskRequest,
    background_tasks: BackgroundTasks,
) -> TaskAcceptedResponse:
    """
    Accept a task and start the agent pipeline asynchronously.

    Example:
    ```json
    {
      "taskId": "TASK-123",
      "title": "Add email validation to user registration API",
      "description": "Repository: https://github.com/...\\nBranch: main\\n..."
    }
    ```
    """
    trace_id = str(uuid.uuid4())
    bind_trace(trace_id)

    # Reject duplicate concurrent runs for the same taskId
    if payload.taskId in _running_tasks:
        logger.warning("task.duplicate_rejected", task_id=payload.taskId)
        from fastapi import HTTPException
        raise HTTPException(
            status_code=409,
            detail=f"Task '{payload.taskId}' is already running. "
                   f"Wait for it to finish or use a different taskId.",
        )

    logger.info("task.received", task_id=payload.taskId, trace_id=trace_id, dry_run=dry_run, require_approval=require_approval)
    _running_tasks.add(payload.taskId)

    background_tasks.add_task(
        _run_pipeline_bg,
        trace_id=trace_id,
        payload=sanitized_payload,
    )
    return TaskAcceptedResponse(traceId=trace_id, taskId=payload.taskId)


# Approval state store (traceId → approved bool)
_approvals: dict[str, bool] = {}


@router.post("/tasks/{trace_id}/approve", tags=["tasks"])
async def approve_task(trace_id: str) -> JSONResponse:
    """
    Approve a paused pipeline (require_approval=true) to continue with push + PR.

    When a task is submitted with ?require_approval=true, the pipeline pauses
    after generating the diff preview but before git push.
    Call this endpoint to resume.
    """
    report = _load_report(trace_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Pipeline not found or still starting")

    if report.get("status") not in ("partial",):
        return JSONResponse(
            status_code=400,
            content={"error": "Pipeline is not awaiting approval",
                     "status": report.get("status")},
        )

    _approvals[trace_id] = True
    logger.info("task.approved", trace_id=trace_id)

    # Re-run pipeline from the saved state with approved=True
    saved_payload = report.get("_raw_payload", {})
    if saved_payload:
        saved_payload["approved"] = True
        saved_payload["require_approval"] = True
        background_tasks_store = BackgroundTasks()
        # We can't use background_tasks here directly, so run in thread
        import asyncio
        asyncio.create_task(_run_pipeline_bg(trace_id + "-approved", saved_payload))

    return JSONResponse(
        status_code=202,
        content={"status": "approved", "traceId": trace_id,
                 "message": "Pipeline approved — push and PR creation will proceed"},
    )


@router.get("/tasks/{trace_id}/report", tags=["tasks"])
async def get_report(trace_id: str) -> JSONResponse:
    """
    Poll for the execution report of a pipeline run.
    Returns 202 while still running, 200 when complete.
    Survives server restarts — report is read from disk if not in memory.
    """
    report = _load_report(trace_id)
    if report is None:
        return JSONResponse(
            status_code=202,
            content={"traceId": trace_id, "status": "running",
                     "message": "Pipeline is still executing"},
        )
    return JSONResponse(status_code=200, content=report)