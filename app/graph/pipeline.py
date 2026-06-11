"""
LangGraph Pipeline — the central StateGraph that wires all agent nodes.

Flow:
    task_parser → repo_manager → repo_analyzer → code_writer
        → test_runner → git_agent → pr_agent → report

Conditional edges:
    - After every node: if status == FAILED → jump directly to report (fail-fast).
    - After test_runner: if status == PARTIAL (tests failed, report mode)
        → continue to git_agent (PR will be opened with failure note).
    - After test_runner: if status == FAILED and mode == "block"
        → jump to report (no PR).

State merging:
    LangGraph merges the dict returned by each node into the shared AgentState.
    Nodes return only the keys they set — everything else is preserved.
"""

from __future__ import annotations

import os
from typing import Any

from langgraph.graph import END, StateGraph

from app.agents import (
    codegeneer,
    gitgeneer,
    prgeneer,
    repoanalyzegeneer,
    repomanager,
    reportgeneer,
    taskparsergeneer,
    testgeneer,
)
from app.models.state import AgentState, PipelineStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

TEST_FAILURE_MODE = os.getenv("TEST_FAILURE_MODE", "report")

# ─────────────────────────────────────────────────────────────────────────────
# LangGraph requires a plain dict as the state type annotation.
# We adapt AgentState (Pydantic) via a thin wrapper.
# ─────────────────────────────────────────────────────────────────────────────

def _node(agent_run):
    """
    Wrap an agent's run(state) function for LangGraph.
    LangGraph passes state as a dict; we convert to/from AgentState.
    """
    def wrapper(state_dict: dict[str, Any]) -> dict[str, Any]:
        state = AgentState(**state_dict)
        return agent_run(state)
    wrapper.__name__ = agent_run.__module__.split(".")[-1]
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Conditional routing
# ─────────────────────────────────────────────────────────────────────────────

def _route_after_node(state_dict: dict[str, Any]) -> str:
    """
    Generic post-node router.
    FAILED → report (fail-fast)
    Otherwise → next node (determined by graph edges)
    """
    raw = state_dict.get("status", PipelineStatus.PENDING)
    status_val = raw.value if isinstance(raw, PipelineStatus) else str(raw)
    if status_val == PipelineStatus.FAILED.value:
        return "report"
    return "continue"


def _route_after_tests(state_dict: dict[str, Any]) -> str:
    """
    Post-test router.
    PARTIAL (failed, report mode) → git_agent (open PR with warning)
    FAILED  (blocked)             → report
    RUNNING (passed)              → git_agent
    """
    raw = state_dict.get("status", PipelineStatus.PENDING)
    status_val = raw.value if isinstance(raw, PipelineStatus) else str(raw)
    if status_val == PipelineStatus.FAILED.value:
        return "report"
    return "git_agent"   # covers both RUNNING and PARTIAL


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────

def build_graph() -> Any:
    """Build and compile the LangGraph StateGraph."""

    # Use dict as state type (LangGraph native)
    graph = StateGraph(dict)

    # ── Register nodes ────────────────────────────────────────────────────
    graph.add_node("task_parser",    _node(taskparsergeneer.run))
    graph.add_node("repo_manager",   _node(repomanager.run))
    graph.add_node("repo_analyzer",  _node(repoanalyzegeneer.run))
    graph.add_node("code_writer",    _node(codegeneer.run))
    graph.add_node("test_runner",    _node(testgeneer.run))
    graph.add_node("git_agent",      _node(gitgeneer.run))
    graph.add_node("pr_agent",       _node(prgeneer.run))
    graph.add_node("report",         _node(reportgeneer.run))

    # ── Entry point ───────────────────────────────────────────────────────
    graph.set_entry_point("task_parser")

    # ── Conditional edges (fail-fast after each node) ─────────────────────
    for node, next_node in [
        ("task_parser",   "repo_manager"),
        ("repo_manager",  "repo_analyzer"),
        ("repo_analyzer", "code_writer"),
        ("code_writer",   "test_runner"),
    ]:
        graph.add_conditional_edges(
            node,
            _route_after_node,
            {"continue": next_node, "report": "report"},
        )

    # ── Test runner has custom routing ────────────────────────────────────
    graph.add_conditional_edges(
        "test_runner",
        _route_after_tests,
        {"git_agent": "git_agent", "report": "report"},
    )

    # ── Git + PR + report (linear tail) ───────────────────────────────────
    graph.add_conditional_edges(
        "git_agent",
        _route_after_node,
        {"continue": "pr_agent", "report": "report"},
    )
    graph.add_conditional_edges(
        "pr_agent",
        _route_after_node,
        {"continue": "report", "report": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point called by the FastAPI background task
# ─────────────────────────────────────────────────────────────────────────────

_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def run_pipeline(raw_task: dict[str, Any], trace_id: str = "") -> dict[str, Any]:
    """
    Execute the full agent pipeline for a given task.

    Args:
        raw_task : the raw task payload dict (taskId, title, description)
        trace_id : optional trace ID for logging correlation

    Returns:
        The final state dict (AgentState fields).
    """
    from app.utils.logger import bind_trace
    if trace_id:
        bind_trace(trace_id)

    logger.info("pipeline.invoked", task_id=raw_task.get("taskId"), trace_id=trace_id)

    initial_state: dict[str, Any] = AgentState(raw_task=raw_task).model_dump(mode='json')

    graph = get_graph()
    final_state: dict[str, Any] = graph.invoke(initial_state)

    # LangGraph merges node outputs into the initial dict.
    # Reconstruct AgentState carefully — enum fields may come back as strings.
    merged = {**initial_state, **{k: v for k, v in final_state.items() if v is not None}}
    final_agent_state = AgentState(**merged)

    from app.agents.reportgeneer import build_report
    report = build_report(final_agent_state)
    report["traceId"] = trace_id

    logger.info("pipeline.finished",
                status=final_agent_state.status.value,
                task_id=raw_task.get("taskId"))

    return report