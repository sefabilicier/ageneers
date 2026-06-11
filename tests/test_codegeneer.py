"""
Unit tests for CodeGeneer.

All LLM calls are mocked. File system operations use pytest's tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.codegeneer import (
    _parse_llm_output,
    _read_files_for_context,
    _validate_changes,
    _write_changes,
    run,
)
from app.models.state import AgentState, CodeChange, ParsedTask, PipelineStatus, RepoAnalysis


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "users.py").write_text("def register(email):\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_users.py").write_text("def test_register(): pass\n")
    return tmp_path


def _make_state(workspace: Path) -> AgentState:
    return AgentState(
        raw_task={"taskId": "TASK-001"},
        workspace_path=str(workspace),
        parsed_task=ParsedTask(
            task_id="TASK-001",
            repository_url="https://github.com/org/repo",
            base_branch="develop",
            requirement="Add email format validation to the register function",
            acceptance_criteria=["Invalid email returns ValueError", "Add unit tests"],
        ),
        repo_analysis=RepoAnalysis(
            language="Python",
            framework="FastAPI",
            build_tool="pip",
            test_command="pytest",
            relevant_files=["app/users.py", "tests/test_users.py"],
            existing_test_files=["tests/test_users.py"],
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _read_files_for_context
# ─────────────────────────────────────────────────────────────────────────────

class TestReadFiles:
    def test_reads_existing_files(self, tmp_path):
        _make_workspace(tmp_path)
        result = _read_files_for_context(tmp_path, ["app/users.py", "tests/test_users.py"])
        assert "app/users.py" in result
        assert "register" in result["app/users.py"]

    def test_skips_missing_files(self, tmp_path):
        _make_workspace(tmp_path)
        result = _read_files_for_context(tmp_path, ["nonexistent.py"])
        assert "nonexistent.py" not in result

    def test_truncates_large_files(self, tmp_path):
        big = tmp_path / "big.py"
        big.write_text("x = 1\n" * 5000)
        result = _read_files_for_context(tmp_path, ["big.py"])
        assert "truncated" in result["big.py"]


# ─────────────────────────────────────────────────────────────────────────────
# _parse_llm_output
# ─────────────────────────────────────────────────────────────────────────────

class TestParseLlmOutput:
    def test_valid_json_array(self):
        raw = '[{"path": "app/users.py", "content": "def register(): pass"}]'
        result = _parse_llm_output(raw)
        assert len(result) == 1
        assert result[0]["path"] == "app/users.py"

    def test_strips_markdown(self):
        raw = '```json\n[{"path": "a.py", "content": "x"}]\n```'
        result = _parse_llm_output(raw)
        assert result[0]["path"] == "a.py"

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            _parse_llm_output("not json")

    def test_non_array_raises(self):
        with pytest.raises(ValueError, match="array"):
            _parse_llm_output('{"path": "a.py", "content": "x"}')


# ─────────────────────────────────────────────────────────────────────────────
# _validate_changes
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateChanges:
    def test_allows_relevant_file(self, tmp_path):
        changes = [{"path": "app/users.py", "content": "new content"}]
        result = _validate_changes(changes, tmp_path, {"app/users.py"})
        assert len(result) == 1

    def test_rejects_path_traversal(self, tmp_path):
        changes = [{"path": "../etc/passwd", "content": "evil"}]
        result = _validate_changes(changes, tmp_path, set())
        assert len(result) == 0

    def test_rejects_absolute_path(self, tmp_path):
        changes = [{"path": "/etc/hosts", "content": "evil"}]
        result = _validate_changes(changes, tmp_path, set())
        assert len(result) == 0

    def test_rejects_unrequested_non_test_file(self, tmp_path):
        changes = [{"path": "app/unrelated.py", "content": "something"}]
        result = _validate_changes(changes, tmp_path, {"app/users.py"})
        assert len(result) == 0

    def test_allows_new_test_file(self, tmp_path):
        changes = [{"path": "tests/test_new.py", "content": "def test_x(): pass"}]
        result = _validate_changes(changes, tmp_path, {"app/users.py"})
        assert len(result) == 1

    def test_rejects_empty_content(self, tmp_path):
        changes = [{"path": "app/users.py", "content": "   "}]
        result = _validate_changes(changes, tmp_path, {"app/users.py"})
        assert len(result) == 0


# ─────────────────────────────────────────────────────────────────────────────
# _write_changes
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteChanges:
    def test_writes_file(self, tmp_path):
        changes = [{"path": "app/users.py", "content": "def register(): raise ValueError"}]
        written = _write_changes(changes, tmp_path)
        assert "app/users.py" in written
        assert "ValueError" in (tmp_path / "app" / "users.py").read_text()

    def test_creates_parent_dirs(self, tmp_path):
        changes = [{"path": "new/deep/file.py", "content": "x = 1"}]
        _write_changes(changes, tmp_path)
        assert (tmp_path / "new" / "deep" / "file.py").exists()


# ─────────────────────────────────────────────────────────────────────────────
# run() node tests
# ─────────────────────────────────────────────────────────────────────────────

VALID_LLM_RESPONSE = '''[
  {
    "path": "app/users.py",
    "content": "import re\\n\\ndef register(email):\\n    if not re.match(r\\"[^@]+@[^@]+\\", email):\\n        raise ValueError(\\"Invalid email format\\")\\n"
  },
  {
    "path": "tests/test_users.py",
    "content": "from app.users import register\\nimport pytest\\n\\ndef test_invalid_email():\\n    with pytest.raises(ValueError):\\n        register(\\"bad-email\\")\\n"
  }
]'''


class TestRunNode:
    @patch("app.agents.codegeneer._get_llm")
    def test_successful_code_change(self, mock_get_llm, tmp_path):
        _make_workspace(tmp_path)
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content=VALID_LLM_RESPONSE,
            usage_metadata=None,
        )
        mock_get_llm.return_value = mock_llm

        state = _make_state(tmp_path)
        result = run(state)

        assert result["status"] == PipelineStatus.RUNNING
        change: CodeChange = result["code_change"]
        assert "app/users.py" in change.changed_files
        # Verify file was actually written
        assert "Invalid email" in (tmp_path / "app" / "users.py").read_text()

    @patch("app.agents.codegeneer._get_llm")
    def test_invalid_llm_json_fails(self, mock_get_llm, tmp_path):
        _make_workspace(tmp_path)
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="not json", usage_metadata=None)
        mock_get_llm.return_value = mock_llm

        state = _make_state(tmp_path)
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    @patch("app.agents.codegeneer._get_llm")
    def test_all_changes_rejected_fails(self, mock_get_llm, tmp_path):
        _make_workspace(tmp_path)
        mock_llm = MagicMock()
        # LLM tries to change an unrelated file
        mock_llm.invoke.return_value = MagicMock(
            content='[{"path": "../outside.py", "content": "evil"}]',
            usage_metadata=None,
        )
        mock_get_llm.return_value = mock_llm

        state = _make_state(tmp_path)
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    def test_missing_state_fields_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED