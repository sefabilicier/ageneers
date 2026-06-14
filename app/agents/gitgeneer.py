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

    # ── Duplicate branch check (via Git provider abstraction) ───────────
    from app.providers.git_provider import get_git_provider as _get_provider
    try:
        _provider = _get_provider()
        _slug     = _provider.get_repo_slug(task.repository_url)
        _exists   = _provider.branch_exists(_slug, branch_name).exists
    except Exception:
        _exists = False  # can't check → let push handle it

    if _exists:
        msg = (
            f"Branch '{branch_name}' already exists on the remote repository. "
            f"A task with the same ID and title was likely already processed. "
            f"To re-run, use a different taskId or modify the title slightly."
        )
        logger.error("git_agent.duplicate_branch", branch=branch_name, hint="Use a different taskId or title to generate a unique branch name")
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

    # Never commit Python/test build artifacts, even if the workspace's
    # .gitignore doesn't cover them (or test execution created them after
    # clone). Leaving these staged causes spurious merge conflicts in PYC
    # files and pollutes the diff with binary noise.
    #
    # Two cases to handle:
    #   1. Newly created/modified .pyc files -> `git reset HEAD --` unstages
    #      them (they remain untracked, never committed).
    #   2. Files that were ALREADY tracked in a previous commit (e.g. an
    #      earlier PR accidentally committed __pycache__/) -> `reset HEAD`
    #      is not enough, since the file stays tracked and shows as
    #      "modified" -> still gets committed. `git rm --cached` removes
    #      them from tracking entirely (the file stays on disk, just no
    #      longer part of the repo).
    _UNWANTED_PATTERNS = [
        "*.pyc", "*.pyo", "__pycache__", ".pytest_cache",
        "*.egg-info", ".coverage", ".mypy_cache", ".ruff_cache",
    ]
    for pattern in _UNWANTED_PATTERNS:
        # Unstage anything newly staged matching this pattern
        try:
            repo.git.execute(["git", "reset", "HEAD", "--", f"**/{pattern}", pattern])
        except git.exc.GitCommandError:
            pass  # nothing staged matching this pattern — fine

        # Remove from tracking anything that was already committed before
        try:
            repo.git.execute([
                "git", "rm", "--cached", "-r", "--ignore-unmatch",
                "-q", f"**/{pattern}", pattern,
            ])
        except git.exc.GitCommandError:
            pass  # nothing tracked matching this pattern — fine

    # If we just untracked files that were part of HEAD, that removal is
    # itself a staged change — which is exactly what we want: the PR will
    # clean these artifacts out of the repo as part of this commit.

    staged = repo.index.diff("HEAD")
    if not staged and not repo.untracked_files:
        msg = "git_agent: no changes to commit — code_writer may not have written any files"
        logger.warning("git_agent.nothing_to_commit", hint="code_writer may have written no files — check code_writer.completed log")
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

    # ── Generate diff preview (before push) ───────────────────────────────
    try:
        # Unified diff of everything committed vs the base branch
        diff_text = repo.git.diff("HEAD~1", "HEAD", unified=3)
        # Cap at 8000 chars for storage — full diff may be large
        if len(diff_text) > 8000:
            diff_text = diff_text[:8000] + "\n\n... [diff truncated] ..."
        logger.info("git_agent.diff_generated", lines=diff_text.count("\n"))
    except Exception as exc:
        logger.warning("git_agent.diff_failed", error=str(exc))
        diff_text = ""

    # ── Human approval gate ──────────────────────────────────────────────
    if state.require_approval and not state.approved:
        msg = (
            f"Pipeline paused — waiting for human approval before push. "
            f"Diff preview is ready. "
            f"Approve via: POST /api/tasks/{{trace_id}}/approve"
        )
        logger.info("git_agent.awaiting_approval", branch=branch_name)
        state.log_step("git_agent", "awaiting_approval",
                       detail=f"[APPROVAL] branch={branch_name} sha={commit.hexsha[:8]}")
        return {
            "feature_branch": branch_name,
            "commit_sha":     commit.hexsha,
            "diff_preview":   diff_text,
            "status":         PipelineStatus.PARTIAL,   # paused, not failed
            "error":          msg,
            "step_logs":      state.step_logs,
        }

    # ── Dry-run: skip push ───────────────────────────────────────────────
    if state.dry_run:
        logger.info("git_agent.dry_run_skip_push", branch=branch_name)
        state.log_step("git_agent", "completed",
                       detail=f"[DRY-RUN] branch={branch_name} sha={commit.hexsha[:8]} (not pushed)")
        return {
            "feature_branch": branch_name,
            "commit_sha":     commit.hexsha,
            "diff_preview":   diff_text,
            "status":         PipelineStatus.RUNNING,
            "step_logs":      state.step_logs,
        }

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
        logger.info("git_agent.pushed", branch=branch_name, remote=original_url.split("github.com/")[-1] if "github.com" in original_url else "remote")

    except git.exc.GitCommandError as exc:
        safe_err = str(exc).replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else str(exc)
        logger.error("git_agent.push_failed", error=safe_err, hint="Check GITHUB_TOKEN has repo write permission, or branch protection rules")
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
        "diff_preview":   diff_text,
        "status":         PipelineStatus.RUNNING,
        "step_logs":      state.step_logs,
    }