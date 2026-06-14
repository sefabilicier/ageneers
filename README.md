# ageneers

**ageneers** is a multi-agent system that helps developers turn a written task 
description into a reviewed, tested pull request æwithout touching the keyboard in 
between. Give it a repository, a requirement, and a list of acceptance
criteria, and it will clone the repo, understand the codebase, write the
code, review its own work, verify the result against your criteria, run the
tests, and open a PR with a full execution report attached.

It is built as a learning-by-doing exploration of the patterns described in
Anthropic's "Building Effective Agents" and "Effective Harnesses for
Long-Running Agents" engineering articles, combined with conventional SDLC,
DevOps, MLOps, and AgentOps practice. The goal was not just to make an agent
that works once, but to build the operational scaffolding around it —
logging, monitoring, rollback, sandboxing, evaluation loops — that separates
a demo from something you could actually run.

---

## Table of Contents

1. [Why this exists](#1-why-this-exists)
2. [Theoretical foundation](#2-theoretical-foundation)
3. [System architecture](#3-system-architecture)
4. [The pipeline — node by node](#4-the-pipeline--node-by-node)
5. [Agent patterns used](#5-agent-patterns-used)
6. [Ports and services](#6-ports-and-services)
7. [Project structure](#7-project-structure)
8. [Installation](#8-installation)
9. [Configuration reference](#9-configuration-reference)
10. [Running the system](#10-running-the-system)
11. [Feature guide and how to test each one](#11-feature-guide-and-how-to-test-each-one)
12. [The frontend — Pipeline Console](#12-the-frontend--pipeline-console)
13. [Execution report reference](#13-execution-report-reference)
14. [Test failure behaviour](#14-test-failure-behaviour)
15. [Security model](#15-security-model)
16. [Observability](#16-observability)
17. [Test suite](#17-test-suite)
18. [Known limitations](#18-known-limitations)
19. [Roadmap](#19-roadmap)

---

## 1. Why this exists

Software teams track work as tickets: "add email validation", "fix the
pagination bug", "add a retry to the payment client". Someone reads the
ticket, opens the repo, makes the change, runs the tests, and opens a PR.
That loop — read, understand, change, verify, ship — is exactly the loop
ageneers automates end to end.

The interesting engineering problem is not "can an LLM write code" — it can.
The problem is everything around that: how do you give the model just enough
context without drowning it or leaking your whole codebase, how do you stop
it from confidently shipping something that does not actually meet the
requirement, how do you recover when a step fails halfway through, and how
do you observe what fifty of these runs did overnight. ageneers is an attempt
to answer those questions concretely, in working code.

---

## 2. Theoretical foundation

### 2.1 Workflow vs. agent

Anthropic's framing draws a line between workflows (predefined code paths
that orchestrate LLM calls in a fixed sequence) and agents (systems where the
LLM dynamically directs its own process and tool use). ageneers is
deliberately built as a workflow — a fixed, inspectable graph of steps —
rather than a free-roaming agent that decides its own next action. The
guidance behind this choice is to start simple and only add agentic
complexity where it earns its keep; for a task with a known shape (parse,
analyse, write, review, verify, test, ship), a workflow gives the same
outcome with far more predictability, and every step is independently
testable and observable.

Within that workflow, individual nodes use LLM calls for the parts that
genuinely need judgement — parsing a free-text requirement, ranking files by
relevance, writing code, reviewing it, checking acceptance criteria — while
everything mechanical (cloning, branching, committing, opening a PR, deleting
a branch) is plain deterministic code. This is the augmented LLM pattern: the
model is one component wired into a larger system, not the system itself.

### 2.2 The reasoning loop

Every LLM-driven node in the pipeline follows the same shape, which maps onto
the classic Reason, Act, Observe, Adjust loop:

```
   ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌─────────────┐
   │  Reason  │ --> │   Act    │ --> │ Observe  │ --> │ Adjust /     │
   │ internal │     │ write    │     │ read     │     │ Decide       │
   │ thinking │     │ file,    │     │ result,  │     │ loop back to │
   │ (LLM)    │     │ run cmd, │     │ parse    │     │ Reason if    │
   │          │     │ API call │     │ output   │     │ needed       │
   └──────────┘     └──────────┘     └──────────┘     └──────┬──────┘
        ^                                                      │
        └──────────────────────────────────────────────────────┘
```

Concretely:

- `code_writer` reasons about the requirement and the relevant files, acts by
  writing files to disk, and the next node observes the result.
- `criteria_verifier` reasons about whether the written code satisfies the
  acceptance criteria (observe), and if not, adjusts by looping the pipeline
  back to `code_writer` for another attempt — a real implementation of the
  loop, not just a metaphor.

### 2.3 Evaluator-Optimizer

One specific workflow pattern is Evaluator-Optimizer: one LLM call generates
a response, and a second LLM call evaluates it against explicit criteria,
looping back to the generator if the evaluation fails. This is the most
direct fix for the most common LLM coding failure mode: the model believes it
solved the problem, but subtly changed the contract (for example, returning
HTTP 422 instead of the required 400). A single-pass system has no way to
catch this; ageneers' `code_writer -> code_reviewer -> criteria_verifier`
sequence, with a bounded retry loop back to `code_writer`, is a direct
implementation of this pattern (see section 5.3).

### 2.4 Long-running agent harnesses

A second line of guidance focuses on what happens when an agentic task runs
longer than a single context window or a single session: context gets lost,
progress needs to be externally legible, and failures need to be recoverable
without a human watching. ageneers borrows three ideas from this directly:

- External progress record — every pipeline run produces a persisted
  execution report (`reports/<traceId>.json`) and a structured audit trail
  (`logs/audit.log`), so the state of a run survives a server restart and can
  be inspected without re-running anything.
- Incremental, bounded steps — each node does one well-scoped thing and hands
  off a typed state object; the Evaluator-Optimizer retry loop is capped
  (`CRITERIA_MAX_RETRIES`, default 2) rather than allowed to spin.
- Recoverable failure — if the pipeline fails after a branch has been pushed
  but before a PR is opened, the `rollback_agent` cleans up the dangling
  branch automatically rather than leaving a half-finished trace on the
  remote (see section 5.5).

These principles also align with general SDLC best practice: clear stage
boundaries, automated testing gates, audit trails, and rollback strategies
are exactly what mature software delivery pipelines look like — ageneers
applies the same discipline to a pipeline whose "developer" happens to be an
LLM.

---

## 3. System architecture

### 3.1 High-level overview

```
+--------------------------------------------------------------------------+
|                      Pipeline Console (frontend)                          |
|             React + Vite -- http://localhost:5173                         |
|  chatbot intake -> POST /api/tasks -> poll /timeline -> render live flow  |
+------------------------------------+--------------------------------------+
                                      | HTTP (CORS-enabled)
                                      v
+--------------------------------------------------------------------------+
|                     FastAPI Application -- :8000                          |
|                                                                            |
|  Middleware:  API Key auth -> CORS -> Rate limiting (slowapi)             |
|                                                                            |
|  Routers:                                                                 |
|   /api/tasks     submit, approve, fetch report                           |
|   /api/webhooks  GitHub Issue -> task trigger                             |
|   /api           metrics, audit, prompts, admin/cleanup                  |
|                                                                            |
|  Background services:                                                    |
|   - workspace cleanup scheduler (daemon thread)                          |
|   - graceful shutdown handler (SIGTERM)                                  |
+------------------------------------+--------------------------------------+
                                      | BackgroundTask
                                      v
+--------------------------------------------------------------------------+
|                  LangGraph StateGraph (12 nodes)                          |
|               see section 4 for the full node-by-node flow               |
+-----------+------------------------+------------------------+-------------+
            |                        |                        |
            v                        v                        v
+--------------------+   +---------------------+   +-------------------------+
|  Groq API          |   |  GitHub API          |   |  Workspace FS +          |
|  (Llama 3.3-70B)    |   |  (PyGithub, via      |   |  Docker Sandbox          |
|  LLM calls          |   |   GitProvider        |   |  (WSL/Linux, test        |
|                     |   |   abstraction)       |   |   isolation)             |
+--------------------+   +---------------------+   +-------------------------+
```

### 3.2 Folder structure
Here we have end-to-end folder structure of the system we have built. By the way, for the title of the agents, I designed as **{the_doer_agent} + geneer**. For examle code generator agent is a Test Engineer that writes codes. That is why it is titles as **codegeneer**


```
ageneers/
├── app/
│   ├── main.py                       FastAPI app factory, middleware, startup
│   ├── api/
│   │   ├── tasks.py                  POST/GET /api/tasks -- submission & reports
│   │   ├── webhooks.py               GitHub Issue webhook receiver
│   │   └── monitoring.py             /api/metrics, /api/audit, /api/prompts, etc.
│   ├── agents/                       one file per pipeline node
│   │   ├── taskparsergeneer.py       Node 1  -- parse task -> ParsedTask
│   │   ├── repomanagergeneer.py      Node 2  -- clone repo into workspace
│   │   ├── repoanalyzegeneer.py      Node 3  -- detect stack, rank relevant files
│   │   ├── codegeneer.py             Node 4  -- LLM writes the code change
│   │   ├── codereviewgeneer.py       Node 5  -- LLM reviews the diff
│   │   ├── criteriaverifiergeneer.py Node 6  -- LLM checks acceptance criteria
│   │   ├── testgeneer.py             Node 7  -- run tests (host or Docker sandbox)
│   │   ├── gitgeneer.py              Node 8  -- branch, commit, push
│   │   ├── prgeneer.py               Node 9  -- open the Pull Request
│   │   ├── rollbackgeneer.py         Node 10 -- clean up on post-push failure
│   │   └── reportgeneer.py           Node 11 -- build the execution report
│   ├── graph/
│   │   └── pipeline.py               LangGraph StateGraph wiring + routing
│   ├── models/
│   │   └── state.py                  AgentState + all typed sub-models
│   ├── prompts/                      versioned system prompts (see section 11.6)
│   │   ├── __init__.py               load_prompt(), version resolution
│   │   ├── task_parser_v1.txt
│   │   ├── code_writer_v1.txt
│   │   ├── code_reviewer_v1.txt
│   │   └── criteria_verifier_v1.txt
│   ├── providers/
│   │   └── git_provider.py           GitProvider abstraction (GitHub today)
│   ├── security/
│   │   └── sanitizer.py              prompt-injection & secret redaction
│   └── utils/
│       ├── logger.py                 structlog setup, log_step, pipeline summary
│       ├── audit.py                  append-only NDJSON audit trail
│       ├── docker_sandbox.py         isolated test execution via Docker/WSL
│       ├── workspace_cleanup.py      scheduled deletion of old workspaces
│       └── vector_index.py           embedding-based relevant-file search
├── tests/                            120 tests, fully mocked
├── workspaces/                       cloned repos (git-ignored, auto-cleaned)
├── reports/                          persisted execution reports (JSON)
├── logs/
│   └── audit.log                     append-only audit trail (NDJSON)
├── ageneers-ui/                      React frontend -- Pipeline Console
├── .env.example
└── README.md
```

---

## 4. The pipeline — node by node

The pipeline is a LangGraph `StateGraph` with 12 nodes (11 always present,
plus a conditional rollback node). Every node receives the shared
`AgentState` object, does its work, and returns a partial state update.
Routing between nodes is driven by `state.status` (`RUNNING`, `FAILED`,
`PARTIAL`, `SUCCESS`) and, for the Evaluator-Optimizer loop, by
`criteria_result.retry_needed`.

```
 ┌────────────┐   ┌─────────────┐   ┌──────────────┐
 │ task_parser│──▶│ repo_manager│──▶│ repo_analyzer│
 └─────┬──────┘   └──────┬──────┘   └──────┬───────┘
       │ FAILED          │ FAILED          │ FAILED
       └─────────────────┴────────┬────────┘
                                   │ RUNNING
                                   ▼
                          ┌──────────────┐
                          │ code_writer  │◀─────────────┐
                          └──────┬───────┘              │
                                  │                       │ retry_needed
                                  ▼                       │ (max 2x)
                          ┌──────────────┐              │
                          │ code_reviewer│              │
                          └──────┬───────┘              │
                                  │                       │
                                  ▼                       │
                       ┌────────────────────┐           │
                       │ criteria_verifier  │───────────┘
                       └─────────┬──────────┘
                                  │ all satisfied / retries exhausted
                                  ▼
                          ┌──────────────┐
                          │ test_runner  │
                          └──────┬───────┘
                     FAILED ◀────┤ PARTIAL / RUNNING
                          │       ▼
                          │  ┌──────────────┐
                          │  │  git_agent   │
                          │  └──────┬───────┘
                          │   FAILED│  continue
                          │         ▼
                          │  ┌──────────────┐
                          │  │  pr_agent    │
                          │  └──────┬───────┘
                          │  continue│  FAILED (after push)
                          │         │         ▼
                          │         │  ┌──────────────────┐
                          │         │  │ rollback_agent   │
                          │         │  └────────┬─────────┘
                          ▼         ▼            ▼
                          ┌────────────────────────┐
                          │        report           │
                          └────────────────────────┘
```

### Node responsibilities

| # | Node | Type | LLM? | Output |
|---|---|---|---|---|
| 1 | `task_parser` | Perception | Yes | `ParsedTask` (repo URL, branch, requirement, criteria) |
| 2 | `repo_manager` | Action | No | Cloned workspace at `workspaces/<taskId>-<uuid8>/` |
| 3 | `repo_analyzer` | Perception + Reasoning | Yes (file ranking) | `RepoAnalysis` (stack, test command, relevant files) |
| 4 | `code_writer` | Reasoning + Action | Yes | `CodeChange`, files written to disk |
| 5 | `code_reviewer` | Reasoning (evaluator) | Yes | `CodeReview` (issues, pass/fail, summary) |
| 6 | `criteria_verifier` | Reasoning (evaluator) | Yes | `CriteriaResult` (per-criterion satisfied/reason, retry flag) |
| 7 | `test_runner` | Action | Retry-fix only | `TestResult` (status, output, retry count) |
| 8 | `git_agent` | Action | No | `feature_branch`, `commit_sha`, diff preview |
| 9 | `pr_agent` | Action | No | `PullRequest` (number, url, title) |
| 10 | `rollback_agent` | Action (conditional) | No | `RollbackResult` |
| 11 | `report` | Output | No | The execution report JSON |

### Context management strategy

Large repositories cannot simply be dumped into a prompt. ageneers handles
this in layers:

1. **File collection** — walk the repo, collect source file paths, ignoring
   `.git`, `node_modules`, `.venv`, `target/`, build artefacts, etc.
2. **Vector-based relevance ranking** — `app/utils/vector_index.py` embeds
   file chunks with `sentence-transformers/all-MiniLM-L6-v2` and ranks files
   by cosine similarity to the requirement text, falling back to an
   LLM-based file-path ranking if the embedding step is unavailable.
3. **Context budget** — when reading the selected files for code generation
   and review, `MAX_FILE_CHARS` (default 6000) caps any single file and
   `MAX_CONTEXT_CHARS` (default 24000) caps the total across all files.

This mirrors the "right altitude" principle: give the model enough signal
to do the job, structured clearly, without flooding the context window with
irrelevant code.

---

## 5. Agent patterns used

### 5.1 Augmented LLM (every LLM-calling node)

Each LLM call is wrapped: a fixed system prompt (loaded from
`app/prompts/`, versioned — see section 11.6), a constructed user message
with exactly the context that node needs, and a parser that turns the raw
response into a typed Python object. The LLM never has open-ended tool
access; its "tools" are the deterministic Python functions that consume its
output (write a file, flag an issue, mark a criterion satisfied).

### 5.2 Prompt chaining (the linear backbone)

`task_parser -> repo_manager -> repo_analyzer -> code_writer -> ... -> report`
is a prompt chain: the output of one step is structured input for the next.
Each step is simple and verifiable in isolation, which is why the test suite
can mock each node independently and still exercise the full graph.

### 5.3 Evaluator-Optimizer

```
                ┌─────────────┐
   requirement ─▶│ code_writer │──┐
                ┌▶└─────────────┘  │ diff + files
                │                  ▼
                │           ┌───────────────┐
                │           │ code_reviewer │ (non-blocking — reports issues)
                │           └───────┬───────┘
                │                   ▼
                │         ┌───────────────────┐
                │         │ criteria_verifier  │
                │         └─────────┬──────────┘
                │      not satisfied │ satisfied
                │     (retry < 2)    │
                └────────────────────┘
                                     ▼
                               test_runner
```

`code_reviewer` is the "quality" evaluator (security, correctness, dead code,
test coverage) — it never blocks the pipeline, but its findings feed into the
quality score and the final report. `criteria_verifier` is the "contract"
evaluator — it checks each acceptance criterion against the actual changed
files and, if any criterion is not satisfied, sends the pipeline back to
`code_writer` with the gap identified. The loop is capped by
`CRITERIA_MAX_RETRIES` (default 2) so a stubborn mismatch degrades to a
`partial` result with a clear report rather than looping forever.

### 5.4 Routing (implicit, via status)

After every node, `_route_after_node` inspects `state.status`. A `FAILED`
status short-circuits straight to `report` from any node — this is the
"fail fast, report clearly" routing rule that runs through the whole graph.
`test_runner` has its own router (`_route_after_tests`) because a failing
test suite is not necessarily a pipeline failure — depending on
`TEST_FAILURE_MODE`, it can still proceed to open a PR with a `partial`
status (see section 14).

### 5.5 Rollback on partial failure

```
git_agent (branch created, committed, pushed)  --- SUCCESS
        │
        ▼
   pr_agent  --- FAILED (e.g. GitHub API error)
        │
        ▼
 rollback_agent
   - GitProvider.delete_branch(repo, branch)
   - audit("branch.deleted", reason=..., success=...)
        │
        ▼
     report (status=failed, "rollback": {...})
```

If `git_agent` itself fails, nothing was pushed, so `rollback_agent` is
skipped entirely (`report.rollback.performed = false`). Rollback only
removes the remote branch; the local workspace is left for the cleanup
scheduler (section 11.3) and for debugging.

### 5.6 Git provider abstraction

All git-hosting interaction goes through `app.providers.git_provider.GitProvider`,
an abstract interface implemented today by `GitHubProvider` (via PyGithub).
Agents call `get_repo_slug`, `branch_exists`, `create_pull_request`,
`get_open_pull_request`, and `delete_branch` — never the GitHub SDK directly.
Adding GitLab or Bitbucket support means implementing this one interface; no
agent code changes.
## 6. Ports and services

| Port | Service | Purpose |
|---|---|---|
| `8000` | FastAPI backend (`app/main.py`) | REST API — task submission, reports, monitoring, webhooks. Configurable via `APP_HOST` / `APP_PORT`. |
| `5173` | Pipeline Console frontend (`ageneers-ui`, Vite dev server) | Visual console — chatbot task intake and live pipeline flow. |
| n/a | Groq API (`api.groq.com`) | External — LLM inference (`llama-3.3-70b-versatile`) |
| n/a | GitHub API (`api.github.com`) | External — repository clone, PR creation, branch deletion |
| n/a | Docker daemon (via WSL on Windows) | Optional — isolated test execution sandbox |

The backend and frontend are two separate processes. The frontend talks to
the backend over HTTP with CORS enabled (`CORS_ALLOW_ORIGINS`, default
`http://localhost:5173`).

---

## 7. Project structure

See section 3.2 for the annotated tree. The short version: `app/agents/`
holds one file per pipeline node, `app/graph/pipeline.py` wires them
together, `app/api/` exposes everything over HTTP, and `app/utils/` +
`app/security/` hold the cross-cutting concerns (logging, audit, sandboxing,
cleanup, prompt-injection defence).

---

## 8. Installation

### Backend

```bash
git clone <this-repo> ageneers
cd ageneers

python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env
# edit .env — fill in GROQ_API_KEY and GITHUB_TOKEN at minimum
```

### Frontend (Pipeline Console)

```bash
cd ageneers-ui
npm install
cp .env.example .env
# edit .env if the backend isn't on localhost:8000, or if API_KEY is set
```

### Optional: Docker sandbox (test isolation)

If you want tests to run inside a Docker container instead of directly on
the host (recommended — see section 11.4), make sure Docker is available:

```bash
# Windows (via WSL)
wsl docker --version
wsl docker run --rm hello-world

# Linux / macOS
docker --version
```

Then set `USE_DOCKER_SANDBOX=true` in `.env`.

### Running the full stack with Docker

The entire system — backend and frontend — can also run as two containers
via Docker Compose, without installing Python or Node locally.

**Layout** — `docker-compose.yml` lives in the backend root and expects the
frontend project as a subdirectory:

```
ageneers/
├── docker-compose.yml
├── Dockerfile
├── .dockerignore
├── .env                  <- create this (GROQ_API_KEY, GITHUB_TOKEN, ...)
├── app/
└── ageneers-ui/          <- frontend project goes here
    ├── Dockerfile
    ├── nginx.conf
    └── .dockerignore
```

If the frontend currently lives in its own directory, move it in first:

```bash
mv ageneers-ui ageneers/ageneers-ui
```

**Build and run** — on Windows, run this from inside WSL so
`/var/run/docker.sock` exists for the sandbox mount:

```bash
cd ageneers
docker compose up --build
```

This starts:

| Service | URL | Image |
|---|---|---|
| `backend` | http://localhost:8000 | `ageneers-backend` — FastAPI + LangGraph pipeline |
| `frontend` | http://localhost:5173 | `ageneers-frontend` — Vite build served by nginx |

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

**What the backend container includes** —

- Python 3.12-slim base, with `git` (required by GitPython) and the Docker
  CLI installed.
- Source code copied in; `workspaces/`, `reports/`, and `logs/` are bind-mounted
  to the host so pipeline output survives container restarts.
- A healthcheck on `/health` (30s interval).

**Docker-out-of-Docker for the test sandbox** — if `USE_DOCKER_SANDBOX=true`,
`test_runner` needs to launch sandbox containers. The compose file mounts the
host's `/var/run/docker.sock` into the backend container and sets
`DOCKER_CMD=docker` (overriding any `wsl` value from `.env`), so the
container's Docker CLI talks directly to the host daemon — sandbox containers
run as siblings of the backend container, not nested inside it.

**Frontend build-time configuration** — `VITE_API_BASE_URL` is baked into the
frontend bundle at build time (a Vite constraint, not a runtime env var). To
point the frontend at a different backend URL, edit the `args:` block under
the `frontend` service in `docker-compose.yml` and rebuild:

```yaml
frontend:
  build:
    args:
      VITE_API_BASE_URL: http://your-backend-host:8000
```

**Stopping and rebuilding** —

```bash
docker compose down              # stop both containers
docker compose up --build         # rebuild after a code change
docker compose logs -f backend    # tail backend logs
```

---

## 9. Configuration reference

All configuration is via environment variables (`.env`, loaded at startup).

### Required

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | Groq API key — get one at [console.groq.com](https://console.groq.com) |
| `GITHUB_TOKEN` | GitHub personal access token with `repo` and `workflow` scopes |

### Repository access control

| Variable | Default | Description |
|---|---|---|
| `REPO_ALLOWLIST` | *(open)* | Comma-separated GitHub owners allowed to be cloned |
| `REPO_DENYLIST` | *(empty)* | Comma-separated GitHub owners always blocked (checked first) |
| `WORKSPACE_BASE_DIR` | `./workspaces` | Where cloned repos live |

### Pipeline behaviour

| Variable | Default | Description |
|---|---|---|
| `MAX_RETRY_COUNT` | `2` | Max LLM fix retries on test failure (`TEST_FAILURE_MODE=retry`) |
| `TEST_FAILURE_MODE` | `report` | `report` \| `retry` \| `block` — see section 14 |
| `TEST_TIMEOUT_SECONDS` | `120` | Timeout for test command execution |
| `CRITERIA_MAX_RETRIES` | `2` | Max Evaluator-Optimizer retries when criteria are not met |
| `PROMPT_VERSION_code_writer` | *(auto)* | Pin a specific prompt version, or auto-select the highest |
| `PROMPT_VERSION_task_parser` | *(auto)* | Same, for the task parser prompt |

### Docker sandbox

| Variable | Default | Description |
|---|---|---|
| `USE_DOCKER_SANDBOX` | `false` | Run tests inside a Docker container |
| `DOCKER_SANDBOX_IMAGE` | `python:3.12-slim` | Image used for the sandbox |
| `DOCKER_SANDBOX_MEMORY` | `512m` | Memory limit per test run |
| `DOCKER_SANDBOX_CPUS` | `1` | CPU limit per test run |
| `DOCKER_SANDBOX_TIMEOUT` | `120` | Seconds before the container is killed |
| `DOCKER_CMD` | `wsl` | `wsl` on Windows, `docker` on native Linux/macOS |

### Security and access

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(empty)* | If set, all `/api/*` routes require `X-API-Key` header (disabled if empty) |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | Comma-separated allowed origins for the frontend |
| `RATE_LIMIT_TASKS` | `20/minute` | Rate limit on `POST /api/tasks` per IP |
| `MAX_CONCURRENT_TASKS` | `5` | Max pipeline runs in flight at once (429 beyond this) |

### Workspace and audit

| Variable | Default | Description |
|---|---|---|
| `WORKSPACE_MAX_AGE_HOURS` | `24` | Delete workspaces older than this |
| `WORKSPACE_CLEANUP_INTERVAL_HOURS` | `6` | How often the cleanup scheduler runs |
| `AUDIT_LOG_PATH` | `./logs/audit.log` | Append-only audit trail location |
| `REPORTS_DIR` | `./reports` | Where execution reports are persisted as JSON |

### GitHub webhook (optional)

| Variable | Default | Description |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret configured in GitHub repo webhook settings |
| `AI_AGENT_LABEL` | `ai-agent` | Issue label that triggers the pipeline |

### Logging and server

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `LOG_FORMAT` | `console` | `console` (coloured, local dev) \| `json` (structured, production) |
| `APP_HOST` | `0.0.0.0` | FastAPI bind host |
| `APP_PORT` | `8000` | FastAPI bind port |
---

## 10. Running the system

### Start the backend

```bash
python app/main.py
```

This starts FastAPI on `http://localhost:8000` with auto-reload, starts the
workspace cleanup scheduler as a background thread, and registers a SIGTERM
handler for graceful shutdown.

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

### Start the frontend

```bash
cd ageneers-ui
npm run dev
```

Open the printed URL (default `http://localhost:5173`).

### Submit your first task (API only)

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "taskId": "TASK-123",
    "title": "Add email validation to user registration API",
    "description": "Repository: https://github.com/your-org/user-service\nBranch: main\n\nRequirement:\nAdd email format validation to the POST /users/register endpoint.\n\nAcceptance Criteria:\n- Invalid email returns HTTP 400\n- Error message: Invalid email format\n- Add or update unit tests"
  }'
```

Response:

```json
{"traceId": "uuid-here", "taskId": "TASK-123", "status": "accepted"}
```

Poll for the result:

```bash
curl http://localhost:8000/api/tasks/{traceId}/report
```

While the pipeline is running, `/report` returns `202` with
`{"status": "running"}`. Once finished, it returns the full execution report
(section 13).

---

## 11. Feature guide and how to test each one

This section walks through every feature in the system, what it does, why
it's there, and exactly how to exercise it.

### 11.1 Core pipeline: task to PR

**What it does** — the 12-node LangGraph pipeline described in section 4.
Given a task description, it produces a Pull Request with the requested
change, a test run, a code review, and an acceptance-criteria check.

**How to test** —

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "taskId": "TASK-001",
    "title": "Add email validation",
    "description": "Repository: https://github.com/<you>/<repo>\nBranch: main\n\nRequirement:\nAdd email format validation to POST /users/register.\n\nAcceptance Criteria:\n- Invalid email returns HTTP 400\n- Error message: Invalid email format\n- Add or update unit tests"
  }'
```

Then poll `/api/tasks/{traceId}/report` until `status` is no longer
`running`. A successful run ends with a `pullRequest` object containing a
real GitHub PR URL.

### 11.2 Live pipeline timeline

**What it does** — `GET /api/tasks/{traceId}/timeline` returns a per-step
breakdown: which steps ran, how long each took, and which step was slowest.
This is what the frontend polls every 1.5 seconds to animate the live flow.

**How to test** —

```bash
curl http://localhost:8000/api/tasks/{traceId}/timeline
```

```json
{
  "traceId": "...",
  "taskId": "TASK-001",
  "status": "success",
  "total_ms": 47568,
  "slowest_step": "test_runner",
  "slowest_ms": 22600,
  "timeline": [
    {"step": "task_parser", "status": "completed", "duration_ms": 1530, "detail": "repo=... branch=main"},
    {"step": "code_writer", "status": "completed", "duration_ms": 1780, "detail": "changed=[...]"}
  ]
}
```

While the pipeline is still running, this returns HTTP `202` with
`{"status": "running"}` so the frontend knows to keep polling.

### 11.3 Workspace isolation and automatic cleanup

**What it does** — every task clones into its own
`workspaces/<taskId>-<uuid8>/` directory, so concurrent tasks never collide.
A daemon thread wakes up every `WORKSPACE_CLEANUP_INTERVAL_HOURS` (default 6)
and deletes any workspace older than `WORKSPACE_MAX_AGE_HOURS` (default 24).

**How to test** —

```bash
# Trigger cleanup immediately instead of waiting for the schedule
curl -X POST http://localhost:8000/api/admin/cleanup
```

```json
{"status": "completed", "deleted": 2, "kept": 1, "errors": 0}
```

Check the logs for `workspace_cleanup.scheduler_started` at startup and
`workspace_cleanup.deleted` / `workspace_cleanup.summary` entries after a run.

### 11.4 Docker sandbox for test execution

**What it does** — when `USE_DOCKER_SANDBOX=true`, `test_runner` runs the
test command inside a throwaway Docker container instead of directly on the
host:

```
docker run --rm --network=none --memory=512m --cpus=1
  -v <workspace>:/workspace --workdir /workspace
  python:3.12-slim
  sh -c "pip install pytest ... && cd /workspace && python -m pytest ..."
```

This means LLM-generated test code that does something unexpected (infinite
loop, excessive memory, attempted network access) is contained: no network,
capped memory and CPU, and the container is destroyed immediately after the
run. If Docker is unreachable, `test_runner` logs a warning and transparently
falls back to running the tests on the host — the pipeline never crashes
because of sandbox unavailability.

**How to test** —

```bash
# Verify Docker is reachable (Windows via WSL)
wsl docker info

# In .env:
USE_DOCKER_SANDBOX=true
DOCKER_CMD=wsl

# Submit a task as in 11.1, then check the logs for:
#   test_runner.using_sandbox   mode=docker
#   docker_sandbox.starting     image=python:3.12-slim ...
#   docker_sandbox.completed    returncode=0 status=passed
```

If Docker is not running, you'll instead see:

```
test_runner.sandbox_unavailable   hint=Docker not reachable -- falling back to host
test_runner.using_host            mode=host
```

### 11.5 Dry run and human approval gate

**What it does** — `POST /api/tasks?dry_run=true` runs the full pipeline
(clone, analyse, write code, review, verify, test) but stops before pushing
or opening a PR — useful for previewing what the agent would do.
`POST /api/tasks?require_approval=true` pauses the pipeline immediately
before the push step; the run only continues once a human calls
`POST /api/tasks/{traceId}/approve`.

**How to test** —

```bash
# Preview only — no branch, no PR
curl -X POST "http://localhost:8000/api/tasks?dry_run=true" \
  -H "Content-Type: application/json" -d '{ ... }'

# Pause before push
curl -X POST "http://localhost:8000/api/tasks?require_approval=true" \
  -H "Content-Type: application/json" -d '{ ... }'

# Inspect the diff in the report, then approve:
curl -X POST http://localhost:8000/api/tasks/{traceId}/approve
```
### 11.6 Versioned system prompts

**What it does** — every LLM-driven node loads its system prompt from
`app/prompts/<agent>_v<N>.txt` via `load_prompt()`. By default the highest
numbered version is used; you can pin a specific version with
`PROMPT_VERSION_<agent>=<N>` in `.env`. This means you can iterate on a
prompt (create `code_writer_v2.txt`), compare results, and roll back without
touching code.

**How to test** —

```bash
curl http://localhost:8000/api/prompts
```

```json
{
  "prompts": {
    "code_writer":  {"active_version": 1, "available_versions": [1], "env_override": null},
    "task_parser":  {"active_version": 1, "available_versions": [1], "env_override": null}
  }
}
```

To try a new version: copy `app/prompts/code_writer_v1.txt` to
`code_writer_v2.txt`, edit it, then either leave it (auto-selects v2 as the
highest) or pin explicitly:

```env
PROMPT_VERSION_code_writer=2
```

The active prompt version is also logged on every `code_writer.completed`
event and recorded in the execution report.

### 11.7 Code review agent (non-blocking evaluator)

**What it does** — after `code_writer` finishes, `code_reviewer` sends the
changed files to the LLM with a review prompt covering four categories:
security, correctness, quality, and tests. It returns a list of issues with
severity (`critical` / `warning` / `info`). This step never blocks the
pipeline — it's a second opinion, not a gate — but its findings directly
affect the quality score (section 11.9) and appear in the final report.

**How to test** — run any task as in 11.1, then check the report's
`codeReview` section:

```json
"codeReview": {
  "passed": false,
  "summary": "1 issue(s) found (1 critical)",
  "issues": [
    {
      "category": "correctness",
      "severity": "critical",
      "file": "app/routes.py",
      "description": "Returns 422 instead of the required 400 for invalid email"
    }
  ]
}
```

### 11.8 Acceptance criteria verifier and retry loop

**What it does** — `criteria_verifier` checks each acceptance criterion from
the task against the actual changed files and reports `satisfied: true/false`
with a reason for each. If any criterion fails and the retry budget
(`CRITERIA_MAX_RETRIES`, default 2) isn't exhausted, the pipeline routes back
to `code_writer` with that context — this is the Evaluator-Optimizer loop
from section 5.3 in action.

**How to test** — the report's `criteriaVerification` section shows the
outcome:

```json
"criteriaVerification": {
  "allSatisfied": true,
  "unsatisfiedCount": 0,
  "retryCount": 1,
  "results": [
    {"criterion": "Invalid email returns HTTP 400", "satisfied": true, "reason": "..."},
    {"criterion": "Add or update unit tests", "satisfied": true, "reason": "..."}
  ]
}
```

A `retryCount` greater than zero means the loop fired at least once — check
the logs for `criteria_verifier.unmet_criterion` followed by a second
`code_writer.started` to see the retry happen live.

### 11.9 Quality score

**What it does** — `reportgeneer` computes a 0-100 score with a letter grade
(A/B/C/F) for every run, combining:

| Component | Points |
|---|---|
| Code was generated | 30 |
| Tests passed | 30 |
| PR created | 20 |
| Diff generated | 10 |
| No retries needed | 10 |
| Code review clean | +5 bonus |
| Code review issues | -10 per critical, -3 per warning |

**How to test** — every report includes:

```json
"qualityScore": {
  "total": 90,
  "grade": "A",
  "breakdown": {
    "code_generated": {"points": 30, "detail": "3 files"},
    "tests_passed":   {"points": 30, "detail": "all tests green"},
    "pr_created":     {"points": 20, "detail": "https://github.com/.../pull/42"},
    "diff_generated": {"points": 10, "detail": "diff available"},
    "no_retries":     {"points": 10, "detail": "succeeded on first attempt"},
    "review_clean":   {"points": 5,  "detail": "no review issues"}
  }
}
```

The aggregate `avg_quality_score` across all runs is in `GET /api/metrics`.

### 11.10 Rollback on partial failure

**What it does** — see section 5.5. If `git_agent` succeeds (branch pushed)
but `pr_agent` fails, `rollback_agent` deletes the remote branch and records
why.

**How to test** — this is hard to trigger deliberately without breaking
something on purpose (e.g. revoking the GitHub token's PR-creation scope
mid-run, or pointing `GITHUB_TOKEN` at a repo where PR creation is blocked
after push succeeds). When it does fire, the report shows:

```json
"rollback": {
  "performed": true,
  "branch": "ai-agent/TASK-123-add-email-validation",
  "reason": "GitHub API error: 422",
  "success": true
}
```

and `logs/audit.log` gets a `branch.deleted` entry. The unit tests in
`tests/test_rollbackgeneer.py` exercise all four paths (no branch to roll
back, successful delete, failed delete, provider exception) without needing
a real failure.

### 11.11 GitHub Issue webhook

**What it does** — `POST /api/webhooks/github` lets a GitHub Issue trigger
the pipeline directly: when an issue is opened or labeled with `ai-agent`
(configurable via `AI_AGENT_LABEL`), the issue body is parsed the same way as
a manual task description, and the pipeline runs automatically.

**How to test** —

1. In your GitHub repo, go to **Settings → Webhooks → Add webhook**.
2. Payload URL: `https://<your-server>/api/webhooks/github`
3. Content type: `application/json`
4. Secret: match `GITHUB_WEBHOOK_SECRET` in `.env`
5. Events: select "Issues"
6. Open an issue with a body containing `Repository:`, `Branch:`,
   `Requirement:`, and `Acceptance Criteria:` sections (same format as the
   manual task description), and add the `ai-agent` label.

The webhook responds `202` immediately and the pipeline runs in the
background, same as a manual `POST /api/tasks`.
### 11.12 Security: API key auth, rate limiting, concurrency limits

**What it does** —

- **API key**: if `API_KEY` is set, every `/api/*` route (except `/health`,
  `/docs`, `/openapi.json`, `/redoc`) requires an `X-API-Key` header matching
  it. CORS preflight (`OPTIONS`) requests are always allowed through so the
  browser can complete its preflight check.
- **Rate limiting**: `POST /api/tasks` is limited to `RATE_LIMIT_TASKS`
  (default 20/minute) per client IP via `slowapi`.
- **Concurrency limit**: if `MAX_CONCURRENT_TASKS` (default 5) pipelines are
  already running, new submissions get `429 Too Many Requests` with a message
  telling the caller to wait.

**How to test** —

```bash
# With API_KEY set in .env:
curl -X POST http://localhost:8000/api/tasks -d '{...}'
# -> 401 {"error": "Unauthorized", "hint": "Provide X-API-Key header"}

curl -X POST http://localhost:8000/api/tasks \
  -H "X-API-Key: <your-key>" -H "Content-Type: application/json" -d '{...}'
# -> 202 accepted

# Rate limit: send 21 requests in under a minute -> the 21st gets 429

# Concurrency: submit 6 tasks back to back with MAX_CONCURRENT_TASKS=5
# -> the 6th gets 429 "Too many concurrent tasks (5/5)"
```

### 11.13 Repository allowlist / denylist and prompt injection defence

**What it does** — before cloning, `repo_manager` checks the repository
owner against `REPO_DENYLIST` (checked first) and `REPO_ALLOWLIST` (if set,
only listed owners are permitted). Separately, `app/security/sanitizer.py`
scans task descriptions for prompt-injection patterns (instruction override
attempts, role hijacking, jailbreak markers) and redacts anything that looks
like a credential (API keys, tokens) before it reaches the LLM.

**How to test** —

```env
REPO_ALLOWLIST=your-username
REPO_DENYLIST=some-untrusted-org
```

Submit a task pointing at a repo owned by `some-untrusted-org` — `repo_manager`
fails immediately with `repo_manager.url_blocked` and a hint to update the
allowlist/denylist. Submit a task description containing
`"ignore all previous instructions"` — the sanitizer flags it before the
parsed task is sent to `code_writer`.

### 11.14 Audit trail

**What it does** — `app/utils/audit.py` appends one JSON object per line to
`logs/audit.log` for every significant event: `task.received`, `pr.created`,
`pipeline.finished`, `branch.deleted`. This is separate from the structured
application logs — it's a minimal, append-only record specifically for
"what happened and when", suitable for compliance review or just answering
"did this PR actually come from the agent".

**How to test** —

```bash
curl http://localhost:8000/api/audit
curl http://localhost:8000/api/audit?limit=10
cat logs/audit.log
```

```json
{"ts": "2026-06-13T20:30:18Z", "event": "pr.created", "task_id": "TASK-67584", "pr_number": 39, "pr_url": "https://github.com/.../pull/39", "branch": "ai-agent/TASK-67584-..."}
```

### 11.15 Structured logging

**What it does** — `app/utils/logger.py` configures `structlog` with a
`severity` field (GCP/Datadog convention), `trace_id` threaded through every
log line via context variables, and a `log_step()` context manager that
automatically logs `<node>.started`, `<node>.completed` (with `duration_ms`),
or `<node>.failed` (with `error` and a `hint` for what to do about it).
`LOG_FORMAT=console` gives coloured human-readable output for local dev;
`LOG_FORMAT=json` gives newline-delimited JSON for log aggregators.

At the end of every run, `pipeline.summary` logs one line with the overall
status, step count, total duration, and PR URL — the single line you'd grep
for to know what happened.

**How to test** — run a task and watch the console output, or set
`LOG_FORMAT=json` and pipe through `jq`:

```bash
python app/main.py | jq 'select(.event == "pipeline.summary")'
```

```json
{"event": "pipeline.summary", "task_id": "TASK-67584", "status": "success", "steps_ok": "9/8", "duration_ms": 47568, "pr_url": "https://github.com/.../pull/39"}
```

### 11.16 Monitoring endpoints

**What it does** — `app/api/monitoring.py` exposes:

| Endpoint | Purpose |
|---|---|
| `GET /api/metrics` | Aggregate counters: success rate, average duration, token usage, average quality score |
| `GET /api/metrics/prometheus` | Same data in Prometheus text exposition format |
| `GET /api/tasks?limit=N` | Recent runs (newest first) |
| `GET /api/tasks/{traceId}/timeline` | Per-step timing breakdown (section 11.2) |
| `GET /api/prompts` | Active prompt versions (section 11.6) |
| `GET /api/audit?limit=N` | Recent audit trail entries (section 11.14) |
| `POST /api/admin/cleanup` | Trigger workspace cleanup immediately (section 11.3) |

All counters are in-memory and reset on server restart — for production,
back this with Redis or a database (see section 19).

**How to test** —

```bash
curl http://localhost:8000/api/metrics
```

```json
{
  "pipeline": {
    "tasks_total": 5, "tasks_success": 4, "tasks_failed": 1,
    "success_rate_pct": 80.0, "avg_duration_ms": 41200, "avg_quality_score": 84
  },
  "llm": {
    "prompt_tokens_total": 4625, "completion_tokens_total": 1610,
    "total_tokens": 6235, "avg_tokens_per_task": 1247
  }
}
```

```bash
curl http://localhost:8000/api/metrics/prometheus
```

```
ai_dev_agent_tasks_total 5
ai_dev_agent_tasks_success_total 4
ai_dev_agent_success_rate 0.8
```

### 11.17 Token usage tracking

**What it does** — every LLM call records `prompt_tokens` /
`completion_tokens` into `state.token_usage[<agent_name>]`. The execution
report includes a `tokenUsage` breakdown per agent, and `/api/metrics`
aggregates this into running totals and an average per task — the basic
building block of LLM cost monitoring.

**How to test** — check the `tokenUsage` field in any report:

```json
"tokenUsage": {
  "code_writer": {"prompt": 925, "completion": 321, "total": 1246}
}
```
---

## 12. The frontend — Pipeline Console

The `ageneers-ui` React app is a real-time console for watching the pipeline
run, built around the same 12-node graph described in section 4.

### 12.1 Design

- Layout — three columns: task history (left), live pipeline flow (center),
  chatbot-style task intake (right).
- Palette — a dark "operations room" theme (#0a0d12 background) with amber
  (#f0b429) for "running", green for success, red for failure, and purple for
  an Evaluator-Optimizer retry in progress.
- Typography — Space Grotesk for headers and agent names, Inter for UI text,
  JetBrains Mono for logs, IDs, and terminal-style detail lines.
- Signature element — each pipeline node renders as a "terminal card" that
  pulses with an amber glow while running, and shows the real detail string
  from the backend's timeline underneath it (for example
  changed=['app/routes.py', 'app/validators.py']).

### 12.2 How it works

1. The right panel asks five questions one at a time — title, repository,
   branch, requirement, and acceptance criteria — chatbot-style.
2. On "Launch pipeline", it POSTs to /api/tasks and starts polling
   /api/tasks/{traceId}/timeline every 1.5 seconds.
3. The center panel renders all 12 nodes. Each is idle until it appears in
   the timeline, then running (amber pulse), then success / failed / skipped
   / retrying based on the step status. rollback_agent only appears in the
   flow if it actually ran.
4. When the pipeline finishes, the full execution report is fetched and a
   summary card shows the quality score and grade, test result, code review
   issues, acceptance criteria results, any rollback, and a link to the
   opened PR.
5. The left panel shows recent runs (/api/tasks) and aggregate metrics
   (/api/metrics); clicking a past run reloads its report.

### 12.3 Running it

```bash
cd ageneers-ui
npm install
cp .env.example .env
npm run dev
```

By default it expects the backend at http://localhost:8000 and itself runs
on http://localhost:5173. If API_KEY is set on the backend, set the same
value as VITE_API_KEY in the frontend's .env so requests include the
X-API-Key header. The backend's CORS_ALLOW_ORIGINS must include the
frontend's origin (default already does).
---

## 13. Execution report reference

GET /api/tasks/{traceId}/report returns this shape once the pipeline
finishes (while running, it returns 202 with {"status": "running"}):

```json
{
  "traceId": "bdf19224-...",
  "taskId": "TASK-23475",
  "status": "success",
  "startedAt": "2026-06-13T17:18:16Z",
  "finishedAt": "2026-06-13T17:19:01Z",
  "error": null,
  "pipeline": {
    "steps": [
      {"step": "task_parser",       "status": "completed", "timestamp": "...", "detail": "repo=... branch=main"},
      {"step": "repo_manager",      "status": "completed", "timestamp": "...", "detail": "workspace=..."},
      {"step": "repo_analyzer",     "status": "completed", "timestamp": "...", "detail": "lang=Python framework=FastAPI test_cmd=pytest relevant=5"},
      {"step": "code_writer",       "status": "completed", "timestamp": "...", "detail": "changed=[...] model=llama-3.3-70b-versatile"},
      {"step": "code_reviewer",     "status": "completed", "timestamp": "...", "detail": "0 issues, 0 critical"},
      {"step": "criteria_verifier", "status": "completed", "timestamp": "...", "detail": "3/3 criteria met"},
      {"step": "test_runner",       "status": "completed", "timestamp": "...", "detail": "tests passed"},
      {"step": "git_agent",         "status": "completed", "timestamp": "...", "detail": "branch=ai-agent/TASK-23475-... sha=..."},
      {"step": "pr_agent",          "status": "completed", "timestamp": "...", "detail": "PR #36: https://github.com/.../pull/36"},
      {"step": "report",            "status": "completed", "timestamp": "...", "detail": "status=success"}
    ]
  },
  "repository": {
    "url": "https://github.com/<owner>/<repo>",
    "baseBranch": "main",
    "featureBranch": "ai-agent/TASK-23475-add-email-validation-to-user-registration-api",
    "commitSha": "45123c32",
    "diffPreview": "diff --git a/app/routes.py b/app/routes.py\n..."
  },
  "analysis": {
    "language": "Python",
    "framework": "FastAPI",
    "buildTool": "pip",
    "testCommand": "pytest",
    "relevantFiles": ["app/routes.py", "tests/test_users.py", "app/validators.py", "app/models.py", "app/main.py"]
  },
  "codeChange": {
    "changedFiles": ["app/routes.py", "app/validators.py", "tests/test_users.py"],
    "modelUsed": "llama-3.3-70b-versatile",
    "promptTokens": 925,
    "completionTokens": 321
  },
  "tokenUsage": {
    "code_writer": {"prompt": 925, "completion": 321, "total": 1246}
  },
  "testResult": {
    "status": "passed",
    "command": "pytest --rootdir=... --override-ini=addopts=",
    "durationSeconds": 21.92,
    "retryCount": 0
  },
  "pullRequest": {
    "number": 36,
    "url": "https://github.com/<owner>/<repo>/pull/36",
    "title": "TASK-23475 Add email validation to user registration API",
    "branch": "ai-agent/TASK-23475-add-email-validation-to-user-registration-api"
  },
  "codeReview": {
    "passed": true,
    "summary": "No issues found",
    "issues": []
  },
  "criteriaVerification": {
    "allSatisfied": true,
    "unsatisfiedCount": 0,
    "retryCount": 0,
    "results": [
      {"criterion": "Invalid email returns HTTP 400", "satisfied": true, "reason": "..."},
      {"criterion": "Error message: Invalid email format", "satisfied": true, "reason": "..."},
      {"criterion": "Add or update unit tests", "satisfied": true, "reason": "..."}
    ]
  },
  "qualityScore": {
    "total": 95,
    "grade": "A",
    "breakdown": {
      "code_generated":  {"points": 30, "detail": "3 files"},
      "tests_passed":    {"points": 30, "detail": "all tests green"},
      "pr_created":      {"points": 20, "detail": "https://github.com/.../pull/36"},
      "diff_generated":  {"points": 10, "detail": "diff available"},
      "no_retries":      {"points": 10, "detail": "succeeded on first attempt"},
      "review_clean":    {"points": 5,  "detail": "no review issues"}
    }
  },
  "rollback": null
}
```

---

## 14. Test failure behaviour

Controlled by TEST_FAILURE_MODE:

| Mode | Behaviour | When to use |
|---|---|---|
| report (default) | The PR is opened anyway. The PR description and execution report clearly show the tests failed; pipeline status becomes partial. | Development / staging -- always get a PR for human review, with the test status visible |
| retry | On failure, the test output is sent back to code_writer along with the current file contents; the LLM attempts a fix and tests re-run, up to MAX_RETRY_COUNT times. If still failing, falls back to report. | When you trust the LLM to self-correct simple issues |
| block | The pipeline stops. No PR is opened. The execution report shows the failure detail. | Production gates -- a PR should only exist if tests are green |

Note this is a separate retry mechanism from the Evaluator-Optimizer loop in
section 5.3: that loop fires before tests run (when acceptance criteria
aren't met), while TEST_FAILURE_MODE=retry fires after tests run.

---

## 15. Security model

| Concern | Mitigation |
|---|---|
| Credential storage | GITHUB_TOKEN / GROQ_API_KEY read from .env (git-ignored), masked as *** in logs |
| Command injection | All git operations via GitPython (no shell=True); test commands run as argument lists |
| Prompt injection | app/security/sanitizer.py scans task text for override/jailbreak patterns before it reaches any LLM |
| Secret leakage to LLM | Sanitizer redacts API-key-shaped strings before sending file contents to the model |
| Repository access control | REPO_ALLOWLIST / REPO_DENYLIST, checked before clone |
| Workspace isolation | One UUID-suffixed directory per task; never shared |
| Test execution isolation | Optional Docker sandbox: no network, capped CPU/memory, ephemeral container (section 11.4) |
| API authentication | Optional X-API-Key header via API_KEY (section 11.12) |
| Abuse / overload | Rate limiting (RATE_LIMIT_TASKS) and concurrency cap (MAX_CONCURRENT_TASKS) |
| Output validation | Every LLM-proposed file change is checked against the workspace root -- no path traversal, no writes outside the clone |
| Auditability | Append-only logs/audit.log for task receipt, PR creation, and rollbacks |

---

## 16. Observability

The three layers from sections 11.15-11.17, summarised:

1. Structured logs -- every node logs started / completed (with
   duration_ms) / failed (with error + hint); one pipeline.summary line per
   run.
2. Metrics -- /api/metrics (JSON) and /api/metrics/prometheus (Prometheus
   text format): success rate, average duration, token usage, average
   quality score.
3. Per-run detail -- /api/tasks/{traceId}/timeline (step durations, slowest
   step) and /api/tasks/{traceId}/report (full execution report, persisted to
   reports/<traceId>.json).
4. Audit trail -- /api/audit and logs/audit.log, an append-only NDJSON record
   of task receipts, PR creations, and rollbacks.

---

## 17. Test suite

120 tests, fully mocked (no real Groq or GitHub calls):

```bash
pytest tests/ -q
```

| File | Tests | Covers |
|---|---|---|
| test_taskparsergeneer.py | 21 | Task parsing, validation, error handling |
| test_codegeneer.py | 19 | Code generation, XML/JSON parsing, validation |
| test_prgeneer.py | 19 | PR creation, duplicate detection, provider errors |
| test_testgeneer.py | 18 | Test execution, retry logic, sandbox fallback |
| test_repoanalyzegeneer.py | 13 | Stack detection, file ranking |
| test_gitgeneer.py | 10 | Branching, committing, pushing |
| test_repomanagergeneer.py | 9 | Cloning, allowlist/denylist, workspace setup |
| test_pipeline.py | 7 | Full graph wiring, routing, fail-fast paths |
| test_rollbackgeneer.py | 4 | Rollback skip/success/failure/exception paths |

---

## 18. Known limitations

- Private repositories are supported via GITHUB_TOKEN in the clone URL, but
  the token needs repo scope.
- Very large repositories: context is capped at MAX_CONTEXT_CHARS (default
  24,000 chars) across relevant files; very large files are truncated, and
  deep monorepos may not surface every relevant file.
- Cross-file dependencies not in the relevant-files set can be missed by the
  LLM.
- Non-standard test frameworks: test command detection is rule-based --
  exotic setups may need a manual override.
- In-memory metrics and reports: counters reset on restart; reports are
  persisted to disk but the metrics aggregation is not.
- No task queue: concurrent tasks run as FastAPI background tasks, capped by
  MAX_CONCURRENT_TASKS -- under heavier load, a real queue (Celery, ARQ)
  would be needed.
- Binary files are skipped during code generation.
- Single git provider: only GitHub is implemented today, though the
  GitProvider abstraction (section 5.6) makes adding others straightforward.

---

## 19. Roadmap

- [ ] Model fallback chain (Groq -> OpenAI -> local model) for resilience
      against provider outages
- [ ] Replace in-memory metrics/report store with Redis or PostgreSQL
- [ ] Task queue (Celery / ARQ) with a worker pool for true horizontal scaling
- [ ] GitLab and Bitbucket GitProvider implementations
- [ ] Structured output mode for the LLM (JSON schema enforcement) instead of
      the current XML/JSON parsing fallback chain
- [ ] Distributed tracing (OpenTelemetry) across LLM calls and pipeline steps
- [ ] Alerting on slow or failing runs (Slack/email on pipeline.slow_warning)