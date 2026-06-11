"""
TestGeneer — Agent Node #5

Responsibility:
    Run the project's test suite after code changes and report the result.
    If tests fail, optionally ask the LLM to fix the code (retry loop).

Design decisions:

    1. Command execution safety
       - subprocess is used with a fixed arg list (no shell=True) to prevent
         command injection.
       - The test command comes from repo_analysis (rule-based detection),
         never directly from user input.
       - Execution is bounded by a timeout (TEST_TIMEOUT_SECONDS).
       - Working directory is always the isolated workspace.

    2. Test failure behaviour  (documented in README)
       Three configurable strategies, selected via env var TEST_FAILURE_MODE:
         a) "report"  — open PR but clearly mark tests as FAILED (default)
         b) "retry"   — ask LLM to fix using the error output, up to MAX_RETRY_COUNT
         c) "block"   — do not open PR if tests fail

    3. AI retry mechanism
       - On failure: test stdout/stderr is sent back to the LLM along with the
         current file contents so it can diagnose and fix.
       - Retry count is tracked in state (TestResult.retry_count).
       - After MAX_RETRY_COUNT exhausted, falls through to the chosen strategy.

    4. Output parsing
       - We parse test output for common patterns (PASSED/FAILED/ERROR counts)
         to produce a structured TestResult regardless of test framework.

LangGraph contract:
    Input  : AgentState  (reads workspace_path, repo_analysis, code_change)
    Output : dict        (sets test_result, status, step_logs)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.models.state import AgentState, CodeChange, PipelineStatus, TestResult, TestStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

TEST_TIMEOUT_SECONDS = int(os.getenv("TEST_TIMEOUT_SECONDS", "120"))
MAX_RETRY_COUNT      = int(os.getenv("MAX_RETRY_COUNT", "2"))
TEST_FAILURE_MODE    = os.getenv("TEST_FAILURE_MODE", "report")   # report | retry | block
MAX_OUTPUT_CHARS     = 4_000   # chars of test output sent to LLM on retry

# ─────────────────────────────────────────────────────────────────────────────
# LLM (lazy singleton — only instantiated on retry)
# ─────────────────────────────────────────────────────────────────────────────

_llm: ChatGroq | None = None

def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.1, max_tokens=4096)
    return _llm


# ─────────────────────────────────────────────────────────────────────────────
# Command builder — safe arg list, no shell interpolation
# ─────────────────────────────────────────────────────────────────────────────

def _build_command(test_command: str, workspace: str = "") -> list[str]:
    """
    Split the test command into a safe arg list.
    Only whitelisted base commands are accepted.

    For pytest: injects --rootdir and --override-ini flags so the test runner
    uses the cloned workspace, not the parent project's pyproject.toml.
    """
    allowed_bases = {
        "pytest", "python", "mvn", "gradle", "./gradlew",
        "npm", "npx", "yarn", "go", "cargo", "jest",
    }
    parts = test_command.strip().split()
    if not parts:
        raise ValueError("Empty test command")

    base = parts[0].lstrip("./")
    if base not in allowed_bases and parts[0] not in allowed_bases:
        raise ValueError(f"Test command '{parts[0]}' is not in the allowed list")

    # pytest: force rootdir to workspace and disable ini-file discovery
    # so the parent project's pyproject.toml does not interfere
    if parts[0] == "pytest" and workspace:
        parts = parts + [
            f"--rootdir={workspace}",
            "--override-ini=addopts=",   # clear any inherited addopts
        ]

    return parts


# ─────────────────────────────────────────────────────────────────────────────
# Test output parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_test_output(output: str, returncode: int) -> TestStatus:
    """Heuristic test status from combined stdout+stderr."""
    if returncode == 0:
        return TestStatus.PASSED

    lowered = output.lower()
    # pytest / jest / go test patterns
    fail_patterns = ["failed", "error", "assertion", "traceback", "build failure"]
    if any(p in lowered for p in fail_patterns):
        return TestStatus.FAILED

    # Non-zero return code with no recognisable output → treat as failed
    return TestStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# Core test runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_tests(command: list[str], workspace: Path) -> tuple[int, str, float]:
    """
    Execute the test command.
    Returns (returncode, combined_output, duration_seconds).
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
        output = result.stdout + "\n" + result.stderr
        return result.returncode, output.strip(), time.monotonic() - start

    except subprocess.TimeoutExpired:
        return 1, f"Test timed out after {TEST_TIMEOUT_SECONDS}s", time.monotonic() - start
    except FileNotFoundError as exc:
        return 1, f"Test command not found: {exc}", time.monotonic() - start


# ─────────────────────────────────────────────────────────────────────────────
# AI retry — ask LLM to fix failing tests
# ─────────────────────────────────────────────────────────────────────────────

_FIX_SYSTEM = """You are a senior software engineer fixing failing tests.
You will receive:
- The test error output
- The current content of changed files

Return ONLY a JSON array of fixed files:
[{"path": "relative/path.ext", "content": "full corrected content"}]
No markdown, no explanation."""


def _llm_fix_tests(
    error_output: str,
    workspace: Path,
    changed_files: list[str],
) -> list[dict[str, str]]:
    """Ask LLM to fix files based on test failure output."""
    import json

    file_block = ""
    for rel in changed_files:
        path = workspace / rel
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")[:3000]
            file_block += f'\n<file path="{rel}">\n{content}\n</file>\n'

    prompt = (
        f"Test failure output:\n{error_output[:MAX_OUTPUT_CHARS]}\n\n"
        f"Files to fix:\n{file_block}"
    )

    try:
        response = _get_llm().invoke([
            SystemMessage(content=_FIX_SYSTEM),
            HumanMessage(content=prompt),
        ])
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        fixes = json.loads(raw)
        if isinstance(fixes, list):
            return fixes
    except Exception as exc:
        logger.warning("test_runner.retry_llm_failed", error=str(exc))

    return []


def _apply_fixes(fixes: list[dict[str, str]], workspace: Path, allowed: set[str]) -> None:
    """Write LLM-suggested fixes to disk (same validation as CodeGeneer)."""
    for item in fixes:
        path = str(item.get("path", "")).strip()
        content = str(item.get("content", ""))
        if not path or ".." in path or path.startswith("/"):
            continue
        is_test = bool(re.search(r"(test_|_test\.|Test\.|\.test\.|\.spec\.)", path))
        if path not in allowed and not is_test:
            continue
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        logger.info("test_runner.fix_applied", path=path)


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node. Reads workspace + repo_analysis + code_change, writes test_result."""
    state.log_step("test_runner", "started")
    logger.info("test_runner.started", failure_mode=TEST_FAILURE_MODE)

    if not state.workspace_path or not state.repo_analysis:
        msg = "test_runner: missing workspace_path or repo_analysis"
        logger.error("test_runner.missing_input")
        state.log_step("test_runner", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    workspace    = Path(state.workspace_path)
    test_command = state.repo_analysis.test_command
    changed      = state.code_change.changed_files if state.code_change else []

    # Validate test command (security gate)
    try:
        cmd = _build_command(test_command, workspace=str(workspace))
    except ValueError as exc:
        msg = f"test_runner: {exc}"
        logger.error("test_runner.invalid_command", error=str(exc))
        state.log_step("test_runner", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    allowed = set(state.repo_analysis.relevant_files)
    retry_count = 0

    # ── Run loop (with optional AI retry) ────────────────────────────────
    while True:
        logger.info("test_runner.running", command=cmd, attempt=retry_count + 1)
        returncode, output, duration = _run_tests(cmd, workspace)
        status = _parse_test_output(output, returncode)

        logger.info(
            "test_runner.result",
            status=status,
            returncode=returncode,
            duration=f"{duration:.1f}s",
            output_preview=output[:200],
        )

        if status == TestStatus.PASSED:
            break

        # Tests failed — apply chosen strategy
        if TEST_FAILURE_MODE == "block":
            logger.warning("test_runner.blocked_on_failure")
            break

        if TEST_FAILURE_MODE == "retry" and retry_count < MAX_RETRY_COUNT:
            logger.info("test_runner.retrying", attempt=retry_count + 1)
            state.log_step("test_runner", "retrying", detail=f"attempt={retry_count + 1}")
            fixes = _llm_fix_tests(output, workspace, changed)
            if fixes:
                _apply_fixes(fixes, workspace, allowed)
            retry_count += 1
            continue

        # "report" mode or retries exhausted — break and continue pipeline
        break

    test_result = TestResult(
        status=status,
        command=" ".join(cmd),
        duration_seconds=round(duration, 2),
        output=output[:2000],   # cap stored output
        retry_count=retry_count,
    )

    # Determine pipeline status
    if status == TestStatus.PASSED:
        next_status = PipelineStatus.RUNNING
        state.log_step("test_runner", "completed", detail="tests passed")
    elif TEST_FAILURE_MODE == "block":
        next_status = PipelineStatus.FAILED
        state.log_step("test_runner", "failed", detail="tests failed — pipeline blocked")
    else:
        # report mode: continue to PR but flag failure
        next_status = PipelineStatus.PARTIAL
        state.log_step("test_runner", "completed",
                       detail="tests failed — continuing with PARTIAL status")

    return {
        "test_result": test_result,
        "status": next_status,
        "step_logs": state.step_logs,
    }