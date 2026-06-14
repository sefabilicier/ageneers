# ageneers backend — FastAPI + LangGraph pipeline
#
# Build:
#   docker build -t ageneers-backend .
#
# Run (standalone, without docker-compose):
#   docker run --rm -p 8000:8000 \
#     --env-file .env \
#     -v $(pwd)/workspaces:/app/workspaces \
#     -v $(pwd)/reports:/app/reports \
#     -v $(pwd)/logs:/app/logs \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     ageneers-backend
#
# The Docker socket mount is required only if USE_DOCKER_SANDBOX=true —
# it lets test_runner launch sandbox containers on the HOST's Docker daemon
# (Docker-out-of-Docker, not Docker-in-Docker).

FROM python:3.12-slim

# git is required by GitPython; curl is used by the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install the Docker CLI so test_runner can call `docker run ...` against
# the mounted host socket (USE_DOCKER_SANDBOX=true). This is the CLI only —
# it talks to whichever daemon /var/run/docker.sock points at.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]"

# Copy application code
COPY app/ ./app/

# Pre-create writable directories (also created at runtime if missing)
RUN mkdir -p workspaces reports logs

# Non-root user — but keep root-group ownership so the mounted docker.sock
# (typically root:docker on the host) remains usable when GID matches.
RUN useradd --create-home --shell /bin/bash ageneers \
    && chown -R ageneers:ageneers /app
USER ageneers

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# DOCKER_CMD=docker (not "wsl") — inside the container, docker CLI talks
# directly to the mounted socket; there's no WSL layer.
ENV DOCKER_CMD=docker \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    WORKSPACE_BASE_DIR=/app/workspaces \
    REPORTS_DIR=/app/reports \
    AUDIT_LOG_PATH=/app/logs/audit.log

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]