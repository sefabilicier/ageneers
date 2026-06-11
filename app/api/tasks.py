"""
Task API — receives task payloads and kicks off the agent pipeline.

Endpoint:
    POST /api/tasks

The actual pipeline execution will be wired in once all agents are built.
For now the endpoint validates input and returns a 202 Accepted.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.utils.logger import bind_trace, get_logger

router = APIRouter(tags=["tasks"])
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
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
# Background pipeline runner (placeholder — will call LangGraph pipeline)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline(trace_id: str, payload: dict[str, Any]) -> None:
    """Placeholder: will be replaced by the full LangGraph pipeline call."""
    bind_trace(trace_id)
    logger.info("pipeline.started", task_id=payload.get("taskId"))
    # TODO: import and invoke run_pipeline(payload) from app.graph.pipeline
    logger.info("pipeline.placeholder", note="pipeline not wired yet")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/tasks", response_model=TaskAcceptedResponse, status_code=202)
async def create_task(
    payload: TaskRequest,
    background_tasks: BackgroundTasks,
) -> TaskAcceptedResponse:
    """
    Accepts a task and starts the agent pipeline asynchronously.

    Example body:
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

    logger.info(
        "task.received",
        task_id=payload.taskId,
        title=payload.title,
        trace_id=trace_id,
    )

    background_tasks.add_task(
        _run_pipeline,
        trace_id=trace_id,
        payload=payload.model_dump(),
    )

    return TaskAcceptedResponse(traceId=trace_id, taskId=payload.taskId)