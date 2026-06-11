"""
Task API — POST /api/tasks and GET /api/tasks/{taskId}/status
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.utils.logger import bind_trace, get_logger

router = APIRouter(tags=["tasks"])
logger = get_logger(__name__)

# In-memory store for execution reports (keyed by traceId)
# Production: replace with Redis or a database
_reports: dict[str, dict[str, Any]] = {}


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


async def _run_pipeline_bg(trace_id: str, payload: dict[str, Any]) -> None:
    """Background task — runs the full LangGraph pipeline."""
    bind_trace(trace_id)
    logger.info("pipeline.background_started", task_id=payload.get("taskId"))
    try:
        from app.graph.pipeline import run_pipeline
        report = run_pipeline(raw_task=payload, trace_id=trace_id)
        _reports[trace_id] = report
        logger.info("pipeline.background_finished",
                    task_id=payload.get("taskId"), status=report.get("status"))
    except Exception as exc:
        logger.error("pipeline.background_error", error=str(exc))
        _reports[trace_id] = {
            "traceId": trace_id,
            "taskId": payload.get("taskId"),
            "status": "failed",
            "error": str(exc),
        }


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
      "description": "Repository: https://github.com/...\\nBranch: develop\\n..."
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
    """
    if trace_id not in _reports:
        return JSONResponse(
            status_code=202,
            content={"traceId": trace_id, "status": "running",
                     "message": "Pipeline is still executing"},
        )
    return JSONResponse(status_code=200, content=_reports[trace_id])