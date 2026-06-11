"""
RepoAnalyzeGeneer — Agent Node #3

Responsibility:
    Analyse the cloned repository to extract:
    - Programming language
    - Framework
    - Build tool
    - Test command
    - Relevant files (based on the task requirement)
    - Existing test files

Design decisions:
    - Two-phase approach:
        1. Rule-based detection (fast, zero LLM cost) — reads known config files
           (pom.xml, package.json, pyproject.toml, build.gradle, etc.)
        2. LLM-assisted relevance ranking — given the requirement text, the model
           selects which source files are most likely to need changes.
    - Context budget: we send at most MAX_FILES_TO_LLM file paths to the LLM.
      For large repos we pre-filter by directory depth and extension first.
    - No file *contents* are sent to the LLM at this stage — only paths.
      This keeps token usage low and avoids leaking sensitive source code.

LangGraph contract:
    Input  : AgentState  (reads workspace_path, parsed_task)
    Output : dict        (sets repo_analysis, status, step_logs)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.models.state import AgentState, PipelineStatus, RepoAnalysis
from app.utils.logger import get_logger

logger = get_logger(__name__)

MAX_FILES_TO_LLM = 60   # never send more than this many paths to the model

# ─────────────────────────────────────────────────────────────────────────────
# LLM (lazy singleton)
# ─────────────────────────────────────────────────────────────────────────────

_llm: ChatGroq | None = None

def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        _llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=1024)
    return _llm


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based detectors
# ─────────────────────────────────────────────────────────────────────────────

def _detect_stack(root: Path) -> dict[str, str]:
    """
    Inspect well-known config files and return language/framework/build/test.
    Returns partial dict — unknown fields stay empty string.
    """
    files = {f.name for f in root.iterdir() if f.is_file()}

    # ── Java ──────────────────────────────────────────────────────────────
    if "pom.xml" in files:
        return {
            "language": "Java",
            "framework": _detect_java_framework(root / "pom.xml"),
            "build_tool": "Maven",
            "test_command": "mvn test",
        }
    if "build.gradle" in files or "build.gradle.kts" in files:
        return {
            "language": "Java",
            "framework": "Spring Boot",
            "build_tool": "Gradle",
            "test_command": "./gradlew test",
        }

    # ── Python ────────────────────────────────────────────────────────────
    if "pyproject.toml" in files:
        content = (root / "pyproject.toml").read_text(errors="ignore")
        framework = "FastAPI" if "fastapi" in content.lower() else (
                    "Django"  if "django"  in content.lower() else
                    "Flask"   if "flask"   in content.lower() else "Python")
        test_cmd  = "pytest" if "pytest" in content.lower() else "python -m pytest"
        return {"language": "Python", "framework": framework,
                "build_tool": "pip/pyproject", "test_command": test_cmd}

    if "requirements.txt" in files:
        content = (root / "requirements.txt").read_text(errors="ignore").lower()
        framework = ("FastAPI" if "fastapi" in content else
                     "Django"  if "django"  in content else
                     "Flask"   if "flask"   in content else "Python")
        return {"language": "Python", "framework": framework,
                "build_tool": "pip", "test_command": "pytest"}

    if "setup.py" in files or "setup.cfg" in files:
        return {"language": "Python", "framework": "Python",
                "build_tool": "setuptools", "test_command": "pytest"}

    # ── Node.js / TypeScript ──────────────────────────────────────────────
    if "package.json" in files:
        content = (root / "package.json").read_text(errors="ignore")
        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        framework = ("NestJS"  if "@nestjs/core"  in deps else
                     "Express" if "express"        in deps else
                     "Next.js" if "next"           in deps else "Node.js")
        scripts   = pkg.get("scripts", {})
        test_cmd  = scripts.get("test", "npm test")
        return {"language": "TypeScript" if (root / "tsconfig.json").exists() else "JavaScript",
                "framework": framework, "build_tool": "npm",
                "test_command": test_cmd}

    # ── Go ────────────────────────────────────────────────────────────────
    if "go.mod" in files:
        return {"language": "Go", "framework": "Go",
                "build_tool": "go", "test_command": "go test ./..."}

    # ── Rust ─────────────────────────────────────────────────────────────
    if "Cargo.toml" in files:
        return {"language": "Rust", "framework": "Rust",
                "build_tool": "cargo", "test_command": "cargo test"}

    return {"language": "unknown", "framework": "unknown",
            "build_tool": "unknown", "test_command": "unknown"}


def _detect_java_framework(pom_path: Path) -> str:
    try:
        content = pom_path.read_text(errors="ignore").lower()
        if "spring-boot" in content:
            return "Spring Boot"
        if "quarkus" in content:
            return "Quarkus"
        if "micronaut" in content:
            return "Micronaut"
    except Exception:
        pass
    return "Java"


# ─────────────────────────────────────────────────────────────────────────────
# File collector
# ─────────────────────────────────────────────────────────────────────────────

_SOURCE_EXTENSIONS = {
    ".py", ".java", ".ts", ".js", ".go", ".rs", ".kt", ".scala",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h",
}
_TEST_PATTERNS = re.compile(
    r"(test_|_test\.|Test\.|\.test\.|\.spec\.|/tests?/|/test/)", re.I
)
_IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
                "dist", "build", "target", ".gradle", ".mvn"}


def _collect_source_files(root: Path) -> tuple[list[str], list[str]]:
    """Return (all_source_files, test_files) relative to root."""
    sources: list[str] = []
    tests:   list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in _SOURCE_EXTENSIONS:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fname), root)
            rel = rel.replace("\\", "/")
            if _TEST_PATTERNS.search(rel):
                tests.append(rel)
            else:
                sources.append(rel)

    return sources, tests


# ─────────────────────────────────────────────────────────────────────────────
# File relevance ranking — vector-first, LLM fallback
# ─────────────────────────────────────────────────────────────────────────────

_RANK_SYSTEM = """You are a code navigation assistant.
Given a task requirement and a list of file paths, return a JSON array of the
file paths most likely to need changes to fulfil the requirement.
Rules:
1. Return ONLY a JSON array of strings — no explanation, no markdown fences.
2. Include at most 10 paths.
3. Prefer files whose names suggest they handle the relevant feature.
4. Always include likely test files if they appear related."""


def _vector_rank_files(requirement: str, workspace: Path,
                        candidates: list[str]) -> list[str] | None:
    """
    Use ChromaDB + sentence-transformers to rank files by semantic similarity.
    Returns ranked file list, or None if vector deps are not installed.
    """
    try:
        from app.utils.vector_index import VectorIndex
        idx = VectorIndex()
        n = idx.build(workspace, candidates)
        if n == 0:
            return None
        return idx.query(requirement, top_k=10)
    except ImportError:
        logger.info("repo_analyzer.vector_deps_missing",
                    note="install chromadb sentence-transformers for semantic ranking")
        return None
    except Exception as exc:
        logger.warning("repo_analyzer.vector_rank_failed", error=str(exc))
        return None


def _llm_rank_files(requirement: str, candidates: list[str]) -> list[str]:
    """Ask the LLM which files are most relevant (fallback when vector search unavailable)."""
    if not candidates:
        return []
    prompt = (
        f"Requirement: {requirement}\n\n"
        f"File paths:\n" + "\n".join(candidates[:MAX_FILES_TO_LLM])
    )
    try:
        response = _get_llm().invoke([
            SystemMessage(content=_RANK_SYSTEM),
            HumanMessage(content=prompt),
        ])
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(p) for p in result if isinstance(p, str)]
    except Exception as exc:
        logger.warning("repo_analyzer.llm_rank_failed", error=str(exc))
    return candidates[:10]   # last-resort fallback


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph node entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(state: AgentState) -> dict[str, Any]:
    """LangGraph node. Reads workspace_path + parsed_task, writes repo_analysis."""
    state.log_step("repo_analyzer", "started")
    logger.info("repo_analyzer.started", workspace=state.workspace_path)

    if not state.workspace_path or not state.parsed_task:
        msg = "repo_analyzer: workspace_path or parsed_task missing"
        logger.error("repo_analyzer.missing_input")
        state.log_step("repo_analyzer", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    root = Path(state.workspace_path)
    if not root.exists():
        msg = f"Workspace directory does not exist: {root}"
        logger.error("repo_analyzer.workspace_missing", path=str(root))
        state.log_step("repo_analyzer", "failed", detail=msg)
        return {"status": PipelineStatus.FAILED, "error": msg, "step_logs": state.step_logs}

    # Phase 1: rule-based stack detection
    stack = _detect_stack(root)
    logger.info("repo_analyzer.stack_detected", **stack)

    # Phase 2: collect source + test files
    sources, test_files = _collect_source_files(root)
    logger.info("repo_analyzer.files_found",
                source_count=len(sources), test_count=len(test_files))

    # Phase 3: relevance ranking — vector search first, LLM as fallback
    requirement = state.parsed_task.requirement
    all_candidates = sources + test_files

    relevant = _vector_rank_files(requirement, root, all_candidates)
    if relevant is not None:
        logger.info("repo_analyzer.relevant_files",
                    method="vector", count=len(relevant), files=relevant)
    else:
        relevant = _llm_rank_files(requirement, all_candidates)
        logger.info("repo_analyzer.relevant_files",
                    method="llm_fallback", count=len(relevant), files=relevant)

    analysis = RepoAnalysis(
        language=stack.get("language", "unknown"),
        framework=stack.get("framework", "unknown"),
        build_tool=stack.get("build_tool", "unknown"),
        test_command=stack.get("test_command", "unknown"),
        relevant_files=relevant,
        existing_test_files=test_files,
        change_targets=relevant,
    )

    state.log_step(
        "repo_analyzer", "completed",
        detail=f"lang={analysis.language} framework={analysis.framework} "
               f"test_cmd={analysis.test_command} relevant={len(relevant)}",
    )

    return {
        "repo_analysis": analysis,
        "status": PipelineStatus.RUNNING,
        "step_logs": state.step_logs,
    }   