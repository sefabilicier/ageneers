"""
Unit tests for TestGeneer.

subprocess calls are mocked — no real test runner invoked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.testgeneer import (
    _build_command,
    _parse_test_output,
    _run_tests,
    run,
)
from app.models.state import (
    TestStatus as TStatus,
    AgentState,
    CodeChange,
    ParsedTask,
    PipelineStatus,
    RepoAnalysis,
    TestStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(test_command: str = "pytest", failure_mode: str = "report") -> AgentState:
    return AgentState(
        raw_task={"taskId": "TASK-001"},
        workspace_path="/tmp/fake-workspace",
        parsed_task=ParsedTask(
            task_id="TASK-001",
            repository_url="https://github.com/org/repo",
            base_branch="develop",
            requirement="Add validation",
            acceptance_criteria=[],
        ),
        repo_analysis=RepoAnalysis(
            language="Python",
            framework="FastAPI",
            build_tool="pip",
            test_command=test_command,
            relevant_files=["app/users.py", "tests/test_users.py"],
            existing_test_files=["tests/test_users.py"],
        ),
        code_change=CodeChange(
            changed_files=["app/users.py", "tests/test_users.py"],
            model_used="llama-3.3-70b-versatile",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _build_command
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildCommand:
    def test_pytest(self):
        assert _build_command("pytest") == ["pytest"]

    def test_mvn_test(self):
        assert _build_command("mvn test") == ["mvn", "test"]

    def test_npm_test(self):
        assert _build_command("npm test") == ["npm", "test"]

    def test_rejects_unknown_command(self):
        with pytest.raises(ValueError, match="not in the allowed list"):
            _build_command("rm -rf /")

    def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            _build_command("")


# ─────────────────────────────────────────────────────────────────────────────
# _parse_test_output
# ─────────────────────────────────────────────────────────────────────────────

class TestParseOutput:
    def test_returncode_0_is_passed(self):
        assert _parse_test_output("5 passed", 0) == TestStatus.PASSED

    def test_returncode_nonzero_with_failed_keyword(self):
        assert _parse_test_output("1 failed, 4 passed", 1) == TestStatus.FAILED

    def test_returncode_nonzero_no_keyword(self):
        assert _parse_test_output("some unexpected output", 1) == TestStatus.FAILED

    def test_returncode_0_always_passes(self):
        assert _parse_test_output("", 0) == TestStatus.PASSED


# ─────────────────────────────────────────────────────────────────────────────
# _run_tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRunTests:
    @patch("app.agents.testgeneer.subprocess.run")
    def test_successful_run(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="5 passed", stderr=""
        )
        rc, output, duration = _run_tests(["pytest"], tmp_path)
        assert rc == 0
        assert "passed" in output
        assert duration >= 0

    @patch("app.agents.testgeneer.subprocess.run")
    def test_failed_run(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="1 failed", stderr="AssertionError"
        )
        rc, output, _ = _run_tests(["pytest"], tmp_path)
        assert rc == 1
        assert "failed" in output

    @patch("app.agents.testgeneer.subprocess.run")
    def test_timeout_returns_failure(self, mock_run, tmp_path):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="pytest", timeout=120)
        rc, output, _ = _run_tests(["pytest"], tmp_path)
        assert rc == 1
        assert "timed out" in output


# ─────────────────────────────────────────────────────────────────────────────
# run() node — report mode (default)
# ─────────────────────────────────────────────────────────────────────────────

class TestRunNodeReport:
    @patch("app.agents.testgeneer.TEST_FAILURE_MODE", "report")
    @patch("app.agents.testgeneer._run_tests")
    def test_passing_tests(self, mock_run_tests, tmp_path):
        mock_run_tests.return_value = (0, "5 passed", 1.2)
        state = _make_state()
        state.workspace_path = str(tmp_path)
        result = run(state)

        assert result["status"] == PipelineStatus.RUNNING
        assert result["test_result"].status == TestStatus.PASSED

    @patch("app.agents.testgeneer.TEST_FAILURE_MODE", "report")
    @patch("app.agents.testgeneer._run_tests")
    def test_failing_tests_report_mode_continues(self, mock_run_tests, tmp_path):
        mock_run_tests.return_value = (1, "1 failed", 2.0)
        state = _make_state()
        state.workspace_path = str(tmp_path)
        result = run(state)

        # In report mode, pipeline continues with PARTIAL status
        assert result["status"] == PipelineStatus.PARTIAL
        assert result["test_result"].status == TestStatus.FAILED

    @patch("app.agents.testgeneer.TEST_FAILURE_MODE", "block")
    @patch("app.agents.testgeneer._run_tests")
    def test_failing_tests_block_mode_stops(self, mock_run_tests, tmp_path):
        mock_run_tests.return_value = (1, "1 failed", 2.0)
        state = _make_state()
        state.workspace_path = str(tmp_path)
        result = run(state)

        assert result["status"] == PipelineStatus.FAILED

    @patch("app.agents.testgeneer.TEST_FAILURE_MODE", "retry")
    @patch("app.agents.testgeneer.MAX_RETRY_COUNT", 1)
    @patch("app.agents.testgeneer._llm_fix_tests")
    @patch("app.agents.testgeneer._run_tests")
    def test_retry_mode_attempts_fix(self, mock_run_tests, mock_fix, tmp_path):
        # First call fails, second passes after fix
        mock_run_tests.side_effect = [
            (1, "1 failed", 1.0),
            (0, "1 passed", 1.0),
        ]
        mock_fix.return_value = []   # no actual file changes needed for test
        state = _make_state()
        state.workspace_path = str(tmp_path)
        result = run(state)

        assert result["status"] == PipelineStatus.RUNNING
        assert result["test_result"].status == TestStatus.PASSED
        assert result["test_result"].retry_count == 1

    def test_missing_inputs_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    @patch("app.agents.testgeneer._run_tests")
    def test_invalid_test_command_fails(self, mock_run, tmp_path):
        state = _make_state(test_command="rm -rf /")
        state.workspace_path = str(tmp_path)
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED