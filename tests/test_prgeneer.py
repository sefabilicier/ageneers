"""
Unit tests for PRGeneer.

GitHub API calls are fully mocked — no network or real token required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.prgeneer import _build_pr_body, _extract_repo_slug, run
from app.models.state import (
    AgentState,
    CodeChange,
    ParsedTask,
    PipelineStatus,
    PullRequest,
    RepoAnalysis,
    TestResult,
    TestStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# _extract_repo_slug
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractRepoSlug:
    def test_standard_url(self):
        assert _extract_repo_slug("https://github.com/my-org/my-repo") == "my-org/my-repo"

    def test_trailing_git(self):
        assert _extract_repo_slug("https://github.com/org/repo.git") == "org/repo"

    def test_trailing_slash(self):
        assert _extract_repo_slug("https://github.com/org/repo/") == "org/repo"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            _extract_repo_slug("https://gitlab.com/org/repo")


# ─────────────────────────────────────────────────────────────────────────────
# _build_pr_body
# ─────────────────────────────────────────────────────────────────────────────

def _make_full_state() -> AgentState:
    return AgentState(
        raw_task={"taskId": "TASK-123"},
        feature_branch="ai-agent/TASK-123-add-email-validation",
        commit_sha="abc123",
        parsed_task=ParsedTask(
            task_id="TASK-123",
            title="Add email validation to user registration API",
            repository_url="https://github.com/org/repo",
            base_branch="develop",
            requirement="Add email format validation to POST /users/register",
            acceptance_criteria=["Invalid email returns HTTP 400", "Add unit tests"],
        ),
        repo_analysis=RepoAnalysis(
            language="Python", framework="FastAPI",
            build_tool="pip", test_command="pytest",
            relevant_files=["app/users.py"],
            existing_test_files=["tests/test_users.py"],
        ),
        code_change=CodeChange(
            changed_files=["app/users.py", "tests/test_users.py"],
            model_used="llama-3.3-70b-versatile",
            prompt_tokens=800,
            completion_tokens=400,
        ),
        test_result=TestResult(
            status=TestStatus.PASSED,
            command="pytest",
            duration_seconds=3.2,
            output="3 passed",
        ),
    )


class TestBuildPrBody:
    def test_contains_task_id(self):
        body = _build_pr_body(_make_full_state())
        assert "TASK-123" in body

    def test_contains_requirement(self):
        body = _build_pr_body(_make_full_state())
        assert "email format validation" in body

    def test_passed_tests_show_checkmark(self):
        body = _build_pr_body(_make_full_state())
        assert "✅" in body

    def test_failed_tests_show_warning(self):
        state = _make_full_state()
        state.test_result.status = TestStatus.FAILED
        state.test_result.output = "1 failed"
        body = _build_pr_body(state)
        assert "❌" in body
        assert "⚠️" in body

    def test_contains_model_name(self):
        body = _build_pr_body(_make_full_state())
        assert "llama-3.3-70b-versatile" in body

    def test_contains_changed_files(self):
        body = _build_pr_body(_make_full_state())
        assert "app/users.py" in body

    def test_ai_agent_footer(self):
        body = _build_pr_body(_make_full_state())
        assert "AI Development Agent" in body


# ─────────────────────────────────────────────────────────────────────────────
# run() node
# ─────────────────────────────────────────────────────────────────────────────

class TestRunNode:
    def test_missing_feature_branch_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    def test_missing_token_fails(self):
        state = _make_full_state()
        with patch("app.agents.prgeneer.GITHUB_TOKEN", ""):
            result = run(state)
        assert result["status"] == PipelineStatus.FAILED
        assert "GITHUB_TOKEN" in result["error"]

    @patch("app.agents.prgeneer.GITHUB_TOKEN", "fake-token")
    @patch("app.agents.prgeneer.Github")
    def test_successful_pr_creation(self, mock_github_cls):
        state = _make_full_state()

        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_pr.html_url = "https://github.com/org/repo/pull/42"
        mock_pr.title = "TASK-123 Add email validation"

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = []       # no existing PRs
        mock_repo.create_pull.return_value = mock_pr

        mock_github_cls.return_value.get_repo.return_value = mock_repo

        result = run(state)

        assert result["status"] == PipelineStatus.SUCCESS
        pr: PullRequest = result["pull_request"]
        assert pr.number == 42
        assert "42" in pr.url

    @patch("app.agents.prgeneer.GITHUB_TOKEN", "fake-token")
    @patch("app.agents.prgeneer.Github")
    def test_duplicate_pr_reused(self, mock_github_cls):
        state = _make_full_state()

        mock_pr = MagicMock()
        mock_pr.number = 7
        mock_pr.html_url = "https://github.com/org/repo/pull/7"
        mock_pr.title = "Existing PR"

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [mock_pr]   # existing PR found

        mock_github_cls.return_value.get_repo.return_value = mock_repo

        result = run(state)

        assert result["status"] == PipelineStatus.SUCCESS
        assert result["pull_request"].number == 7
        mock_repo.create_pull.assert_not_called()

    @patch("app.agents.prgeneer.GITHUB_TOKEN", "fake-token")
    @patch("app.agents.prgeneer.Github")
    def test_github_api_error_fails(self, mock_github_cls):
        from github import GithubException
        state = _make_full_state()

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = []
        mock_repo.create_pull.side_effect = GithubException(422, {"message": "Validation Failed"})

        mock_github_cls.return_value.get_repo.return_value = mock_repo

        result = run(state)
        assert result["status"] == PipelineStatus.FAILED
        assert "422" in result["error"]