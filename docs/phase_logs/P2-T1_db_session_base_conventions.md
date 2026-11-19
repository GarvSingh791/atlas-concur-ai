# Phase Summary

Phase:      Phase 2 — Core Backend Development
Sub-Phase:  P2-T1 — DB Session & Base Conventions
Date:       2026-06-07

## Objective

Lay the database foundation that every Phase 3 ORM model will inherit. This
includes: a single `DeclarativeBase` with a deterministic `MetaData` naming
convention for stable Alembic autogenerate; six composable, single-responsibility
convention mixins (one per DATABASE_SCHEMA.md convention); a shared sync
engine/session factory with a FastAPI `get_db` dependency sourced exclusively
from `Settings`; and a refactor of the P1-T4 lifespan boot check onto that
shared engine so there is one connection path. A live-DB round-trip test proves
every convention fires against a running Postgres (Validate gate).

No real schema tables, no Alembic migration, no auth/RBAC — those are Phase 3.

## Work Completed

- Made `database/` and `database/models/` real importable Python packages by
  adding `__init__.py` files (both were `.gitkeep`-only stubs).
- Defined `database/models/base.py`: a single `Base(DeclarativeBase)` carrying
  a `MetaData(naming_convention=...)` with the canonical Alembic ix/uq/ck/fk/pk
  patterns.
- Defined `database/models/mixins.py`: six composable plain-class mixins using
  SQLAlchemy 2.0 `Mapped[]` + `mapped_column()` —
  `UUIDPrimaryKeyMixin`, `TimestampMixin`, `TenantMixin`, `AuthorshipMixin`,
  `SoftDeleteMixin`, `VersionMixin` — each owning exactly one convention.
- Created `backend/app/core/database.py`: lazy `lru_cache` factories
  (`get_engine`, `get_sessionmaker`) and a `get_db` dependency generator that
  rolls back on error and always closes, sourcing the DSN exclusively from
  `Settings.DATABASE_URL`.
- Refactored the P1-T4 lifespan DB boot-check in `backend/app/main_entry/app.py`
  to call the shared `get_engine()` instead of building a throwaway engine;
  moved `engine.dispose()` to the shutdown side of `yield`.
- Registered the `db` pytest marker in `pyproject.toml` to avoid
  `PytestUnknownMarkWarning`.
- Wrote `backend/tests/test_db_conventions.py`: a test-local scratch model
  composing four mixins, a module-scoped fixture that self-skips when Postgres
  is unreachable, and a round-trip test asserting every convention fires.

## Files Created

| File | Purpose |
|---|---|
| `database/__init__.py` | Makes `database` an importable package |
| `database/models/__init__.py` | Re-exports `Base` + all six mixins (`__all__`) |
| `database/models/base.py` | `DeclarativeBase` + `MetaData` naming convention |
| `database/models/mixins.py` | Six composable convention mixins |
| `backend/app/core/database.py` | Shared engine, session factory, `get_db` dep |
| `backend/tests/test_db_conventions.py` | Live-DB Validate gate test |

## Files Modified

| File | Change |
|---|---|
| `backend/app/main_entry/app.py` | Lifespan refactored to shared `get_engine()`; removed throwaway `create_engine`; `dispose()` moved to shutdown |
| `pyproject.toml` | Added `markers = ["db: requires a live Postgres"]` to pytest config |

## Integration Points

- **Phase 3 models** (`database/models/*`): will `from database.models import Base,
  <mixins>` and compose the subset they need. `Base` last in the bases list.
  FK columns (`organization_id → organizations`, `created_by/updated_by → users`)
  are currently column-only; FKs are wired in P3 when the target tables exist.
- **FastAPI routes** (P2-T2+): will declare `db: Session = Depends(get_db)` — no
  other wiring needed.
- **Alembic** (P3-T2): the `naming_convention` on `Base.metadata` is what makes
  `alembic revision --autogenerate` produce stable, deterministic constraint names.
  The `env.py` must reference this same `Base.metadata` object when it is created.
- **Lifespan / startup**: `get_engine()` is now the only engine construction in the
  app. The boot SELECT 1 and all future request sessions share one pool.

## Technical Decisions

### Sync SQLAlchemy 2.0 (not async)

The project ships `psycopg2-binary`, a synchronous DBAPI. There is no `asyncpg`
or `psycopg[async]` in `backend/requirements.txt` and the lifespan handler is
already sync. Introducing async SQLAlchemy here would require a new driver, an
`AsyncEngine`/`async_sessionmaker` split, and `async def` request handlers — all
out of scope and without any benefit at this stage. Decision: **sync SQLAlchemy 2.0
throughout**. May be revisited if a high-concurrency workload later justifies it.

### MetaData naming_convention

```python
{
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Without this, SQLAlchemy generates random or dialect-specific constraint names
(e.g. `pg_constraint_1a2b3c…`). Alembic's `autogenerate` compares names to decide
what changed; without a stable convention it emits spurious `alter_constraint`
operations on every run. Setting it on `Base.metadata` now (before any migration
is created) means all P3 migrations inherit it automatically. The `ck` pattern
requires that **every `CheckConstraint` carry an explicit `name=`** — this is
documented in `base.py` as a forward rule for P3.

### Composable mixins — conventions are deliberately NOT universal

`DATABASE_SCHEMA.md` specifies conventions that are not uniform across all tables:

- **`audit_log`** is append-only: `created_at` only, no `updated_at`, no
  `version`, no soft-delete, no `updated_by`.
- **`policy_embeddings`** has `created_at` only; no `updated_at`, authorship, or
  `version`.
- **`version` (optimistic lock)** appears only on records subject to concurrent
  edits: `expense_reports`, `approvals`, `trips`.
- **`created_by`/`updated_by`** appear only on mutable business tables.

A monolithic base that inherits all columns would force every column onto every
table, violating the schema. Six single-responsibility mixins let P3 models
compose exactly the subset they need. No `@declarative_mixin`/`declared_attr` is
required for these plain scalar columns; MRO resolution handles everything with
`Base` last in the bases list.

### Single engine path (lifespan refactor)

The P1-T4 lifespan created a throwaway `create_engine(...)` for the boot check
and disposed it immediately, with a comment "Phase 2 introduces the real shared
engine." That is now done: the lifespan calls `get_engine()` (the same factory
every request session uses), making there exactly one SQLAlchemy engine and one
connection pool for the whole process. `engine.dispose()` moved from startup
`finally` to after `yield` (shutdown), so the pool lives for the app's lifetime.

### DSN driver normalization

The real `.env`/docker-compose use the bare `postgresql://` DSN prefix; the
conftest/CI dummy uses `postgresql+psycopg2://`. `get_engine()` normalizes both
forms with `make_url(...).set(drivername="postgresql+psycopg2")`, keeping the sync
psycopg2 path deterministic regardless of which form is configured.

## Testing Considerations

**The Validate gate test is NOT hermetic** — unlike the P1-T5 smoke tests
(`test_smoke.py` uses `TestClient` without the `with` context manager to avoid
triggering the lifespan), `test_db_conventions.py` requires a running Postgres
instance to be useful. A module-scoped fixture attempts `engine.connect()` and
calls `pytest.skip()` on failure, so:

- In **CI** (no Postgres service): the test self-skips, keeping the hermetic gate
  green. The skip is intentional, not a bypass.
- Locally (or with a DB container): the test runs in full and asserts every
  convention fires — UUID PK auto-generated DB-side, `created_at`/`updated_at`
  set, `version` defaults to 1, `deleted_at` is NULL, and `updated_at` advances
  on a second-transaction update while `created_at` is unchanged.

**The scratch model is a gate-satisfier, not schema.** `_ScratchConventions` is
defined inside `test_db_conventions.py` (never in `database/models/`) and its
table is created/dropped within the fixture scope. It does not represent any real
entity; the actual schema tables (`organizations`, `users`, `trips`, etc.) arrive
in Phase 3 starting with P3-T1.

Verified against a live `pgvector/pgvector:pg16` container with both the
`+psycopg2` and bare `postgresql://` DSN forms.

## Risks Identified

1. **`ck` naming pattern requires explicit `CheckConstraint` names in P3.** If a
   P3 model creates an unnamed `CheckConstraint(...)`, the MetaData convention
   will raise at DDL render time (`constraint_name` is not interpolable). This is
   a build-time error, not a silent defect, but it would block P3-T2 migrations if
   not caught.
2. **FK columns are column-only for now.** `organization_id`, `created_by`, and
   `updated_by` have no FK constraint today. Until P3-T1 wires them to the target
   tables, referential integrity for these columns is not enforced at the DB level.
3. **Sync engine limits I/O concurrency.** Under high load, synchronous
   psycopg2 sessions block the event loop. Acceptable for the current single-tenant
   build; a migration to async (`asyncpg`/psycopg v3) would be a later,
   scoped change.

## Resolution Actions

Issues discovered and resolved during QA:

- **DSN driver mismatch (M1):** bare `postgresql://` and `postgresql+psycopg2://`
  forms both needed to work. Fixed by normalizing the URL in `get_engine()`.
- **`mypy` `[list-item]` error:** `Model.__table__` is typed as `FromClause`, not
  `Table`; added a documented `cast(Table, ...)` in the test for the scoped
  `create_all`/`drop_all` calls.
- **`ruff format`:** the test file's class signature was reformatted onto one line;
  applied with `ruff format`.
- **`sessionmaker(autocommit=...)`:** removed in SQLAlchemy 2.0 (raises if passed);
  omitted with a comment.
- **Removed stale `create_engine` import** from `app.py` (unused after refactor;
  would fail ruff F401).
