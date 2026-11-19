# Phase 7 · P7-T4 — End-to-End (Playwright) Tests

**Date:** 2026-06-20
**Branch:** test/e2e
**Track:** end-to-end tests

## Goal

Validate the full Atlas UX flow through the **real UI, per role**, against a **live frontend + live
backend** with all providers local (deterministic, no live Gemini): plan trip → approve trip →
capture receipt → assemble report → approve report → view analytics. Closes the Phase-7 DoD
(unit + API + AI-eval + E2E all green).

## Stack & determinism

- Live Dockerized backend on `:8000` (health = **root `/health`**; `/api/v1/health` 404s) + Postgres
  + MinIO; frontend `next dev` `:3000` (Node 20). Playwright `webServer` boots the frontend with
  `reuseExistingServer:!process.env.CI`.
- Seed: `alembic upgrade head && python -m database.seeds.seed_all` (idempotent — skips existing rows).
- Determinism: `LLM_PROVIDER=OCR_PROVIDER=EMBEDDINGS_PROVIDER=STORAGE_PROVIDER=local`. No live network.
- Creds (seeded test users): `employee@ / manager@ / finance@ atlas-demo.internal`, password
  `change-me-seed-password` (finance role = `finance_admin`).
- Auth: storageState seeds `localStorage['atlas_rt']` (refresh token, 7-day TTL) →
  `SessionProvider.hydrateSession` refreshes on mount (access token is memory-only).

## Suite home & layout

`frontend/e2e/p7t4/`. The existing `frontend/playwright.config.ts` was extended into projects:

- `legacy` — the 3 P6-T1 specs (`auth-refresh`/`screens`/`state-actions`), `testIgnore **/p7t4/**`, no
  storageState — runtime behavior unchanged.
- `setup` — `auth.setup.ts`; logs each role in **once**, writes `.auth/{role}.json`.
- `p7t4-employee` / `p7t4-manager` / `p7t4-finance` — per-role storageState, `dependencies:["setup"]`.

## What was built

- `global-setup.ts` — fail-fast probe of root `/health` + `:3000`.
- `auth.setup.ts` — per-role API login → seed `atlas_rt` → save storageState.
- `helpers.ts` — `addChip`, `apiLogin*`, `seedDraftReport` (>$500), `submitReport`, `seedTrip`,
  `triggerApproval`, `bootApp`; re-exports `EMAIL`/`PASSWORD`/`navTo` from the legacy helpers.
- `employee-flow.spec.ts` — plan (chip intent → OptionCard → "Trip saved") → receipt (deterministic
  OCR) → assemble/submit (self-seeded draft → "Report submitted").
- `manager-flow.spec.ts` — approve a self-seeded pending REPORT (>$500 → escalates) and a pending
  TRIP, both via the UI.
- `finance-flow.spec.ts` — org-wide Analytics, Policy reachable, Audit/Users render, full admin nav.
- `cross-role-handoff.spec.ts` — employee SUBMITS via UI → documented API bridge enqueues the
  approval → a second (manager) browser context APPROVES via UI (true single-subject handoff).
- `rbac-ui.spec.ts` — nav-absence (with positive control); employee→/approvals 403 + stays on route;
  analytics/policy finance-only empty-state; unauthenticated → /login.
- Config: `playwright.config.ts` (projects / globalSetup / frontend webServer), `package.json`
  (`e2e` + `e2e:p7t4` scripts, no new deps), `.gitignore` (`.auth/`).

## Planner-under-local

Confirmed selectable & deterministic. Intent is **chip-driven structured slots** (not free-text):
From=SFO / To=JFK / Depart=2026-06-10 / Travelers=1 (Currency pre-defaults USD) completes the intent
→ seeded `services/mock_inventory` SFO→JFK route returns real OptionCards → Select persists a draft
Trip + "Trip saved" toast. Verified at the SSE `result`/`shortlist` frame and through the UI. The
local LLM provider only narrates; it does not generate options.

## Receipt determinism (and a caveat)

Under `STORAGE_PROVIDER=local` (filesystem `LocalStorageProvider`, stdlib-only) the upload round-trips
`put_object → get_object` in-process; a fixture whose bytes contain `airfare` yields the canned airfare
extraction (6 fields at confidence ≥0.90 ≥ the 0.70 routing threshold) → `processing_path=deterministic`,
`field_provenance=deterministic`. The UI asserts: `POST /api/v1/receipts` 201, the "Deterministic OCR"
chip, the "Extracted fields" review, the "OCR Extracted" provenance badge, real extracted values
(United Airlines / $450.00), and the absence of any failure/gap-fill state. See QA finding #1 re
minio/boto3.

## Per-role results

- **employee** — PASS — plan → deterministic-OCR receipt → submit.
- **manager** — PASS — approve pending REPORT + TRIP via UI.
- **finance** — PASS — org-wide Analytics + Policy/Audit/Users reachable, full admin nav.
- **cross-role handoff** — PASS — employee submits via UI → manager approves via UI (two contexts,
  single subject).

## RBAC-in-UI

- Employee nav excludes approvals/analytics/policy/users/audit (positive control asserts planner +
  expenses present, so an empty nav can't trivially pass).
- Employee → /approvals: 403 queue error AND stays on /approvals (deny ≠ redirect).
- /analytics & /policy: finance-only empty-state for non-finance.
- Unauthenticated → /dashboard redirects to /login.

## Selectors

Semantic-only (`getByRole` / `getByLabel` / `getByText`, nav `a[href]`). **No `data-testid` added**
(the tests-only fence held). Future-hardening recommendations (option cards, queue items, report rows,
the decision-reason textarea) are noted only — no production change.

## Self-verification

- **Tier 1:** p7t4 **18/18 green, run twice** (re-runnable via self-seeded fixtures); legacy
  `auth-refresh` + `screens` 7/7 green; `type-check` + `lint` clean; storageState reused (3 logins /
  15 role tests).
- **Tier 2 (break → observe → revert):** (a) manager storageState → `.auth/employee.json` made the
  approve step **FAIL** → reverted; (b) selector `Add From` → `Add Origin` made the plan spec **FAIL**
  → reverted; (c) employee → /approvals block confirmed (error state, not redirected).
- Independently re-verified in 3 separate review passes (code/spec audit, suite re-run, receipt remediation).

## QA findings

1. **boto3 missing from the backend image** (Phase-8 carry-forward) — under `STORAGE_PROVIDER=minio`,
   `POST /api/v1/receipts` 500s at `storage.put_object` (`backend/app/modules/receipt/router.py:86` →
   `services/storage_provider/real.py`) **before OCR runs**. E2E used `STORAGE_PROVIDER=local`. Add boto3
   to the backend image for any minio/S3 deployment.
2. **Running backend image was stale vs HEAD** — its `LocalOcrProvider` ignored byte input; a rebuild
   deployed already-committed HEAD source (no code change) to enable the deterministic-OCR path.
3. **Legacy `state-actions.spec` 3 failures are pre-existing** — consumed one-shot seed fixtures
   (`P6T1 B3 Submit OK` draft; the "Client Visit Expenses" pending approval); the idempotent re-seed
   does not restore decided rows. Confirmed at the data layer + via git history. NOT P7-T4-caused — the
   legacy project runs these specs with identical runtime behavior; P7-T4 self-seeds to avoid the
   non-idempotency.
4. **Live OpenRouter key in `atlas/.env`** — never used by E2E (local providers); `.env` is gitignored;
   rotation recommended.

## Out of scope

Unit (P7-T1), API (P7-T2), AI-eval (P7-T3), backend/frontend production code changes, git push/merge.
No production code modified; no `data-testid` added.
