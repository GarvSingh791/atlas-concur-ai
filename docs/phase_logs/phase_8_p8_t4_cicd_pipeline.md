# Phase 8 / P8-T4 — CI/CD Pipeline
**Date:** 2026-06-24 (original build); 2026-11-07 (completion)
**Author:** Garv Singh Saini
**Status:** COMPLETE (2026-11-07) — G1–G7 all PASS on PR #4 / squash `d468dda`
  (CI runs #113 PR, #114 main); rollback drill done on both services

## P8-T3 closure confirmation (2026-11-05, entry probe for P8-T4)

First Git-sourced production deploy after P8-T3 (PR #1, squash-merge `e66e84d`). Read-only checks; secrets never inspected.

| Check | Result |
|---|---|
| Vercel deployment for the merge SHA, held for checks, then promoted | **PASS.** Built from `main` via Git integration, sat in "Running Checks" (~8 min) until the five Deployment Checks passed, then became current Production. The 2026-06-24 CLI deployment is no longer current. |
| CI on `main` (run #108) | **PASS**, 8m 11s: `backend`, `frontend`, `eval-db`, `e2e`, `docker-build`, `deploy-staging` all green. `deploy-staging` = Render deploy hook 2xx, `/health` 200, frontend 200/307, three-role API login smoke test OK. |
| Production Checklist / Active Branches | **PASS.** "Git not connected" gone (checklist 3/5: Git, Preview); Active Branches now lists Git branches (`p8-t3-env-audit` preview). |
| `/login` bundle | **PASS.** 22 script files; only backend URL is `https://atlas-backend-tsf8.onrender.com/api/v1` (4 occurrences). One `localhost:8000` string is the unreachable `??` fallback in the inlined expression. |
| R6 trust-surface UI (`6bfaae6`) live | **PASS.** "Grounding confidence" / "About grounding confidence" present in the live bundle. |
| Three demo roles log in from the live frontend | API logins PASS (deploy-staging). Browser logins: confirmed by owner on 2026-11-07. |
| Preview | Dashboard value readable (Config) — **PASS**. Preview deployment exists; its API calls are refused while `CORS_ALLOWED_ORIGINS` allows only production — **known, accepted limit**. |
| Post-rotation recheck (2026-11-07, after OpenRouter key rotation) | `/health` ok/staging; `/login` 200; CORS preflight echoes the production origin. |

## P8-T4 completion — commit-level release verification (2026-11-07)

The 2026-06-24 sections below added `deploy-staging`, but its `/health` poll was a
**false green**: the old release keeps answering while Render builds the new one.
This entry closes that gap and the rest of the plan's P8-T4 items (W0–W6). Branch
`p8-t4-cicd`.

### W0 diagnostics (read-only)
| Item | Finding |
|---|---|
| `deploy-staging` gating | `needs:` already listed all five jobs; `if:` push-to-`main` only. No `concurrency`, no `permissions`, no job timeout. The hook URL was interpolated inline (not printed). |
| False green, measured | Run #112 (`06b8ba6`): `deploy-staging` took **54 s** in total while Render's deploy of that commit took 1 m 37 s, so the poll passed against the previous release. |
| Render build time | Last 7 deploys: 40 s – 1 m 37 s (hook, manual and env-change deploys). Budget set to 900 s per wait. |
| `render.yaml` `healthCheckPath` | Already `/health`. No Blueprint change needed. |
| Frontend auth | No `middleware.ts`; auth is a client-side session guard, so `/api/version` is public without changes. |
| Vercel system env vars | "Automatically expose System Environment Variables" is **on**, so `VERCEL_GIT_COMMIT_SHA` is available at build. |
| Rollback doc | `docs/runbooks/rollback.md` existed but told readers to `alembic downgrade` staging and use Render Shell (not on the free tier), and missed Vercel's post-rollback promotion trap. Rewritten in place (kept the path; the plan's `rollback_staging.md` name would have duplicated it). |

### What changed
- Backend: setting `APP_COMMIT_SHA` (aliases `APP_COMMIT_SHA`, `RENDER_GIT_COMMIT`; default `local`) and `"commit"` in `GET /health`. Tests: `test_smoke.py` (key present), `unit/test_unit_health_commit.py` (default, Render mapping, override).
- Frontend: `app/api/version/route.ts` (`force-static`, `VERCEL_GIT_COMMIT_SHA || "local"`) + vitest; `vercel.json` `ignoreCommand: git diff HEAD^ HEAD --quiet -- .`.
- CI: `scripts/ci/wait_for_commit.sh`; `deploy-staging` now has `concurrency` (queued, not cancelled), `permissions: contents: read`, `timeout-minutes: 40`, secrets passed through `env` only, URL normalisation, a Render hook **pinned to the tested commit** (`ref=GITHUB_SHA`), backend wait for `GITHUB_SHA` → frontend wait for the newest commit that touched `frontend/` (full-history checkout) → liveness → three-role login → CORS pair. `backend` job: exactly one Alembic head.
- Docs: rollback runbook rewrite; `staging_env_map.md` "Deploy pipeline" section.
- Not touched: `database/`, `ai_agents/`, `render.yaml`, GitHub secrets (still three).

### Decisions (D1–D4, recommended options taken)
- D1: services report their own commit; no `RENDER_API_KEY`.
- D2: forward-only migrations, expand-then-contract, Neon restore for data. No CI database URL.
- D3: documented manual rollback plus one drill; no automatic rollback.
- D4: Vercel ignore step **and** the matching CI rule, shipped together.

### Finding: Render rollback cannot cross a migration
The plan assumed expand-then-contract makes code rollback safe after a migration. On
this stack it does not: a rolled-back build still runs `alembic upgrade head`, which
stops with "Can't locate revision" when the database is at a revision the older code
does not have, so the old build never boots and Render keeps the current one. The
runbook therefore says: releases with a migration roll **forward**, and a `git revert`
keeps the migration file. Expand-then-contract still matters, because Render migrates
from the new container while the old release is serving.

### Gates
| Gate | Evidence | Result |
|---|---|---|
| G1 Gates hold | PR run shows `deploy-staging` skipped; `needs:` on five jobs; Vercel Deployment Checks | **PASS.** PR #4 run #113 (7m 38s): `backend`, `frontend`, `eval-db`, `e2e`, `docker-build` green; `deploy-staging` skipped (0s); Vercel preview check listed and Ready. |
| G2 Auto-deploys | Squash-merge run: backend and frontend report the squash SHA within budget | **PASS.** Main run #114 (8m 50s, `deploy-staging` 1m 15s): hook HTTP 200 pinned to `d468dda`; `/health` reported `d468dda` after 61 s (none before); `/api/version` reported `d468dda` at 0 s (Vercel promoted it while CI ran). Budget 900 s. |
| G3 Migrates | Render deploy log shows the entrypoint's Alembic step for the new SHA; new SHA live | **PASS.** Render deploy for `d468dda` (trigger: Deploy Hook, 51 s): `[atlas-entrypoint] Applying database migrations (alembic upgrade head)`, `Context impl PostgresqlImpl`; no-op upgrade (no new migration in this release); deploy live. |
| G4 Health checks pass | Same run: `/health` with new commit, three-role login, CORS pair | **PASS.** Same run: `/health` `status: ok` + `commit: d468dda…`; frontend 307; employee/manager/finance login OK; CORS allowed origin 200 (ACAO matches), `https://not-atlas.invalid` 400. |
| G5 Failure is loud | `wait_for_commit.sh` vs staging, fake SHA, 60 s budget | **PASS.** Pre-merge: exit 1, `still on none after 60s`. Post-merge (30 s budget, all-zero SHA): exit 1, `::error::…/health still on d468ddac… after 30s (expected 0000…)`. |
| G6 Rollback works | Drill, both services, times logged | **PASS.** 2026-11-07 (UTC 05:10–05:14), `d468dda` → `06b8ba6` → `d468dda`. Render Rollback: decision → previous release serving in 77 s; roll-forward (Rollback to the `d468dda` deploy) 51 s. Vercel Instant Rollback: 32 s; Undo (Manage Rollback → promote `d468dda`) ~6 s. Rolled-back state verified (backend `status: ok`, no `commit` field; `/api/version` 404; login OK). After roll-forward, section 7 checks all pass and Vercel shows no active rollback. See `docs/runbooks/rollback.md` drill log. |
| G7 No secret in logs | Run log grep for `api.render.com/deploy` / `srv-` | **PASS.** `deploy-staging` log of run #114 (26.6 KB): 0 matches for `api\.render\.com/deploy\|srv-`; staging URLs shown masked (`***`). |

### Independent review (adversarial, 4 lenses + 1 verifier per finding, 17 review passes)
Confirmed and fixed before the PR:
- **High:** the unpinned hook deployed `main`'s head, so with two overlapping merges a newer, untested (or failing) commit could reach staging while an older run went red. Fixed with `ref=GITHUB_SHA` (Render deploy-hook `ref` parameter; 404 on an unknown SHA) and queued, non-cancelling concurrency.
- **Medium:** with cancel-in-progress and a per-commit `frontend/` diff, a cancelled run could leave a frontend change unverified while the newer run was green. Fixed: always wait for the newest commit that touched `frontend/`.
- **Runbook:** Hobby rollback beyond one step (revert PR / Redeploy, not Promote); Undo Rollback is a promote dialog; the trap only shows in CI for frontend merges; a Neon restore past a migration needs the code rolled back first and the migration removed in the revert.
Rejected (verified not defects): curl error output leaking the hook URL (stderr is discarded), and three timing/wording items.

### Local verification
- ruff check, ruff format --check, mypy (backend config evals): clean
- pytest: exit 0, 0 failures
- frontend: next lint clean, prettier clean, tsc clean, vitest 203/203 (36 files)
- `next build` with a test SHA: `/api/version` prerendered static, body `{"commit":"deadbeefcafe"}`
- `alembic heads` offline: one head (`0012_hnsw_ef_construction_200`)
- CORS pair against live staging: Vercel origin 200 with matching ACAO; foreign origin 400

## Original P8-T4 build (2026-06-24)

## Objective
Extend the CI pipeline so a green push to main automatically deploys
Atlas to staging, proves migrations ran, and verifies all three RBAC
roles authenticate — with a documented rollback path for each layer.

## Files Created or Modified
- .github/workflows/ci.yml (modified, 366 → 459 lines)
  → Added deploy-staging job: additive-only (all 5 existing jobs
    unchanged). Trigger: push to main only. Needs all 5 existing jobs.
    Steps: Render deploy hook → /health poll → Vercel verify → 3-role
    smoke test.
- docs/runbooks/rollback.md (new, 151 lines)
  → Four-layer rollback runbook (backend/Render, frontend/Vercel,
    database/Alembic, full reset). Decision tree for triage. Post-
    rollback checklist. Designed to be executable in under 5 minutes.
- docs/staging_env_map.md (modified, 255 → 289 lines)
  → Added "GitHub Actions Secrets (P8-T4)" section: documents
    RENDER_DEPLOY_HOOK_URL, STAGING_BACKEND_URL, STAGING_FRONTEND_URL
    and their rotation procedure.

## Deploy-Staging Job Design
  Trigger:  if: github.ref == 'refs/heads/main' && github.event_name == 'push'
  Needs:    [backend, frontend, eval-db, e2e, docker-build] — ALL 5 jobs
            Every existing gate must be green before any deploy fires.
  Steps:
    1. POST to RENDER_DEPLOY_HOOK_URL → require HTTP 200/201
    2. Poll STAGING_BACKEND_URL/health 30× × 30s (timeout-minutes: 20)
       /health returning 200 IS the migration proof:
       backend-entrypoint.sh exits non-zero if alembic fails →
       container never starts → /health never returns 200
    3. Verify STAGING_FRONTEND_URL (accept 200 or 307)
    4. 3-role smoke test: employee / manager / finance → access_token
       ALL must return OK → exit 0

  Architecture decision: Render autoDeploy: false kept intentionally.
  CI is the sole deploy trigger (deploy hook), not Render's GitHub
  watcher. This ensures: tests must pass BEFORE a deploy fires.
  A push that fails any of the 5 existing gates never reaches
  deploy-staging and never touches the live staging environment.

## Engineering Findings (caught during gating)

  Finding 1 — YAML multi-line Python breaks run: block scalars
    The brief's smoke-test snippet embedded a multi-line Python block
    (`try:` / `except:` at column 0) inside a `run: |` YAML block
    scalar. YAML terminates the scalar at any line that dedents to or
    past the parent key's indentation — this silently corrupts the
    workflow. Fixed with a single-line Python expression:
      python3 -c "import sys,json; d=json.load(sys.stdin); \
        print('OK' if d.get('access_token') else 'FAIL')"
    Functionally identical. This is a universal rule: never embed
    multi-line Python with unindented keywords inside CI run: blocks.

  Finding 2 — render.yaml key is autoDeploy, not autoDeployEnabled
    The Render Blueprint spec uses `autoDeploy: false` (not the key
    name the brief assumed). render.yaml was correct all along; the
    task spec had the wrong key name. Gate 7 adapted to reality.
    render.yaml was NOT modified. Intent (CI is the trigger) is
    satisfied. Documented here for carry-forward accuracy.

## Validate Gate Proof
  The P8-T4 push is itself the proof. On git push:
    1. CI starts on the HEAD commit (P8-T4)
    2. backend + frontend + eval-db + e2e + docker-build run → all green
    3. deploy-staging triggers → POST to Render deploy hook
    4. Render rebuilds from the P8-T4 commit → alembic upgrade head
       (idempotent on Neon — checks migration history, exits cleanly)
    5. /health returns 200 → BACKEND_HEALTHY (migration proof)
    6. Vercel frontend verified → FRONTEND_OK
    7. employee / manager / finance → ALL_ROLES_AUTH_OK
  Phase 8 Checkpoint satisfied: "one-command/path deploy reproduces
  the system" — git push origin main is that one command.

## Phase 8 Complete — Full DoD
  "staging environment reachable"        ✅  live since P8-T2 (69ba4b7)
  "migrated"                             ✅  alembic upgrade head in entrypoint
  "seeded"                               ✅  one-time idempotent Neon seed
  "health-green"                         ✅  /health 200 confirmed every session
  "deployable via pipeline"              ✅  deploy-staging job proves it

  Phase 8 Checkpoint: ✅
  "one-command/path deploy reproduces the system"
    → git push origin main → CI → deploy hook → /health 200 → 3-role auth

## Atlas Build Plan — Complete
  Phase 1 Bootstrap:           COMPLETE
  Phase 2 Core Backend:        COMPLETE
  Phase 3 Database:            COMPLETE
  Phase 4 AI System:           COMPLETE (P4-T13)
  Phase 5 Frontend:            COMPLETE (P5-T10)
  Phase 6 Integration:         COMPLETE (P6-T5)
  Phase 7 Testing:             COMPLETE (P7-T4)
  Phase 8 Deployment:          COMPLETE (P8-T4)

  Live staging: https://atlas-concur-ai.vercel.app
  Demo accounts: employee@ / manager@ / finance@atlas-demo.internal
  Password: change-me-seed-password

## Carry-Forward Register at Project Close
  P8-T3a: .env.example hygiene (17 orphan keys, 4 undocumented fields)
           → recommend cleanup before adding contributors
  Approval required-reason: returns 400 not 422
  Whitespace-only reason passes falsiness check
  rbac_catalog UniqueViolation flake under live Postgres
  16 pre-existing mypy errors in ai_agents/tests/
  GET /receipts/{id} and GET /audit not implemented
  correlation_id absent on ai_decision_logs
  NFR-SEC4 receipt-image serving unimplemented
  Render free-tier cold-start ~30s after 15min idle (Starter $7/mo removes)
  R2 token rotation: rotate after demo (transcript exposure, P8-T2)
  JWT_SECRET rotation: rotate before public sharing
