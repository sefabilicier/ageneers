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

from dotenv import load_dotenv
import os

load_dotenv()

# ── Configure logging before anything else ───────────────────────────────────
configure_logging()
logger = get_logger(__name__)


# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    _validate_env()
    logger.info("ageneers starting up")
    yield
    logger.info("ageneers shutting down")


def _validate_env() -> None:
    """
    Check required environment variables at startup.
    Logs clear warnings so the operator knows what is missing
    before the first request hits a broken pipeline.
    """
    import os
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

    # ── Routers (registered here, implemented in app/api/) ───────────────────
    from app.api.tasks import router as tasks_router        # noqa: PLC0415
    from app.api.webhooks import router as webhooks_router  # noqa: PLC0415

    app.include_router(tasks_router, prefix="/api")
    app.include_router(webhooks_router, prefix="/api")

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
        reload_dirs=["app"],          # watch ONLY app/ — never workspaces/
        log_config=None,
    )