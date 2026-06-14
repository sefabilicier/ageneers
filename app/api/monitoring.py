"""
Monitoring API — lightweight observability without external dependencies.

Endpoints:
    GET /api/metrics          → pipeline success rate, token usage, durations
    GET /api/tasks            → recent task list with status
    GET /api/tasks/{id}/timeline → per-step timing breakdown

All data is in-memory — resets on server restart.
For production, swap _store with Redis or PostgreSQL.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["monitoring"])

# ─────────────────────────────────────────────────────────────────────────────
# In-memory store (thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

_counters: dict[str, int | float] = {
    "tasks_total":        0,
    "tasks_success":      0,
    "tasks_failed":       0,
    "tasks_partial":      0,
    "llm_prompt_tokens":  0,
    "llm_completion_tokens": 0,
    "total_duration_ms":  0,
    "quality_score_total": 0,
}

_recent_tasks: deque = deque(maxlen=50)   # last 50 tasks


def record_pipeline_result(report: dict[str, Any]) -> None:
    """
    Called by the pipeline background task after each run.
    Updates all counters and appends to the recent task list.
    """
    with _lock:
        status = report.get("status", "failed")
        _counters["tasks_total"] += 1

        if status == "success":
            _counters["tasks_success"] += 1
        elif status == "partial":
            _counters["tasks_partial"] += 1
        else:
            _counters["tasks_failed"] += 1

        # Token usage
        cc = report.get("codeChange") or {}
        _counters["llm_prompt_tokens"]     += cc.get("promptTokens", 0)
        _counters["llm_completion_tokens"] += cc.get("completionTokens", 0)

        # Duration
        started  = report.get("startedAt", "")
        finished = report.get("finishedAt", "")
        if started and finished:
            try:
                s = datetime.fromisoformat(started.replace("Z", "+00:00"))
                f = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                _counters["total_duration_ms"] += int((f - s).total_seconds() * 1000)
            except Exception:
                pass

        # Quality score
        quality = report.get("qualityScore") or {}
        _counters["quality_score_total"] += quality.get("total", 0)

        # Task summary for the list endpoint
        pr = report.get("pullRequest") or {}
        _recent_tasks.appendleft({
            "traceId":    report.get("traceId"),
            "taskId":     report.get("taskId"),
            "status":     status,
            "startedAt":  started,
            "finishedAt": finished,
            "pr_url":     pr.get("url"),
            "error":      report.get("error"),
        })


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics() -> JSONResponse:
    """
    Aggregated pipeline metrics.

    Returns success rate, average duration, token usage totals.
    Resets on server restart (in-memory only).

    Example:
        curl http://localhost:8000/api/metrics
    """
    with _lock:
        total    = _counters["tasks_total"]
        success  = _counters["tasks_success"]
        failed   = _counters["tasks_failed"]
        partial  = _counters["tasks_partial"]
        dur_total = _counters["total_duration_ms"]

        success_rate = round(success / total * 100, 1) if total else 0.0
        avg_duration = round(dur_total / total) if total else 0

        return JSONResponse({
            "pipeline": {
                "tasks_total":    total,
                "tasks_success":  success,
                "tasks_failed":   failed,
                "tasks_partial":  partial,
                "success_rate_pct": success_rate,
                "avg_duration_ms":  avg_duration,
                "avg_quality_score": round(_counters["quality_score_total"] / total) if total else 0,
            },
            "llm": {
                "prompt_tokens_total":     _counters["llm_prompt_tokens"],
                "completion_tokens_total": _counters["llm_completion_tokens"],
                "total_tokens":            _counters["llm_prompt_tokens"] + _counters["llm_completion_tokens"],
                "avg_tokens_per_task":     round(
                    (_counters["llm_prompt_tokens"] + _counters["llm_completion_tokens"]) / total
                ) if total else 0,
            },
            "server": {
                "uptime_note": "counters reset on server restart",
                "recent_tasks_buffered": len(_recent_tasks),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        })


@router.get("/tasks")
async def list_tasks(limit: int = 20) -> JSONResponse:
    """
    List recent pipeline runs (newest first).

    Query params:
      limit (int, default 20, max 50): number of tasks to return

    Example:
        curl http://localhost:8000/api/tasks
        curl http://localhost:8000/api/tasks?limit=5
    """
    limit = min(limit, 50)
    with _lock:
        tasks = list(_recent_tasks)[:limit]

    return JSONResponse({"tasks": tasks, "total_buffered": len(_recent_tasks)})

@router.get("/tasks/{trace_id}/timeline")
async def get_timeline(trace_id: str) -> JSONResponse:
    """
    Step-by-step timing breakdown for a pipeline run.

    Shows each agent step with start time, duration, and status.
    Useful for identifying which step is slowest.

    Example:
        curl http://localhost:8000/api/tasks/{traceId}/timeline
    """
    from app.api.tasks import _load_report
    report = _load_report(trace_id)

    if report is None:
        return JSONResponse(
            status_code=202,
            content={"traceId": trace_id, "status": "running",
                     "message": "Pipeline still executing — timeline not yet available"},
        )

    steps = report.get("pipeline", {}).get("steps", [])

    # Calculate duration per step by pairing started/completed events
    timeline = []
    step_starts: dict[str, str] = {}

    for s in steps:
        step   = s.get("step", "?")
        status = s.get("status", "?")
        ts     = s.get("timestamp", "")

        if status == "started":
            step_starts[step] = ts
            continue

        start_ts = step_starts.get(step, "")
        duration_ms = None
        if start_ts and ts:
            try:
                t0 = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                duration_ms = int((t1 - t0).total_seconds() * 1000)
            except Exception:
                pass

        timeline.append({
            "step":        step,
            "status":      status,
            "started_at":  start_ts,
            "finished_at": ts,
            "duration_ms": duration_ms,
            "detail":      s.get("detail", ""),
        })

    # Overall duration
    started  = report.get("startedAt", "")
    finished = report.get("finishedAt", "")
    total_ms = None
    if started and finished:
        try:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            total_ms = int((t1 - t0).total_seconds() * 1000)
        except Exception:
            pass

    # Slowest step
    timed = [t for t in timeline if t["duration_ms"] is not None]
    slowest = max(timed, key=lambda x: x["duration_ms"]) if timed else None

    return JSONResponse({
        "traceId":      trace_id,
        "taskId":       report.get("taskId"),
        "status":       report.get("status"),
        "total_ms":     total_ms,
        "slowest_step": slowest["step"] if slowest else None,
        "slowest_ms":   slowest["duration_ms"] if slowest else None,
        "timeline":     timeline,
    })

@router.get("/metrics/prometheus", response_class=None)
async def get_prometheus_metrics():
    """
    Prometheus-compatible text format metrics.

    Scrape with Prometheus or view raw with curl:
        curl http://localhost:8000/api/metrics/prometheus

    Add to prometheus.yml:
        scrape_configs:
          - job_name: ageneers
            static_configs:
              - targets: [localhost:8000]
            metrics_path: /api/metrics/prometheus
    """
    from fastapi.responses import PlainTextResponse

    with _lock:
        total    = _counters["tasks_total"]
        success  = _counters["tasks_success"]
        failed   = _counters["tasks_failed"]
        partial  = _counters["tasks_partial"]
        prompt   = _counters["llm_prompt_tokens"]
        completion = _counters["llm_completion_tokens"]
        dur      = _counters["total_duration_ms"]

    lines = [
        "# HELP ai_dev_agent_tasks_total Total pipeline runs",
        "# TYPE ai_dev_agent_tasks_total counter",
        f"ai_dev_agent_tasks_total {total}",
        "",
        "# HELP ai_dev_agent_tasks_success_total Successful pipeline runs",
        "# TYPE ai_dev_agent_tasks_success_total counter",
        f"ai_dev_agent_tasks_success_total {success}",
        "",
        "# HELP ai_dev_agent_tasks_failed_total Failed pipeline runs",
        "# TYPE ai_dev_agent_tasks_failed_total counter",
        f"ai_dev_agent_tasks_failed_total {failed}",
        "",
        "# HELP ai_dev_agent_tasks_partial_total Partial pipeline runs (tests failed but PR opened)",
        "# TYPE ai_dev_agent_tasks_partial_total counter",
        f"ai_dev_agent_tasks_partial_total {partial}",
        "",
        "# HELP ai_dev_agent_llm_prompt_tokens_total Total LLM prompt tokens consumed",
        "# TYPE ai_dev_agent_llm_prompt_tokens_total counter",
        f"ai_dev_agent_llm_prompt_tokens_total {prompt}",
        "",
        "# HELP ai_dev_agent_llm_completion_tokens_total Total LLM completion tokens consumed",
        "# TYPE ai_dev_agent_llm_completion_tokens_total counter",
        f"ai_dev_agent_llm_completion_tokens_total {completion}",
        "",
        "# HELP ai_dev_agent_pipeline_duration_ms_total Total pipeline duration in ms",
        "# TYPE ai_dev_agent_pipeline_duration_ms_total counter",
        f"ai_dev_agent_pipeline_duration_ms_total {dur}",
        "",
        "# HELP ai_dev_agent_success_rate Pipeline success rate (0-1)",
        "# TYPE ai_dev_agent_success_rate gauge",
        f"ai_dev_agent_success_rate {round(success / total, 4) if total else 0}",
    ]

    return PlainTextResponse(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )

@router.post("/admin/cleanup")
async def trigger_cleanup() -> JSONResponse:
    """
    Manually trigger workspace cleanup (admin use).

    Deletes workspaces older than WORKSPACE_MAX_AGE_HOURS immediately.

    Example:
        curl -X POST http://localhost:8000/api/admin/cleanup
    """
    from app.utils.workspace_cleanup import cleanup_now
    result = cleanup_now()
    return JSONResponse({"status": "completed", **result})

@router.get("/audit")
async def get_audit_log(limit: int = 50) -> JSONResponse:
    """
    Recent audit trail entries (newest first).

    Shows all significant pipeline events: tasks received, PRs created,
    failures, etc. Written to disk in append-only NDJSON format.

    Example:
        curl http://localhost:8000/api/audit
        curl http://localhost:8000/api/audit?limit=10
    """
    from app.utils.audit import get_recent_audit_entries
    entries = get_recent_audit_entries(limit=min(limit, 500))
    return JSONResponse({"entries": entries, "count": len(entries)})

@router.get("/prompts")
async def get_prompt_versions() -> JSONResponse:
    """
    Show active prompt versions for all agents.

    Useful for debugging — tells you which system prompt version
    is currently loaded for each agent.

    To change a version: set PROMPT_VERSION_code_writer=2 in .env

    Example:
        curl http://localhost:8000/api/prompts
    """
    from app.prompts import list_versions, get_active_version
    import os

    agents = ["code_writer", "task_parser"]
    result = {}
    for agent in agents:
        versions = list_versions(agent)
        active   = get_active_version(agent)
        env_key  = f"PROMPT_VERSION_{agent}"
        result[agent] = {
            "active_version":    active,
            "available_versions": versions,
            "env_override":       os.getenv(env_key),
        }

    return JSONResponse({"prompts": result})