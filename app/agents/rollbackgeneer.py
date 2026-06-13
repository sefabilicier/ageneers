"""
Rollback Agent — Node that runs ONLY when pr_agent fails after a successful push.

Why this exists:
    If git_agent succeeds (branch created, committed, pushed) but pr_agent
    fails (e.g. GitHub API error, network issue), the remote is left with
    a dangling branch that has no PR. This is a "half-finished" state.

What it does:
    1. Deletes the remote branch via GitProvider.delete_branch()
    2. Records the rollback in the audit trail
    3. Adds a "rollback" section to the execution report

What it does NOT do:
    - Does not touch the local workspace (cleanup scheduler handles that)
    - Does not retry pr_agent — the pipeline already failed, this just cleans up
    - Never raises — if deletion fails, it's recorded but doesn't crash anything

Routing:
    pr_agent FAILED + git_agent SUCCESS (branch was pushed)
        → rollback_agent
            → report (status=failed, with rollback details)

    pr_agent FAILED but git_agent did NOT push (e.g. duplicate branch detected
    before push) → rollback_agent is skipped, nothing to clean up
"""

from __future__ import annotations

from typing import Any

from app.models.state import AgentState, RollbackResult
from app.providers.git_provider import get_git_provider
from app.utils.audit import audit
from app.utils.logger import get_logger

logger = get_logger(__name__)


def run(state: AgentState) -> dict[str, Any]:
    """
    LangGraph node — rollback the pushed branch if PR creation failed.

    Only does work if:
      - state.feature_branch is set (git_agent created a branch)
      - A push actually happened (we infer this from feature_branch being set
        AND status being FAILED at this point, meaning pr_agent failed after)
    """
    task   = state.parsed_task
    branch = state.feature_branch

    if not branch:
        logger.info("rollback_agent.skipped", reason="no branch was created")
        state.log_step("rollback_agent", "skipped", detail="no branch to roll back")
        return {
            "rollback_result": RollbackResult(
                performed=False, branch="", reason="no branch created", success=False,
            ),
            "step_logs": state.step_logs,
        }

    logger.info("rollback_agent.started", branch=branch)

    try:
        provider = get_git_provider()
        slug     = provider.get_repo_slug(task.repository_url) if task else ""
        deleted  = provider.delete_branch(slug, branch)

        reason = state.error or "pipeline failed after push"

        audit(
            "branch.deleted",
            task_id=task.task_id if task else None,
            branch=branch,
            repo=slug,
            reason=reason,
            success=deleted,
        )

        if deleted:
            logger.info("rollback_agent.completed", branch=branch, deleted=True)
            state.log_step("rollback_agent", "completed",
                           detail=f"deleted branch '{branch}' after pipeline failure")
        else:
            logger.warning(
                "rollback_agent.delete_failed",
                branch=branch,
                hint="Branch may not exist on remote, or GITHUB_TOKEN lacks delete permission. "
                     "Manual cleanup may be required.",
            )
            state.log_step("rollback_agent", "completed",
                           detail=f"could not delete branch '{branch}' — manual cleanup may be needed")

        return {
            "rollback_result": RollbackResult(
                performed=True, branch=branch, reason=reason, success=deleted,
            ),
            "step_logs": state.step_logs,
        }

    except Exception as exc:
        logger.error("rollback_agent.error", error=str(exc),
                     hint="Rollback failed — branch may remain on remote, manual cleanup required")
        state.log_step("rollback_agent", "completed", detail=f"rollback error: {exc}")
        return {
            "rollback_result": RollbackResult(
                performed=True, branch=branch, reason=str(exc), success=False,
            ),
            "step_logs": state.step_logs,
        }