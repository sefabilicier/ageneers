"""
Unit tests for RepoManager.

Real git clone calls are mocked — no network required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.repomanager import _inject_token, _workspace_path, run
from app.models.state import AgentState, ParsedTask, PipelineStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helper builders
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(repo_url: str = "https://github.com/my-org/my-repo",
                branch: str = "develop") -> AgentState:
    return AgentState(
        raw_task={"taskId": "TASK-001"},
        parsed_task=ParsedTask(
            task_id="TASK-001",
            repository_url=repo_url,
            base_branch=branch,
            requirement="Add email validation",
            acceptance_criteria=[],
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _inject_token
# ─────────────────────────────────────────────────────────────────────────────

class TestInjectToken:
    def test_injects_token(self):
        url = "https://github.com/org/repo"
        result = _inject_token(url, "mytoken")
        assert result == "https://mytoken@github.com/org/repo"

    def test_no_token_returns_original(self):
        url = "https://github.com/org/repo"
        assert _inject_token(url, "") == url


# ─────────────────────────────────────────────────────────────────────────────
# _workspace_path
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkspacePath:
    def test_contains_task_id(self):
        path = _workspace_path("TASK-123")
        assert "TASK-123" in path

    def test_unique_per_call(self):
        assert _workspace_path("TASK-1") != _workspace_path("TASK-1")

    def test_sanitises_slashes(self):
        path = _workspace_path("ORG/TASK-1")
        folder_name = path.split("/")[-1]; assert "ORG-TASK-1" in folder_name


# ─────────────────────────────────────────────────────────────────────────────
# run() — success path
# ─────────────────────────────────────────────────────────────────────────────

class TestRunNode:
    @patch("app.agents.repomanager.git.Repo.clone_from")
    @patch("app.agents.repomanager.os.makedirs")
    @patch("app.agents.repomanager._clean_existing")
    @patch("app.agents.repomanager.REPO_ALLOWLIST", [])   # open allowlist
    def test_successful_clone(self, mock_clean, mock_makedirs, mock_clone):
        mock_repo = MagicMock()
        mock_repo.active_branch.name = "develop"
        mock_repo.head.commit.hexsha = "abc12345"
        mock_clone.return_value = mock_repo

        state = _make_state()
        result = run(state)

        assert result["status"] == PipelineStatus.RUNNING
        assert result["repo_cloned"] is True
        assert "workspace_path" in result

    @patch("app.agents.repomanager.REPO_ALLOWLIST", ["allowed-org"])
    def test_allowlist_blocks_unknown_org(self):
        state = _make_state(repo_url="https://github.com/evil-org/repo")
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED
        assert "allowed" in result["error"].lower()

    def test_missing_parsed_task_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    @patch("app.agents.repomanager.git.Repo.clone_from")
    @patch("app.agents.repomanager.os.makedirs")
    @patch("app.agents.repomanager._clean_existing")
    @patch("app.agents.repomanager.REPO_ALLOWLIST", [])
    def test_clone_exception_returns_failed(self, mock_clean, mock_makedirs, mock_clone):
        import git as _git
        mock_clone.side_effect = _git.exc.GitCommandError("clone", 128)
        state = _make_state()
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED