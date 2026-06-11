"""
GitGeneer — Agent Node #6

Responsibility:
    Create a feature branch, stage all changes, commit, and push to the
    remote repository.

Design decisions:
    - GitPython only — no subprocess shell calls, zero command injection risk.
    - Branch name is derived from taskId and title, sanitised for git safety.
    - Token is injected into the remote URL temporarily for the push, then
      the remote URL is reset to the token-free version immediately after.
      This prevents the token from persisting in .git/config on disk.
    - Duplicate branch detection: if the branch already exists on the remote,
      the node fails with a clear error (don't silently overwrite).

LangGraph contract:
    Input  : AgentState  (reads workspace_path, parsed_task, code_change)
    Output : dict        (sets feature_branch, commit_sha, status, step_logs)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import git

from app.models.state import AgentState, PipelineStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


# ─────────────────────────────────────────────────────────────────────────────
# Branch name builder
# ─────────────────────────────────────────────────────────────────────────────

def _make_branch_name(task_id: str, title: str) -> str:
    """
    Build a git-safe branch name.
    Example: "TASK-123" + "Add email validation" → "ai-agent/TASK-123-add-email-validation"
    """
    safe_title = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower())
    safe_title = re.sub(r"-{2,}", "-", safe_title).strip("-")
    safe_title = safe_title[:50]   # keep branch names reasonable
    safe_task  = re.sub(r"[^a-zA-Z0-9\-]", "-", task_id)
    return f"ai-agent/{safe_task}-{safe_title}"


# ─────────────────────────────────────────────────────────────────────────────
# Remote URL helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inject_token(url: str, token: str) -> str:
    if not token:
        return url
    return url.replace("https://", f"https://{token}@", 1)


def _strip_token(url: str) -> str:
    """Remove embedded token from URL (for safe logging / storage)."""
    return re.sub(r"https://[^@]+@", "https://", url)


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node. Reads workspace + parsed_task + code_change, writes feature_branch + commit_sha."""
    state.log_step("git_agent", "started")
    logger.info("git_agent.started")

    if not state.workspace_path or not state.parsed_task or not state.code_change:
        msg = "git_agent: missing workspace_path, parsed_task, or code_change"
        logger.error("git_agent.missing_input")
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    workspace = Path(state.workspace_path)
    task      = state.parsed_task

    try:
        repo = git.Repo(str(workspace))
    except git.exc.InvalidGitRepositoryError:
        msg = f"git_agent: {workspace} is not a git repository"
        logger.error("git_agent.not_a_repo", path=str(workspace))
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    branch_name = _make_branch_name(task.task_id, task.title)
    logger.info("git_agent.branch_name", branch=branch_name)

    # ── Check for duplicate branch on remote ─────────────────────────────
    remote_refs = [ref.name for ref in repo.remotes[0].refs] if repo.remotes else []
    remote_branch = f"origin/{branch_name}"
    if remote_branch in remote_refs:
        msg = f"git_agent: branch '{branch_name}' already exists on remote — possible duplicate task"
        logger.error("git_agent.duplicate_branch", branch=branch_name)
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    # ── Create and checkout feature branch ───────────────────────────────
    try:
        feature_branch = repo.create_head(branch_name)
        feature_branch.checkout()
        logger.info("git_agent.branch_created", branch=branch_name)
    except git.exc.GitCommandError as exc:
        msg = f"git_agent: failed to create branch: {exc}"
        logger.error("git_agent.branch_create_failed", error=msg)
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    # ── Stage all changes ─────────────────────────────────────────────────
    repo.git.add(A=True)
    staged = repo.index.diff("HEAD")
    if not staged and not repo.untracked_files:
        msg = "git_agent: no changes to commit — code_writer may not have written any files"
        logger.warning("git_agent.nothing_to_commit")
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    # ── Commit ────────────────────────────────────────────────────────────
    commit_message = f"{task.task_id} {task.title}"
    commit = repo.index.commit(
        commit_message,
        author=git.Actor("AI Development Agent", "ai-agent@noreply.local"),
        committer=git.Actor("AI Development Agent", "ai-agent@noreply.local"),
    )
    logger.info("git_agent.committed", sha=commit.hexsha[:8], message=commit_message)

    # ── Push (token injected temporarily, cleaned up immediately) ─────────
    if not repo.remotes:
        msg = "git_agent: repository has no remotes — cannot push"
        logger.error("git_agent.no_remote")
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    origin = repo.remotes[0]
    original_url = origin.url
    push_url = _inject_token(original_url, GITHUB_TOKEN)

    try:
        origin.set_url(push_url)
        push_info = origin.push(refspec=f"{branch_name}:{branch_name}")
        # GitPython push_info flags: 0 = success, ERROR flag if failed
        for info in push_info:
            if info.flags & info.ERROR:
                raise git.exc.GitCommandError("push", info.summary)
        logger.info("git_agent.pushed", branch=branch_name)
    except git.exc.GitCommandError as exc:
        safe_err = str(exc).replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else str(exc)
        msg = f"git_agent: push failed: {safe_err}"
        logger.error("git_agent.push_failed", error=safe_err)
        state.log_step("git_agent", "failed", detail=safe_err)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}
    finally:
        # Always restore the clean URL — token must not persist in .git/config
        origin.set_url(original_url)

    state.log_step("git_agent", "completed",
                   detail=f"branch={branch_name} sha={commit.hexsha[:8]}")

    return {
        "feature_branch": branch_name,
        "commit_sha": commit.hexsha,
        "status": PipelineStatus.RUNNING,
        "step_logs": state.step_logs,
    }