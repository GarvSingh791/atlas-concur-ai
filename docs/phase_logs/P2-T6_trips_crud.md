# Phase Summary
Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T6 — Trips CRUD (skeleton, pre-AI)
Date:       2026-06-12

## Objective

Build the travel vertical slice: `trips` + `trip_segments` models exactly per
`DATABASE_SCHEMA.md`, an incremental Alembic migration for those two tables only,
the deferred `trip.*` permission keys and their RBAC seed mappings, a legal
state-transition map, and five RBAC-gated scope-filtered endpoints
(`POST /trips`, `GET /trips`, `GET /trips/{id}`, `PATCH /trips/{id}` state
transition, `GET /trips/{id}/segments`) with create and transition audit via the
existing `record_audit` interface. No approval workflow, no AI, no non-travel
tables — strictly the skeleton to be extended in Phase 4.

## Work Completed

- **Trip + TripSegment ORM models** per the explicit schema field tables.
  `Trip` composes `UUIDPrimaryKeyMixin + TimestampMixin + TenantMixin +
  AuthorshipMixin + VersionMixin + Base` (no `SoftDeleteMixin`). `TripSegment`
  composes only `UUIDPrimaryKeyMixin + TimestampMixin + Base`.
- **Incremental migration `0002_trips`** covering only `trips` and
  `trip_segments`: native PostgreSQL `trip_status` and `segment_type` enum types,
  `CHECK (end_date >= start_date)`, trip_segments cascade FK (`ON DELETE
  CASCADE`), and all FK constraints. Applies and downgrades cleanly.
- **Three `trip.*` permission constants** (`trip.read.own`, `trip.create.own`,
  `trip.update.own`) added to the constants catalog; seeded idempotently into
  the RBAC catalog — read mapped to all three roles (employee, manager,
  finance_admin), create/update mapped to employee and manager only.
- **`LEGAL_TRANSITIONS` map** in the travel service (the single source of
  transition truth): `draft→{pending_approval, cancelled}`,
  `pending_approval→{approved, rejected, cancelled}`,
  `approved→{completed, cancelled}`, terminal states have no outgoing
  transitions.
- **`backend/app/modules/travel/`** — `schemas.py` (`TripCreate`,
  `SegmentCreate`, `TripResponse`, `TripListItem`, `SegmentResponse`,
  `TripTransition`), `service.py` (five service functions), `router.py` (five
  thin endpoints registered under `/api/v1/trips`).
- **`ConflictError` (409)** added to the shared `AppError` hierarchy in
  `backend/app/core/errors.py` for illegal transition responses.
- **21 new live-DB tests** in `backend/tests/test_trips_api.py` covering all
  Validate gate conditions.

## Files Created

| File | Purpose |
|---|---|
| `database/models/trip.py` | `Trip` model + `TripStatus` enum |
| `database/models/trip_segment.py` | `TripSegment` model + `SegmentType` enum |
| `database/migrations/versions/0002_trips.py` | Incremental migration (trips + trip_segments only) |
| `backend/app/modules/travel/__init__.py` | Module package marker |
| `backend/app/modules/travel/schemas.py` | Pydantic request/response schemas |
| `backend/app/modules/travel/service.py` | Business logic, transition map, service functions |
| `backend/app/modules/travel/router.py` | Five FastAPI endpoints (thin router) |
| `backend/tests/test_trips_api.py` | 21 live-DB Validate gate tests |
| `docs/phase_logs/P2-T6_trips_crud.md` | This file |

## Files Modified

| File | Change |
|---|---|
| `database/models/__init__.py` | Added `Trip`, `TripStatus`, `TripSegment`, `SegmentType` exports so Alembic `env.py`'s single `import database.models` picks up all tables |
| `config/constants/permissions.py` | Added `TRIP_READ_OWN`, `TRIP_CREATE_OWN`, `TRIP_UPDATE_OWN` constants and three entries to `PERMISSION_CATALOG`; updated docstring noting `trip.*` keys are no longer deferred |
| `database/seeds/identity_seed.py` | Added `trip.*` key imports; updated `_ROLE_PERMISSIONS` mapping (add-only, idempotent via `seed_rbac_catalog`) |
| `backend/app/core/errors.py` | Added `ErrorCode.CONFLICT` and `class ConflictError(AppError)` (409) |
| `backend/app/api/v1.py` | Registered travel router: `api_router.include_router(travel_router, prefix="/trips", tags=["trips"])` |

## Integration Points

- **P2-T1 mixins + session**: `Trip`/`TripSegment` composed from `database/models/mixins.py`; sync `Session` via `get_db()`.
- **P2-T2 `record_audit`**: the single write path for `trip.created` and `trip.status_changed` events — called inside the mutation transaction; no direct writes to `audit_log`.
- **P2-T2 error envelope + pagination**: `NotFoundError`, `ForbiddenError`, `ConflictError` (new), `BadRequestError`; `Page[TripListItem]` + `paginated` dependency for `GET /trips`.
- **P2-T3 authentication**: `get_current_user` via `Depends` on every endpoint.
- **P2-T4 `scope_query`**: applied with `ownership_column=Trip.traveler_id` and `org_column=Trip.organization_id`; employee→own, manager→own+direct-reports, finance_admin→org-wide. `require_permission` gates each endpoint.
- **Segment access pattern**: segments are never queried directly — `get_trip_segments` resolves the parent trip through `scope_query` first; if the trip is out of scope or nonexistent the caller receives 404 and segments are never reached.

## Technical Decisions

1. **`Trip` omits `deleted_at` (no `SoftDeleteMixin`).** The explicit
   `DATABASE_SCHEMA.md` trips field table (lines 101–121) does not include
   `deleted_at`. This takes precedence over the general convention note
   ("soft-delete where records must be retained for audit") and over the
   build spec's "full convention set" parenthetical. Trips retire via
   `status = 'cancelled'`; no hard-delete endpoint exists this phase. Safe
   because no global soft-delete query filter is applied anywhere in the
   codebase yet.

2. **`ConflictError` (409) added to the shared `AppError` hierarchy.** The
   existing error module explicitly notes "add more as needed." The 409
   response is used exclusively for illegal state transitions — the transition
   guard fires before any mutation, guaranteeing no state change and no audit
   row on the illegal path.

3. **`trip_segments` carries only UUID PK + timestamps.** Exact per the
   schema field table: no `organization_id`, `created_by`, `updated_by`,
   `version`, or `deleted_at`. Trip segments are true children reached only
   through a parent trip; their tenant context is inherited transitively.

4. **Permission mapping: read → all three roles; create/update → employee +
   manager only.** `TECHNICAL_DESIGN.md` (lines 176–179) defines
   `finance_admin` as "org-wide read, policy management, audit, and override";
   the existing expense seed withholds `expense.create.own` from finance as
   precedent. Finance override for trip approval/rejection is a Phase 4
   (Approval module) concern.

5. **`version` bumped on transition; full optimistic-lock enforcement deferred
   to P3-T4.** The `VersionMixin` column is present and incremented on each
   transition, maintaining consistency for future P3-T4 conflict-detection
   work without over-building.

6. **Approve/reject authorization routing deferred to Phase 4.** The
   `LEGAL_TRANSITIONS` map includes `pending_approval → approved/rejected` as
   valid targets at the state-machine level; the Phase 4 Approval module will
   add role-level authorization gates on top of that generic transition
   machinery.

## Testing Considerations

- **Full 76-test live-DB suite passed** (against `atlas_db` with live
  PostgreSQL). This applies the standing post-P2-T4 rule: always run the
  complete live-DB suite to catch breakage that hermetic self-skips hide.
  Suite covers `test_audit.py` (3), `test_db_conventions.py` (1),
  `test_identity_auth.py` (13), `test_rbac.py` (9), `test_record_scoping.py`
  (7), `test_trips_api.py` (21), `test_users_api.py` (22).
- **39-test hermetic suite also green** (self-skips all DB tests when
  Postgres unreachable — CI baseline).
- **`test_trips_api.py` 21/21**: exercises all Validate gate conditions
  including scope isolation per role, legal/illegal transitions, audit row
  count verification (before/after state), cross-org isolation, 404 on
  out-of-scope and nonexistent IDs, 403 on missing permission, 422 on invalid
  date range, and structural column assertions on `trip_segments`.
- **FK RESTRICT teardown ordering fix**: the fixture's stale-prune block and
  teardown both required FK-safe deletion order — `audit_log` →
  `trips` (cascades `trip_segments`) → `users` → `organizations` — because
  `trips.traveler_id → users` and `audit_log.organization_id → organizations`
  carry `RESTRICT`. The initial teardown reversed the order and failed on
  the first full run.

## Risks Identified

- **Trip `deleted_at` divergence is intentional and recorded here.** Future
  table designs and code readers should understand that the absence of
  `deleted_at` on `trips` is a deliberate schema-fidelity decision, not an
  omission. If a soft-delete requirement emerges for trips it will require a
  new migration column and a query-filter update.
- **Approve/reject routing not yet built.** Any caller that transitions to
  `pending_approval` will find no role-level authorization gate on the
  `approved`/`rejected` outgoing transitions. That gate is Phase 4 scope;
  the `LEGAL_TRANSITIONS` map at the state-machine level is the only
  enforcement in place today.

## Resolution Actions

- Schema fidelity win for `trip_segments` confirmed (structural test
  `test_trips_no_deleted_at` + `test_trip_segments_no_org_authorship_version`
  enforce it going forward).
- FK-RESTRICT teardown order documented and fixed; pattern established for
  future test fixtures that involve the trips table.
