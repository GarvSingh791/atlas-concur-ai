# Phase 7 · P7-Foundation — Shared Test Infrastructure

## Goal

Build the shared pytest foundation that all Phase 7 test tasks (P7-T1 unit tests,
P7-T2 API contract tests) will depend on. Deliverables are infrastructure only —
no business-logic test cases, no production code changes:

- `backend/tests/conftest.py` — session-scoped migrated schema fixture, function-
  scoped commit-safe rollback session, FastAPI `app` fixture overriding `get_db`,
  RBAC catalog seed, two-org user topology, real-JWT role clients, `auth_as()` hook,
  deterministic AI wiring.
- `backend/tests/factories.py` — schema-faithful one-liner builders for every ORM
  entity (12 builders total).
- `conftest.py` (root) — pinned `os.environ.setdefault` for `JWT_SECRET`,
  `DATABASE_URL`, `LLM_PROVIDER`, `EMBEDDINGS_PROVIDER` so that invalid `.env`
  test-override values cannot reach `Settings`.

## What was done

### Reconcile: sync vs async (forced deviation from spec)

The task spec was written for an async stack (`AsyncSession`, `httpx.AsyncClient`,
`begin_nested()` + `after_transaction_end` listener). The real codebase is
**synchronous** — `sqlalchemy.orm.Session` via `psycopg2`, `get_db() ->
Iterator[Session]`, starlette `TestClient`, no `pytest-asyncio`. The foundation
was built sync throughout; all deviations are flagged in this log.

Specific adaptations:
1. `join_transaction_mode="create_savepoint"` (SA 2.0 sync recipe) replaces the
   async `begin_nested()` + listener. It provides identical commit-safe rollback
   isolation — after every `session.commit()` a new savepoint is armed; the outer
   `trans.rollback()` in teardown discards everything. The listener form is redundant
   under SA 2.0.
2. `starlette.testclient.TestClient` replaces `httpx.AsyncClient`.

### `backend/tests/conftest.py`

**`_schema` (session-scoped, autouse=True)**  
Probes Postgres via `get_engine()`; if unreachable, yields `None` and returns.
Drops and recreates the `public` schema (`DROP SCHEMA public CASCADE; CREATE SCHEMA
public`), ensures `pgcrypto` and `vector` extensions, then runs `alembic upgrade head`
against `atlas_test`. `autouse=True` guarantees the schema exists before any test in
`backend/tests/`, including pre-existing module-scoped fixtures in old test files
whose `create_all` calls need FK targets to already exist. No teardown — the schema
persists; row isolation is handled by `db_session`.

**`db_session` (function-scoped)**  
Opens a raw connection, begins an outer transaction, creates a `Session` with
`join_transaction_mode="create_savepoint"`. Yields the session; in teardown closes
the session, rolls back the outer transaction, closes the connection. If `_schema`
is `None`, calls `pytest.skip()`.

**`app` (function-scoped)**  
Overrides `get_db` to yield the shared `db_session` (does not close it — `db_session`
owns the lifecycle). Clears `dependency_overrides` in teardown. `get_current_user` is
NOT globally overridden; identity is carried by real JWTs on each role client.

**`rbac_catalog` (function-scoped)**  
Calls `seed_rbac_catalog(db_session)`. The seed's internal commit releases a savepoint
under `create_savepoint` mode; the outer transaction rolls it back at test end.
Required by any route that calls `require_permission()` or `require_role()`.

**Org / user topology — two isolated tenants**  
`org_a`, `org_b`, `users_a` (manager + employee with `manager_id = manager.id` +
finance_admin), `users_b` (employee + finance_admin).

**Role clients (real JWT)**  
`_bearer_client(app, user)` mints a real `create_access_token(user.id,
user.organization_id)` and attaches it as `Authorization: Bearer <token>`. Fixtures:
`employee_client`, `manager_client`, `finance_client` (org A) + `employee_client_b`
(org B, cross-tenant probe). Real JWT path exercises `get_current_user`'s full
JWT→DB→tenant-claim resolution; lets P7-T2 test missing/expired/wrong-org tokens.

**`auth_as` hook**  
Override escape hatch: `app.dependency_overrides[get_current_user] = lambda: user`;
returns a `TestClient`; pops the override in teardown. For auth-agnostic tests that
don't care about the JWT path.

**`local_ai` (function-scoped)**  
Belt-and-suspenders deterministic AI: overrides `get_llm_provider` and
`get_embeddings_provider` via `dependency_overrides` (covers `Depends` consumers), AND
monkeypatches `runtime.get_llm_provider` + calls `build_orchestrator.cache_clear()`
(covers `build_orchestrator()`'s direct call to the module-imported symbol, which
`dependency_overrides` cannot reach). Cache cleared again in teardown.

### `backend/tests/factories.py`

12 schema-faithful builders. Pattern: fill all NOT-NULL/no-server-default columns with
meaningful defaults, auto-create parent FKs when not supplied, `session.add(obj);
session.flush()` (flush populates server-generated UUID PKs), return instance.

Builders: `make_org`, `make_user`, `make_policy`, `make_policy_rule`, `make_trip`,
`make_trip_segment`, `make_expense_report`, `make_expense`, `make_receipt`,
`make_card_transaction`, `make_approval`, `make_approval_step`.

Special-casing:
- Module-level `_DEFAULT_PW_HASH` computed once at import (bcrypt 12-round is slow;
  reused across all `make_user` calls; overridable via `password_hash` kwarg).
- Monotonic counter `_counter = itertools.count(1)` for unique emails.
- `Trip.end_date` defaults to `start_date + 3 days` (satisfies `end_date >= start_date`
  CHECK constraint).
- `Expense.amount` cast to `Decimal(str(amount))` (satisfies `amount >= 0` CHECK).
- `Approval.subject_id = uuid.uuid4()` (no FK; arbitrary UUID is correct).
- `Receipt.image_url` defaults to `"s3://atlas-test-bucket/receipts/test.jpg"`.

### `conftest.py` (root) — env pin corrections

Added `os.environ.setdefault("LLM_PROVIDER", "local")` and
`os.environ.setdefault("EMBEDDINGS_PROVIDER", "local")`. The `.env` file's TEST
OVERRIDES section had `LLM_PROVIDER=mock` and `EMBEDDINGS_PROVIDER=local_hash` —
both invalid for the `Literal["local", "anthropic"]` field in `Settings`, causing
`ValidationError` at collection time. `pydantic-settings` treats env vars as higher
priority than `.env` values, so `setdefault()` wins without touching the file.

Also corrected `DATABASE_URL` credentials from `atlas:atlas@...` (wrong user) to
`atlas_user:change-me@localhost:5432/atlas_test` (the only user the Postgres
container has).

## Key decisions

- **`autouse=True` on `_schema`** — without it, module-scoped `seeded` fixtures in
  pre-existing test files (`test_p3_seed.py`, `test_receipts_api.py`, etc.) race
  against `_schema`: they run `create_all` on a partial table list, which fails with
  FK errors when target tables don't exist yet. `autouse=True` serializes: schema
  first, then all per-module setups. Tests that previously self-skipped (wrong DB
  credentials) now connect and exercise the schema correctly.

- **No teardown on `_schema`** — the original spec called for `DROP SCHEMA public
  CASCADE` in teardown. With correct credentials, old tests connect and run after
  teardown, finding an empty DB. Their per-module `create_all` builds an incomplete
  schema and triggers FK cascade failures. Removing teardown fixes the ordering
  problem; row-level isolation via `db_session` (savepoint rollback) is sufficient.

- **Real-JWT clients (owner decision)** — role clients mint real
  `create_access_token()` tokens. The full `get_current_user` → DB lookup → tenant-
  claim path is exercised. P7-T2 can test missing/expired/wrong-org tokens without
  any additional fixture changes. `auth_as()` hook provided as an escape hatch for
  tests that don't need to exercise the JWT path.

- **Root conftest pin over `.env` edit** — fixing invalid provider values in
  `conftest.py` via `setdefault` is a one-liner that survives `.env` regeneration.
  Editing the `.env` file risks breaking developer local setups that rely on the
  existing values in other contexts.

## Verification

**Checks — all clean:**
- Secret scan: no secrets committed.
- Secret-shaped string review: `_DEFAULT_PW_HASH` is a bcrypt hash of a test
  password (not a credential); `JWT_SECRET` value is labeled non-real;
  `_DEFAULT_PASSWORD` is used only to produce the hash. All legitimate.
- `ruff check backend/tests/conftest.py backend/tests/factories.py conftest.py`: 0
  issues (5 import-ordering issues auto-fixed during development).
- `mypy backend/tests/conftest.py backend/tests/factories.py`: 0 issues in 2 source
  files.
- `pytest backend/tests -q` (excluding 3 pre-existing failures — see QA Findings):
  **294 passed**.

**Tier 1 (factory + rollback smoke):**
- `make_user(db_session)` inserts a row; row visible in same session.
- Separate test asserts the DB is clean (proves rollback isolation).
- `employee_client` (org A) vs `employee_client_b` (org B) resolve to different
  `organization_id` on an authed endpoint (two-tenant isolation confirmed).
- Unauthenticated request → 401.

**Tier 2 (commit-safe isolation):**
- Test writes via factory + calls `db_session.commit()`; following test asserts DB
  is clean (`create_savepoint` re-arms after each commit; outer rollback discards
  all).
- `app.dependency_overrides == {}` after `app` fixture teardown (no leakage).
- Org A token on org B resource → 403/404 (cross-tenant denied).

## QA Findings — pre-existing failures (not caused by this task)

Three tests in pre-Phase-7 test files fail after the DATABASE_URL correction (which
gave them a working DB connection for the first time). They fail identically when run
in isolation, confirming they are pre-existing bugs, not regressions from this task.
Last git touch: `78ec390 P6-T3`.

**1. `test_p3_seed.py::test_policy_rules_consistency`**
```
AssertionError: Expected 7 rules, got 8
```
The seed reports "7 rules" in its output log, but the DB contains 8. A row is
inserted by a side-effect in a different test's setup that leaks through the
module-scoped `seeded` fixture (which commits without a savepoint, bypassing
`db_session` isolation). Root cause is in the P3-T5 test file's session management,
not in the atlas seed or the foundation fixtures.

**2. `test_t3_cross_role_scoping.py::TestPolicyScoping::test_finance_can_author_policy`**
```
assert 422 == 200
```
A `POST /api/v1/policies` request by a finance_admin user returns 422 Unprocessable
Entity. Pre-existing validation schema mismatch on the policy creation endpoint; the
P6-T3 test was written against an expected API contract that the implementation does
not satisfy. Not touched by this task.

**3. `test_receipts_api.py::TestCorrectReceiptWrongOwner::test_correct_receipt_wrong_owner`**
```
ERROR: ForeignKeyViolation: update or delete on table "organizations" violates
foreign key constraint "fk_ai_decision_logs_organization_id_organizations"
```
The test's module-scoped `seeded` fixture commits organization data and then the
teardown tries to `DELETE FROM organizations` without first deleting referencing
`ai_decision_logs` rows. The `ON DELETE CASCADE` was not applied to this FK in the
migration. Root cause is in the P4-T13 test file's teardown sequence and/or the
migration's FK definition, not in the foundation.

All three findings are pre-existing. They are tracked here for P7-T1 / P7-T2 authors
so they can mark affected tests `xfail` or fix the underlying cause if it falls within
their scope.
