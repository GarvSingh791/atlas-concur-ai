# Phase Summary

Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T4 — RBAC & Record Scoping
Date:       2026-06-12

## Objective

Implement server-side authorization on top of the P2-T3 `get_current_user`
seam: a permission-key catalog in `config/constants`, role→permission seed
mappings, reusable `require_permission` / `require_role` FastAPI dependencies
that resolve permissions from the DB-loaded user (never the token), and a
generic tenant-first record-level scope filter (`scope_query`) in the
data-access layer. Validate that a protected route denies an unauthorized role
(403 standard envelope) and that a manager query returns only self + direct
reports.

---

## Work Completed

1. **Permission-key catalog** (`config/constants/permissions.py`): 8 named
   string constants covering the three roles' PRD user stories — the 6
   provisional P2-T3 keys plus `policy.read` and `users.read`. Exports
   `PERMISSION_CATALOG` (ordered `(key, description)` tuples) and
   `ALL_PERMISSION_KEYS` (frozenset). This is the single source of permission-key
   truth; both the seed and the enforcement layer import from here.

2. **Row-level idempotent seed refactor** (`database/seeds/identity_seed.py`):
   Replaced the P2-T3 whole-seed short-circuit (which silently skipped new keys
   on an already-seeded DB) with four composable helpers — `upsert_roles`,
   `upsert_permissions`, `sync_role_permissions` (add-only; never deletes), and
   `seed_rbac_catalog(session)` — that converge the DB to the authoritative
   catalog regardless of prior state. The org + users block retains its existence
   guard. `seed_rbac_catalog` is also callable standalone by tests.

3. **RBAC dependencies** (`backend/app/core/rbac.py`):
   - `get_user_permissions(user, session) -> set[str]`: joins
     `permissions → role_permissions → roles` where `role.key == user.role`;
     returns the resolved permission-key set. Never reads a role claim from the
     token.
   - `require_permission(key) -> Callable[..., User]`: FastAPI dependency factory;
     resolves permissions from the DB, raises `ForbiddenError` (403, standard
     envelope) on failure, returns the user on success. Each factory instance is
     given a unique `__name__` so FastAPI's dependency cache does not collapse
     calls with different keys.
   - `require_role(*allowed) -> Callable[..., User]`: thin coarse convenience;
     checks `user.role in allowed`, raises `ForbiddenError` otherwise. Permission
     keys remain the primary mechanism.

4. **Record-level scope filter** (`backend/app/core/rbac.py`):
   - `direct_report_ids(manager, session) -> set[UUID]`: queries `users` where
     `manager_id == manager.id AND organization_id == manager.organization_id`
     (one level, not transitive; tenant re-asserted as a belt-and-suspenders
     guard).
   - `scope_query(stmt, *, user, ownership_column, org_column, session)`:
     the mandatory choke point every future domain query MUST route through.
     Always applies `org_column == user.organization_id` first (tenant isolation,
     including for finance_admin — org-wide ≠ all-orgs), then narrows by role:
     employee → `ownership_column == user.id`; manager → `ownership_column.in_([
     user.id, *direct_report_ids(...)])`; finance_admin → no further narrowing;
     unknown role → `ForbiddenError` (deny-by-default, Dev Rule 9).
     Parameterized by ORM `InstrumentedAttribute[UUID]` column objects (not
     strings) for type safety and SQLAlchemy 2.0 compatibility.

5. **Test suite** (`backend/tests/`):
   - `test_rbac.py`: throwaway FastAPI app (mirrors `create_app` error wiring)
     with three guarded routes. Live-DB tests self-skip when Postgres is
     unreachable. The no-token→401 cases run hermetically in all CI environments.
     Includes a forged-role-claim test (extra `"role"` field injected into JWT
     payload) that confirms the employee still receives 403 on `policy.manage`
     because permissions resolve from the DB user, not the token.
   - `test_record_scoping.py`: scratch model `_ScratchScoped`
     (`organization_id`, `owner_id`, `label`) created via scoped `create_all`
     (P2-T1 precedent, no migration). Two-org topology: org A (manager M, direct
     reports R1 + R2, unrelated same-org U, finance Fin) + org B (user O). Asserts
     employee(R1)→{R1}, manager(M)→{M, R1, R2} (excludes U and O), finance(Fin)→
     {M, R1, R2, U, Fin} (excludes O).

---

## Files Created

| Path | Purpose |
|---|---|
| `config/constants/__init__.py` | Makes `config.constants` an importable Python package |
| `config/constants/permissions.py` | Authoritative 8-key permission catalog (constants + catalog tuple + frozenset) |
| `backend/app/core/rbac.py` | RBAC dependencies and record-level scope filter |
| `backend/tests/test_rbac.py` | RBAC dep tests: authorized/forbidden/no-token/forged-claim |
| `backend/tests/test_record_scoping.py` | Scope filter tests against live Postgres (scratch model, self-skip) |

---

## Files Modified

| Path | Change |
|---|---|
| `database/seeds/identity_seed.py` | Full idempotency refactor: replaced whole-seed guard with row-level `upsert_*` / `seed_rbac_catalog`; replaced inline string literals with imports from `config.constants.permissions`; updated role→permission mapping to the authoritative 8-key catalog (`policy.read` and `users.read` added) |

---

## Integration Points

- **Built on P2-T3** (`get_current_user`): `require_permission` and `require_role`
  both declare `current_user: Annotated[..., Depends(get_current_user)]` as an
  inner dependency, inheriting the P2-T3 `UnauthorizedError` (401) path for
  missing/invalid tokens.
- **Built on P2-T2** (`ForbiddenError`): all authorization failures render through
  the standard `{"error": {"code": "FORBIDDEN", "message": ..., "correlation_id":
  ...}}` envelope via the existing `app_error_handler`.
- **Built on P2-T1** (sync SQLAlchemy, `get_db`): `require_permission` and
  `scope_query` both use the sync `Session` dependency.
- **`scope_query` is the mandatory non-bypassable choke point** for every domain
  query returning user-owned records. P2-T5/T6/T7 and all AI tools MUST route
  through it. Bypassing `scope_query` breaks tenant isolation and the RBAC
  scoping guarantee.
- **`config/constants/permissions.py` is the single source of truth**: the seed,
  the RBAC deps, and the tests all import from it. No permission key string
  literal should appear outside this file.

---

## Technical Decisions

| Decision | Rationale |
|---|---|
| Permission KEYS (not role names) as the enforcement mechanism | Permissions can evolve without code changes to checks; roles are too coarse for fine-grained access control; matches the TECHNICAL_DESIGN.md security model |
| Permissions resolved from the DB-loaded user, never from any token claim | The JWT carries only `sub`, `org`, and `type` — no role claim exists; even if injected, `get_user_permissions` joins against `role_permissions` using the DB user's `role` column |
| `scope_query` tenant-first + generic `InstrumentedAttribute[UUID]` parameterization | Ensures `organization_id` is always applied regardless of role (org-wide ≠ all-orgs); column-attribute params give type-checked `.in_()` / `==` without `getattr` indirection |
| `ForbiddenError` (not `WHERE false`) for unknown roles | An unrecognized role is an authorization failure, not "zero rows"; more honest semantics + avoids SQLAlchemy stub friction with `sqlalchemy.false()` |
| Direct reports: one level only | PRD US-M1: "for my direct reports only"; `users.manager_id` is a single self-reference; transitive hierarchy is not in scope |
| `trip.*` / `receipt.*` permission keys deliberately omitted | Dev Rule 10 (no redesign beyond scope); those models don't exist at this phase; seeding keys for non-existent resources creates drift risk |
| Seed made row-level idempotent (add-only) | The P2-T3 whole-seed short-circuit on org existence would silently fail to add new permission keys to an already-seeded DB — a real defect given P2-T4 extends the catalog |
| `dep.__name__` set uniquely per factory invocation | Prevents FastAPI's dependency-deduplication cache from collapsing two `require_permission(...)` calls with different keys into the same resolved instance |

---

## Testing Considerations

- **`test_rbac.py` — hermetic + live-DB layers**:
  - The no-token → 401 tests (`test_no_token_returns_401`,
    `test_no_token_returns_401_for_role_route`) do NOT request the `db_engine`
    fixture; they run in all CI environments including hermetic. This is safe
    because `get_db` builds a lazy `Session` (no network connection until first
    query) and `get_current_user` raises `UnauthorizedError` before any query
    executes.
  - The DB-backed tests exercise the full chain: token minted via
    `create_access_token`, thrown against a real FastAPI app, with permissions
    resolved by joining `role_permissions` in Postgres. This proves the DB
    resolution path, not a mock.
  - The forged-role-claim test injects `"role": "finance_admin"` into the JWT
    payload. Because `get_user_permissions` queries `Role.key == user.role` from
    the DB user (not the token), the employee's DB role wins and the claim is
    ignored.
- **`test_record_scoping.py` — live Postgres, self-skip**:
  - Uses `_ScratchScoped` (throwaway model defined in-module, never in
    `database/models/`) via scoped `create_all` + explicit `drop` on teardown.
    No migration is created or run.
  - Module-level `db_engine` fixture probes `engine.connect()` and calls
    `pytest.skip()` on failure — the entire module skips in hermetic CI.
  - `seed_rbac_catalog(session)` is called from both test fixtures to populate
    the catalog on a fresh test DB without running the full `seed()`.

---

## Risks Identified

- **`scope_query` bypassability**: The scope filter is only effective if every
  domain query that returns user-owned records routes through it. A developer
  writing a direct `session.query(...)` without calling `scope_query` would
  silently bypass tenant isolation and role scoping. **Standing rule for
  P2-T5 / P2-T6 / P2-T7 and all AI-agent tools**: every `SELECT` returning
  records from a tenant-scoped table MUST be wrapped with `scope_query` before
  execution.
- **`sync_role_permissions` is add-only**: rows removed from `_ROLE_PERMISSIONS`
  in a future refactor will not be deleted from the DB by re-running the seed.
  A manual cleanup migration would be needed if role→permission mappings need to
  shrink.

---

## Resolution Actions

- ruff I001 (import sort), F401 (unused import), UP037 (quoted type annotations),
  and format issues across `rbac.py`, `identity_seed.py`, `test_rbac.py`, and
  `test_record_scoping.py` — all auto-fixed via `ruff --fix` + `ruff format`.
  No manual code changes required.
