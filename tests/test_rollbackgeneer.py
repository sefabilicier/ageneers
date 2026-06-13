"""Tests for the rollback agent."""
from unittest.mock import MagicMock, patch

from app.agents.rollbackgeneer import run
from app.models.state import AgentState, ParsedTask, PipelineStatus


def _make_state(branch: str = "ai-agent/TASK-1-test", error: str = "PR creation failed") -> AgentState:
    state = AgentState(
        raw_task={"taskId": "TASK-1"},
        status=PipelineStatus.FAILED,
        error=error,
    )
    state.parsed_task = ParsedTask(
        task_id="TASK-1",
        title="Test",
        requirement="Test requirement",
        repository_url="https://github.com/org/repo",
        base_branch="main",
        acceptance_criteria=[],
    )
    state.feature_branch = branch
    return state


class TestRollbackAgent:
    def test_skips_when_no_branch(self):
        state = _make_state(branch="")
        result = run(state)
        assert result["rollback_result"].performed is False

    @patch("app.agents.rollbackgeneer.get_git_provider")
    def test_deletes_branch_on_failure(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.get_repo_slug.return_value = "org/repo"
        mock_provider.delete_branch.return_value = True
        mock_get_provider.return_value = mock_provider

        state = _make_state()
        result = run(state)

        mock_provider.delete_branch.assert_called_once_with("org/repo", "ai-agent/TASK-1-test")
        rb = result["rollback_result"]
        assert rb.performed is True
        assert rb.success is True
        assert rb.branch == "ai-agent/TASK-1-test"

    @patch("app.agents.rollbackgeneer.get_git_provider")
    def test_handles_delete_failure_gracefully(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.get_repo_slug.return_value = "org/repo"
        mock_provider.delete_branch.return_value = False
        mock_get_provider.return_value = mock_provider

        state = _make_state()
        result = run(state)

        rb = result["rollback_result"]
        assert rb.performed is True
        assert rb.success is False

    @patch("app.agents.rollbackgeneer.get_git_provider")
    def test_handles_provider_exception(self, mock_get_provider):
        mock_get_provider.side_effect = RuntimeError("GITHUB_TOKEN not set")

        state = _make_state()
        result = run(state)

        rb = result["rollback_result"]
        assert rb.performed is True
        assert rb.success is False
        assert "GITHUB_TOKEN" in rb.reason