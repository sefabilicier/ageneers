"""
Audit Trail — append-only log of all pipeline actions.

Every significant event (task received, PR created, branch deleted, etc.)
is written to audit.log in NDJSON format (one JSON object per line).

Properties:
  - Append-only: lines are never deleted or modified
  - Machine-readable: NDJSON, easy to grep or ingest into SIEM
  - Lightweight: no external dependency, pure stdlib

Usage:
    from app.utils.audit import audit

    audit("task.received",   trace_id=trace_id, task_id=task_id, repo=repo_url)
    audit("pr.created",      trace_id=trace_id, pr_url=pr_url, branch=branch)
    audit("branch.deleted",  trace_id=trace_id, branch=branch, reason="rollback")
    audit("task.failed",     trace_id=trace_id, error=error, step=step)

File location: AUDIT_LOG_PATH env var (default: ./logs/audit.log)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", "./logs/audit.log"))
_lock = threading.Lock()


def _ensure_log_dir() -> None:
    _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def audit(event: str, **fields) -> None:
    """
    Write a single audit entry. Thread-safe, append-only.

    Args:
        event:   Event name, e.g. "task.received", "pr.created"
        **fields: Any additional context (trace_id, task_id, repo, error, ...)

    Example output (one line in audit.log):
        {"ts":"2026-06-12T17:00:00Z","event":"pr.created","trace_id":"abc","pr_url":"..."}
    """
    entry = {
        "ts":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **fields,
    }
    line = json.dumps(entry, default=str) + "\n"
    try:
        _ensure_log_dir()
        with _lock:
            with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # audit must never crash the main flow


def get_recent_audit_entries(limit: int = 100) -> list[dict]:
    """Read the last `limit` lines from the audit log (newest first)."""
    if not _AUDIT_LOG_PATH.exists():
        return []
    try:
        lines = _AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
        recent = lines[-limit:][::-1]  # newest first
        return [json.loads(line) for line in recent if line.strip()]
    except Exception:
        return []