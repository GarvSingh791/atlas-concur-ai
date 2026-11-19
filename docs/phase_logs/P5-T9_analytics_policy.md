# P5-T9 — Analytics & Policy/Admin UI

**Sub-phase:** P5-T9
**Title:** Finance Analytics & Policy/Admin surfaces (+ policy read/activation API + analytics aggregates)
**Date:** 2026-06-16
**Author:** Garv Singh Saini
**User stories:** US-F3 (finance analytics), US-F1 (policy view + near-NL rule authoring + versioning), FR-A4 (authoring creates a DRAFT for human review, never auto-active)

---

## What Was Built

### STAGE A — Approved option-2 backend (read / aggregate / activate only — NO schema/migration)

The Section-2.E reconciliation found that the policy READ surface did not exist (only the authoring action did), the draft-activation endpoint was missing, and the finance analytics payload lacked compliance + per-department budget data. With explicit approval, a backend-first path was taken: read/aggregate/activate logic + seed only, no schema or migration.

**Modified:**

- `backend/app/modules/policy/schemas.py` — `PolicyResponse`, `PolicyRuleRead`, `PolicyRuleStatusUpdate` (`status: Literal["active","archived"]` — draft is never a PATCH target), and an enriched `PolicyRuleResponse` that now echoes `rule_type / applies_to / definition / source_clause_ref / version` so the UI renders the AI-Generated draft card without a second fetch.
- `backend/app/modules/policy/service.py` — `get_active_policy_for_org`, `list_active_rules` (org-scoped via the parent policy; optional `status` / `applies_to` filters), `update_rule_status` (tenant guard + forward-only transition guard: draft→active, draft→archived, active→archived; flush-only, caller commits).
- `backend/app/modules/policy/router.py` — `GET /policies`, `GET /policy-rules`, `PATCH /policy-rules/{id}` (POLICY_MANAGE; writes `record_audit` with `policy_rule.activated` / `policy_rule.archived`, then commits). The authoring endpoint now returns the enriched draft. Read endpoints are POLICY_READ-gated.
- `backend/app/modules/analytics/schemas.py` + `service.py` — `FinanceOverview` gains `policy_compliance {compliant, exceptions, violations}` (derived from org-scoped `Approval.current_status`) and a per-department `budget` on `spend_by_department` (from `Trip.budget_amount`, joined via `traveler_id`, grouped by the same email-prefix proxy). The existing `amount` (actual) key is RETAINED so the P5-T4 dashboard is untouched.
- `database/seeds/seed_all.py` — idempotent `_seed_draft_rule` (one `car` `amount_cap` DRAFT) wired after `_seed_policy`, so the navigator exercises draft status dots and the activation flow.

### STAGE B — Frontend (P5-T9)

**New:**

- `frontend/lib/types/policy.ts` — DTO mirror (Policy, PolicyRule, enums, AuthorRuleRequest/Response, UpdateRuleStatusRequest).
- `frontend/lib/query/policy-hooks.ts` — `usePolicy`, `usePolicyRules`, `useAuthorRule`, `useUpdateRuleStatus` (+ `policyKeys` factory).
- `frontend/components/dashboard/widgets/area-chart-widget.tsx`, `compliance-donut-widget.tsx`, `department-budget-widget.tsx` — recharts; siblings to the P5-T4 BarChart widget (which is left untouched).
- `frontend/components/policy/rule-status-badge.tsx`, `rule-definition.tsx`, `rule-authoring.tsx`.
- `frontend/__tests__/analytics/finance-widgets.test.tsx`, `frontend/__tests__/policy/rule-authoring.test.tsx`, `frontend/__tests__/policy/policy-reads.test.tsx` — 11 tests.

**Modified:**

- `frontend/lib/types/analytics.ts` — `SpendByDepartment` gains `budget`; `PolicyComplianceBreakdown` added and wired onto `FinanceOverview`. (No barrel change: `lib/types/index.ts` does not re-export `policy.ts`, mirroring `analytics.ts` which is also imported directly.)
- `frontend/components/atlas/status-badge.tsx` — `rule-draft` / `rule-active` / `rule-archived` variants (same sanctioned-extension precedent as P5-T8's `auto-approved` / risk variants).
- `frontend/app/(app)/analytics/page.tsx` + `frontend/app/(app)/policy/page.tsx` — stubs replaced with the real screens.

---

## Key Decisions

- **Backend-first (approved):** policy reads + activation, and the two analytics aggregates — read/aggregate/activate only, NO schema/migration.
- **Dashboard fence held WITHOUT editing it:** kept `amount`, ADDED `budget` (the dashboard reads `valueKey="amount"`), so no change to the P5-T4 dashboard was needed.
- **Status dots:** extended the shared `StatusBadge` primitive (P5-T8 precedent) rather than introducing a one-off pill.
- **Server owns truth:** risk/policy stays server-side; the client renders aggregates and the server's structured draft — no client metric-compute, no client NL→rule translation, no auto-activation.

---

## Deviations / Interpretive Notes (code wins)

- **The compliance donut is DERIVED, not measured.** There is no `policy_evaluations` table, so the breakdown maps approval outcomes: `auto_approved` = compliant, `approved + changes_requested` = exceptions, `rejected` = violations. Documented as interpretive; swappable to a `pre_submit_flags` source later.
- **Per-rule version is sourced from the PARENT policy.** `policy_rules` has no `version` column; a per-rule version would require a migration (out of scope).
- **Department spend still uses the email-prefix PROXY.** A real `User.department` column exists, but the existing analytics intentionally uses the proxy; budget is aggregated by the same proxy for a consistent key space.

---

## Issues Found / QA

- **Backend test gap (flagged honestly):** the new policy read/activation endpoints and analytics aggregates are verified by LIVE CURL (G1–G4) but have NO new pytest coverage — the 11 new tests are all frontend. `py_compile` is clean and the existing policy/analytics DB tests pass (no regressions). A follow-up backend-test task is recommended.
- **Verification side-effect (local dev DB only):** one extra authored `car` `amount_cap` rule was created and activated during the live G4 run — harmless test data; the idempotent seed will not recreate it. No code or commit impact.

---

## Validation Evidence

**Live (seeded backend, finance token):**

- **G1** — `GET /analytics/overview` (finance): 5 KPIs (Consolidated Spend, Automation Rate, Exception Rate, Avg Cycle Time, Budget Posture) + `spend_over_time` + `spend_by_department` with `amount`+`budget` + `policy_compliance {compliant:1, exceptions:1, violations:1}` + `spend_by_category`.
- **G2** — employee `GET /analytics/overview` → `finance: null` (server-scoped; no client re-scope).
- **G3** — `GET /policies` (v1) + `GET /policy-rules` (8 rows; the seeded draft surfaces; `?status=draft` filter returns 1).
- **G4** — `POST /policies/{id}/rules` → `status='draft'` enriched, NOT auto-activated; `PATCH /policy-rules/{id}` draft→active; revert-to-draft rejected (422); audit `['policy_rule.drafted', 'policy_rule.activated']`.
- **G5/G6** — greps: the policy screen only passes the server's `definition` (no client synthesis); the analytics page has no raw-row aggregation.
- **G7** — draft-only: activation only via the real PATCH.
- **G9** — employee receives 403 on authoring + activation (POLICY_MANAGE).

**Static:** `tsc` 0 errors; `eslint` clean; `next build` ✓ (`/analytics` + `/policy`); 149/149 vitest (11 new); backend `py_compile` clean; existing policy/analytics DB tests pass. (ruff / mypy on the new backend confirmed at commit-prep CI parity.)

---

## Deferred / Flagged

- Backend pytest for the new policy/analytics endpoints (recommended follow-up).
- **In-place rule editing:** no rule-edit endpoint — "Edit" re-opens the intent for re-authoring (surfaced honestly in the UI).
- **Version history:** shows `policy.version` + an honest "full change history coming soon" gap (PATCH writes audit, but there is no audit-READ endpoint in scope).
- Compliance derivation is interpretive (above).
- A per-rule `version` column / a real department dimension would each require a migration.
- The reusable FAB AI assistant remains P5-T10.
