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

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.utils.logger import bind_trace, get_logger

router = APIRouter(tags=["tasks"])
logger = get_logger(__name__)

# ── Persistence config ────────────────────────────────────────────────────────
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "./reports"))
REPORTS_DIR.mkdir(exist_ok=True)

_reports: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


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
    logger.info("pipeline.background_started", task_id=payload.get("taskId"))
    try:
        from app.graph.pipeline import run_pipeline
        report = run_pipeline(raw_task=payload, trace_id=trace_id)
        await _save_report(trace_id, report)
        logger.info("pipeline.background_finished",
                    task_id=payload.get("taskId"), status=report.get("status"))
    except Exception as exc:
        logger.error("pipeline.background_error", error=str(exc))
        error_report = {
            "traceId": trace_id,
            "taskId": payload.get("taskId"),
            "status": "failed",
            "error": str(exc),
        }
        await _save_report(trace_id, error_report)


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
    logger.info("task.received", task_id=payload.taskId, trace_id=trace_id)

    background_tasks.add_task(
        _run_pipeline_bg,
        trace_id=trace_id,
        payload=payload.model_dump(),
    )
    return TaskAcceptedResponse(traceId=trace_id, taskId=payload.taskId)


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