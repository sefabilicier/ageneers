"""
ageneers — FastAPI application entry point.

Startup sequence:
1. Load .env
2. Configure structured logging
3. Register middleware (API key auth)
4. Register API routers
5. Start background services (workspace cleanup)
6. Expose health check
"""

from __future__ import annotations

import os
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.utils.logger import configure_logging, get_logger

from dotenv import load_dotenv
import os

load_dotenv()


# ── Configure logging before anything else ───────────────────────────────────
configure_logging()
logger = get_logger(__name__)


# ── Lifespan (startup / shutdown hooks) ──────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    _validate_env()

    # Workspace cleanup scheduler
    from app.utils.workspace_cleanup import start_cleanup_scheduler
    start_cleanup_scheduler()

    # Graceful shutdown handler
    def _on_sigterm(signum, frame):
        from app.api.tasks import _running_tasks
        if _running_tasks:
            logger.warning(
                "shutdown.waiting_for_tasks",
                running_tasks=list(_running_tasks),
                hint="Server received SIGTERM — waiting for running tasks to finish",
            )
        else:
            logger.info("shutdown.clean", hint="No running tasks — shutting down immediately")

    signal.signal(signal.SIGTERM, _on_sigterm)

    api_key_enabled = bool(os.getenv("API_KEY", ""))
    logger.info("ageneers starting up", api_key_enabled=api_key_enabled)
    yield
    logger.info("ageneers shutting down")


# ── API Key middleware (defined at module level, registered in create_app) ───
class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Require X-API-Key header on all API routes.
    Disabled when API_KEY env var is empty (local dev).

    Open paths (no auth needed): /health, /docs, /openapi.json, /redoc
    """
    OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next):
        api_key = os.getenv("API_KEY", "")
        # CORS preflight requests never carry custom headers — let them through
        # so the browser can complete its preflight check before the real request.
        if request.method == "OPTIONS":
            return await call_next(request)
        if not api_key or request.url.path in self.OPEN_PATHS:
            return await call_next(request)
        key = request.headers.get("X-API-Key", "")
        if key != api_key:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "hint": "Provide X-API-Key header"},
            )
        return await call_next(request)


def _validate_env() -> None:
    """
    Check required environment variables at startup.
    Logs clear warnings so the operator knows what is missing
    before the first request hits a broken pipeline.
    """
    missing: list[str] = []
    warnings: list[str] = []

    if not os.getenv("GROQ_API_KEY"):
        missing.append("GROQ_API_KEY")

    if not os.getenv("GITHUB_TOKEN"):
        missing.append("GITHUB_TOKEN")

    if not os.getenv("REPO_ALLOWLIST"):
        warnings.append(
            "REPO_ALLOWLIST is not set — any GitHub repository can be cloned. "
            "Set REPO_ALLOWLIST=your-org to restrict access."
        )

    for w in warnings:
        logger.warning("config.warning", detail=w)

    if missing:
        for key in missing:
            logger.error(
                "config.missing_required_env",
                key=key,
                detail=(
                    f"{key} is not set. "
                    f"The pipeline will fail when this credential is needed. "
                    f"Set it in your .env file."
                ),
            )
        logger.warning(
            "config.incomplete",
            missing=missing,
            detail="Server started with missing credentials — some pipeline steps will fail.",
        )


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Development Agent",
        description=(
            "Turns a task description into a Pull Request: "
            "clone → analyse → AI code change → test → PR."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Middleware (must be added before first request, here in create_app) ──
    app.add_middleware(APIKeyMiddleware)

    # ── CORS — allow the frontend dev console to call the API ────────────────
    from fastapi.middleware.cors import CORSMiddleware
    cors_origins = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from app.api.tasks import _limiter
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.api.tasks import router as tasks_router
    from app.api.webhooks import router as webhooks_router
    from app.api.monitoring import router as monitoring_router

    app.include_router(tasks_router, prefix="/api")
    app.include_router(webhooks_router, prefix="/api")
    app.include_router(monitoring_router, prefix="/api")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "ageneers"})

    return app


app = create_app()


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
        reload_dirs=["app"],
        log_config=None,
    )