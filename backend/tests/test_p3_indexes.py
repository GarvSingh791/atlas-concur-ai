"""Live-DB index-strategy gate for P3-T3.

Proves that migration 0005 realizes the full DATABASE_SCHEMA.md Indexing
Strategy on a throwaway database:

  1. Every strategy index exists in pg_indexes by exact name (38 added by 0005
     plus the one composite from 0001).
  2. Representative queries use the expected index (EXPLAIN plan-shape checks
     via SET LOCAL enable_seqscan=off — no permanent data inserted).
  3. Negative: a non-pending status query does NOT use the partial approvals
     index ix_approvals_org_pending.

NOT hermetic — needs a running Postgres with CREATE DATABASE privilege.
Self-skips when Postgres is unreachable; hermetic CI stays green.

Run against a live DB:
    pytest backend/tests/test_p3_indexes.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from config.constants.embeddings import EMBEDDING_DIMENSION
from config.settings import get_settings

pytestmark = pytest.mark.db

_SCRATCH_DB = "atlas_index_check"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# All strategy indexes that must exist after upgrade head (0001 → 0005)
# ---------------------------------------------------------------------------

# Created by 0001 (must still exist after 0005).
_EXISTING_FROM_PRIOR = frozenset(
    {
        "ix_audit_log_entity_type_entity_id_created_at",
    }
)

# Created by 0005.
_ADDED_BY_0005 = frozenset(
    {
        # FK — users / role_permissions
        "ix_users_manager_id",
        "ix_role_permissions_permission_id",
        # FK — audit_log
        "ix_audit_log_organization_id",
        "ix_audit_log_actor_id",
        # trips — composites + FK
        "ix_trips_traveler_id_status",
        "ix_trips_start_date",
        "ix_trips_organization_id",
        # FK — trip_segments
        "ix_trip_segments_trip_id",
        # expense_reports — composite + FK
        "ix_expense_reports_owner_id_status",
        "ix_expense_reports_organization_id",
        "ix_expense_reports_trip_id",
        # expenses — composite + FK
        "ix_expenses_report_id_status",
        "ix_expenses_organization_id",
        "ix_expenses_trip_segment_id",
        # approvals — composite + partial + FK
        "ix_approvals_organization_id_current_status",
        "ix_approvals_submitter_id",
        "ix_approvals_org_pending",
        # FK — approval_steps
        "ix_approval_steps_approval_id",
        "ix_approval_steps_approver_id",
        # receipts — GIN + partial + FK
        "ix_receipts_extracted_fields",
        "ix_receipts_org_ocr_pending",
        "ix_receipts_organization_id",
        "ix_receipts_uploaded_by",
        "ix_receipts_expense_id",
        # policy_rules — GIN + FK
        "ix_policy_rules_definition",
        "ix_policy_rules_policy_id",
        # policy_embeddings — FK + HNSW
        "ix_policy_embeddings_policy_id",
        "ix_policy_embeddings_embedding_hnsw",
        # FK — card_transactions
        "ix_card_transactions_organization_id",
        "ix_card_transactions_user_id",
        "ix_card_transactions_expense_id",
        # FK — calendar_events
        "ix_calendar_events_organization_id",
        "ix_calendar_events_user_id",
        # FK — conversations
        "ix_conversations_organization_id",
        "ix_conversations_user_id",
        # FK — conversation_messages
        "ix_conversation_messages_conversation_id",
        # FK — ai_decision_logs
        "ix_ai_decision_logs_organization_id",
        "ix_ai_decision_logs_user_id",
    }
)

ALL_STRATEGY_INDEXES = _EXISTING_FROM_PRIOR | _ADDED_BY_0005


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


def _run_alembic(args: list[str], db_url: str) -> None:
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


def _query_pg_indexes(engine: Engine) -> set[str]:
    """Return all index names in the public schema."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        ).fetchall()
    return {r[0] for r in rows}


def _explain_json(conn: sa.Connection, query: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run EXPLAIN (FORMAT JSON) and return the root Plan dict.

    psycopg2 auto-deserializes the JSON column into a Python list; we accept
    both a pre-parsed list and a raw JSON string so the helper is robust.
    """
    row = conn.execute(
        sa.text(f"EXPLAIN (FORMAT JSON) {query}"),
        params,
    ).fetchone()
    assert row is not None
    raw = row[0]
    plan_list: list[dict[str, Any]] = raw if isinstance(raw, list) else json.loads(raw)
    return plan_list[0]["Plan"]


def _collect_index_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Recursively collect all nodes in the plan tree that reference an index."""
    nodes = []
    node_type: str = plan.get("Node Type", "")
    if "Index" in node_type:
        nodes.append(plan)
    for sub in plan.get("Plans", []):
        nodes.extend(_collect_index_nodes(sub))
    return nodes


def _assert_uses_index(
    conn: sa.Connection,
    expected_index: str,
    query: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Assert that the EXPLAIN plan for *query* contains a node referencing *expected_index*."""
    conn.execute(sa.text("SET LOCAL enable_seqscan = off"))
    plan = _explain_json(conn, query, params or {})
    index_nodes = _collect_index_nodes(plan)
    names = {n.get("Index Name", "") for n in index_nodes}
    assert expected_index in names, (
        f"Expected index '{expected_index}' not found in plan.\n"
        f"Index nodes found: {names}\n"
        f"Full plan: {json.dumps(plan, indent=2)}"
    )


def _assert_does_not_use_index(
    conn: sa.Connection,
    forbidden_index: str,
    query: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Assert that the EXPLAIN plan does NOT reference *forbidden_index*."""
    # Run without seqscan=off so the planner can choose the right index.
    plan = _explain_json(conn, query, params or {})
    index_nodes = _collect_index_nodes(plan)
    names = {n.get("Index Name", "") for n in index_nodes}
    assert forbidden_index not in names, (
        f"Index '{forbidden_index}' appeared in plan but should NOT have.\n"
        f"Index nodes found: {names}"
    )


# ---------------------------------------------------------------------------
# Fixture — throwaway database with migrations applied
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def index_db_engine() -> Iterator[Engine]:
    """Create atlas_index_check, apply all migrations, yield an engine, then clean up."""
    raw_url = get_settings().DATABASE_URL
    admin_url = _url_with_db(raw_url, "postgres")
    scratch_url = _url_with_db(raw_url, _SCRATCH_DB)

    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping P3-T3 index tests")
        return

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

    _run_alembic(["upgrade", "head"], scratch_url)

    engine = create_engine(scratch_url)
    try:
        yield engine
    finally:
        engine.dispose()
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
# Test 1 — pg_indexes completeness
# ---------------------------------------------------------------------------


def test_all_strategy_indexes_present(index_db_engine: Engine) -> None:
    """Every strategy index must exist in pg_indexes by exact name."""
    actual = _query_pg_indexes(index_db_engine)
    missing = ALL_STRATEGY_INDEXES - actual
    assert not missing, f"{len(missing)} strategy index(es) missing from pg_indexes:\n" + "\n".join(
        f"  {n}" for n in sorted(missing)
    )


def test_no_unexpected_strategy_duplicates(index_db_engine: Engine) -> None:
    """Verify the consolidations hold: consolidated FK columns have no extra index."""
    actual = _query_pg_indexes(index_db_engine)
    # These would be near-duplicates that the plan explicitly excluded.
    forbidden = {
        # org_id on users — covered by leading column of uq_users_organization_id_email
        "ix_users_organization_id",
        # org_id on policies — covered by leading column of uq_policies_*
        "ix_policies_organization_id",
        # owner_id on expense_reports — leads composite ix_expense_reports_owner_id_status
        "ix_expense_reports_owner_id",
        # traveler_id on trips — leads composite ix_trips_traveler_id_status
        "ix_trips_traveler_id",
        # report_id on expenses — leads composite ix_expenses_report_id_status
        "ix_expenses_report_id",
        # org_id on approvals — leads composite ix_approvals_organization_id_current_status
        "ix_approvals_organization_id",
    }
    present = forbidden & actual
    assert not present, (
        "Near-duplicate indexes should NOT exist (consolidation violated):\n"
        + "\n".join(f"  {n}" for n in sorted(present))
    )


# ---------------------------------------------------------------------------
# Test 2 — EXPLAIN plan-shape checks (one per index family)
# ---------------------------------------------------------------------------


def test_explain_expense_reports_owner_id_status(index_db_engine: Engine) -> None:
    """Composite ix_expense_reports_owner_id_status — my reports by status."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM expense_reports"
        " WHERE owner_id = :oid AND status = 'draft'::expense_report_status"
    )
    with index_db_engine.begin() as conn:
        _assert_uses_index(conn, "ix_expense_reports_owner_id_status", sql, {"oid": dummy_id})


def test_explain_approvals_org_pending_partial(index_db_engine: Engine) -> None:
    """Partial ix_approvals_org_pending — pending approval queue."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM approvals"
        " WHERE organization_id = :oid AND current_status = 'pending'::approval_status"
    )
    with index_db_engine.begin() as conn:
        _assert_uses_index(conn, "ix_approvals_org_pending", sql, {"oid": dummy_id})


def test_explain_approvals_partial_negative(index_db_engine: Engine) -> None:
    """Non-pending status must NOT use the partial index ix_approvals_org_pending."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM approvals"
        " WHERE organization_id = :oid AND current_status = 'approved'::approval_status"
    )
    with index_db_engine.begin() as conn:
        _assert_does_not_use_index(conn, "ix_approvals_org_pending", sql, {"oid": dummy_id})


def test_explain_trips_traveler_id_status(index_db_engine: Engine) -> None:
    """Composite ix_trips_traveler_id_status — trip list by status."""
    dummy_id = str(uuid.uuid4())
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_trips_traveler_id_status",
            "SELECT id FROM trips WHERE traveler_id = :tid AND status = 'approved'::trip_status",
            {"tid": dummy_id},
        )


def test_explain_trips_start_date(index_db_engine: Engine) -> None:
    """ix_trips_start_date — date range queries."""
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_trips_start_date",
            "SELECT id FROM trips WHERE start_date >= '2025-01-01' AND start_date < '2026-01-01'",
        )


def test_explain_expenses_report_id_status(index_db_engine: Engine) -> None:
    """Composite ix_expenses_report_id_status — report detail."""
    dummy_id = str(uuid.uuid4())
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_expenses_report_id_status",
            "SELECT id FROM expenses WHERE report_id = :rid AND status = 'draft'::expense_status",
            {"rid": dummy_id},
        )


def test_explain_audit_log_entity_history(index_db_engine: Engine) -> None:
    """Composite ix_audit_log_entity_type_entity_id_created_at — entity history."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM audit_log"
        " WHERE entity_type = 'trip' AND entity_id = :eid ORDER BY created_at"
    )
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn, "ix_audit_log_entity_type_entity_id_created_at", sql, {"eid": dummy_id}
        )


def test_explain_receipts_extracted_fields_gin(index_db_engine: Engine) -> None:
    """GIN ix_receipts_extracted_fields — JSONB containment query."""
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_receipts_extracted_fields",
            "SELECT id FROM receipts WHERE extracted_fields @> '{\"amount\": 100}'::jsonb",
        )


def test_explain_policy_rules_definition_gin(index_db_engine: Engine) -> None:
    """GIN ix_policy_rules_definition — JSONB containment query."""
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_policy_rules_definition",
            'SELECT id FROM policy_rules WHERE definition @> \'{"type": "amount_cap"}\'::jsonb',
        )


def test_explain_receipts_ocr_pending_partial(index_db_engine: Engine) -> None:
    """Partial ix_receipts_org_ocr_pending — OCR processing backlog."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM receipts"
        " WHERE organization_id = :oid AND ocr_status = 'pending'::ocr_status"
    )
    with index_db_engine.begin() as conn:
        _assert_uses_index(conn, "ix_receipts_org_ocr_pending", sql, {"oid": dummy_id})


def test_explain_receipts_ocr_partial_negative(index_db_engine: Engine) -> None:
    """Non-pending ocr_status must NOT use the partial index ix_receipts_org_ocr_pending."""
    dummy_id = str(uuid.uuid4())
    sql = (
        "SELECT id FROM receipts"
        " WHERE organization_id = :oid AND ocr_status = 'processed'::ocr_status"
    )
    with index_db_engine.begin() as conn:
        _assert_does_not_use_index(conn, "ix_receipts_org_ocr_pending", sql, {"oid": dummy_id})


def test_explain_policy_embeddings_hnsw_ann(index_db_engine: Engine) -> None:
    """HNSW ix_policy_embeddings_embedding_hnsw — ANN similarity search."""
    zero_vec = "[" + ",".join("0" for _ in range(EMBEDDING_DIMENSION)) + "]"
    with index_db_engine.begin() as conn:
        _assert_uses_index(
            conn,
            "ix_policy_embeddings_embedding_hnsw",
            f"SELECT id FROM policy_embeddings ORDER BY embedding <=> '{zero_vec}'::vector LIMIT 5",
        )


# ---------------------------------------------------------------------------
# Test 3 — pgvector extension version (confirms HNSW availability)
# ---------------------------------------------------------------------------


def test_pgvector_supports_hnsw(index_db_engine: Engine) -> None:
    """pgvector extension version must be >= 0.5.0 for HNSW support."""
    with index_db_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).fetchone()
    assert row is not None, "pgvector extension not installed"
    version_str: str = row[0]
    parts = version_str.split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (0, 5), f"pgvector {version_str} does not support HNSW (need >= 0.5.0)"
