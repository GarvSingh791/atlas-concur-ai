"""Live-DB full-chain migration gate for P3-T2.

Proves the accumulated migration chain (0001 → 0006) satisfies the P3-T4
Validate gate against a throwaway database so the main ``atlas_test`` DB is
never touched during this test:

  1. ``alembic upgrade head`` on a clean DB succeeds.
  2. All 22 schema tables + pgvector extension + all 22 enum types are present.
  3. ``alembic autogenerate`` produces an empty diff (zero drift between ORM
     models and the migration-produced schema).
  4. ``alembic upgrade head`` again is a no-op (idempotency).
  5. ``alembic downgrade base`` leaves zero Atlas tables and zero Atlas enum
     types (pgvector extension intentionally remains per 0004 design decision).
  6. ``alembic upgrade head`` after downgrade succeeds without enum-collision
     errors (the classic failure mode when enum DROP is missing from a downgrade).

NOT hermetic — needs a running Postgres with CREATE DATABASE privilege.
Self-skips when Postgres is unreachable; hermetic CI stays green.

Run against a live DB:
    pytest backend/tests/test_migration_chain.py -v
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from config.settings import get_settings
from database.models.base import Base

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Constants — the expected post-upgrade schema state
# ---------------------------------------------------------------------------

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "organizations",
        "users",
        "roles",
        "permissions",
        "role_permissions",
        "audit_log",
        "trips",
        "trip_segments",
        "expense_reports",
        "expenses",
        "policies",
        "policy_rules",
        "policy_embeddings",
        "card_transactions",
        "calendar_events",
        "receipts",
        "approvals",
        "approval_steps",
        "conversations",
        "conversation_messages",
        "ai_decision_logs",
        "idempotency_keys",
        # 0020 (product gap 3: per-user read state for derived notifications)
        "notification_reads",
        # 0024 (phase 12 follow-ups: one-time account tokens)
        "auth_tokens",
    }
)

EXPECTED_ENUMS: frozenset[str] = frozenset(
    {
        # 0001
        "user_role",
        "user_status",
        "role_key",
        # 0002
        "trip_status",
        "segment_type",
        # 0003
        "expense_report_status",
        "expense_category",
        "expense_status",
        "expense_match_source",
        # 0004
        "policy_rule_type",
        "policy_applies_to",
        "policy_rule_status",
        "ocr_status",
        "processing_path",
        "approval_subject_type",
        "approval_status",
        "approval_decision",
        "conversation_context_type",
        "message_role",
        "ai_agent",
        "ai_route",
        "reflection_outcome",
        # 0008 (pre-R6; EXPECTED_ENUMS was stale — never updated when 0008 landed)
        "policy_document_type",
    }
)

_SCRATCH_DB = "atlas_chain_check"
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_with_db(raw_url: str, db_name: str) -> str:
    """Return the URL string with the database swapped, preserving real password.

    IMPORTANT: must use render_as_string(hide_password=False) — the default
    str(URL) masks the password as '***', breaking authentication in both
    create_engine() calls and subprocess DATABASE_URL env vars.
    """
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


def _run_alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    """Run an alembic CLI command in the repo root with the given DATABASE_URL."""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        ["alembic", *args],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed:\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    return result


def _query_tables(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name != 'alembic_version'"
            )
        ).fetchall()
    return {r[0] for r in rows}


def _query_enums(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")).fetchall()
    return {r[0] for r in rows}


def _extension_present(engine: sa.Engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = :n"),
            {"n": name},
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Fixture — throwaway database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain_db_url() -> Iterator[str]:
    """Create atlas_chain_check, yield its URL string, drop it on teardown.

    Skips the module if Postgres is unreachable.
    """
    raw_url = get_settings().DATABASE_URL
    admin_url = _url_with_db(raw_url, "postgres")
    scratch_url = _url_with_db(raw_url, _SCRATCH_DB)

    # Check connectivity first; self-skip if Postgres is down.
    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping migration chain test")
        return  # unreachable, makes type-checker happy

    # Create the scratch DB (drop first in case a prior run left it).
    with admin_engine.connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": _SCRATCH_DB},
        )
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
        conn.execute(sa.text(f"CREATE DATABASE {_SCRATCH_DB}"))

    try:
        yield scratch_url
    finally:
        # Terminate any lingering connections before dropping.
        with admin_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": _SCRATCH_DB},
            )
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
        admin_engine.dispose()


# ---------------------------------------------------------------------------
# The full-chain gate test
# ---------------------------------------------------------------------------


def test_full_chain_and_drift(chain_db_url: str) -> None:
    """Full P3-T2 gate: apply, verify, drift-check, idempotency, downgrade, re-apply."""
    # ---- 1. Upgrade head on clean DB ----------------------------------------
    _run_alembic(["upgrade", "head"], chain_db_url)

    engine = create_engine(chain_db_url)

    # ---- 2. Assert all 22 tables + pgvector + all 22 enum types ---------------
    actual_tables = _query_tables(engine)
    missing_tables = EXPECTED_TABLES - actual_tables
    extra_tables = actual_tables - EXPECTED_TABLES
    assert not missing_tables, f"Tables missing after upgrade: {missing_tables}"
    assert not extra_tables, f"Unexpected extra tables: {extra_tables}"

    assert _extension_present(engine, "vector"), "pgvector extension not present after upgrade"

    actual_enums = _query_enums(engine)
    missing_enums = EXPECTED_ENUMS - actual_enums
    extra_enums = actual_enums - EXPECTED_ENUMS
    assert not missing_enums, f"Enum types missing after upgrade: {missing_enums}"
    assert not extra_enums, f"Unexpected extra enum types: {extra_enums}"

    # ---- 3. Drift guard — autogenerate must produce an empty diff ------------
    # Ensure all models are registered on Base.metadata by importing the package.
    import database.models  # noqa: F401 — side-effect populates Base.metadata

    def _include_atlas_tables_only(
        object_: object,
        name: str | None,
        type_: str,
        reflected: bool,
        compare_to: object,
    ) -> bool:
        # Exclude test-only scratch tables (defined in other test modules on
        # the shared Base); only compare the 22 canonical Atlas tables.
        # include_object is called for BOTH metadata-side and DB-side objects,
        # covering 'add_table' (metadata only) as well as reflected tables.
        if type_ == "table":
            return name in EXPECTED_TABLES
        return True

    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "include_object": _include_atlas_tables_only},
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], (
        "Drift detected between ORM models and migration-produced schema:\n"
        + "\n".join(str(op) for op in diff)
    )

    # ---- 4. Idempotency — second upgrade head must be a no-op ---------------
    _run_alembic(["upgrade", "head"], chain_db_url)

    # ---- 5. Downgrade to base — zero Atlas tables + zero Atlas enum types ---
    _run_alembic(["downgrade", "base"], chain_db_url)

    tables_after_down = _query_tables(engine)
    enums_after_down = _query_enums(engine)
    assert (
        not tables_after_down
    ), f"Atlas tables still present after downgrade base: {tables_after_down}"
    assert (
        not enums_after_down
    ), f"Atlas enum types still present after downgrade base: {enums_after_down}"
    # P8-T2 (pre-R6): 0001 now SYMMETRICALLY owns the extensions — CREATE on upgrade,
    # DROP on downgrade (proven on live Neon). A full downgrade-base therefore removes
    # pgvector. This supersedes the earlier 0004-era "extension intentionally remains"
    # expectation (stale: 0004 only CREATEs, but 0001's downgrade now drops it).
    assert not _extension_present(
        engine, "vector"
    ), "pgvector extension should be dropped by 0001's symmetric downgrade (P8-T2)"

    # ---- 6. Re-upgrade after downgrade — proves no enum-collision block -----
    _run_alembic(["upgrade", "head"], chain_db_url)

    final_tables = _query_tables(engine)
    assert (
        EXPECTED_TABLES == final_tables
    ), f"Table mismatch after re-upgrade: missing={EXPECTED_TABLES - final_tables}"

    engine.dispose()
