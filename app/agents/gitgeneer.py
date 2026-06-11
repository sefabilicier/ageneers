"""
GitGeneer — Agent Node #6

Responsibility:
    Create a feature branch, stage all changes, commit, and push to the
    remote repository.

Design decisions:
    - GitPython only — no subprocess shell calls, zero command injection risk.
    - Branch name derived from taskId + title, sanitised for git safety.
    - Duplicate branch detection via GitHub API (reliable even with shallow clones).
      If branch already exists on remote → FAILED with a clear, actionable message
      telling the user to change their taskId or title to avoid the collision.
    - Token injected into remote URL only for the push, then immediately restored
      to the token-free version so it never persists in .git/config.

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
    safe_title = re.sub(r"[^a-zA-Z0-9\-]", "-", title.lower())
    safe_title = re.sub(r"-{2,}", "-", safe_title).strip("-")[:50]
    safe_task  = re.sub(r"[^a-zA-Z0-9\-]", "-", task_id)
    return f"ai-agent/{safe_task}-{safe_title}"


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_token(url: str) -> str:
    return re.sub(r"https://[^@]+@", "https://", url)


def _inject_token(url: str, token: str) -> str:
    if not token:
        return url
    clean = _strip_token(url)
    return clean.replace("https://", f"https://{token}@", 1)


# ─────────────────────────────────────────────────────────────────────────────
# Remote duplicate check (GitHub API — works with shallow clones)
# ─────────────────────────────────────────────────────────────────────────────

def _branch_exists_on_remote(repo_url: str, branch_name: str) -> bool:
    """
    Check if branch_name already exists on the GitHub remote.
    Uses PyGithub so it works even with shallow clones where
    repo.remotes[0].refs may not list all remote branches.
    Returns False if the check cannot be performed (no token, non-GitHub URL).
    """
    if not GITHUB_TOKEN:
        return False

    slug_match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    if not slug_match:
        return False

    try:
        from github import Auth, Github, GithubException
        gh   = Github(auth=Auth.Token(GITHUB_TOKEN))
        repo = gh.get_repo(slug_match.group(1))
        repo.get_branch(branch_name)   # raises GithubException(404) if not found
        return True
    except Exception:
        return False


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

    # ── Duplicate branch check (GitHub API, reliable for shallow clones) ──
    if _branch_exists_on_remote(task.repository_url, branch_name):
        msg = (
            f"Branch '{branch_name}' already exists on the remote repository. "
            f"A task with the same ID and title was likely already processed. "
            f"To re-run, use a different taskId or modify the title slightly."
        )
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

    # ── Push (token injected temporarily, cleaned up in finally) ──────────
    if not repo.remotes:
        msg = "git_agent: repository has no remotes — cannot push"
        logger.error("git_agent.no_remote")
        state.log_step("git_agent", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    origin       = repo.remotes[0]
    original_url = origin.url
    push_url     = _inject_token(original_url, GITHUB_TOKEN)

    try:
        origin.set_url(push_url)
        push_info = origin.push(refspec=f"{branch_name}:{branch_name}")
        for info in push_info:
            if info.flags & info.ERROR:
                # Detect the "rejected / fetch first" case specifically
                summary = str(info.summary).strip()
                if "fetch first" in summary or "rejected" in summary.lower():
                    raise git.exc.GitCommandError(
                        "push",
                        (
                            f"Branch '{branch_name}' was rejected by the remote. "
                            f"This usually means another concurrent request already "
                            f"pushed this branch. "
                            f"Please retry with a different taskId or title."
                        ),
                    )
                raise git.exc.GitCommandError("push", summary)
        logger.info("git_agent.pushed", branch=branch_name)

    except git.exc.GitCommandError as exc:
        safe_err = str(exc).replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else str(exc)
        logger.error("git_agent.push_failed", error=safe_err)
        state.log_step("git_agent", "failed", detail=safe_err)
        return {"status": PipelineStatus.FAILED, "error": safe_err, "step_logs": state.step_logs}

    finally:
        # Always restore clean URL — token must never persist in .git/config
        origin.set_url(original_url)

    state.log_step("git_agent", "completed",
                   detail=f"branch={branch_name} sha={commit.hexsha[:8]}")

    return {
        "feature_branch": branch_name,
        "commit_sha":     commit.hexsha,
        "status":         PipelineStatus.RUNNING,
        "step_logs":      state.step_logs,
    }