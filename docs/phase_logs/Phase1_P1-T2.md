---
# Phase Log

**Phase:**       Phase 1 — Project Bootstrap
**Sub-Phase:**   P1-T2 — Backend Skeleton (FastAPI)
**Task ID:**     P1-T2
**Date:**        2026-05-31
**Author:**      Garv Singh Saini
**Status:**      ✔ Complete, Verified, Security Reviewed

---

## Objective
Stand up a clean, bootable FastAPI backend skeleton with a typed
settings layer, /api/v1 aggregate router, GET /health endpoint,
correlation ID middleware, global error handler, and ASGI entry
point. No business logic, no database, no auth — bootable skeleton
only.

## Work Completed
- Implemented Pydantic Settings singleton (`config/settings/base.py`) with fail-fast `sys.exit(1)` on missing `JWT_SECRET` and `DATABASE_URL`; validation errors print a human-readable message to stderr, not a traceback
- Implemented `lru_cache`-backed `get_settings()` factory (`config/settings/__init__.py`) ensuring the settings object is constructed once per process and re-used by all importers
- Created thin re-export shim at `backend/app/core/config.py` so all app code imports settings through `backend.app.core.config` and stays decoupled from the `config/` package path
- Built `GET /health` endpoint (`backend/app/api/health.py`) returning `{"status": "ok", "app": ..., "env": ..., "version": ...}` sourced from settings — no hardcoded values
- Built `/api/v1` aggregate router (`backend/app/api/v1.py`) with `prefix` driven by `API_V1_PREFIX` setting and a debug-only `/_debug/error` endpoint (guard: `APP_DEBUG=true`) for validate-gate error-envelope testing; all future module routers register here via one `include_router()` call each
- Implemented `CorrelationIDMiddleware` (`backend/app/core/correlation.py`) as a pure ASGI middleware that reads `X-Correlation-ID` from inbound headers or generates a `uuid4`, stores the ID in both `scope["_correlation_id"]` (durable for full request lifetime including `ServerErrorMiddleware`-level handlers) and a `ContextVar` (available to log filters), and injects the header on every response via a `send()` wrapper
- Implemented global exception handlers (`backend/app/core/errors.py`) covering `Exception` (unhandled 500), `HTTPException`, and `RequestValidationError`; all handlers return the standard error envelope `{"error": {"code": ..., "message": ..., "correlation_id": ...}}`; full tracebacks logged server-side only, never in the response body
- Built structured logging (`backend/app/core/logging.py`) with JSON format (`LOG_FORMAT=json`) emitting `{timestamp, level, logger, message, correlation_id}` per record, and plain-text format for local convenience; `_CorrelationFilter` injects the correlation ID from the `ContextVar` into every log record
- Assembled `create_app()` factory (`backend/app/main_entry/app.py`) wiring CORS middleware, `CorrelationIDMiddleware`, the three exception handlers, the health router, and the `/api/v1` aggregate router; exposed an empty `lifespan` async context manager with placeholder comments for Phase 2/3/4 startup tasks (DB engine, connection pool, vector-index readiness, provider health checks)
- Created `backend/main.py` Uvicorn launcher runnable via `python backend/main.py`; patches `sys.path` with the monorepo root so both `backend.*` and `config.*` are importable regardless of invocation directory
- Wrote `backend/requirements.txt` pinning all Phase 1–4 Python dependencies to minor-version ranges: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `python-dotenv`, `sqlalchemy`, `alembic`, `psycopg2-binary`, `python-jose[cryptography]`, `passlib[bcrypt]`, `httpx`
- Expanded `.env.example` with all variables the skeleton reads at startup (`APP_NAME`, `APP_ENV`, `APP_DEBUG`, `LOG_LEVEL`, `LOG_FORMAT`, `API_HOST`, `API_PORT`, `API_V1_PREFIX`, `JWT_SECRET`, `DATABASE_URL`, `CORS_ALLOWED_ORIGINS`, `CORS_ALLOW_CREDENTIALS`) plus forward-declared variables for Phase 2–4 (LLM, embeddings, OCR, object storage, AI behaviour, approval workflow, multi-tenant seam, and seed configuration)

## Files Created
  backend/__init__.py
  backend/main.py
  backend/requirements.txt
  backend/app/__init__.py
  backend/app/api/__init__.py
  backend/app/api/health.py
  backend/app/api/v1.py
  backend/app/core/__init__.py
  backend/app/core/config.py
  backend/app/core/correlation.py
  backend/app/core/errors.py
  backend/app/core/logging.py
  backend/app/main_entry/__init__.py
  backend/app/main_entry/app.py
  config/__init__.py
  config/settings/__init__.py
  config/settings/base.py

## Files Modified
  .env.example — expanded from the P1-T1 skeleton with all backend startup variables (`API_HOST`, `API_PORT`, `API_V1_PREFIX`, `JWT_SECRET`, `DATABASE_URL`, `CORS_*`, `LOG_LEVEL`, `LOG_FORMAT`) and forward-declared Phase 2–4 variables (LLM, embeddings, OCR, storage, AI behaviour, approval workflow, multi-tenant seam, seed configuration)

## Integration Points
- `config/settings/` is the singleton imported by all future backend modules — Phase 2 auth, Phase 3 database session, Phase 4 AI providers all read from here; the `backend/app/core/config.py` shim is the single import seam
- `backend/app/api/v1.py` is the aggregate router where Phase 2 module routers (identity, travel, expense, policy, approval, analytics, ai) are registered with one `include_router()` call each
- The `lifespan` handler in `backend/app/main_entry/app.py` is where Phase 3 database engine initialisation and Phase 4 provider health checks will be added on startup/shutdown
- The global exception handlers and `CorrelationIDMiddleware` established here apply to every endpoint added in all future phases — correlation IDs appear on all responses including errors without per-route configuration
- `backend/requirements.txt` lists all Phase 1–4 Python dependencies (SQLAlchemy, Alembic, python-jose, passlib) so they are available to install before those phases begin

## Technical Decisions
- **Pydantic Settings over raw `python-dotenv`:** `pydantic-settings` `BaseSettings` provides typed, validated env reading with field validators. The `JWT_SECRET` and `DATABASE_URL` validators call `sys.exit(1)` on a missing/empty value, giving operators a clean startup error with field-level messages rather than a `KeyError` at an arbitrary call site.
- **`lru_cache` singleton:** Settings are constructed once and cached for the process lifetime. This avoids repeated `.env` file reads and ensures a consistent view across all modules while remaining patchable by tests (monkeypatching env before first import).
- **Correlation ID stored in two places (scope + ContextVar):** `request.scope["_correlation_id"]` is durable for the entire ASGI request lifetime including `ServerErrorMiddleware`-level handlers that fire after the ContextVar token is reset in `finally`. The ContextVar is used by log filters where it is reliable. Exception handlers always read from scope to guarantee the ID is present.
- **Correlation ID injected in response body AND header:** The `send()` wrapper injects the `X-Correlation-ID` response header on every response. Exception handlers also embed the ID directly in the `JSONResponse` headers, covering the case where an unhandled exception reaches `ServerErrorMiddleware` and the `send_with_header` wrapper is no longer in the send chain.
- **`lifespan` handler present with empty body:** The lifespan pattern is established now so Phase 2/3/4 can add startup tasks (DB connection, provider health check) without restructuring the application factory.
- **`/api/v1` prefix from settings:** `API_V1_PREFIX` drives the aggregate router prefix, not a hardcoded string, so prefix changes require only an env var update.
- **Debug-only error trigger at `/_debug/error`:** Guarded by `APP_DEBUG`, this route allows the validate gate to exercise the 500 error envelope without coupling test fixtures to business logic. It is never registered in production.
- **Requirements pinned to minor-version ranges (`>=x.y,<x.(y+1)`):** Prevents silent major-version upgrades while allowing patch releases. Phase 2/3/4 dependencies (SQLAlchemy, Alembic, psycopg2, python-jose, passlib) are included now so `pip install -r requirements.txt` does not need to be re-run when those phases begin.

## Testing Performed
All seven Validate gate conditions from MASTER_EXECUTION_PLAN P1-T2 were satisfied:
  ✔ Server boots with: `python backend/main.py`
  ✔ `GET /health` returns HTTP 200 with correct JSON structure `{"status": "ok", "app": ..., "env": ..., "version": ...}`
  ✔ Missing `JWT_SECRET` produces clean startup error with field-level message to stderr, exits with code 1, no traceback
  ✔ Missing `DATABASE_URL` produces clean startup error with field-level message to stderr, exits with code 1, no traceback
  ✔ Unhandled exception returns standard error envelope `{"error": {"code": "INTERNAL_SERVER_ERROR", "message": "...", "correlation_id": "..."}}` with no stack trace in response body
  ✔ `/api/v1` router mounts without error; prefix resolves to `/api/v1` from settings
  ✔ `pip install -r backend/requirements.txt` completes cleanly

Additional testing:
  - Verified `X-Correlation-ID` response header is present on both 200 and 500 responses
  - Verified structured JSON log output on startup and per-request
  - Verified CORS middleware configuration does not block same-origin health check

## Risks Identified
- `psycopg2-binary` is installed in requirements but no database connection is established yet; a future phase must wire the connection before the binary is exercised
- The debug `/_debug/error` route is guarded by `APP_DEBUG` but relies on the operator setting `APP_DEBUG=false` in production; this is enforced by the settings default (`APP_DEBUG: bool = False`)

## Resolution Actions
- `psycopg2-binary` risk accepted and documented; Phase 3 (P3-T2) will establish the connection and run migrations against a live database, validating the dependency at that point
- `APP_DEBUG` default-false risk mitigated by the Settings class default; documented in `.env.example` with a note that the value must be `false` in production

---

## Next Sub-Phase Preview
P1-T3 — Frontend Skeleton (Next.js)
Objective: Initialize Next.js App Router with TypeScript, create root
layout and protected app layout stub, add /login placeholder page,
configure Tailwind CSS and shadcn/ui baseline, and verify type-check
passes on the empty skeleton.

---
