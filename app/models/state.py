"""
AgentState — the single shared state object that flows through every node
in the LangGraph pipeline.

Design principles:
- All fields are Optional so nodes can be added incrementally.
- Immutable-style: each node returns a *partial* dict; LangGraph merges it.
- Sensitive fields (tokens, secrets) are explicitly excluded from log output
  via the `safe_dict()` helper.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class PipelineStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    PARTIAL   = "partial"   # PR opened but tests failed


class TestStatus(str, Enum):
    PASSED  = "passed"
    FAILED  = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────────────────────

class ParsedTask(BaseModel):
    task_id: str
    repository_url: str
    base_branch: str
    requirement: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    title: str = ""


class RepoAnalysis(BaseModel):
    language: str = "unknown"
    framework: str = "unknown"
    build_tool: str = "unknown"
    test_command: str = "unknown"
    relevant_files: list[str] = Field(default_factory=list)
    existing_test_files: list[str] = Field(default_factory=list)
    change_targets: list[str] = Field(default_factory=list)


class CodeChange(BaseModel):
    changed_files: list[str] = Field(default_factory=list)
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class TestResult(BaseModel):
    status: TestStatus = TestStatus.UNKNOWN
    command: str = ""
    duration_seconds: float = 0.0
    output: str = ""
    retry_count: int = 0


class PullRequest(BaseModel):
    number: int = 0
    url: str = ""
    title: str = ""
    branch: str = ""


class StepLog(BaseModel):
    step: str
    status: str          # "started" | "completed" | "failed"
    timestamp: str
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Main AgentState
# ─────────────────────────────────────────────────────────────────────────────

class AgentState(BaseModel):
    # ── Input ──────────────────────────────────────────────────────────────
    raw_task: dict[str, Any] = Field(default_factory=dict)

    # ── Parsed task ────────────────────────────────────────────────────────
    parsed_task: ParsedTask | None = None

    # ── Workspace ──────────────────────────────────────────────────────────
    workspace_path: str = ""       # absolute path to cloned repo on disk
    repo_cloned: bool = False

    # ── Analysis ───────────────────────────────────────────────────────────
    repo_analysis: RepoAnalysis | None = None

    # ── Code change ────────────────────────────────────────────────────────
    code_change: CodeChange | None = None

    # ── Tests ──────────────────────────────────────────────────────────────
    test_result: TestResult | None = None

    # ── Git ────────────────────────────────────────────────────────────────
    feature_branch: str = ""
    commit_sha: str = ""

    # ── Pull Request ───────────────────────────────────────────────────────
    pull_request: PullRequest | None = None

    # ── Pipeline bookkeeping ───────────────────────────────────────────────
    status: PipelineStatus = PipelineStatus.PENDING
    error: str = ""                # last error message, if any
    step_logs: list[StepLog] = Field(default_factory=list)
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: str = ""

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────

    def log_step(self, step: str, status: str, detail: str = "") -> None:
        """Append a structured step log entry."""
        self.step_logs.append(
            StepLog(
                step=step,
                status=status,
                timestamp=datetime.now(timezone.utc).isoformat(),
                detail=detail,
            )
        )

    def safe_dict(self) -> dict[str, Any]:
        """Return a loggable dict with sensitive fields redacted."""
        data = self.model_dump()
        # Redact anything that might carry secrets from raw task description
        if "raw_task" in data:
            data["raw_task"] = {k: "***" if "token" in k.lower() or "secret" in k.lower()
                                else v for k, v in data["raw_task"].items()}
        return data