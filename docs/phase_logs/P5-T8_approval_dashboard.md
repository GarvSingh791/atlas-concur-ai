# P5-T8 — Approval Dashboard UI

**Sub-phase:** P5-T8  
**Title:** Approval Dashboard — role-scoped queue + decision panel + auto-approved lane  
**Date:** 2026-06-16  
**Author:** Garv Singh Saini  
**User stories:** US-M1, US-M2 (reverse-within-policy), US-M3 (team-scoping), US-F2 (org-wide oversight)

---

## What Was Built

### STAGE A — Backend Prerequisite (approved; original P5-T8 fenced this out)

`GET /approvals` and `GET /approvals/{id}` did not exist (only POST + PATCH). The backend prerequisite path was chosen to create the approval READ surface before building the frontend against it. This is a documented, approved scope expansion — not a fence breach.

**Modified:**

- `config/constants/permissions.py` — Added `APPROVAL_READ = "approval.read"` constant with the mirrored TRIP_READ_OWN multi-role gate pattern; added entry to `PERMISSION_CATALOG`.
- `database/seeds/identity_seed.py` — Granted `approval.read` to the manager role; granted `approval.read` and `approval.decide.reports` to finance_admin (finance previously had no approval permissions at all).
- `database/seeds/seed_all.py` — Added a pending expense approval for Morgan's team (submitter = Alex): real `build_risk_summary` escalate shape with HOTEL-CAP + MEALS-PD clause refs and a blocking flag; upgraded auto-approved approval #5 to the real agent shape (real risk_summary dict + a system `ApprovalStep` with `approver_id=None, decision=auto_approve, reason=` rationale text); added a trip-subject pending approval (LAX→ORD, hotel segment out-of-policy) with real trip risk_summary shape. Summary: 6 reports, 6 approvals (incl. 1 trip-subject), 3 decision steps.
- `backend/app/modules/approval/router.py` — Added `GET /approvals` (paginated, scope-gated via `APPROVAL_READ`); added `GET /approvals/{id}` (scope-gated); rewrote `PATCH /approvals/{id}` as a thin wrapper that adds `Depends(if_match_version)` and delegates to `service.decide_approval()`, returning `ApprovalDetailResponse`.
- `backend/app/modules/approval/schemas.py` — Added `ApprovalListItem` (with server-derived `risk_level`, `auto_approve_rationale`), `ApprovalStepResponse`, `ApprovalDetailResponse` (embeds raw `risk_summary` + `risk_level` + `steps` + mutually-exclusive `expense_report: ExpenseReportContext | None` or `trip: TripContext | None`), `ExpenseReportContext`, `TripContext`.
- `backend/app/modules/approval/service.py` — Added `list_approvals()` (scope_query + allow-listed filters + prioritized default order + `_to_list_items()` batch resolution); `get_approval_detail()` (loads subject polymorphically with org_id guard, queries `ApprovalStep` directly, batches approver names); `decide_approval()` (`guarded_version_update` CAS, validates decidable status ∈ {pending, auto_approved}, reason-enforcement for reject/request_changes, appends step, audit, returns detail). Helpers: `_parse_bool`, `_apply_filters`, `_apply_sort`, `_apply_default_order`, `_as_decimal`, `_subject_summary_fields`, `_to_list_items`, `_load_subject_context`.
- `backend/tests/test_approvals_api.py` — Updated `test_decide_approval_not_pending` → `test_decide_reverse_on_auto_approved` (reverse now 200); added `test_decide_on_resolved_approval_conflict` (second decide on already-approved → 409).

**New:**

- `backend/app/modules/approval/risk.py` — Server-side `derive_risk_level(*, risk_summary, is_auto_approved) -> Literal["low","medium","high"]`. Deterministic: auto_approve route or is_auto_approved → low; blocking_flag_count ≥ 1 → high; escalate → medium.
- `backend/tests/test_approval_risk.py` — 7 pure unit tests (no DB); covers all branches including None risk_summary, is_auto_approved override, int vs non-int blocking_flag_count.
- `backend/tests/test_approvals_read_api.py` — 32 DB integration tests (`@pytest.mark.db`). Covers: manager sees only team, finance sees org-wide, employee 403, no-token 401; blocking → high risk, auto-approved → low + rationale, trip → medium; filter by status; invalid filter field → 400; expense_report context (entries, clause ref, steps=0, version); trip context (segments, no clause ref); auto-approved step rationale; manager cannot read out-of-team → 404; stale If-Match → 409; success increments version 1→2.

---

### STAGE B — Frontend P5-T8

**Modified:**

- `frontend/app/(app)/approvals/page.tsx` — Replaced 14-line EmptyState placeholder with `<ApprovalsScreen />`.
- `frontend/components/atlas/status-badge.tsx` — Extended `StatusBadgeVariant` type and CONFIG entries: `auto-approved` (Sparkles, AI surface/primary colors), `changes-requested` (RotateCcw, amber), `risk-low` (ShieldCheck, green), `risk-medium` (ShieldAlert, amber), `risk-high` (ShieldAlert, red). Sanctioned extension — no new primitive.
- `frontend/lib/types/identity.ts` — Added `"approval.read"` to `PermissionKey` union; added `"approval.read"` to manager and `"approval.read"` + `"approval.decide.reports"` to finance_admin in `ROLE_PERMISSIONS` (mirrors identity_seed.py exactly).
- `frontend/lib/types/index.ts` — Added `export * from "./approvals"` barrel export.

**New:**

- `frontend/lib/types/approvals.ts` — DTO mirrors: `ApprovalListItem`, `ApprovalDetail`, `ApprovalStepResponse`, `ApprovalExpenseContext`, `ApprovalTripContext`, `ApprovalTripSummary`, `ApprovalTripSegment`, `RiskSummary` (with `[key: string]: unknown` for forward-compatibility), `ExpenseRiskFlag`, `TripRiskFlag`. Money fields typed as `string | null` (Decimal→JSON wire format).
- `frontend/lib/query/approval-hooks.ts` — `useApprovals(params?)` (GET /approvals, TanStack Query), `useApproval(id: string | null)` (GET /approvals/{id}, disabled when null), `useDecideApproval()` (PATCH mutation with `If-Match` header, invalidates list + detail on success, surfaces 409 CONFLICT code).
- `frontend/lib/approvals/format.ts` — `formatMoney()`, `formatDate()`, `statusBadgeVariant()`, `statusLabel()`, `riskBadgeVariant()` (ApprovalStatus / RiskLevel → StatusBadgeVariant).
- `frontend/lib/approvals/risk-summary.ts` — `toDecisionView(riskSummary, subjectType) → DecisionView`. Pure reshaping — no scoring or risk derivation. Expense: all flags → `ExceptionView` with `entry_ref` as label, `source_clause_ref` as clauseRef. Trip: only `in_policy === false` flags become exceptions, `clauseRef = null` always, generates `inPolicyNote`. `ExceptionSeverity`: "blocking" | "advisory" | "info".
- `frontend/components/approvals/ai-analysis.tsx` — AI analysis section on `--color-ai-surface` / `--color-ai-primary`; renders route verdict + 4 policy-signal fields (inPolicy, blockingCount, requiresApprover, totalUsd); label reads "Deterministic policy evaluation — structured signals only, no generated narrative."
- `frontend/components/approvals/exceptions-list.tsx` — Renders `ExceptionView` list; `CitationChip` when `clauseRef` present; `AlertTriangle` color by severity (blocking=red, advisory=amber); empty state with `CheckCircle2`.
- `frontend/components/approvals/trip-segments.tsx` — Segment list; StatusBadge in-policy/out-of-policy; `policy_notes` shown as amber warning.
- `frontend/components/approvals/decision-history.tsx` — Read-only `<ol>` of `ApprovalStepResponse`; `approver_id === null` → "System"; shows reason in quotes.
- `frontend/components/approvals/decision-actions.tsx` — Approve (green, immediate) / Reject (red, reason REQUIRED, disabled until filled) / Request changes (amber, reason required) via `Modal variant="reason"`; carries `version` in `If-Match` header via `useDecideApproval`; 409 → error toast "This approval changed — never silent overwrite; reverse note shown for `auto_approved` status; resolved statuses show disabled message; `canDecide` prop gates all actions.
- `frontend/components/approvals/auto-approved-lane.tsx` — Collapsed chevron section "Auto-Approved (N)"; expands to show each item with `auto_approve_rationale` italic (line-clamp-2) + Low risk badge. GUARDRAIL: no auto-rejected items rendered.
- `frontend/components/approvals/approval-queue.tsx` — Splits items: `autoItems` → `AutoApprovedLane`; `queueItems` → active list. Display filters: risk pill filters + text search (submitter or subject; display-only, NOT authority filtering). Unread dots for unseen pending items. Selected row: 4px indigo border + `bg-[var(--color-primary-50)]`. Loading/error/empty states.
- `frontend/components/approvals/decision-panel.tsx` — Full orchestrator; calls `toDecisionView()`; renders `AiAnalysis` + `ExceptionsList` + `EntriesTable` (expense) or `TripSegments` (trip) + `DecisionHistory` + `DecisionActions`; mobile "Back to queue" button (lg:hidden); "Select an approval to review its details." prompt when `approvalId === null`.
- `frontend/components/approvals/approvals-screen.tsx` — Split layout `lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]`; `useApprovals({ size: 100 })` single scoped fetch; `selectedId` state + `viewedIds` Set; mobile stacks queue/panel on selection; `canDecide = hasPermission("approval.decide.reports")`.
- `frontend/__tests__/approvals/risk-summary.test.ts` — 6 pure unit tests for `toDecisionView`.
- `frontend/__tests__/approvals/approval-queue.test.tsx` — 10 component tests: active rows, auto-approved lane collapsed/expanded, risk filter, search filter, unread dots, onSelect, empty state, error+retry, aria-current.
- `frontend/__tests__/approvals/decision-panel.test.tsx` — 5 tests with `QueryClientProvider` wrapper: select-prompt, expense_report subject (AI analysis + citation chip + entries, G10), trip subject (segments, no citation), error state, decision history.
- `frontend/__tests__/approvals/decision-actions.test.tsx` — 7 tests: no-permission hides buttons, resolved shows disabled, approve sends version (If-Match), reject requires reason, request_changes requires reason, 409 → error toast, auto_approved shows reverse note.

---

## Key Decisions

- **Reverse-within-policy implemented live (US-M2 AC2):** A human decision on an `auto_approved` item records a new `ApprovalStep` and transitions status; `_DECIDABLE_STATUSES = frozenset({pending, auto_approved})`. Already-resolved statuses (approved, rejected, changes_requested) remain 409.
- **Risk level derived server-side:** `derive_risk_level()` in `backend/app/modules/approval/risk.py` computes Low/Medium/High from deterministic signals at serialization time. The client only renders the field — no client scoring, no client policy evaluation.

---

## Authorized Adjustments / Deviations from Design Docs (code wins)

| # | Deviation | Reason |
|---|-----------|--------|
| 1 | **Backend-first path (approved):** Stage A built the read endpoints + RBAC that the original frontend-only P5-T8 fenced out. | `GET /approvals` / `GET /approvals/{id}` did not exist; the frontend could not be built without a real contract. Documented scope expansion, explicitly approved. |
| 2 | **RBAC simplified to one `approval.read` gate** (manager + finance) + `scope_query` narrowing, instead of the plan's three granular keys (`approval.read.reports` + `approval.read.org`). | Single-key `require_permission()` cannot OR keys; mirrors the codebase's `TRIP_READ_OWN` multi-role precedent. Same access outcomes. |
| 3 | **No confidence indicator.** | The Approval Agent (`evaluate.py::build_risk_summary`) is a deterministic policy router — it emits no confidence score and no narrative. The plan's UI confidence indicator was derived from the idealized design doc, not the real agent output. The AI analysis surface instead carries the label "Deterministic policy evaluation — structured signals only, no generated narrative." |
| 4 | **`StatusBadge` extended** to the full status enum + Low/Med/High risk variants. | Sanctioned extension of an existing P5-T1 primitive; no new design-system primitive introduced. |
| 5 | **`PATCH /approvals/{id}` gained `If-Match` concurrency** (was unwired); reverse on `auto_approved` was unblocked. | Both were gaps vs the P5-T8 acceptance criteria. Fixed as part of Stage A — minimal change to the existing decision endpoint. |

---

## Reconcile Findings (the real API before Stage A)

| Item | State before Stage A |
|------|---------------------|
| `GET /approvals` | Did not exist — only POST + PATCH. |
| `GET /approvals/{id}` | Did not exist. |
| `risk_summary` shape | Untyped `dict[str,Any]` from `evaluate.py::build_risk_summary`: `{route, subject_type, subject_in_policy, blocking_flag_count, requires_approver, subject_total_usd, flags[], routed_to_approver_id?}`. No confidence, no narrative, no risk level. Auto-approve rationale lives in the `auto_approve` step's `reason` field (`persistence.py::_build_auto_approve_rationale`). |
| `PATCH` concurrency | `If-Match` / `version` not wired — now added via `if_match_version` + `guarded_version_update`. |
| `PATCH` reverse | Blocked on non-pending statuses — now allowed for `auto_approved`. |
| Detail payload | Missing `steps` and subject (only `subject_id` / `subject_type`) — now serialized reusing `ExpenseReport` / `Trip` schemas. |
| Finance permissions | No approval permissions at all — `APPROVAL_READ` and `APPROVAL_DECIDE_REPORTS` added to `finance_admin`. |

---

## Issues Found / QA

- **Pre-existing test failure (NOT introduced by P5-T8):** `backend/tests/test_receipts_api.py` teardown leaks `ai_decision_logs` rows (P2-T6 test hygiene). Fails in isolation. Zero receipt files were changed by P5-T8. Left for a separate hygiene fix; must not be touched in this commit.
- **Live-DB enrichment caveat:** The running dev DB was not wiped (destructive-op policy). The full enriched-seed browser demo (Morgan's pending expense + trip + rationale) requires `docker compose down -v` → migrate → `python -m database.seeds.seed_all`. Manager2 (`manager2@atlas-demo.internal`) already has demonstrable pending + auto-approved items from the pre-enrichment seed. The enriched shapes are proven by the matching pytest fixtures + clean seed import (no import errors).

---

## Validation Evidence

| Gate | Result |
|------|--------|
| G1 — live curl read contract | `GET /approvals` → 401 unauth ✔; manager2 → 2 items (own report only) ✔; manager → 2 items ✔; finance → 4 (org superset) ✔ |
| G2/G3 — scoping proof over HTTP | Manager-scoped and finance org-superset confirmed live ✔ |
| G4 — risk_summary → panel mapper | `test_approval_risk.py` (7 unit) + panel render tests (decision-panel.test.tsx) ✔ |
| G5 — decisions persist + audit | `pytest test_approvals_api.py` + `test_approvals_read_api.py` (approve / reject-requires-reason / request_changes) ✔ |
| G6 — request_changes | Tested in backend (reason required) + frontend action test ✔ |
| G7 — version conflict 409 | `test_decide_on_resolved_approval_conflict` (backend) + `decision-actions.test.tsx` 409 toast path ✔ |
| G8 — auto-approved rationale, no auto-rejected | Rationale field present in list item; lane renders rationale; no auto-rejected lane rendered ✔ |
| G9 — no client intelligence | `grep` gate: no client risk scoring / policy evaluation in frontend ✔ |
| G10 — polymorphism (expense + trip) | `test_approvals_read_api.py` TestDetail; `decision-panel.test.tsx` expense + trip cases ✔ |
| G11 — token hygiene | grep gate: no raw token in frontend source ✔ |
| G12 — primitives / tokens only | grep gate: no raw hex; AI surface uses `--color-ai-*` ✔ |
| Backend pytest (live Postgres) | **39/39 approval tests pass** (7 unit + 32 DB integration); regression suite green; mypy clean; ruff clean ✔ |
| Frontend build chain | `tsc` 0 errors · `next lint` clean · `next build` ✔ (`/approvals` 7.4 kB) · **138/138 vitest** (28 new + 110 pre-existing, zero regressions) · `/approvals` → HTTP 200 ✔ |

---

## Deferred / Flagged

- **Finance exception-type / department / date filters** — limited to `status`, `subject_type`, `is_auto_approved` in this sub-phase; deeper filter surface deferred to P5-T9.
- **Pagination UI** — queue fetches `size=100` in a single call; paginated navigation UI deferred.
- **Pre-existing receipts test-teardown hygiene (P2-T6)** — separate hygiene fix, not in this commit.
- **Full enriched browser demo** — requires a re-seed; see Issues section above.
