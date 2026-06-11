"""
Structured logger for ai-dev-agent.

- In production (LOG_FORMAT=json): emits newline-delimited JSON → easy ingestion
  by log aggregators (Datadog, Loki, CloudWatch, etc.)
- In dev (LOG_FORMAT=console): rich, colourful human-readable output.

Every log entry automatically includes:
  - timestamp (UTC ISO-8601)
  - log level
  - trace_id  (bound per pipeline run)
  - step      (which agent/node is logging)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

_LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = os.getenv("LOG_FORMAT", "console").lower()   # json | console


def _add_severity(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """Rename structlog's 'level' key to 'severity' (GCP / Datadog convention)."""
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

    if _LOG_FORMAT == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

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


def get_logger(name: str = "ai-dev-agent") -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Bind extra context with `.bind(key=value)`."""
    return structlog.get_logger(name)


def bind_trace(trace_id: str) -> None:
    """Bind a trace_id to the current async context (per pipeline run)."""
    structlog.contextvars.bind_contextvars(trace_id=trace_id)


def clear_trace() -> None:
    structlog.contextvars.clear_contextvars()