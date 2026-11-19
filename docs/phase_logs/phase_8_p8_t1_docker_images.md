# Phase 8 / P8-T1 — Docker Images
**Date:** 2026-06-23
**Author:** Garv Singh Saini
**Status:** COMPLETE — 14/14 gates green

## Objective
Finalize Dockerfile.backend and Dockerfile.frontend for production.
Close the compose reproducibility gap so a clean `down -v && up`
produces a fully migrated, seeded, and healthy stack from zero.

## Files Created or Modified
- docker/entrypoints/backend-entrypoint.sh (new, chmod +x)
- docker/postgres/init/00-extensions.sql (new)
- docker/Dockerfile.backend (modified)
- .dockerignore (modified)
- docker/docker-compose.yml (modified)
- .github/workflows/ci.yml (modified, additive only)

## What Was Built

**docker/entrypoints/backend-entrypoint.sh**
`set -e` entrypoint. Runs `alembic upgrade head`, then conditionally
runs `python -m database.seeds.seed_all` when SEED_ON_STARTUP=true
(idempotent: `.first()` guards + ON CONFLICT DO NOTHING in seed
scripts). Ends with `exec "$@"` so the CMD args (uvicorn) are passed
through and the dev `--reload` override in docker-compose.dev.yml is
preserved without touching the production image.

**docker/postgres/init/00-extensions.sql**
`CREATE EXTENSION IF NOT EXISTS pgcrypto;`
`CREATE EXTENSION IF NOT EXISTS vector;`
Provisioned via /docker-entrypoint-initdb.d on first volume init.
Closes the critical gap: migration 0004 requires the vector type;
conftest.py was the only place creating it (test-only). Production
compose had no equivalent — a clean `down -v && up` would fail at
0004 without this file.

**docker/Dockerfile.backend**
Three additions to the existing multi-stage build:
  - COPY alembic.ini /app/alembic.ini (was missing; `alembic upgrade
    head` crashes immediately on a fresh container without it)
  - COPY + RUN chmod 0755 the entrypoint script (--chmod flag avoided:
    BuildKit-only, not reliable on the classic builder)
  - ENTRYPOINT ["/usr/local/bin/backend-entrypoint.sh"] added;
    existing CMD preserved as its args

**.dockerignore**
Replaced the broad `docker/` exclude + `!docker/entrypoints` negation
pattern (unreliable on the classic builder when the parent dir is
excluded) with explicit per-path excludes that keep
docker/entrypoints/ COPY-able.

**docker/docker-compose.yml**
Four changes:
  - postgres: init-mount ./postgres/init:/docker-entrypoint-initdb.d:ro
  - backend: compose-level healthcheck (curl /health,
    start_period: 90s to cover migrate+seed time)
  - backend: SEED_ON_STARTUP: ${SEED_ON_STARTUP:-true} env var
  - frontend: depends_on.backend.condition service_started →
    service_healthy

**.github/workflows/ci.yml (additive only)**
In the existing docker-build job:
  - load: true on both build-push-action steps (makes images available
    to subsequent steps in the same job)
  - New step: boto3-verify (docker run --rm atlas-backend:ci python -c
    "import boto3; print(boto3.__version__)")
  - New step: full-stack compose smoke test (cp .env.example .env →
    compose up -d --build → wait → curl :8000/health + :3000 → tail
    backend logs → down -v)
No existing job, step, or service was modified or removed.

## Key Decisions

1. exec "$@" over hardcoded uvicorn in the entrypoint
   The CMD in the Dockerfile becomes the args to the entrypoint, so
   docker-compose.dev.yml's `--reload` override works without a
   separate dev Dockerfile.

2. --chmod avoided for builder-agnostic compatibility
   COPY --chmod=0755 is BuildKit-only. RUN chmod in the builder stage
   works on both the classic builder and BuildKit.

3. .dockerignore negation removed
   The classic Docker builder does not reliably honour `!docker/entrypoints`
   when the parent `docker/` directory is already excluded. Explicit
   excludes (docker/postgres/init, docker/entrypoints) are unambiguous
   on both builders.

4. SEED_ON_STARTUP defaults true locally, false in CI smoke
   Local `up` self-seeds with no extra steps. CI smoke test uses
   `.env.example` (SEED_ON_STARTUP omitted or false) → migrate-only,
   fast, deterministic.

5. Postgres NOT added to the hermetic backend CI job
   Un-skipping db-marked tests would surface known pre-existing
   failures (rbac_catalog UniqueViolation flake, test_p3_seed 7-vs-8
   rules, test_t3_cross_role_scoping stale is_active) and turn a
   currently green lane red — violates the additive-only rule. The
   eval-db job already provides a live-Postgres CI lane. Deferred.

## Reproducibility Proof (decision 1a — full down -v)
down -v wiped atlas-postgres-data + atlas-minio-data → fresh up:
  postgres: 00-extensions.sql ran once on empty volume → pgcrypto +
    vector provisioned before any migration touched those types
  backend entrypoint: alembic upgrade head ran migrations 0001→0007
    in sequence without error → SEED_ON_STARTUP=true seeded:
    demo users (employee@ / manager@ / finance@atlas-demo.internal,
    pw: change-me-seed-password), 7 policy rules, 8 embeddings,
    3 receipt images uploaded to MinIO atlas-receipts-dev
  All 4 services healthy in approximately 8 minutes from zero

## Gates (14/14 — all passed)
1.  Backend --no-cache build                  PASS (501 MB)
2.  boto3 importable from image               PASS (1.43.36)
3.  Non-root user                             PASS (uid=10001 atlas)
4.  Frontend --no-cache build (frontend/ ctx) PASS (235 MB standalone)
5a. No baked secrets — backend               PASS (0 matches)
5b. No baked secrets — frontend              PASS (0 matches)
6.  Compose stack healthy                     PASS (all 4 healthy)
7.  Backend /health → 200                     PASS ({"status":"ok"...})
8.  Frontend :3000 → response                 PASS (307 → /login)
9.  Backend logs clean                        PASS (0 genuine errors)
10. Drift guard                               PASS (frozen dirs clean)
11. Next.js standalone confirmed              PASS (already set)
12. Teardown down -v clean                    PASS
13. 3-role login via compose stack            PASS (all 3 → 200 + token)
14. Receipt upload → MinIO (boto3 E2E)        PASS (201, object in bucket)

## Independent Verification
4 read-only adversarial auditors (scope-drift, Dockerfile/entrypoint/
init, compose, CI-additive): all PASS. 0 blocker/high findings.
2 low/nit findings documented in carry-forward below.

## Carry-Forward Resolved
- boto3 missing from backend image: CLOSED
  boto3==1.43.36 verified importable (Gate 2) and proven end-to-end
  via seed MinIO uploads + Gate 14 receipt upload (201, 4 objects in
  atlas-receipts-dev bucket).
- test_embed_is_stable_across_processes CI failure: CLOSED
  Commit cf597b6 (Garv Singh Saini, 2026-06-23) — subprocess was wiping
  JWT_SECRET and DATABASE_URL from os.environ; fix preserves env.

## New Carry-Forward Entering P8-T2
- Extension provisioning is compose-only
  pgcrypto/vector are created by /docker-entrypoint-initdb.d on a
  fresh volume. A pre-existing volume lacking these extensions would
  fail at migration 0004. Belt-and-suspenders fix: CREATE EXTENSION
  IF NOT EXISTS in an early Alembic migration — a database/ change,
  deferred.
- Google Fonts egress ETIMEDOUT under heavy concurrent builds
  next/font/google Inter in app/layout.tsx requires outbound network
  during next build. Builds fine normally but can ETIMEDOUT under
  heavy load. Fix: switch to next/font/local for hermetic builds —
  frozen frontend code, deferred.

## Existing Carry-Forward (unchanged, P8-T2 queue)
- Approval required-reason returns 400 not 422
- Whitespace-only reason passes falsiness check
- Travel service uses manual version increment instead of CAS
- rbac_catalog UniqueViolation flake under live Postgres
- 16 pre-existing mypy errors in ai_agents/tests/
- GET /receipts/{id} and GET /audit not implemented
- correlation_id absent on ai_decision_logs
- expense_categorize_simple has no deterministic handler
- LocalEmbeddingsProvider per-process hash salting
- NFR-SEC4 receipt-image serving unimplemented
