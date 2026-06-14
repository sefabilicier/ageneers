# ageneers — Pipeline Console (Frontend)

A real-time visual console for the ageneers LangGraph pipeline. Watch each
of the 12 pipeline agents light up as they run, and submit new tasks through
a chatbot-style intake form.

## Design

- Layout: three-column console — task history (left), live pipeline flow
  (center), chatbot task intake (right).
- Palette: graphite/operations-room dark theme (#0a0d12 background) with
  an amber accent (#f0b429) for "running" state, green for success, red for
  failure, purple for retries.
- Typography: Space Grotesk (headers, agent names), Inter (UI text),
  JetBrains Mono (logs, IDs, terminal output).
- Signature element: each pipeline node is a "terminal card" that pulses
  with an amber glow while running, and shows the real log detail
  (e.g. changed_files=[...]) from the backend underneath it.

## Setup

```bash
npm install
cp .env.example .env
# edit .env if your backend isn't on localhost:8000, or if API_KEY is set
npm run dev
```

Open the printed local URL (default http://localhost:5173).

## Backend requirements

This UI talks to the FastAPI backend in D:\ageneers via:

- POST /api/tasks                       — submit a new task
- GET  /api/tasks/{traceId}/timeline     — poll pipeline progress (every 1.5s)
- GET  /api/tasks/{traceId}/report       — full execution report
- GET  /api/tasks                        — recent task list (left sidebar)
- GET  /api/metrics                      — success rate / quality score

Make sure the backend is running (python app/main.py) and CORS allows
http://localhost:5173. If CORS isn't configured yet, add this to app/main.py:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

If API_KEY is set in the backend's .env, set the same value in this
project's .env as VITE_API_KEY so requests include the X-API-Key header.

## How it works

1. The right panel asks five questions (title, repository, branch, requirement,
   acceptance criteria) one at a time, chatbot-style.
2. On "Launch pipeline", it POSTs to /api/tasks and starts polling
   /api/tasks/{traceId}/timeline every 1.5s.
3. The center panel renders all 12 pipeline nodes. Each node is idle until
   its entry appears in the timeline, then becomes running (amber pulse),
   then success / failed / skipped / retrying based on the step status.
   The rollback_agent node only appears if it actually ran.
4. When the pipeline finishes, the full execution report is fetched and a
   summary card shows the quality score, test result, code review issues,
   criteria verification, and a link to the opened PR.
5. The left panel shows recent runs (from /api/tasks) and aggregate metrics
   (from /api/metrics). Clicking a past run loads its report.

## Build

```bash
npm run build
```

Output goes to dist/ — serve with any static file server.
