"""
Workspace Cleanup Scheduler

Automatically deletes workspace directories older than WORKSPACE_MAX_AGE_HOURS.
Runs as a background thread every WORKSPACE_CLEANUP_INTERVAL_HOURS hours.

Why:
    Each pipeline run creates a workspace directory (~50MB clone).
    Without cleanup, disk fills up after dozens of runs.

Config (.env):
    WORKSPACE_MAX_AGE_HOURS=24      # delete workspaces older than this
    WORKSPACE_CLEANUP_INTERVAL_HOURS=6  # how often to run cleanup
    WORKSPACE_BASE_DIR=./workspaces # where workspaces live
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

WORKSPACE_BASE_DIR        = Path(os.getenv("WORKSPACE_BASE_DIR", "./workspaces"))
WORKSPACE_MAX_AGE_HOURS   = float(os.getenv("WORKSPACE_MAX_AGE_HOURS", "24"))
CLEANUP_INTERVAL_HOURS    = float(os.getenv("WORKSPACE_CLEANUP_INTERVAL_HOURS", "6"))


def _cleanup_once() -> dict[str, int]:
    """
    Scan workspace directory and delete entries older than WORKSPACE_MAX_AGE_HOURS.
    Returns a summary dict: {deleted, kept, errors}.
    """
    if not WORKSPACE_BASE_DIR.exists():
        return {"deleted": 0, "kept": 0, "errors": 0}

    now        = datetime.now(timezone.utc).timestamp()
    max_age_s  = WORKSPACE_MAX_AGE_HOURS * 3600
    deleted = kept = errors = 0

    for entry in WORKSPACE_BASE_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            age_s = now - entry.stat().st_mtime
            if age_s > max_age_s:
                shutil.rmtree(entry)
                logger.info(
                    "workspace_cleanup.deleted",
                    workspace=entry.name,
                    age_hours=round(age_s / 3600, 1),
                )
                deleted += 1
            else:
                kept += 1
        except Exception as exc:
            logger.error(
                "workspace_cleanup.error",
                workspace=entry.name,
                error=str(exc),
                hint="Check file permissions on the workspace directory",
            )
            errors += 1

    logger.info(
        "workspace_cleanup.summary",
        deleted=deleted,
        kept=kept,
        errors=errors,
        max_age_hours=WORKSPACE_MAX_AGE_HOURS,
    )
    return {"deleted": deleted, "kept": kept, "errors": errors}


def _cleanup_loop() -> None:
    """Background thread: run cleanup every CLEANUP_INTERVAL_HOURS."""
    interval_s = CLEANUP_INTERVAL_HOURS * 3600
    logger.info(
        "workspace_cleanup.scheduler_started",
        interval_hours=CLEANUP_INTERVAL_HOURS,
        max_age_hours=WORKSPACE_MAX_AGE_HOURS,
        workspace_dir=str(WORKSPACE_BASE_DIR),
    )
    while True:
        time.sleep(interval_s)
        _cleanup_once()


def start_cleanup_scheduler() -> threading.Thread:
    """
    Start the workspace cleanup scheduler as a daemon thread.
    Call once at application startup.

    Returns the thread (for testing / inspection).
    """
    thread = threading.Thread(
        target=_cleanup_loop,
        name="workspace-cleanup",
        daemon=True,   # dies when main process exits
    )
    thread.start()
    return thread


def cleanup_now() -> dict[str, int]:
    """Run cleanup immediately (for admin use or testing)."""
    return _cleanup_once()