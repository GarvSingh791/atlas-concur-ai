# Phase Summary

Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T7 — Expenses & Reports CRUD (skeleton, pre-AI)
Date:       2026-06-12

## Objective

Build the expense vertical slice — the third and final RBAC-protected, scope-limited, audited CRUD domain of Phase 2 (after Users and Trips). This sub-phase closes the Phase 2 DoD and Checkpoint.

Deliverables: `expense_reports` + `expenses` ORM models, an incremental migration (`0003_expenses`), the `expense.update.own` permission key + seed mappings, a single-source LEGAL_TRANSITIONS state machine, five RBAC-gated endpoints, derived-total semantics (never client-supplied), audit on create / transition / entry-update, and full live-DB test coverage. No AI, no approval routing — those are Phase 4.

## Work Completed

- **Models**: `ExpenseReport` (full convention set: UUID PK + timestamps + tenant + authorship + version; no `deleted_at` per schema field table and P2-T6 Trip precedent) and `Expense` (UUID PK + timestamps + tenant + authorship only; no `version`, no `deleted_at` — hard-deleted via CASCADE from report). Four SQLAlchemy ENUMs: `ExpenseReportStatus` (draft / submitted / approved / rejected / reimbursed), `ExpenseCategory` (airfare / lodging / meals / ground_transport / other), `ExpenseStatus` (draft / matched / flagged / approved), `ExpenseMatchSource` (receipt / card_txn / manual / trip; nullable).
- **Migration `0003_expenses`**: hand-written (native PG ENUM create/drop is Alembic autogenerate's known weak spot). Creates `expense_reports` then `expenses` with all FKs named via `op.f(...)` (org RESTRICT, owner RESTRICT, trip_id SET NULL, report_id CASCADE, trip_segment_id SET NULL, created_by/updated_by SET NULL), the named CHECK `ck_expenses_amount_non_negative` (`amount >= 0`), and PKs. Clean up/down cycle verified: `alembic upgrade head` → `alembic downgrade 0002` → `alembic upgrade head` all clean.
- **Permissions + seed**: `EXPENSE_UPDATE_OWN = "expense.update.own"` added to `PERMISSION_CATALOG` (the only missing key — read/create/read.reports/read.org pre-existed from P2-T3). Seed: employee and manager gain `EXPENSE_UPDATE_OWN`; finance_admin gains `EXPENSE_READ_OWN` (mirrors `TRIP_READ_OWN` on finance — `scope_query` widens to org-wide; `EXPENSE_READ_ORG` retained as a semantic marker). Seed is idempotent add-only via `sync_role_permissions`.
- **LEGAL_TRANSITIONS state machine**: `draft→submitted` (owner, resubmit after reject allowed via `rejected→draft`); `submitted→approved/rejected`; `approved→reimbursed`; `reimbursed` terminal. Single source of truth for the PATCH handler.
- **Five endpoints** mounted with no prefix (identity router pattern — dual base paths `/expense-reports` and `/expenses` can't collapse to a single prefix):
  - `POST /expense-reports` — create report (draft) + optional entries; total derived
  - `GET /expense-reports` — paginated, scoped, deny-by-default filter/sort
  - `GET /expense-reports/{id}` — single report, 404 on out-of-scope or nonexistent
  - `PATCH /expense-reports/{id}` — state transition; illegal → 409; submit sets `submitted_at`
  - `GET /expense-reports/{id}/entries` — entries only via scope-resolved parent (404 propagates)
  - `PATCH /expenses/{id}` — entry update; parent recomputes `total_amount` atomically
- **Derived total semantics**: `total_amount` is set from `sum(entry.amount)` on report creation (after `session.flush()` makes entries visible to the aggregate) and recomputed via `SELECT COALESCE(SUM(amount), 0) WHERE report_id = ...` after every entry update — in the same transaction as the mutation. No client-supplied total is ever accepted; `total_amount` is absent from all request schemas.
- **Audit**: `record_audit()` (P2-T2, single write path) called in the caller's transaction for `expense_report.created`, `expense_report.status_changed` (with before/after snapshots), and `expense.updated` (with before/after snapshots). JSON-safe snapshots use `str()` for `Decimal`/`date`/`datetime` fields to avoid `Mapped[object]` attribute-access issues.
- **Tests**: 29 new live-DB tests in `test_expenses_api.py`, covering all P2-T7 gate assertions. Full suite: 139/139 passing.

## Files Created

| File | Purpose |
|---|---|
| `database/models/expense_report.py` | `ExpenseReport` model + `ExpenseReportStatus` enum |
| `database/models/expense.py` | `Expense` model + `ExpenseCategory` / `ExpenseStatus` / `ExpenseMatchSource` enums |
| `database/migrations/versions/0003_expenses.py` | Incremental migration: `expense_reports` + `expenses` tables |
| `backend/app/modules/expense/__init__.py` | Module init (empty) |
| `backend/app/modules/expense/schemas.py` | Pydantic request/response schemas |
| `backend/app/modules/expense/service.py` | Business logic: create, list, get, transition, update_entry |
| `backend/app/modules/expense/router.py` | Thin FastAPI router (6 routes, no-prefix mount) |
| `backend/tests/test_expenses_api.py` | 29 live-DB tests covering P2-T7 gate + Phase 2 DoD |
| `docs/phase_logs/P2-T7_expenses_reports_crud.md` | This document |

## Files Modified

| File | Change |
|---|---|
| `database/models/__init__.py` | Added imports + `__all__` entries for all 6 expense symbols |
| `config/constants/permissions.py` | Added `EXPENSE_UPDATE_OWN` constant + `PERMISSION_CATALOG` entry |
| `database/seeds/identity_seed.py` | Added `EXPENSE_UPDATE_OWN` import; updated `_ROLE_PERMISSIONS` for all three roles |
| `backend/app/api/v1.py` | Imported `expense_router`; registered with no prefix |
| `backend/app/modules/expense/service.py` | ruff format reformatted; `str()` snapshot fix; security fix (see §Security) |
| `database/models/expense.py` | ruff I001 import-sort fix + format reformat |
| `backend/tests/test_expenses_api.py` | ruff E501 line-length fix + format reformat |

## Integration Points

| Layer | Reuse |
|---|---|
| P2-T1 mixins | `UUIDPrimaryKeyMixin`, `TimestampMixin`, `TenantMixin`, `AuthorshipMixin`, `VersionMixin` composed into `ExpenseReport`; first three + `AuthorshipMixin` into `Expense` |
| P2-T1 session | Sync SQLAlchemy 2.0 / `get_db` dependency; `expire_on_commit=False` |
| P2-T2 audit | `record_audit()` called in the caller's transaction for all three event types |
| P2-T2 errors | `NotFoundError` (404), `ConflictError` (409), `BadRequestError` (400), `ForbiddenError` (403) — standard envelope via existing handlers |
| P2-T2 pagination | `Page`, `PaginationParams`, `paginated`, `parse_filters`, `parse_sort` |
| P2-T3 auth | `get_current_user` dependency via `require_permission` |
| P2-T4 RBAC | `require_permission(key)` on every route; `scope_query(stmt, user, ownership_column=ExpenseReport.owner_id, org_column=ExpenseReport.organization_id, session)` for all reads and writes |
| P2-T6 pattern | ConflictError reused for illegal transitions and non-draft entry edits; transition validated BEFORE any mutation; version bumped only on state change; `_trip_snapshot` pattern mirrored as `_report_snapshot`/`_entry_snapshot` |
| P2-T6 Trip FK | `expense_reports.trip_id` FK → `trips.id` (SET NULL) — the connected loop; `trip_segments.id` FK on `expenses.trip_segment_id` (SET NULL) — AI smart-match link (Phase 4) |

## Technical Decisions

1. **`total_amount` DERIVED, never client-supplied**: The field is absent from all request schemas. On `create_report`, the total is computed via `SELECT COALESCE(SUM(amount), 0)` after flushing entries. On `update_entry`, the same query recomputes the parent total atomically in the same transaction. This means even if a client sends `"total_amount"` in the JSON body, Pydantic ignores it (no field on the schema) and the derived value is always written.

2. **Entry edits blocked outside `draft`**: `update_entry` checks the parent report's status before any mutation. Non-draft → `ConflictError(409)`, no state change, no audit row. This matches the spec ("entries editable only in draft") and mirrors the illegal-transition pattern from P2-T6.

3. **`ExpenseReport` omits `deleted_at`; `Expense` omits `version` and `deleted_at`**: Matches DATABASE_SCHEMA.md field tables exactly and follows the P2-T6 Trip precedent (no soft-delete where the schema omits it).

4. **Dual base paths, no-prefix mount**: The five endpoints span `/expense-reports/*` and `/expenses/*`, which can't be collapsed to a single prefix. The expense router is registered with `api_router.include_router(expense_router)` (no prefix), matching the identity router pattern.

5. **`trip_id` scope-validated on create**: If `trip_id` is provided, `scope_query` is applied to the `trips` table. Both nonexistent and out-of-scope `trip_id` values return `BadRequestError(400)` — no existence leak.

6. **Entries default to `status=draft`, `match_source=manual`**: On hand-entry (pre-AI Phase 4), entries are manually entered draft items. The AI Expense Agent (Phase 4) will set `match_source` to receipt / card_txn / trip.

7. **`submitted_at` set with `datetime.now(timezone.utc)`**: Keeps the in-memory object correct for the response under `expire_on_commit=False`. Uses `timezone.utc` (not `datetime.UTC`, which requires Python 3.11+).

8. **Approve/reject/reimburse routing deferred to Phase 4**: The LEGAL_TRANSITIONS map includes `submitted→approved/rejected` and `approved→reimbursed`, but those transitions are blocked with `ForbiddenError` until Phase 4 wires up proper per-role authorization (see §Security).

## Security

### Self-Approval Bypass — Closed

**Finding (HIGH)**: `transition_report` checked only that a transition was *legal* (in LEGAL_TRANSITIONS) but not that the *current user was authorized* to make it. Because `scope_query` with `owner_id` lets a user see their own reports, and because `submitted→approved` is a legal transition, any employee or manager with `EXPENSE_UPDATE_OWN` could call `PATCH /expense-reports/{id}` with `{"status": "approved"}` on their own submitted report — a self-approval bypass.

**Fix**: A `_PHASE4_GATED_TARGETS` frozenset (`{approved, rejected, reimbursed}`) is checked immediately after the legality check and before any mutation. If the target is in that set, `ForbiddenError` is raised: *"Approve, reject, and reimburse transitions require Phase 4 Approval module authorization — not yet available."* No state change occurs, no audit row is written.

The LEGAL_TRANSITIONS map is intentionally left intact as the authoritative state machine. Phase 4 will remove this guard and replace it with proper per-role authorization:
- `submitted → approved/rejected`: requires `APPROVAL_DECIDE_REPORTS` AND `report.owner_id != current_user.id` (no self-approval)
- `approved → reimbursed`: requires a finance-admin-only permission

**Dev Rule 9 applied**: fail safe rather than silently permit.

### Open Follow-Up

The P2-T6 Trip transition surface (`transition_trip` in `backend/app/modules/travel/service.py`) has the same structural pattern — legality check without per-role authorization guards. The trip workflow (pending_approval → approved/rejected) faces the same self-approval risk once an approval workflow is layered on. This must be verified and guarded before Phase 4 builds on the workflow.

## Testing Considerations

- **Live-DB suite**: the standing rule (applied since P2-T4) — all tests run against a real Postgres instance. 139 tests total (29 new expense tests + 110 existing), all passing.
- **Hermetic CI**: tests marked `pytest.mark.db` self-skip when Postgres is unreachable. No DB-touching tests run in hermetic CI, only the smoke test (TestClient without lifespan context).
- **`Mapped[object]` date snapshot fix**: SQLAlchemy's `Mapped[object]` type annotation (used for `Date` and `TIMESTAMP` columns throughout the codebase, matching the Trip model precedent) does not expose `.isoformat()` to mypy. Snapshot helpers use `str()` instead — correct at runtime (Python's `date.__str__` returns ISO 8601 format) and mypy-clean.
- **ruff auto-fixes applied**: import sort (I001) in `expense.py`; line-length (E501) in `test_expenses_api.py`; formatter reformatted `service.py`, `expense.py`, and `test_expenses_api.py`.

## Risks Identified

1. **P2-T6 trip self-approval surface**: `transition_trip` has the same structural gap as the fixed `transition_report`. The trip workflow doesn't yet have an approval scenario that makes this exploitable, but Phase 4 approval routing will target trips too. Must be addressed before Phase 4.

2. **`total_amount` staleness under concurrent edits**: `expense_reports` has `version` (optimistic lock field) but `transition_report` does not perform an optimistic-lock check. Under concurrent edits the total could drift if two entry updates race. Optimistic-lock enforcement is a Phase 3 integrity concern (P3-T4 integrity pass).

## Resolution Actions

| Issue | Action | Outcome |
|---|---|---|
| ruff I001 import sort in `expense.py` | `ruff check --fix` | Clean |
| ruff E501 line-length in test file | Manual wrap | Clean |
| ruff format (3 files) | `ruff format` | Clean |
| mypy attr-defined on `Mapped[object]` dates | Changed snapshot helpers to `str()` | mypy clean |
| Self-approval bypass (HIGH) | Added `_PHASE4_GATED_TARGETS` + `ForbiddenError` guard in `transition_report` | 139/139 passing, ruff + mypy clean |

## Phase 2 Closure

**DoD satisfied**: Authenticated, RBAC-protected CRUD for Users, Trips, and Expense Reports/Entries — all mutations audited via the single `record_audit()` interface; consistent error envelope throughout.

**Checkpoint satisfied**: A REST client can log in and exercise core CRUD within role limits — employee sees/edits only own records; manager sees own plus direct reports'; finance_admin sees org-wide — across all three domains, end-to-end.

**Phase 3 — Database Implementation** begins next: remaining models in dependency order (policy, receipts, card_transactions, calendar_events, approvals, conversations, ai_decision_logs), incremental migrations, indexing pass (P3-T3), integrity rules (P3-T4), and full seed. Immediate pre-Phase-3 follow-up: verify and guard the P2-T6 trip transition surface.
