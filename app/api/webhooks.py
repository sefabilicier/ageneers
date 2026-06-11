"""
GitHub Webhook — POST /api/webhooks/github

Allows the agent to be triggered directly from a GitHub Issue.

Setup (in your GitHub repo Settings → Webhooks):
    Payload URL : https://your-server/api/webhooks/github
    Content type: application/json
    Secret      : set GITHUB_WEBHOOK_SECRET in .env
    Events      : Issues

Trigger convention:
    An issue triggers the pipeline when it has the label  "ai-agent"
    AND the event is "opened" or "labeled".

    The issue body must follow this format (same as POST /api/tasks description):

        Repository: https://github.com/org/repo
        Branch: main

        Requirement:
        <what needs to be done>

        Acceptance Criteria:
        - criterion 1
        - criterion 2

    taskId  is derived from the issue number:  ISSUE-{number}
    title   is taken from the issue title.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.utils.logger import bind_trace, get_logger

router = APIRouter(tags=["webhooks"])
logger = get_logger(__name__)

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
AI_AGENT_LABEL        = os.getenv("AI_AGENT_LABEL", "ai-agent")   # label to watch for


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature_header: str) -> bool:
    """
    Verify the GitHub webhook HMAC-SHA256 signature.
    Returns True if valid or if GITHUB_WEBHOOK_SECRET is not configured.
    """
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("webhook.no_secret_configured",
                       detail="GITHUB_WEBHOOK_SECRET is not set — skipping signature verification")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    mac = hmac.new(key=GITHUB_WEBHOOK_SECRET.encode(), msg=body, digestmod=hashlib.sha256)
    expected = mac.hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature_header)


# ─────────────────────────────────────────────────────────────────────────────
# Issue payload parser
# ─────────────────────────────────────────────────────────────────────────────

def _should_trigger(event: str, payload: dict[str, Any]) -> bool:
    """Return True if this webhook event should trigger the pipeline."""
    if event not in ("issues",):
        return False

    action = payload.get("action", "")
    if action not in ("opened", "labeled"):
        return False

    issue  = payload.get("issue", {})
    labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
    return AI_AGENT_LABEL in labels


def _build_task_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a task payload from a GitHub Issue webhook payload."""
    issue  = payload.get("issue", {})
    number = issue.get("number", 0)
    title  = issue.get("title", f"Issue #{number}")
    body   = issue.get("body", "")

    return {
        "taskId":      f"ISSUE-{number}",
        "title":       title,
        "description": body or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Background runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline_bg(trace_id: str, task_payload: dict[str, Any]) -> None:
    try:
        from app.api.tasks import _run_pipeline_bg as _base_runner
        await _base_runner(trace_id, task_payload)
    except Exception as exc:
        logger.error("webhook.pipeline_error", trace_id=trace_id, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
) -> JSONResponse:
    """
    Receive GitHub webhook events and trigger the pipeline on labeled Issues.

    Required GitHub webhook settings:
      - Event type : Issues
      - Label      : `ai-agent` (configurable via AI_AGENT_LABEL env var)

    The issue body must contain a valid task description with Repository URL,
    Branch, Requirement, and Acceptance Criteria.
    """
    body = await request.body()

    # ── Signature verification ────────────────────────────────────────────
    if not _verify_signature(body, x_hub_signature_256):
        logger.warning("webhook.invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # ── Parse payload ─────────────────────────────────────────────────────
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.error("webhook.json_parse_error", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = x_github_event
    logger.info("webhook.received", github_event=event, action=payload.get("action"))

    # ── Check trigger conditions ──────────────────────────────────────────
    if not _should_trigger(event, payload):
        return JSONResponse(
            status_code=200,
            content={"status": "ignored", "reason": "Event does not match trigger conditions"},
        )

    task_payload = _build_task_payload(payload)
    trace_id     = str(uuid.uuid4())
    bind_trace(trace_id)

    logger.info("webhook.triggering_pipeline",
                task_id=task_payload["taskId"],
                trace_id=trace_id,
                issue_title=task_payload["title"])

    try:
        background_tasks.add_task(_run_pipeline_bg, trace_id, task_payload)
    except Exception as exc:
        logger.error("webhook.background_task_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    return JSONResponse(
        status_code=202,
        content={
            "status":  "accepted",
            "traceId": trace_id,
            "taskId":  task_payload["taskId"],
            "message": "Pipeline started from GitHub Issue",
        },
    )