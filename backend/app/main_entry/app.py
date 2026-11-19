"""
FastAPI application factory for Atlas.

create_app() builds and returns a fully configured FastAPI instance:
  - Settings read at construction time (fail-fast on missing required vars).
  - Structured JSON logging configured before any request is served.
  - CORS middleware from CORS_ALLOWED_ORIGINS + CORS_ALLOW_CREDENTIALS.
  - Correlation-ID middleware (attaches X-Correlation-ID to every request).
  - Global exception handlers (standard error envelope, no stack-trace leakage).
  - Lifespan handler with startup/shutdown hooks (empty now; Phase 2/3 populate).
  - Health router (GET /health) at root level.
  - /api/v1 aggregate router (empty now; module routers added in later phases).

The module-level `app` is imported by Uvicorn:
    uvicorn backend.app.main_entry.app:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from backend.app.api.health import router as health_router
from backend.app.api.v1 import api_router
from backend.app.core.body_limit import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from backend.app.core.config import get_settings
from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.database import get_engine
from backend.app.core.demo_reset_scheduler import (
    DemoResetScheduler,
    resolve_config,
    running_under_pytest,
)
from backend.app.core.errors import (
    AppError,
    app_error_handler,
    http_exception_handler,
    integrity_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from backend.app.core.logging import setup_logging
from backend.app.core.rate_limit import (
    RATE_LIMIT_EXPOSED_HEADERS,
    RateLimitMiddleware,
)
from backend.app.core.rate_limit_redis import build_rate_limiter

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle hooks.

    Startup tasks:
      - Verify the database is reachable (fail fast if not). This is a
        CONNECTION CHECK ONLY — no models, tables, ORM session, or migrations
        (those arrive in Phase 3). Deeper readiness (pool warm-up, vector-index,
        provider health) lands in Phase 2/3/4.

    Shutdown tasks:
      - Graceful disposal of the shared connection pool.
    """
    settings = get_settings()
    logger.info(
        "Atlas starting up | env=%s debug=%s version=%s",
        settings.APP_ENV,
        settings.APP_DEBUG,
        settings.APP_VERSION,
    )

    # ---- Database connectivity check (connection only — no ORM / models) ----
    # Use the shared engine (backend.app.core.database) so startup and request
    # handlers share ONE connection path. Log the HOST only: make_url parses the
    # DSN so credentials never reach the logs (DATABASE_URL contains the password).
    db_host = make_url(settings.DATABASE_URL).host or "unknown"
    engine = get_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("Database connection verified | host=%s", db_host)
    except Exception as err:
        logger.critical(
            "Database connection failed | host=%s — aborting startup",
            db_host,
            exc_info=True,
        )
        raise RuntimeError(f"Database unreachable at host={db_host}; aborting startup") from err

    # ---- Scheduled demo reset (phase 12; default OFF) ----
    # Invalid DEMO_RESET_* values disable it with a warning (never a failed boot).
    # Never started implicitly under pytest: tests start it explicitly.
    demo_reset = DemoResetScheduler(resolve_config())
    application.state.demo_reset_scheduler = demo_reset
    if demo_reset.config.enabled and not running_under_pytest():
        demo_reset.start()

    yield

    # ---- Shutdown: stop the scheduler, then dispose the connection pool ----
    logger.info("Atlas shutting down")
    await demo_reset.stop()
    engine.dispose()


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    settings = get_settings()

    # Configure logging first so all subsequent log calls are formatted
    setup_logging(settings)

    application = FastAPI(
        title=settings.APP_NAME,
        description=(
            "AI-Powered Travel & Expense Copilot — "
            "conversational booking, automated expense reporting, "
            "policy-grounded approvals."
        ),
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.APP_DEBUG else None,
        redoc_url="/redoc" if settings.APP_DEBUG else None,
        lifespan=lifespan,
    )

    # ---- Middleware (order matters — outermost = first to receive request) --
    # Request path (phase 11): CorrelationID -> CORS -> nosniff headers ->
    # RATE LIMITER -> BODY-SIZE GUARD -> routes.  Starlette wraps in reverse
    # order of add_middleware, so the innermost is added first.
    #
    # Body-size cap (innermost of the two): a request the limiter lets through
    # is still capped before any route reads it (413 PAYLOAD_TOO_LARGE).
    application.add_middleware(
        BodySizeLimitMiddleware,
        upload_paths=frozenset({f"{settings.API_V1_PREFIX}/receipts"}),
    )
    # Rate limiter runs BEFORE the body-size guard, so a throttled flood is
    # refused (429) without any of its body being read or counted.  The limiter
    # lives on app.state (tests swap it for one with tiny limits / a fake
    # clock).  It sits inside CORS and the correlation id, so its 429 carries
    # both, and CORS preflights (OPTIONS) are answered before they reach it.
    # RATE_LIMIT_STORE=redis shares the counters across instances (follow-up);
    # the default stays the in-process store, built exactly as before.
    application.state.rate_limiter = build_rate_limiter(settings)
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
        # Let the browser client read the throttle headers on cross-origin calls.
        expose_headers=list(RATE_LIMIT_EXPOSED_HEADERS),
    )
    application.add_middleware(CorrelationIDMiddleware)

    # ---- Exception handlers -------------------------------------------------
    application.add_exception_handler(IntegrityError, integrity_error_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(AppError, app_error_handler)

    # ---- Routers ------------------------------------------------------------
    # Health endpoint at root (not versioned — used by load balancers)
    application.include_router(health_router)

    # Versioned API aggregate router (empty now; modules plug in via v1.py)
    application.include_router(api_router)

    return application


# Module-level app instance — imported by Uvicorn and backend/main.py
app: FastAPI = create_app()
