# AGENEERS

An autonomous AI agent that reads a task description, clones the target repository, implements the required code change using an LLM, runs the test suite, and opens a Pull Request — all in a single pipeline run.

---

## Table of Contents

- [Project Purpose](#project-purpose)
- [Technology Stack](#technology-stack)
- [AI Model](#ai-model)
- [Architecture](#architecture)
- [AI Agent Flow](#ai-agent-flow)
- [Security Approach](#security-approach)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [Running the Agent](#running-the-agent)
- [Example Task Payload](#example-task-payload)
- [Example Execution Report](#example-execution-report)
- [Test Failure Behaviour](#test-failure-behaviour)
- [Known Limitations](#known-limitations)
- [Production Improvements](#production-improvements)

---

## Project Purpose

Software teams use task management systems (Jira, GitHub Issues, Trello) to track development requests. This agent automates the journey from a written task to a reviewable Pull Request:

1. Receives a task via REST API
2. Parses repository URL, base branch, requirement, and acceptance criteria
3. Clones the repository into an isolated workspace
4. Analyses the codebase to identify relevant files
5. Uses Groq / Llama 3.3-70B to implement the required change
6. Runs the existing test suite
7. Opens a detailed Pull Request on GitHub

---

## Technology Stack

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python 3.11 + FastAPI | Richest AI/LLM ecosystem, async support |
| Agent Framework | LangGraph | State-machine graph, explicit control flow, fail-fast routing |
| AI Model | Groq + Llama 3.3-70B | Free tier, fast inference, strong coding ability |
| Git operations | GitPython | No shell=True — eliminates command injection risk |
| GitHub API | PyGithub | Safe PR creation, duplicate detection |
| Logging | structlog | Structured JSON logs, per-run trace IDs |
| Config | pyproject.toml + pydantic-settings | Modern Python packaging |
| Testing | pytest | 111 tests, all mocked — no real API calls needed |

---

## AI Model

**Provider:** [Groq](https://console.groq.com) (free tier)  
**Model:** `llama-3.3-70b-versatile`  
**Temperature:** 0 for parsing/analysis, 0.1 for code generation  
**Used in:** TaskParserGeneer, RepoAnalyzeGeneer (file ranking), CodeGeneer, TestGeneer (retry fix)

The model is accessed via `langchain-groq` and integrated into the LangGraph state machine as tool-calling nodes.

---

## Architecture

**Image-based**

<img width="124" height="150" alt="Image" src="https://github.com/user-attachments/assets/d10937de-b322-4039-9ad2-432a62c247b7" />

**Text-based**

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Application                       │
│   POST /api/tasks            GET /api/tasks/{traceId}/report     │
└───────────────────────────┬─────────────────────────────────────┘
                            │  BackgroundTask
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    LangGraph StateGraph                          │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐       │
│  │ TaskParser   │───▶│ RepoManager  │───▶│ RepoAnalyzer │       │
│  │ Geneer       │    │ Geneer       │    │ Geneer       │       │
│  └──────────────┘    └──────────────┘    └──────────────┘       │
│         │ FAILED           │ FAILED            │ FAILED          │
│         └──────────────────┴───────────────────┘                │
│                                                 │ RUNNING        │
│                                                 ▼                │
│                                        ┌──────────────┐         │
│                                        │  Code        │         │
│                                        │  Geneer      │         │
│                                        └──────┬───────┘         │
│                                               │ FAILED           │
│                                               ▼                  │
│                                        ┌──────────────┐         │
│                                        │  Test        │         │
│                                        │  Geneer      │         │
│                                        └──────┬───────┘         │
│                              PARTIAL/RUNNING  │  FAILED(block)  │
│                                        ┌──────▼───────┐         │
│                                        │  Git         │         │
│                                        │  Geneer      │         │
│                                        └──────┬───────┘         │
│                                               ▼                  │
│                                        ┌──────────────┐         │
│                                        │  PR          │         │
│                                        │  Geneer      │         │
│                                        └──────┬───────┘         │
│                                               ▼                  │
│                                        ┌──────────────┐         │
│                                        │  Report      │◀── ALL  │
│                                        │  Geneer      │  FAILED │
│                                        └──────────────┘         │
└─────────────────────────────────────────────────────────────────┘

┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  Groq / Llama    │    │    GitHub API     │    │  Workspace FS    │
│  (LLM calls)     │    │  (PR creation)   │    │  (cloned repos)  │
└──────────────────┘    └──────────────────┘    └──────────────────┘
```

### Folder Structure

```
ageneers/
├── app/
│   ├── api/
│   │   └── tasks.py                # POST /api/tasks, GET /api/tasks/{id}/report
│   ├── agents/
│   │   ├── taskparsergeneer.py     # Node 1 — LLM task parsing
│   │   ├── repomanager.py    # Node 2 — git clone, workspace isolation
│   │   ├── repoanalyzegeneer.py    # Node 3 — stack detection, file ranking
│   │   ├── codegeneer.py           # Node 4 — AI code change
│   │   ├── testgeneer.py           # Node 5 — test execution, retry
│   │   ├── gitgeneer.py            # Node 6 — branch, commit, push
│   │   ├── prgeneer.py             # Node 7 — GitHub PR creation
│   │   └── reportgeneer.py         # Node 8 — execution report
│   ├── graph/
│   │   └── pipeline.py             # LangGraph StateGraph definition
│   ├── models/
│   │   └── state.py                # AgentState (shared Pydantic model)
│   ├── security/
│   │   └── sanitizer.py            # Prompt injection + secret redaction
│   ├── utils/
│   │   └── logger.py               # structlog JSON logging
│   └── main.py                     # FastAPI app factory
├── tests/                          # 111 unit + integration tests
├── workspaces/                     # Cloned repos (git-ignored)
├── pyproject.toml
├── .env.example
└── README.md
```

---

## AI Agent Flow

The pipeline uses a **ReAct-inspired** (Reasoning + Acting) pattern implemented as a LangGraph StateGraph:

### Node responsibilities

| Node | Agent Type | LLM Used | Key Output |
|---|---|---|---|
| TaskParserGeneer | Perception | ✅ Yes | `ParsedTask` |
| repomanager | Action | ❌ No | `workspace_path` |
| RepoAnalyzeGeneer | Perception + Reasoning | ✅ Yes (file ranking) | `RepoAnalysis` |
| CodeGeneer | Reasoning + Action | ✅ Yes | `CodeChange`, files on disk |
| TestGeneer | Action (+ Reasoning on retry) | ✅ Retry only | `TestResult` |
| GitGeneer | Action | ❌ No | `feature_branch`, `commit_sha` |
| PRGeneer | Action | ❌ No | `PullRequest` |
| ReportGeneer | Output | ❌ No | Execution report JSON |

### Context management strategy

Large repositories are handled in three steps:

1. **File collection**: walk the repo, collect only source file paths (ignore `.git`, `node_modules`, `.venv`, `target/`, etc.)
2. **LLM file ranking**: send at most 60 file *paths* (not contents) to the LLM and ask it to select the top 10 most relevant
3. **Context budget**: when reading selected files for code generation, enforce `MAX_FILE_CHARS = 6000` per file and `MAX_CONTEXT_CHARS = 24000` total across all files

No file contents are sent to the LLM during analysis — only paths.

### Prompt injection defence

Source files could contain crafted comments intended to hijack the LLM. Defences:

- File contents are wrapped in `<file path="...">...</file>` XML tags so the model treats them as data, not instructions
- The system prompt explicitly instructs the model to ignore instructions inside source files
- User-supplied task descriptions are sanitised by `app/security/sanitizer.py` before reaching any LLM
- 13 injection patterns are checked; matches are replaced with `[INJECTION_ATTEMPT_REMOVED]`

---

## Security Approach

| Risk | Mitigation |
|---|---|
| Token leakage | `GITHUB_TOKEN` injected into git remote URL only for push duration, then immediately removed from `.git/config` |
| Command injection | GitPython used for all git ops (no `shell=True`). Test commands validated against an allowlist of safe base commands |
| Prompt injection | Sanitizer runs on all user input before LLM; source files XML-wrapped in prompts |
| Secret in source files | Sanitizer redacts GitHub PATs, AWS keys, API key patterns before sending to LLM |
| Path traversal | All LLM-generated file paths validated — `../` and absolute paths rejected; paths resolved and checked against workspace root |
| Repository allowlist | `REPO_ALLOWLIST` env var restricts which GitHub orgs/users can be cloned |
| Workspace isolation | Each task runs in `workspaces/<taskId>-<uuid8>/` — no cross-task contamination |
| Sensitive code exposure | Only `relevant_files` contents are sent to the LLM, within a strict token budget |

---

## Installation

```bash
# 1. Clone this repository
git clone https://github.com/your-username/ageneers.git
cd ageneers

# 2. Create virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -e ".[dev]"

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in GROQ_API_KEY and GITHUB_TOKEN
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | ✅ | — | Groq API key ([console.groq.com](https://console.groq.com)) |
| `GITHUB_TOKEN` | ✅ | — | GitHub PAT with `repo` and `workflow` scopes |
| `REPO_ALLOWLIST` | ⚠️ | *(open)* | Comma-separated GitHub owners allowed to be cloned |
| `WORKSPACE_BASE_DIR` | ❌ | `./workspaces` | Directory for cloned repos |
| `MAX_RETRY_COUNT` | ❌ | `2` | Max LLM fix retries on test failure |
| `TEST_FAILURE_MODE` | ❌ | `report` | `report` \| `retry` \| `block` (see below) |
| `TEST_TIMEOUT_SECONDS` | ❌ | `120` | Timeout for test command execution |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `LOG_FORMAT` | ❌ | `console` | `console` (coloured) \| `json` (structured) |
| `APP_HOST` | ❌ | `0.0.0.0` | FastAPI bind host |
| `APP_PORT` | ❌ | `8000` | FastAPI bind port |

---

## Running the Agent

```bash
# Start the server
python app/main.py

# Health check
curl http://localhost:8000/health

# Submit a task
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "taskId": "TASK-123",
    "title": "Add email validation to user registration API",
    "description": "Repository: https://github.com/your-org/user-service\nBranch: develop\n\nRequirement:\nAdd email format validation to the POST /users/register endpoint.\n\nAcceptance Criteria:\n- Invalid email returns HTTP 400\n- Error message: Invalid email format\n- Add or update unit tests"
  }'

# Response: {"traceId": "uuid-here", "taskId": "TASK-123", "status": "accepted"}

# Poll for result
curl http://localhost:8000/api/tasks/{traceId}/report
```

---

## Example Task Payload

```json
{
  "taskId": "TASK-123",
  "title": "Add email validation to user registration API",
  "description": "Repository: https://github.com/example-company/user-service\nBranch: develop\n\nRequirement:\nUser registration endpoint currently accepts invalid email formats. Add email format validation to the POST /users/register endpoint.\n\nAcceptance Criteria:\n- If email format is invalid, API should return HTTP 400\n- Error message should be: Invalid email format\n- Existing valid registration flow should continue working\n- Add or update unit tests"
}
```

---

## Example Execution Report

```json
{
  "traceId": "3f4a1b2c-...",
  "taskId": "TASK-123",
  "status": "success",
  "startedAt": "2025-06-01T10:00:00Z",
  "finishedAt": "2025-06-01T10:02:34Z",
  "error": null,
  "pipeline": {
    "steps": [
      {"step": "task_parser",   "status": "completed", "timestamp": "...", "detail": "repo=https://github.com/... branch=develop"},
      {"step": "repo_manager",  "status": "completed", "timestamp": "...", "detail": "workspace=/workspaces/TASK-123-a1b2c3d4"},
      {"step": "repo_analyzer", "status": "completed", "timestamp": "...", "detail": "lang=Python framework=FastAPI test_cmd=pytest"},
      {"step": "code_writer",   "status": "completed", "timestamp": "...", "detail": "changed=['app/users.py','tests/test_users.py']"},
      {"step": "test_runner",   "status": "completed", "timestamp": "...", "detail": "tests passed"},
      {"step": "git_agent",     "status": "completed", "timestamp": "...", "detail": "branch=ai-agent/TASK-123-... sha=deadbeef"},
      {"step": "pr_agent",      "status": "completed", "timestamp": "...", "detail": "PR #42: https://github.com/.../pull/42"},
      {"step": "report",        "status": "completed", "timestamp": "...", "detail": "status=success"}
    ]
  },
  "repository": {
    "url": "https://github.com/example-company/user-service",
    "baseBranch": "develop",
    "featureBranch": "ai-agent/TASK-123-add-email-validation-to-user-registrat",
    "commitSha": "deadbeef"
  },
  "analysis": {
    "language": "Python",
    "framework": "FastAPI",
    "buildTool": "pip/pyproject",
    "testCommand": "pytest",
    "relevantFiles": ["app/users.py", "tests/test_users.py"]
  },
  "codeChange": {
    "changedFiles": ["app/users.py", "tests/test_users.py"],
    "modelUsed": "llama-3.3-70b-versatile",
    "promptTokens": 1240,
    "completionTokens": 480
  },
  "testResult": {
    "status": "passed",
    "command": "pytest",
    "durationSeconds": 3.2,
    "retryCount": 0
  },
  "pullRequest": {
    "number": 42,
    "url": "https://github.com/example-company/user-service/pull/42",
    "title": "TASK-123 Add email validation to user registration API",
    "branch": "ai-agent/TASK-123-add-email-validation-to-user-registrat"
  }
}
```

---

## Test Failure Behaviour

Controlled via `TEST_FAILURE_MODE` environment variable:

| Mode | Behaviour | When to use |
|---|---|---|
| `report` *(default)* | PR is opened. PR description and execution report clearly show tests failed. Pipeline status = `partial`. | Dev/staging — always get a PR for human review |
| `retry` | On failure, test stdout/stderr is sent back to the LLM with the current file contents. The LLM attempts a fix and tests re-run. Repeats up to `MAX_RETRY_COUNT` times. If still failing, falls back to `report` mode. | When you trust the LLM to self-correct simple issues |
| `block` | Pipeline stops. No PR is opened. Execution report shows failure detail. | Production gates — PR only on green tests |

---

## Known Limitations

- **Private repositories**: supported via `GITHUB_TOKEN` in clone URL, but the token must have `repo` scope.
- **Large repositories**: context is capped at 24 000 chars across relevant files. Very large files are truncated. Deep monorepos may not have all relevant files identified.
- **Multi-file circular changes**: the LLM may miss dependencies between files not in the relevant list.
- **Non-standard test frameworks**: test command detection is rule-based; exotic setups may require manual `test_command` override.
- **Execution report persistence**: reports are stored in-memory. Server restart loses all pending reports. Production requires Redis or a database.
- **Concurrency**: no queue — many simultaneous tasks all run in FastAPI background threads. Under load, add a task queue (Celery, ARQ).
- **Binary files**: skipped silently during code change phase.

---

## Production Improvements

- [ ] Replace in-memory report store with Redis or PostgreSQL
- [ ] Add a task queue (Celery / ARQ) with worker pool
- [ ] Docker sandbox for test execution (prevent workspace escape)
- [ ] Jira / GitHub Issues / Trello webhook receivers
- [ ] Human approval step before PR is opened (dry-run mode)
- [ ] `REPO_ALLOWLIST` enforcement mandatory (currently optional)
- [ ] Rate limiting on `POST /api/tasks`
- [ ] Workspace cleanup scheduler (remove old workspaces)
- [ ] LLM cost tracking dashboard (token usage per task)
- [ ] Support for GitLab and Bitbucket providers
- [ ] Repository context indexing for very large codebases (vector search over files)
- [ ] Structured output mode for Groq (JSON schema enforcement)
- [ ] `dry_run=true` flag — show diff without committing
