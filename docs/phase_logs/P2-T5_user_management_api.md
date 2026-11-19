# Phase Summary
Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T5 — User Management API
Date:       2026-06-12

## Objective
Add `GET /users` and `GET /users/{id}` endpoints, gated by `USERS_READ` permission
and scoped by `scope_query`, producing the first production wiring of the P2-T4 RBAC
infrastructure onto live API routes. The endpoints must return a safe `UserResponse`
schema (no `password_hash`), support pagination/filtering/sorting with deny-by-default
validation, and respect the full role-based scoping matrix (finance→org, manager→self
+reports, employee→self, always tenant-bounded).

## Work Completed
- `GET /users`: paginated (`?page`, `?size`), filterable (`?role`, `?status`, `?email`,
  `?name`, `?department`), sortable (`?sort=field:asc|desc` or `?sort=-field`); gated by
  `require_permission(USERS_READ)`; scoped via `scope_query(User.id, User.organization_id)`.
- `GET /users/{user_id}`: single-record lookup; same gate and scope; returns 404 for
  both out-of-scope and nonexistent IDs (no existence leak).
- `UserResponse` Pydantic v2 schema (`from_attributes=True`): safe fields only
  (`id`, `name`, `email`, `role`, `department`, `manager_id`, `status`, `organization_id`,
  `created_at`); `password_hash` has no schema field and is structurally impossible to return.
- Deny-by-default filter/sort allow-lists: unknown fields raise `BadRequestError` (400)
  rather than being silently ignored.
- 22 test cases in `test_users_api.py` covering all role/scope/pagination/filter/sort
  combinations, plus the Validate gate cases.

## Files Created
- `backend/tests/test_users_api.py` — 22 test cases (live-DB; self-skips when Postgres
  unreachable)

## Files Modified
- `backend/app/modules/identity/router.py` — added `GET /users` and `GET /users/{user_id}`
  routes with `require_permission(USERS_READ)` gate and `scope_query` composition
- `backend/app/modules/identity/service.py` — added `list_users` and `get_user` functions;
  `_apply_filters`/`_apply_sort` helpers; `USER_FILTER_FIELDS` and `USER_SORT_FIELDS`
  allow-lists
- `backend/app/modules/identity/schemas.py` — added `UserResponse` (Pydantic v2,
  `from_attributes=True`, no `password_hash` field)
- Three files auto-reformatted by `ruff format` (no logic changes)

## Integration Points
- **First production wiring of P2-T4 RBAC**: `require_permission(USERS_READ)` (gate) +
  `scope_query` (record filter) are composed together for the first time on live endpoints.
  `scope_query` is called with `ownership_column=User.id` and `org_column=User.organization_id`,
  which maps the general scoping contract onto the users resource: finance→all org users,
  manager→self+direct reports, employee→self only.
- **P2-T2 reuse**: `Page`, `PaginationParams`, `paginated`, `parse_filters`, `parse_sort`,
  `BadRequestError`, `NotFoundError` all reused unchanged.
- **P2-T3 identity module**: routes mounted in the existing identity router; auth/session
  dependencies reused.

## Technical Decisions
- **`scope_query` ownership mapping**: `User.id` is the ownership column for the users
  resource, making the manager-branch predicate `User.id IN (manager.id, *direct_report_ids)`
  — exactly the intended "self + reports" set. Tenant isolation via `User.organization_id`
  is unconditional and applied first.
- **`UserResponse` schema design**: Pydantic v2 `from_attributes=True`; `password_hash` is
  absent from the schema definition rather than excluded via `exclude=` at serialization time,
  so it is impossible to accidentally return it through future refactors.
- **403 vs 404 distinction**: `require_permission` raises 403 when the caller lacks the
  permission entirely (permission check happens before any DB query). `get_user` raises 404
  for both out-of-scope and nonexistent IDs — the caller never learns whether a record exists
  outside their scope.
- **Deny-by-default filter/sort**: unknown query parameter keys raise 400 immediately;
  `role` and `status` values are validated against their enums with descriptive 400 messages.
- **Seed mapping already correct**: `finance_admin` already had `USERS_READ` via the P2-T4
  seed; `employee` did not. No seed change was needed.
- **No new model, no migration**: `GET /users` reads the existing `users` table introduced
  in P2-T3 with no schema additions.

## Testing Considerations
- 22 test cases in `test_users_api.py`; all pass on a live Postgres.
- Tests self-skip in hermetic CI (same `db_engine` skip mechanism as P2-T4 scoping tests).
- Validate gate cases explicitly verified:
  - `finance_admin → GET /users → 200`, response contains org-scoped users, no `password_hash`
  - `employee → GET /users → 403` standard envelope
- Full live-DB run: 90 total tests pass (22 P2-T5 + 7 P2-T4 scoping + remainder of suite).

## Risks Identified
- **Pre-existing P2-T4 live-DB failures (NOT caused by P2-T5):** Two tests in
  `test_record_scoping.py` (`test_manager_sees_self_and_direct_reports`,
  `test_direct_reports_one_level_only`) failed on a live Postgres. Confirmed pre-existing:
  reproduced identically with all P2-T5 changes stashed; root cause was a fixture seed bug
  (see Resolution Actions). These tests self-skip in hermetic CI so the pipeline stays green,
  but the failures were latent since P2-T4.
- `scope_query` is the single non-bypassable chokepoint for all future domain routes
  (P2-T6 trips, P2-T7 expenses, etc.). Any future misuse (wrong ownership column, skipped
  call) would be a security regression — the pattern established here must be followed
  consistently.

## Resolution Actions
- **P2-T4 fixture bug diagnosed and fixed** in commit `7ff26f9`: `user_fin` was seeded with
  `manager_id=user_m.id` (copy-paste from R1/R2 blocks), making the finance-admin a genuine
  direct report. `direct_report_ids` correctly returned `{R1, R2, Fin}`, so the manager scope
  yielded 4 rows against assertions of 3. Fix: `manager_id=None` for `user_fin`; topology
  comment corrected. Production `scope_query`/`direct_report_ids` proven correct against the
  Security Model — no `rbac.py` change needed. All 7 scoping tests green after fix.
