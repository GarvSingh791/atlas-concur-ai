# Phase Log

**Phase:**       Phase 1 — Project Bootstrap
**Sub-Phase:**   P1-T4 — Datastore via Docker
**Task ID:**     P1-T4
**Date:**        2026-06-07
**Author:**      Garv Singh Saini
**Status:**      ✔ Complete, Verified, Security Reviewed

---

## Objective
Add the local-dev datastore layer for Atlas: a Docker Compose stack
bringing up PostgreSQL (pgvector), MinIO (S3-compatible object store),
and the backend and frontend services, with multi-stage non-root
Dockerfiles and a boot-time database connection check in the FastAPI
lifespan handler. Connection check only — no models, migrations, or
tables (those are Phase 3).

## Work Completed
- Created docker/docker-compose.yml with four services (postgres,
  minio, backend, frontend), named volumes, a single bridge network,
  and ${VAR:?msg} fail-fast guards on un-defaultable secrets
- Created multi-stage docker/Dockerfile.backend (python:3.12-slim,
  non-root atlas user, repo-root build context, PYTHONPATH=/app,
  copies backend/ + config/ only)
- Created multi-stage docker/Dockerfile.frontend (node:20-alpine,
  non-root nextjs user, Next.js standalone output, NEXT_PUBLIC_* as
  build args)
- Created .dockerignore at repo root (backend build context) and
  frontend/.dockerignore
- Added a boot-time DB connection check to the FastAPI lifespan
  handler in backend/app/main_entry/app.py (SELECT 1, host-only
  logging via make_url, CRITICAL log + RuntimeError on failure,
  engine disposed in finally — no ORM, no models, no migrations)
- Enabled output: 'standalone' in frontend/next.config.mjs (required
  for the multi-stage runtime Dockerfile to find server.js)
- Updated .env.example with NEXT_PUBLIC_API_URL and a Docker note
  explaining the DATABASE_URL host and MinIO credential mapping
  (no duplicate secret block — reused existing STORAGE_* and
  POSTGRES_* vars)
- Updated README.md with a "Running Locally" section and fixed the
  stale uvicorn path (app.main_entry.main:app →
  backend.app.main_entry.app:app); documented required --env-file .env flag

## Files Created
  docker/docker-compose.yml
  docker/Dockerfile.backend
  docker/Dockerfile.frontend
  .dockerignore                  (repo root — backend build context)
  frontend/.dockerignore
  docs/phase_logs/Phase1_P1-T4.md

## Files Modified
  backend/app/main_entry/app.py — added DB connection check to the
    lifespan handler (startup SELECT 1, CRITICAL log + RuntimeError
    on failure, engine disposed in finally block)
  frontend/next.config.mjs — changed nextConfig to
    { output: 'standalone' } for the multi-stage runtime Dockerfile
  .env.example — added NEXT_PUBLIC_API_URL and a Docker note
    (no duplicate secret block — reused existing STORAGE_* and
    POSTGRES_* vars)
  README.md — added "Running Locally" section; fixed stale uvicorn
    path (app.main_entry.main:app → backend.app.main_entry.app:app);
    documented the required --env-file .env flag; updated Project
    Status to reflect P1-T4 complete

## Integration Points
- The PostgreSQL service is the database Phase 3 Alembic migrations
  will run against; the pgvector/pgvector:pg16 image ships the
  extension prebuilt for the Phase 4 RAG policy_embeddings index.
  NOTE: When ORM models land in Phase 3, Dockerfile.backend must
  add COPY database/ /app/database/ (currently copies only
  backend/ + config/).
- The MinIO service is the object store the Phase 4 storage provider
  adapter will point to for receipt images (STORAGE_ENDPOINT_URL =
  http://minio:9000 inside the cluster)
- The backend container's DATABASE_URL uses the compose service name
  "postgres" as the host — internal Docker DNS resolution
- The lifespan DB check established here is where Phase 3 will add
  ORM session wiring and Phase 4 will add provider initialization
- depends_on with service_healthy ensures the backend never races
  the datastore — a pattern every future containerized service relies on
- P1-T5 (CI) will spin up these same compose services for integration
  tests

## Technical Decisions
- pgvector/pgvector:pg16 chosen over plain postgres so the vector
  extension is prebuilt — no custom image build needed for Phase 4 RAG
- Multi-stage Dockerfiles keep runtime images lean and credential-free
- Non-root container users (atlas uid 10001 / nextjs uid 10001) —
  never run containers as root
- Named volumes (not bind mounts) for Postgres and MinIO data so
  data survives restarts and stays portable
- depends_on: service_healthy (not service_started) on the datastores
  so the backend DB check never races an unready Postgres
- RECONCILIATION 1: backend build context set to repo root with
  PYTHONPATH=/app and COPY of both backend/ and config/ — the app
  imports the sibling config package via repo-root-absolute imports,
  so a ../backend context would break imports and the module path.
  Reconciled per approval.
- RECONCILIATION 2: compose env var names matched the real Settings
  field names (JWT_SECRET, APP_DEBUG, CORS_ALLOWED_ORIGINS,
  STORAGE_ENDPOINT_URL, STORAGE_ACCESS_KEY_ID,
  STORAGE_SECRET_ACCESS_KEY) — the task spec's names
  (SECRET_KEY/DEBUG/ALLOWED_ORIGINS/STORAGE_*) did not match and
  would have crashed the boot (JWT_SECRET is required with no
  default). Reconciled per approval.
- MinIO root credentials mapped from existing STORAGE_ACCESS_KEY_ID /
  STORAGE_SECRET_ACCESS_KEY rather than introducing a duplicate
  MINIO_ROOT_* block
- --env-file .env required on all compose commands because .env lives
  at the repo root, not docker/ — documented in README; omitting it
  triggers the intended ${VAR:?} fail-fast

## Testing Performed
All Validate-gate and QA conditions were satisfied live:
  ✔ docker compose up --build completed without errors
  ✔ docker compose ps — postgres healthy, minio healthy
  ✔ backend started only after postgres + minio were healthy
    (depends_on: service_healthy confirmed)
  ✔ backend logs showed "Database connection verified | host=postgres"
    at INFO level on fresh boot
  ✔ GET http://localhost:8000/health returned HTTP 200 (host and
    in-container)
  ✔ http://localhost:9001 served the MinIO web console (HTTP 200)
  ✔ bad DATABASE_URL (wrong password) produced a CRITICAL log and
    the container exited (exit code 3) — no silent hang
  ✔ docker compose down -v removed both named volumes cleanly with
    no orphan containers
  ✔ no credentials hardcoded in any Dockerfile, compose file, or
    source file
  ✔ docker exec atlas-backend whoami → atlas (not root)
  ✔ docker exec atlas-frontend whoami → nextjs (not root)
  ✔ volume persistence confirmed: marker row survived down → up;
    down -v → up gave a fresh empty database
  ✔ frontend image validated on an alternate host port (3001):
    in-container HTTP 200, host HTTP 307, non-root user confirmed,
    server.js present, bound to 0.0.0.0:3000

## Risks Identified
- Host port 3000 occupied by a pre-existing next dev server, preventing
  the frontend container from binding 3000 on the host
- curl is absent from python:3.12-slim and node:20-alpine base images,
  which the HEALTHCHECK directives require
- MinIO healthcheck assumes curl is present in the minio/minio:latest
  image (confirmed present in current tag; not contractually guaranteed
  across all future tags)
- next/font/google (Inter) fetches at build time — an offline builder
  would fail the frontend build (out of P1-T4 scope, flagged only)

## Resolution Actions
- Frontend image validated on an alternate port rather than terminating
  the user's running dev server — the Dockerfile and compose wiring are
  proven correct; host port binding is an environment quirk, not a defect
- curl installed in both backend and frontend runtime stages so
  HEALTHCHECKs work without extra tooling
- libpq-dev corrected to libpq5 (the runtime library; psycopg2-binary
  bundles its own libpq so only the runtime .so is needed)
- next/font risk noted for future offline-build awareness; no action
  required for P1-T4

---

## Next Sub-Phase Preview
**P1-T5 — Tooling & CI baseline.**
Add linter/formatter for both languages; add a test runner per layer (empty suites OK). Add a CI workflow: install, lint, type-check, run tests, build images.

Validate: CI passes green on the empty skeleton.

---
