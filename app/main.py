"""
ageneers — FastAPI application entry point.

Startup sequence:
1. Load .env
2. Configure structured logging
3. Register API routers
4. Expose health check
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.utils.logger import configure_logging, get_logger

# ── Configure logging before anything else ───────────────────────────────────
configure_logging()
logger = get_logger(__name__)


# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    logger.info("ageneers starting up")
    yield
    logger.info("ageneers shutting down")


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

    # ── Routers (registered here, implemented in app/api/) ───────────────────
    from app.api.tasks import router as tasks_router  # noqa: PLC0415

    app.include_router(tasks_router, prefix="/api")

    # ── Health check ─────────────────────────────────────────────────────────
    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "ageneers"})

    return app


app = create_app()


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
        log_config=None,   # structlog handles logging, not uvicorn's default
    )