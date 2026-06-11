"""
Unit tests for GitGeneer.

Git operations are performed on a real temporary git repo (no network).
Push is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from app.agents.gitgeneer import _inject_token, _make_branch_name, _strip_token, run
from app.models.state import AgentState, CodeChange, ParsedTask, PipelineStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _init_repo(path: Path) -> git.Repo:
    """Create a minimal git repo with one commit so we have a HEAD."""
    repo = git.Repo.init(str(path))
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "test@test.com").release()
    (path / "README.md").write_text("# test\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    return repo


def _make_state(workspace: Path) -> AgentState:
    (workspace / "app").mkdir(exist_ok=True)
    (workspace / "app" / "users.py").write_text("def register(email): pass\n")
    return AgentState(
        raw_task={"taskId": "TASK-123"},
        workspace_path=str(workspace),
        parsed_task=ParsedTask(
            task_id="TASK-123",
            title="Add email validation to user registration API",
            repository_url="https://github.com/org/repo",
            base_branch="develop",
            requirement="Add email validation",
            acceptance_criteria=[],
        ),
        code_change=CodeChange(
            changed_files=["app/users.py"],
            model_used="llama-3.3-70b-versatile",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _make_branch_name
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeBranchName:
    def test_basic(self):
        name = _make_branch_name("TASK-123", "Add email validation")
        assert name == "ai-agent/TASK-123-add-email-validation"

    def test_special_chars_stripped(self):
        name = _make_branch_name("TASK-1", "Fix: user's login/logout!")
        assert " " not in name
        assert "'" not in name
        assert "!" not in name

    def test_title_truncated(self):
        long_title = "A" * 100
        name = _make_branch_name("T-1", long_title)
        assert len(name) < 80


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenHelpers:
    def test_inject(self):
        url = "https://github.com/org/repo"
        assert _inject_token(url, "tok") == "https://tok@github.com/org/repo"

    def test_inject_empty_token_unchanged(self):
        url = "https://github.com/org/repo"
        assert _inject_token(url, "") == url

    def test_strip_token(self):
        url = "https://mytoken@github.com/org/repo"
        assert _strip_token(url) == "https://github.com/org/repo"

    def test_strip_no_token_unchanged(self):
        url = "https://github.com/org/repo"
        assert _strip_token(url) == url


# ─────────────────────────────────────────────────────────────────────────────
# run() node
# ─────────────────────────────────────────────────────────────────────────────

class TestRunNode:
    def test_missing_inputs_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    @patch("app.agents.gitgeneer.GITHUB_TOKEN", "fake-token")
    def test_successful_branch_and_commit(self, tmp_path):
        repo = _init_repo(tmp_path)
        state = _make_state(tmp_path)

        mock_push_info = MagicMock()
        mock_push_info.flags = 0
        mock_push_info.ERROR = 32

        mock_remote = MagicMock()
        mock_remote.push.return_value = [mock_push_info]
        mock_remote.url = "https://github.com/org/repo"
        mock_remote.refs = []

        # Temporarily override the remotes property at class level
        original_fget = git.Repo.remotes.fget
        git.Repo.remotes = property(lambda self: [mock_remote])
        try:
            with patch("app.agents.gitgeneer.git.Repo", return_value=repo):
                result = run(state)
        finally:
            git.Repo.remotes = property(original_fget)

        assert result["status"] == PipelineStatus.RUNNING
        assert result["feature_branch"].startswith("ai-agent/TASK-123")
        assert result["commit_sha"]

    def test_invalid_git_repo_fails(self, tmp_path):
        state = AgentState(
            raw_task={"taskId": "T-1"},
            workspace_path=str(tmp_path),
            parsed_task=ParsedTask(
                task_id="T-1", title="test",
                repository_url="https://github.com/a/b",
                base_branch="main", requirement="x", acceptance_criteria=[],
            ),
            code_change=CodeChange(changed_files=["f.py"], model_used="llama"),
        )
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED
        assert "not a git repository" in result["error"]