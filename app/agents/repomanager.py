"""
repomanager — Agent Node #2

Responsibility:
    Clone the target repository into an isolated workspace directory,
    checkout the base branch, and prepare the environment for analysis.

Design decisions:
    - Each task gets its own workspace: workspaces/<taskId>-<short_uuid>/
      This prevents cross-task contamination and makes cleanup trivial.
    - Allowlist check happens before any network call — fail fast.
    - If the same taskId is re-submitted, the existing workspace is removed
      and re-cloned (idempotent behaviour, documented in README).
    - Private repo support: GITHUB_TOKEN injected into clone URL, never logged.
    - GitPython is used for all git operations — no subprocess shell calls,
      which eliminates command-injection risk entirely.

LangGraph contract:
    Input  : AgentState  (reads  parsed_task)
    Output : dict        (sets   workspace_path, repo_cloned, status, step_logs)
"""

from __future__ import annotations

import os
import time
import shutil
import uuid
from pathlib import Path
from typing import Any

import git

from app.models.state import AgentState, PipelineStatus
from app.security.sanitizer import is_safe_repo_url
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config (from environment)
# ─────────────────────────────────────────────────────────────────────────────

WORKSPACE_BASE = os.getenv("WORKSPACE_BASE_DIR", "./workspaces")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
REPO_ALLOWLIST = [
    s.strip() for s in os.getenv("REPO_ALLOWLIST", "").split(",") if s.strip()
]
REPO_DENYLIST  = [
    s.strip() for s in os.getenv("REPO_DENYLIST", "").split(",") if s.strip()
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inject_token(url: str, token: str) -> str:
    """
    Inject a GitHub token into the clone URL for private repo access.
    https://github.com/org/repo  →  https://<token>@github.com/org/repo
    Token is NEVER written to logs — callers must not log the return value.
    """
    if not token:
        return url
    return url.replace("https://", f"https://{token}@", 1)


def _workspace_path(task_id: str) -> str:
    short = uuid.uuid4().hex[:8]
    safe_task_id = task_id.replace("/", "-").replace("\\", "-")
    return os.path.abspath(os.path.join(WORKSPACE_BASE, f"{safe_task_id}-{short}"))


def _clean_existing(path: str) -> None:
    """Remove workspace if it already exists (idempotent re-run)."""
    if os.path.exists(path):
        logger.warning("repo_manager.workspace_exists_cleaning", path=path)
        shutil.rmtree(path, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node. Reads parsed_task, writes workspace_path + repo_cloned."""
    state.log_step("repo_manager", "started")

    if not state.parsed_task:
        msg = "repo_manager: parsed_task is missing — task_parser may have failed"
        logger.error("repo_manager.missing_parsed_task")
        state.log_step("repo_manager", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    task    = state.parsed_task
    repo_url = task.repository_url
    branch   = task.base_branch
    task_id  = task.task_id

    logger.info("repo_manager.started", repo=repo_url, branch=branch, task_id=task_id)

    # ── Early duplicate branch check — before any clone/compute work ──────
    # Branch name mirrors gitgeneer._make_branch_name logic
    import re as _re
    _safe_title = _re.sub(r"[^a-zA-Z0-9\-]", "-", task.title.lower())
    _safe_title = _re.sub(r"-{2,}", "-", _safe_title).strip("-")[:50]
    _safe_task  = _re.sub(r"[^a-zA-Z0-9\-]", "-", task_id)
    _branch     = f"ai-agent/{_safe_task}-{_safe_title}"

    if GITHUB_TOKEN:
        _slug_match = _re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
        if _slug_match:
            try:
                from github import Auth, Github
                _gh   = Github(auth=Auth.Token(GITHUB_TOKEN))
                _repo = _gh.get_repo(_slug_match.group(1))
                _repo.get_branch(_branch)
                # Branch exists — fail fast with a clear message
                msg = (
                    f"Branch '{_branch}' already exists on the remote. "
                    f"This task was likely already processed. "
                    f"Please use a different taskId or modify the title."
                )
                logger.error("repo_manager.duplicate_branch_early", branch=_branch)
                state.log_step("repo_manager", "failed", detail=msg)
                return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}
            except Exception as _e:
                if "404" not in str(_e) and "Not Found" not in str(_e):
                    logger.warning("repo_manager.duplicate_check_skipped", reason=str(_e)[:80])
                # 404 = branch not found = safe to proceed

    # ── Security: allowlist / denylist check ─────────────────────────────
    safe, reason = is_safe_repo_url(repo_url, REPO_ALLOWLIST, REPO_DENYLIST)
    if not safe:
        logger.error("repo_manager.url_blocked", repo=repo_url, reason=reason, hint="Add repo owner to REPO_ALLOWLIST or remove from REPO_DENYLIST in .env")
        state.log_step("repo_manager", "failed", detail=reason)
        return {"status": PipelineStatus.FAILED, "error": reason, "step_logs": state.step_logs}

    # ── Prepare workspace ─────────────────────────────────────────────────
    os.makedirs(WORKSPACE_BASE, exist_ok=True)
    workspace = _workspace_path(task_id)
    _clean_existing(workspace)

    logger.info("repo_manager.cloning", workspace=workspace)
    state.log_step("repo_manager", "cloning", detail=f"workspace={workspace}")

    # ── Clone ─────────────────────────────────────────────────────────────
    try:
        clone_url = _inject_token(repo_url, GITHUB_TOKEN)
        # depth=1: shallow clone — faster, less data, less exposure
        repo = git.Repo.clone_from(
            clone_url,
            workspace,
            branch=branch,
            depth=1,
        )
    except git.exc.GitCommandError as exc:
        # Scrub any token from the error message before logging
        safe_msg = str(exc).replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else str(exc)
        logger.error("repo_manager.clone_failed", error=safe_msg, hint="Check GITHUB_TOKEN permissions and repository URL")
        state.log_step("repo_manager", "failed", detail=safe_msg)
        return {"status": PipelineStatus.FAILED, "error": safe_msg, "step_logs": state.step_logs}

    # ── Verify branch ─────────────────────────────────────────────────────
    current_branch = repo.active_branch.name
    if current_branch != branch:
        msg = f"Expected branch '{branch}' but got '{current_branch}'"
        logger.error("repo_manager.wrong_branch", expected=branch, got=current_branch)
        state.log_step("repo_manager", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    # ── Ensure Python build artifacts are gitignored ────────────────────────
    # If the repo's .gitignore doesn't already cover these, append them.
    # Without this, running tests (host or Docker sandbox) creates
    # __pycache__/*.pyc files that git_agent would otherwise stage and
    # commit, causing spurious binary-file merge conflicts in the PR.
    try:
        _BUILD_ARTIFACT_PATTERNS = ["__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/"]
        gitignore_path = Path(workspace) / ".gitignore"
        existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
        missing = [p for p in _BUILD_ARTIFACT_PATTERNS if p not in existing]
        if missing:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n# Added by ai-dev-agent — never commit Python build artifacts\n")
                f.write("\n".join(missing) + "\n")
            logger.info("repo_manager.gitignore_patched", added=missing)
    except OSError as exc:
        # Non-fatal — gitgeneer also strips these patterns at commit time
        logger.warning("repo_manager.gitignore_patch_skipped", error=str(exc))

    logger.info(
        "repo_manager.completed",
        workspace=workspace,
        branch=current_branch,
        commit=repo.head.commit.hexsha[:8],
    )
    state.log_step(
        "repo_manager", "completed",
        detail=f"workspace={workspace} branch={current_branch}",
    )

    return {
        "workspace_path": workspace,
        "repo_cloned": True,
        "status": PipelineStatus.RUNNING,
        "step_logs": state.step_logs,
    }