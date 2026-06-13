"""
LangGraph Pipeline — the central StateGraph that wires all agent nodes.

Key insight about LangGraph StateGraph(dict):
    LangGraph passes the FULL accumulated state dict to each node.
    Each node returns a PARTIAL dict of only the keys it changed.
    LangGraph shallow-merges the partial dict INTO the accumulated state.
    
    This means: if node1 sets "parsed_task" and node2 only sets "workspace_path",
    the accumulated state after node2 will have BOTH keys.
    
    HOWEVER: our _node wrapper builds an AgentState from the state dict,
    calls the agent (which may mutate step_logs on the state object),
    and must return ALL updated fields — not just the ones the agent explicitly
    returned. Otherwise step_logs mutations are lost.

Conditional edges:
    - After every node: FAILED → report (fail-fast)
    - After test_runner: PARTIAL → git_agent (open PR with failure note)
"""

from __future__ import annotations

import os
from typing import Any

from langgraph.graph import END, StateGraph

from app.agents import (
    codereviewgeneer,
    criteriaverifiergeneer,
    codegeneer,
    gitgeneer,
    prgeneer,
    rollbackgeneer,
    repoanalyzegeneer,
    repomanager,
    reportgeneer,
    taskparsergeneer,
    testgeneer,
)
from app.models.state import AgentState, PipelineStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# State (de)serialization helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dict_to_state(state_dict: dict[str, Any]) -> AgentState:
    """
    Build an AgentState from a LangGraph state dict.
    Handles string→enum conversion and skips None values so Pydantic
    defaults are not overwritten.
    """
    clean: dict[str, Any] = {}
    for k, v in state_dict.items():
        if v is None:
            continue
        if k == "status" and isinstance(v, str):
            try:
                clean[k] = PipelineStatus(v)
            except ValueError:
                pass
        else:
            clean[k] = v
    return AgentState(**clean)


def _state_to_partial(state: AgentState, agent_result: dict[str, Any]) -> dict[str, Any]:
    """
    Merge agent_result into the full AgentState and return a serializable dict
    of ALL fields. This ensures LangGraph's accumulated state is always complete.
    """
    # Apply agent result to state
    for k, v in agent_result.items():
        if hasattr(state, k) and v is not None:
            setattr(state, k, v)

    # Serialize to plain dict (Pydantic objects are fine in LangGraph dict state)
    result: dict[str, Any] = {}
    for field_name in AgentState.model_fields:
        val = getattr(state, field_name)
        # Serialize enums to string values
        if isinstance(val, PipelineStatus):
            result[field_name] = val.value
        else:
            result[field_name] = val
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Node wrapper
# ─────────────────────────────────────────────────────────────────────────────

def _node(agent_run):
    """
    Wrap an agent's run(state) function for LangGraph.
    
    - Deserializes the full LangGraph state dict → AgentState
    - Runs the agent
    - Returns the COMPLETE updated state (not just changed keys)
      so the next node always has access to all fields.
    """
    def wrapper(state_dict: dict[str, Any]) -> dict[str, Any]:
        state = _dict_to_state(state_dict)
        agent_result = agent_run(state)
        return _state_to_partial(state, agent_result)

    wrapper.__name__ = agent_run.__module__.split(".")[-1]
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Conditional routing
# ─────────────────────────────────────────────────────────────────────────────

def _route_after_node(state_dict: dict[str, Any]) -> str:
    status = str(state_dict.get("status", "pending"))
    if status == PipelineStatus.FAILED.value:
        return "report"
    return "continue"


def _route_after_tests(state_dict: dict[str, Any]) -> str:
    status = str(state_dict.get("status", "pending"))
    if status == PipelineStatus.FAILED.value:
        return "report"
    return "git_agent"   # RUNNING and PARTIAL both proceed to git


def _route_after_criteria(state_dict: dict[str, Any]) -> str:
    """
    Evaluator-Optimizer routing: retry code_writer if acceptance criteria
    are not met and retries remain. Otherwise proceed to test_runner.
    """
    status = str(state_dict.get("status", "pending"))
    if status == PipelineStatus.FAILED.value:
        return "report"

    criteria = state_dict.get("criteria_result")
    if criteria is not None:
        retry_needed = (
            criteria.get("retry_needed")
            if isinstance(criteria, dict)
            else getattr(criteria, "retry_needed", False)
        )
        if retry_needed:
            return "code_writer"
    return "test_runner"


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(dict)

    graph.add_node("task_parser",       _node(taskparsergeneer.run))
    graph.add_node("repo_manager",      _node(repomanager.run))
    graph.add_node("repo_analyzer",     _node(repoanalyzegeneer.run))
    graph.add_node("code_writer",       _node(codegeneer.run))
    graph.add_node("code_reviewer",     _node(codereviewgeneer.run))
    graph.add_node("criteria_verifier", _node(criteriaverifiergeneer.run))
    graph.add_node("test_runner",       _node(testgeneer.run))
    graph.add_node("git_agent",         _node(gitgeneer.run))
    graph.add_node("pr_agent",          _node(prgeneer.run))
    graph.add_node("rollback_agent",     _node(rollbackgeneer.run))
    graph.add_node("report",            _node(reportgeneer.run))

    graph.set_entry_point("task_parser")

    for node, next_node in [
        ("task_parser",   "repo_manager"),
        ("repo_manager",  "repo_analyzer"),
        ("repo_analyzer", "code_writer"),
    ]:
        graph.add_conditional_edges(
            node, _route_after_node,
            {"continue": next_node, "report": "report"},
        )

    # code_writer → code_reviewer (always, even on failure → report)
    graph.add_conditional_edges(
        "code_writer", _route_after_node,
        {"continue": "code_reviewer", "report": "report"},
    )

    # code_reviewer → criteria_verifier (review never blocks the pipeline)
    graph.add_conditional_edges(
        "code_reviewer", _route_after_node,
        {"continue": "criteria_verifier", "report": "report"},
    )

    # criteria_verifier → test_runner, OR retry → code_writer (Evaluator-Optimizer)
    graph.add_conditional_edges(
        "criteria_verifier", _route_after_criteria,
        {"code_writer": "code_writer", "test_runner": "test_runner", "report": "report"},
    )

    graph.add_conditional_edges(
        "test_runner", _route_after_tests,
        {"git_agent": "git_agent", "report": "report"},
    )

    # git_agent → pr_agent (normal), or → report if git_agent itself failed
    # (nothing was pushed yet, so no rollback needed)
    graph.add_conditional_edges(
        "git_agent", _route_after_node,
        {"continue": "pr_agent", "report": "report"},
    )

    # pr_agent → report (success), or → rollback_agent (failed after push)
    graph.add_conditional_edges(
        "pr_agent", _route_after_node,
        {"continue": "report", "report": "rollback_agent"},
    )

    graph.add_edge("rollback_agent", "report")

    graph.add_edge("report", END)
    return graph.compile()


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(raw_task: dict[str, Any], trace_id: str = "") -> dict[str, Any]:
    from app.utils.logger import bind_trace
    if trace_id:
        bind_trace(trace_id)

    logger.info("pipeline.invoked", task_id=raw_task.get("taskId"), trace_id=trace_id)

    dry_run          = raw_task.pop("dry_run", False)
    require_approval = raw_task.pop("require_approval", False)
    approved         = raw_task.pop("approved", False)
    initial_state: dict[str, Any] = AgentState(
        raw_task=raw_task,
        dry_run=dry_run,
        require_approval=require_approval,
        approved=approved,
    ).model_dump(mode="json")

    graph = get_graph()
    final_state: dict[str, Any] = graph.invoke(initial_state)

    final_agent_state = _dict_to_state(final_state)

    from app.agents.reportgeneer import build_report
    from app.utils.logger import log_pipeline_summary
    report = build_report(final_agent_state)
    report["traceId"] = trace_id

    # Human-readable one-line summary
    log_pipeline_summary(
        logger,
        task_id=raw_task.get("taskId", "unknown"),
        status=final_agent_state.status.value,
        steps=final_agent_state.step_logs,
        pr_url=report.get("pullRequest", {}).get("url") if report.get("pullRequest") else None,
        total_ms=int((
            __import__("datetime").datetime.fromisoformat(
                final_agent_state.finished_at.replace("Z", "+00:00")
            ) -
            __import__("datetime").datetime.fromisoformat(
                final_agent_state.started_at.replace("Z", "+00:00")
            )
        ).total_seconds() * 1000) if final_agent_state.finished_at and final_agent_state.started_at else 0,
    )

    return report