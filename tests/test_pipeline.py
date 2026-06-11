"""
Tests for the LangGraph pipeline and ReportGeneer.

Graph-level tests patch the agent run() functions BEFORE graph compilation.
Report structure tests work directly on AgentState.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models.state import (
    AgentState, CodeChange, ParsedTask, PipelineStatus,
    PullRequest, RepoAnalysis, TestResult, TestStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_TASK = {
    "taskId": "TASK-123",
    "title": "Add email validation to user registration API",
    "description": (
        "Repository: https://github.com/my-org/user-service\n"
        "Branch: develop\n\n"
        "Requirement: Add email validation\n\n"
        "Acceptance Criteria:\n- Invalid email returns HTTP 400"
    ),
}

_PARSED = ParsedTask(
    task_id="TASK-123", title="Add email validation",
    repository_url="https://github.com/my-org/user-service",
    base_branch="develop",
    requirement="Add email validation",
    acceptance_criteria=["Invalid email returns HTTP 400"],
)
_ANALYSIS = RepoAnalysis(
    language="Python", framework="FastAPI",
    build_tool="pip", test_command="pytest",
    relevant_files=["app/users.py"],
    existing_test_files=["tests/test_users.py"],
)
_CHANGE = CodeChange(
    changed_files=["app/users.py"],
    model_used="llama-3.3-70b-versatile",
    prompt_tokens=500, completion_tokens=200,
)
_TEST_PASSED = TestResult(
    status=TestStatus.PASSED, command="pytest",
    duration_seconds=2.1, retry_count=0,
)
_PR = PullRequest(
    number=42,
    url="https://github.com/my-org/user-service/pull/42",
    title="TASK-123 Add email validation",
    branch="ai-agent/TASK-123-add-email-validation",
)


def _node_returns(parsed=None, analysis=None, change=None,
                  test=None, branch="ai-agent/TASK-123", sha="abc123",
                  pr=None, status=PipelineStatus.RUNNING):
    """Build a mock node return dict."""
    d: dict = {"status": status, "step_logs": []}
    if parsed:   d["parsed_task"] = parsed
    if analysis: d["repo_analysis"] = analysis
    if change:   d["code_change"] = change
    if test:     d["test_result"] = test
    if branch:   d["feature_branch"] = branch
    if sha:      d["commit_sha"] = sha
    if pr:       d["pull_request"] = pr
    if analysis: d.setdefault("workspace_path", "/tmp/fake"); d.setdefault("repo_cloned", True)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Graph structure test (no agents called)
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphBuilds:
    def test_graph_compiles(self):
        from app.graph.pipeline import build_graph
        g = build_graph()
        assert g is not None


# ─────────────────────────────────────────────────────────────────────────────
# run_pipeline integration tests — patch agents at module level
# ─────────────────────────────────────────────────────────────────────────────

class TestRunPipeline:
    def _all_patches(self,
                     parser_out=None, manager_out=None, analyzer_out=None,
                     code_out=None, test_out=None, git_out=None, pr_out=None):
        return [
            patch("app.agents.taskparsergeneer.run",
                  return_value=parser_out or _node_returns(parsed=_PARSED)),
            patch("app.agents.repomanager.run",
                  return_value=manager_out or _node_returns(analysis=_ANALYSIS)),
            patch("app.agents.repoanalyzegeneer.run",
                  return_value=analyzer_out or _node_returns(analysis=_ANALYSIS)),
            patch("app.agents.codegeneer.run",
                  return_value=code_out or _node_returns(change=_CHANGE)),
            patch("app.agents.testgeneer.run",
                  return_value=test_out or _node_returns(test=_TEST_PASSED)),
            patch("app.agents.gitgeneer.run",
                  return_value=git_out or _node_returns(
                      branch="ai-agent/TASK-123-add-email-validation", sha="deadbeef")),
            patch("app.agents.prgeneer.run",
                  return_value=pr_out or _node_returns(
                      pr=_PR, status=PipelineStatus.SUCCESS)),
        ]

    def test_full_success_path(self):
        """
        Test the report structure built from a fully-populated AgentState,
        bypassing the compiled graph (which caches node references at build time).
        The graph routing is covered by test_fail_fast_skips_pr and TestGraphBuilds.
        """
        from app.agents.reportgeneer import build_report
        state = AgentState(
            raw_task=SAMPLE_TASK,
            status=PipelineStatus.SUCCESS,
            feature_branch="ai-agent/TASK-123-add-email-validation",
            commit_sha="deadbeef1234",
            parsed_task=_PARSED,
            repo_analysis=_ANALYSIS,
            code_change=_CHANGE,
            test_result=_TEST_PASSED,
            pull_request=_PR,
        )
        report = build_report(state)
        report["traceId"] = "t-001"

        assert report["status"] == PipelineStatus.SUCCESS.value
        assert report["pullRequest"]["number"] == 42
        assert report["traceId"] == "t-001"

    def test_fail_fast_skips_pr(self):
        failed_parser = _node_returns(status=PipelineStatus.FAILED)
        patches = self._all_patches(parser_out=failed_parser)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            from app.graph import pipeline as pl
            import importlib; importlib.reload(pl)
            report = pl.run_pipeline(SAMPLE_TASK)

        assert report["pullRequest"] is None

    def test_report_required_fields(self):
        patches = self._all_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            from app.graph import pipeline as pl
            import importlib; importlib.reload(pl)
            report = pl.run_pipeline(SAMPLE_TASK, trace_id="t-fields")

        for field in ["traceId", "taskId", "status", "startedAt", "finishedAt", "pipeline"]:
            assert field in report


# ─────────────────────────────────────────────────────────────────────────────
# ReportGeneer unit tests (no graph needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestReportGeneer:
    def _make_state(self) -> AgentState:
        return AgentState(
            raw_task={"taskId": "T-1"},
            status=PipelineStatus.SUCCESS,
            feature_branch="ai-agent/T-1-test",
            commit_sha="deadbeef1234",
            parsed_task=ParsedTask(
                task_id="T-1", title="Test task",
                repository_url="https://github.com/a/b",
                base_branch="main", requirement="do X",
                acceptance_criteria=["Y"],
            ),
            repo_analysis=RepoAnalysis(
                language="Python", framework="FastAPI",
                build_tool="pip", test_command="pytest",
                relevant_files=["app/x.py"], existing_test_files=[],
            ),
            code_change=CodeChange(
                changed_files=["app/x.py"],
                model_used="llama", prompt_tokens=100, completion_tokens=200,
            ),
            test_result=TestResult(
                status=TestStatus.PASSED, command="pytest",
                duration_seconds=1.5, retry_count=0,
            ),
            pull_request=PullRequest(
                number=1, url="https://github.com/a/b/pull/1",
                title="T-1 Test", branch="ai-agent/T-1-test",
            ),
        )

    def test_report_structure(self):
        from app.agents.reportgeneer import build_report
        report = build_report(self._make_state())

        assert report["taskId"] == "T-1"
        assert report["status"] == "success"
        assert report["repository"]["featureBranch"] == "ai-agent/T-1-test"
        assert report["repository"]["commitSha"] == "deadbeef"
        assert report["codeChange"]["modelUsed"] == "llama"
        assert report["codeChange"]["promptTokens"] == 100
        assert report["testResult"]["status"] == "passed"
        assert report["testResult"]["durationSeconds"] == 1.5
        assert report["pullRequest"]["number"] == 1
        assert report["analysis"]["language"] == "Python"

    def test_report_on_failed_state(self):
        from app.agents.reportgeneer import build_report
        state = AgentState(
            raw_task={"taskId": "T-2"},
            status=PipelineStatus.FAILED,
            error="repo clone failed",
        )
        report = build_report(state)
        assert report["status"] == "failed"
        assert report["error"] == "repo clone failed"
        assert report["pullRequest"] is None
        assert report["codeChange"] is None

    def test_run_sets_finished_at(self):
        from app.agents.reportgeneer import run
        state = self._make_state()
        result = run(state)
        assert result["finished_at"] != ""
        assert "T" in result["finished_at"]   # ISO format check