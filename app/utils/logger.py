"""
Structured logger for ageneers.

Design principles:
  - Every log line answers: WHAT happened, WHERE (which agent), WHY it matters
  - Errors always include a 'hint' field — what the operator should do
  - Every agent node logs duration_ms so slow steps are immediately visible
  - trace_id threads through every line — one grep finds the full pipeline run
  - LOG_FORMAT=json  → newline-delimited JSON for log aggregators
  - LOG_FORMAT=console → coloured human-readable output for local dev

Automatic fields on every line:
  timestamp, severity, logger (module), trace_id, duration_ms (on *completed lines)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = os.getenv("LOG_FORMAT", "console").lower()


def _add_severity(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """Rename 'level' → 'severity' (GCP / Datadog convention)."""
    event_dict["severity"] = event_dict.pop("level", method)
    return event_dict


def configure_logging() -> None:
    """Call once at application startup."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_severity,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if _LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))


def get_logger(name: str = "ageneers") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_trace(trace_id: str) -> None:
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_trace() -> None:
    structlog.contextvars.clear_contextvars()


# ─────────────────────────────────────────────────────────────────────────────
# Agent timing helper
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def log_step(logger: Any, node: str, **start_ctx: Any):
    """
    Context manager that logs node start and completion with duration.

    Usage:
        with log_step(logger, "code_writer", repo="...", files=5):
            ... do work ...
            # Raise on error — the context manager logs the failure

    Emits:
        {node}.started   — at entry, with start_ctx
        {node}.completed — at exit, with duration_ms
        {node}.failed    — if an exception is raised, with error + hint
    """
    logger.info(f"{node}.started", **start_ctx)
    t0 = time.perf_counter()
    try:
        yield
        ms = int((time.perf_counter() - t0) * 1000)
        logger.info(f"{node}.completed", duration_ms=ms)
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.error(
            f"{node}.failed",
            duration_ms=ms,
            error=str(exc)[:200],
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline summary
# ─────────────────────────────────────────────────────────────────────────────

def log_pipeline_summary(
    logger: Any,
    task_id: str,
    status: str,
    steps: list[dict],
    pr_url: str | None = None,
    total_ms: int = 0,
) -> None:
    """
    Emit a single summary line after the pipeline finishes.
    Makes it easy to grep one line and understand the full outcome.

    Example output:
        pipeline.summary  status=success  task=TASK-123  steps=8/8
                          duration_ms=12400  pr=https://github.com/.../pull/42
    """
    passed = sum(1 for s in steps if (s.get("status") if isinstance(s, dict) else getattr(s, "status", "")) not in ("failed", "started"))
    total  = len({(s["step"] if isinstance(s, dict) else getattr(s, "step", "?")) for s in steps})

    logger.info(
        "pipeline.summary",
        task_id=task_id,
        status=status,
        steps_ok=f"{passed}/{total}",
        duration_ms=total_ms,
        pr_url=pr_url or "—",
    )