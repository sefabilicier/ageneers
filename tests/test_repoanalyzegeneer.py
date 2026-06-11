"""
Unit tests for RepoAnalyzeGeneer.

Uses tmp_path fixture to create fake repo structures on disk.
LLM ranking call is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.repoanalyzegeneer import (
    _collect_source_files,
    _detect_stack,
    _llm_rank_files,
    run,
)
from app.models.state import AgentState, ParsedTask, PipelineStatus


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — fake repo layouts
# ─────────────────────────────────────────────────────────────────────────────

def _make_python_repo(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname="myapp"\n\n[project.dependencies]\nfastapi = "*"\npytest = "*"\n'
    )
    src = root / "app"
    src.mkdir()
    (src / "main.py").write_text("# main")
    (src / "users.py").write_text("# users")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_users.py").write_text("# test")


def _make_java_maven_repo(root: Path) -> None:
    (root / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework.boot</groupId>"
        "</dependency></dependencies></project>"
    )
    src = root / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "UserController.java").write_text("// controller")
    test = root / "src" / "test" / "java" / "com" / "example"
    test.mkdir(parents=True)
    (test / "UserControllerTest.java").write_text("// test")


def _make_node_repo(root: Path) -> None:
    (root / "package.json").write_text(
        '{"dependencies": {"express": "^4"}, "scripts": {"test": "jest"}}'
    )
    (root / "index.js").write_text("// app")


# ─────────────────────────────────────────────────────────────────────────────
# _detect_stack tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectStack:
    def test_python_fastapi(self, tmp_path):
        _make_python_repo(tmp_path)
        stack = _detect_stack(tmp_path)
        assert stack["language"] == "Python"
        assert stack["framework"] == "FastAPI"
        assert "pytest" in stack["test_command"]

    def test_java_maven_spring(self, tmp_path):
        _make_java_maven_repo(tmp_path)
        stack = _detect_stack(tmp_path)
        assert stack["language"] == "Java"
        assert stack["build_tool"] == "Maven"
        assert stack["test_command"] == "mvn test"

    def test_node_express(self, tmp_path):
        _make_node_repo(tmp_path)
        stack = _detect_stack(tmp_path)
        assert stack["language"] == "JavaScript"
        assert stack["framework"] == "Express"
        assert stack["test_command"] == "jest"

    def test_unknown_project(self, tmp_path):
        stack = _detect_stack(tmp_path)
        assert stack["language"] == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# _collect_source_files tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCollectSourceFiles:
    def test_python_repo_finds_sources_and_tests(self, tmp_path):
        _make_python_repo(tmp_path)
        sources, tests = _collect_source_files(tmp_path)
        assert any("users.py" in f for f in sources)
        assert any("test_users.py" in f for f in tests)

    def test_java_repo_finds_test_file(self, tmp_path):
        _make_java_maven_repo(tmp_path)
        _, tests = _collect_source_files(tmp_path)
        assert any("Test" in f for f in tests)

    def test_ignores_venv(self, tmp_path):
        _make_python_repo(tmp_path)
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "something.py").write_text("# venv file")
        sources, _ = _collect_source_files(tmp_path)
        assert not any(".venv" in f for f in sources)


# ─────────────────────────────────────────────────────────────────────────────
# _llm_rank_files tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLlmRankFiles:
    @patch("app.agents.repoanalyzegeneer._get_llm")
    def test_returns_llm_selection(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content='["app/users.py", "tests/test_users.py"]'
        )
        mock_get_llm.return_value = mock_llm

        result = _llm_rank_files("Add email validation", ["app/users.py", "app/main.py", "tests/test_users.py"])
        assert "app/users.py" in result

    @patch("app.agents.repoanalyzegeneer._get_llm")
    def test_falls_back_on_bad_json(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="not json")
        mock_get_llm.return_value = mock_llm

        candidates = [f"file{i}.py" for i in range(15)]
        result = _llm_rank_files("do something", candidates)
        assert len(result) == 10   # fallback: first 10

    def test_empty_candidates_returns_empty(self):
        result = _llm_rank_files("something", [])
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# run() node tests
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(tmp_path: Path) -> AgentState:
    _make_python_repo(tmp_path)
    return AgentState(
        raw_task={"taskId": "TASK-001"},
        workspace_path=str(tmp_path),
        parsed_task=ParsedTask(
            task_id="TASK-001",
            repository_url="https://github.com/org/repo",
            base_branch="main",
            requirement="Add email validation to the user registration endpoint",
            acceptance_criteria=[],
        ),
    )


class TestRunNode:
    @patch("app.agents.repoanalyzegeneer._llm_rank_files")
    def test_successful_analysis(self, mock_rank, tmp_path):
        mock_rank.return_value = ["app/users.py", "tests/test_users.py"]
        state = _make_state(tmp_path)
        result = run(state)

        assert result["status"] == PipelineStatus.RUNNING
        analysis = result["repo_analysis"]
        assert analysis.language == "Python"
        assert analysis.framework == "FastAPI"
        assert analysis.test_command == "pytest"
        assert len(analysis.relevant_files) == 2

    def test_missing_workspace_fails(self):
        state = AgentState(
            raw_task={"taskId": "T-1"},
            parsed_task=ParsedTask(
                task_id="T-1",
                repository_url="https://github.com/a/b",
                base_branch="main",
                requirement="something",
                acceptance_criteria=[],
            ),
            workspace_path="/nonexistent/path",
        )
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED

    def test_missing_inputs_fails(self):
        state = AgentState(raw_task={"taskId": "T-1"})
        result = run(state)
        assert result["status"] == PipelineStatus.FAILED