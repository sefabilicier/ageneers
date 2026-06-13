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

from dataclasses import dataclass
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


@dataclass
class ReviewIssue:
    """A single issue found during code review."""
    category:    str   # security | correctness | quality | tests
    severity:    str   # critical | warning | info
    file:        str
    description: str


@dataclass
class CodeReview:
    """Result of the code review agent."""
    issues:  list[ReviewIssue]
    passed:  bool    # False if any critical issues found
    summary: str


@dataclass
class CriteriaResult:
    """Result of the acceptance criteria verifier."""
    results:           list[dict]   # raw per-criterion results
    all_satisfied:     bool
    unsatisfied_count: int
    retry_needed:      bool


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
    diff_preview: str = ""   # unified diff of all changes before PR

    # ── Pull Request ───────────────────────────────────────────────────────
    pull_request: PullRequest | None = None

    # ── Code review result (populated by code_reviewer node)
    code_review:          CodeReview | None = None
    # ── Criteria verification result
    criteria_result:      CriteriaResult | None = None
    criteria_retry_count: int = 0

    # ── Token usage tracking (populated by each LLM-calling agent)
    token_usage: dict[str, dict[str, int]] = {}  # {agent_name: {prompt: N, completion: N}}

    # ── Evaluator-optimizer state (criteria verifier feedback loop)
    criteria_feedback:     str  = ""    # feedback from criteria_verifier to code_writer
    criteria_retry_count:  int  = 0     # how many times criteria_verifier has retried
    criteria_all_passed:   bool = True  # last verifier result

    # ── Pipeline bookkeeping ───────────────────────────────────────────────
    dry_run: bool = False          # if True: skip push, PR creation, and commit
    require_approval: bool = False  # if True: pause before git push, wait for /approve
    approved: bool = False          # set to True by POST /api/tasks/{traceId}/approve
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