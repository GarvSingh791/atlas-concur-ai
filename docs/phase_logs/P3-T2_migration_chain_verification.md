# Phase Summary

Phase:       Phase 3 — Database Implementation
Sub-Phase:   P3-T2 — Migration Chain Verification + Drift Check
Date:        2026-06-12
Author: Garv Singh Saini

## Reframing Note

P3-T2 as written in the plan ("create an initial all-tables migration") was
already satisfied by the authorized per-slice chain 0001–0004, built during
Phase 3's slice delivery. This task therefore verified the accumulated chain
meets the Validate gate rather than rebuilding it. No new initial migration
was created, and 0001–0004 were not squashed.

## Objective

Prove the migration chain (0001 → 0002 → 0003 → 0004) satisfies the P3-T2
Validate gate:
- Applies cleanly to a fresh database.
- Downgrades completely (zero Atlas tables, zero Atlas enum types remaining).
- Re-upgrades after downgrade without enum-collision errors.
- Produces zero autogenerate drift against the ORM models.

Lock the gate in with an automated live-DB test that self-skips in hermetic CI.

## What Was Verified

Manually and in-process, on live PG 16.14 against a throwaway database
(`atlas_chain_check`):

| Check | Result |
|---|---|
| `alembic upgrade head` on clean DB | All 21 tables created; pgvector extension present; all 22 enum types present |
| All 21 tables present, each exactly once | Confirmed; set matches EXPECTED_TABLES exactly |
| 22 enum types (3 + 2 + 4 + 13 across 0001–0004) | Confirmed; symmetric DROP TYPE checkfirst=True in each downgrade |
| pgvector extension | Created once in 0004; intentionally NOT dropped on downgrade (documented design decision in 0004) |
| `alembic upgrade head` (idempotency) | No-op; no "type/relation already exists" |
| `alembic downgrade base` | Zero Atlas tables; zero Atlas enum types; pgvector extension remains |
| `alembic upgrade head` after downgrade | Succeeds; 21 tables restored; enum-collision failure mode disproven |
| `alembic heads` | Single head: `0004` |
| `alembic history` | Linear 4-entry chain: `0001 → 0002 → 0003 → 0004` |

## Zero-Drift Result

In-process `compare_metadata` (Alembic autogenerate API, `compare_type=True`)
against head on the throwaway DB produced an **empty diff** — `diff == []`.
No table, column type, or constraint divergence between the ORM model
definitions in `database/models/` and the schema produced by the migration
chain. The chain was already drift-free; no reconciliation was required.

## What Was Added

**`backend/tests/test_migration_chain.py`** — a live-DB full-chain gate test:

- `pytestmark = pytest.mark.db` — self-skips when Postgres is unreachable;
  hermetic CI stays green.
- Module-scoped fixture `chain_db_url` creates `atlas_chain_check` on a
  `postgres` maintenance connection (AUTOCOMMIT), yields the scratch URL,
  terminates lingering connections and drops the scratch DB on teardown.
  `atlas_test` is never disturbed.
- Single test `test_full_chain_and_drift` covers:
  1. `upgrade head` on clean DB.
  2. Assert 21 tables + pgvector extension + 22 enum types against
     `EXPECTED_TABLES` / `EXPECTED_ENUMS` frozensets.
  3. In-process `MigrationContext` + `compare_metadata` drift guard
     (scoped via `include_object` — see Key Decisions).
  4. `upgrade head` again (idempotency).
  5. `downgrade base`; assert zero tables + zero enums; pgvector still present.
  6. `upgrade head` after downgrade; assert 21 tables restored.
- `_run_alembic(args, db_url)` helper exercises the real CLI path via
  `subprocess.run`, passing `DATABASE_URL` as an env-var override so
  Settings picks it up without hardcoding credentials.
- `_url_with_db(raw_url, db_name)` helper uses
  `make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)`
  — see Key Decisions.

## Key Decisions

1. **Throwaway scratch DB (`atlas_chain_check`)**: true clean-DB isolation
   rather than downgrading `atlas_test` destructively. The scratch DB is
   created and dropped entirely at runtime; no artifact is committed.

2. **`include_object` filter (not `include_name`)**: the drift guard uses
   `opts={"include_object": _include_atlas_tables_only}` in
   `MigrationContext.configure`. `include_name` only filters DB-reflected
   names; it does not suppress metadata-side "add_table" operations for
   models that are in `Base.metadata` but not yet in the DB. When the full
   pytest suite runs, `test_db_conventions.py` registers `_scratch_p2t1_conventions`
   and `test_record_scoping.py` registers `_scratch_p2t4_scoped` on the shared
   `Base`. `include_object` is called for both metadata-side and DB-side
   objects, so those scratch tables are correctly excluded from the drift check.

3. **`render_as_string(hide_password=False)`**: SQLAlchemy's default `str(URL)`
   replaces the real password with `***`. This broke both `create_engine()` calls
   (authentication failure against the `postgres` maintenance DB) and subprocess
   `DATABASE_URL` env-var overrides (Alembic CLI could not connect). All URL
   construction in `test_migration_chain.py` uses
   `make_url(...).set(...).render_as_string(hide_password=False)` to preserve
   the real password at runtime. No literal password is committed — the URL is
   always derived from `get_settings().DATABASE_URL` at test runtime.

4. **No new initial migration; no squash**: the per-slice chain 0001–0004 is
   the authorized realization of the P3-T2 intent. It covers all 21 tables,
   all 22 enum types, and pgvector in their correct dependency order. A new
   initial migration or squash would be redundant, contrary to the documented
   design, and would break the per-slice chain contract.

## Drift Found and Reconciled

None. The chain was already drift-free. The throwaway autogenerate revision
generated during manual investigation produced `pass` in both `upgrade()` and
`downgrade()` and was deleted without being committed.

## Issues Found and Fixed in QA

| Issue | Root Cause | Fix |
|---|---|---|
| Test self-skipping even when `DATABASE_URL` was set | `str(SQLAlchemy URL)` masks the password as `***`, causing admin connection to `postgres` maintenance DB to fail silently and fall through to `pytest.skip` | Changed all URL construction to `make_url(...).set(...).render_as_string(hide_password=False)` |
| Drift guard reported false positives in full suite | `include_name` only filters DB-reflected names; test-only scratch models on the shared `Base` (`_scratch_p2t1_conventions`, `_scratch_p2t4_scoped`) appeared as `add_table` ops on the metadata side | Switched to `include_object` filter scoped to `EXPECTED_TABLES` |

## Validation Evidence

- **Live Postgres (PG 16.14), throwaway DB**: full cycle green:
  - `upgrade head` → 21 tables + 22 enums + pgvector ✔
  - `upgrade head` (idempotency) → no-op ✔
  - Autogenerate diff → empty ✔
  - `downgrade base` → 0 tables, 0 enums, pgvector remains ✔
  - `upgrade head` (after downgrade) → 21 tables restored ✔
- **`alembic heads`** → single head `0004` ✔
- **`alembic history`** → linear 4-entry chain ✔
- **`pytest backend/tests/test_migration_chain.py -v`** → PASSED ✔
- **Full suite** → 160 passed ✔
- **`ruff check .`** → 0 violations ✔
- **`ruff format --check .`** → clean ✔
- **`mypy backend config`** → Success: no issues found in 50 source files ✔

## Files Created

| File | Purpose |
|---|---|
| `backend/tests/test_migration_chain.py` | Live-DB full-chain gate test (P3-T2 lock-in) |
| `docs/phase_logs/P3-T2_migration_chain_verification.md` | This document |

## Files Modified

None. This task added one test file and one phase log. No migration, model,
configuration, or application file was changed.
