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
