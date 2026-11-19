# 0005 — Commit-pinned deploys, verified by the SHA each service reports

- **Status:** Accepted
- **Date:** 2026-11-07 (phase P8-T4, commit-level release verification)
- **Decider:** Garv Singh Saini

## Context

Staging is a Render web service (backend, Docker) and a Vercel project (frontend), deployed from `main` by GitHub Actions. The first version of the `deploy-staging` job triggered the Render deploy hook and then polled `/health` until it answered 200.

That check was a **false green**. While Render builds a new release, the old one keeps serving, so `/health` answers 200 throughout. Run #112 (`06b8ba6`) measured it: the job finished in 54 seconds while Render's deploy of that commit took 1 minute 37 seconds, so the poll passed against the previous release. A second problem: a deploy hook without a pinned commit deploys whatever `main` is at that moment, which may be newer than what CI tested.

## Decision

1. **Each service reports the commit it runs.** The backend's `GET /health` returns `commit` (setting `APP_COMMIT_SHA`, mapped from Render's `RENDER_GIT_COMMIT`, default `local`). The frontend serves `GET /api/version` returning `VERCEL_GIT_COMMIT_SHA`.
2. **The deploy is pinned to the tested commit.** CI calls the Render hook with `ref=$GITHUB_SHA`. `render.yaml` sets `autoDeploy: false`, so CI is the only trigger. `deploy-staging` needs all six other jobs (backend, frontend, eval-db, e2e, docker-build, secret-scan) and runs only on a push to `main`.
3. **CI waits for the SHA, not for a 200.** `scripts/ci/wait_for_commit.sh` polls `/health` until `commit` equals `GITHUB_SHA` (budget 900 s), then polls the frontend's `/api/version` until it equals the newest commit that touched `frontend/` (Vercel's `ignoreCommand` skips builds when `frontend/` is unchanged).
4. **Then smoke tests:** frontend liveness, a three-role login (password from a masked secret, response bodies never printed) and a CORS preflight pair (allowed origin 200, foreign origin 400).
5. Deploys queue and are never cancelled midway (`concurrency: deploy-staging`, `cancel-in-progress: false`).

Because the backend entrypoint runs `alembic upgrade head` under `set -e`, a failed migration stops the new container, Render keeps the old release, and the wait times out naming the old SHA. **The new SHA being live is therefore also proof that the migration succeeded.**

## Alternatives considered

| Option | Why not |
|---|---|
| Keep polling `/health` for 200 | Measured false green (above). |
| Query Render's API for deploy status | Needs a Render API key in CI with broader rights than a deploy hook; a service reporting its own commit needs no extra credentials and also covers Vercel. |
| Render auto-deploy on push to `main` | Would deploy before, or regardless of, CI, and could ship an untested head. |
| Automatic rollback on a failed smoke test | More moving parts than a single-environment demo justifies; a documented manual rollback, drilled once, was chosen instead. |
| Connect CI to the database to check the migration | Puts a database URL in CI. The SHA wait already proves the migration ran. |

## Consequences

**Positive**

- "Green deploy" now means the tested commit is serving traffic on both services.
- No Render API key and no database credentials in CI; GitHub holds only the deploy hook URL, the two staging URLs and an optional demo password.
- A failed migration cannot silently leave staging on old code while CI reports success.

**Negative / trade-offs**

- **A Render rollback cannot cross a migration.** A rolled-back build still runs `alembic upgrade head` against a database at a newer revision, fails with "Can't locate revision", and never boots. Releases that include a migration roll **forward**; migrations stay additive (expand, then contract).
- The wait adds several minutes to each deploy and depends on the free-tier backend waking up within the budget.
- The frontend check relies on Vercel's system environment variables being exposed at build time.

## Evidence

- Workflow: [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (`deploy-staging`, and the single-Alembic-head guard in `backend`); script: [`scripts/ci/wait_for_commit.sh`](../../scripts/ci/wait_for_commit.sh).
- Backend: [`backend/app/api/health.py`](../../backend/app/api/health.py); test `backend/tests/unit/test_unit_health_commit.py`. Frontend: [`frontend/app/api/version/route.ts`](../../frontend/app/api/version/route.ts).
- Blueprint: [`render.yaml`](../../render.yaml) (`autoDeploy: false`); entrypoint: `docker/entrypoints/backend-entrypoint.sh`.
- Rollback runbook and drill (2026-11-07: Render rollback 77 s, Vercel Instant Rollback 32 s): [`docs/runbooks/rollback.md`](../runbooks/rollback.md).
- Phase log: [`phase_8_p8_t4_cicd_pipeline.md`](../phase_logs/phase_8_p8_t4_cicd_pipeline.md).
