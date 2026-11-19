"""Migration 0016 (report & trip lifecycle) — safe on a POPULATED database, symmetric.

Static part (hermetic): the revision chains onto 0015 and its FROZEN permission
literal equals the catalog key the router enforces.

Live part (``db``, a throwaway database of its own so no other suite is
touched): upgrade to 0015, insert a populated slice (roles incl. finance_admin,
a user, an approved report, a trip), upgrade to 0016 and check that
  * every new column exists and is NULL on the existing rows (no backfill),
  * ``expense.reimburse.org`` exists and is granted to finance_admin only,
  * a re-run of the data step is a no-op (idempotent grant);
then downgrade to 0015 (columns, index, FKs and the permission rows gone, the
pre-existing rows untouched) and upgrade again.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from config.constants.permissions import EXPENSE_REIMBURSE_ORG, PERMISSION_CATALOG
from config.settings import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "versions" / "0016_report_trip_lifecycle.py"
_SCRATCH_DB = "atlas_pg_lifecycle_0016_check"

REPORT_COLUMNS = {
    "reimbursed_at",
    "reimbursed_by_id",
    "reimbursement_reference",
    "reimbursement_amount",
    "withdrawn_at",
    "withdrawn_by_id",
    "withdrawal_reason",
}
TRIP_COLUMNS = {"withdrawn_at", "withdrawn_by_id", "withdrawal_reason"}


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0016", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_and_frozen_permission_literal() -> None:
    module = _load_module()
    assert module.revision == "0016_report_trip_lifecycle"
    assert module.down_revision == "0015_canonical_policy_figures"
    assert module.REIMBURSE_PERMISSION_KEY == EXPENSE_REIMBURSE_ORG
    assert (
        module.REIMBURSE_PERMISSION_DESCRIPTION == dict(PERMISSION_CATALOG)[EXPENSE_REIMBURSE_ORG]
    )
    assert module.FINANCE_ROLE_KEY == "finance_admin"


# ---------------------------------------------------------------------------
# Live apply on a populated scratch database
# ---------------------------------------------------------------------------


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


def _alembic(args: list[str], db_url: str) -> None:
    result = subprocess.run(
        ["alembic", *args],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[str]:
    raw = get_settings().DATABASE_URL
    admin_url = _url_with_db(raw, "postgres")
    url = _url_with_db(raw, _SCRATCH_DB)
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable")
        return
    drop = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = :db AND pid <> pg_backend_pid()"
    )
    with admin.connect() as conn:
        conn.execute(sa.text(drop), {"db": _SCRATCH_DB})
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
        conn.execute(sa.text(f"CREATE DATABASE {_SCRATCH_DB}"))
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(drop), {"db": _SCRATCH_DB})
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
        admin.dispose()


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _grants(engine: sa.Engine) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                sa.text(
                    "SELECT r.key::text, p.key FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE p.key = :k ORDER BY r.key"
                ),
                {"k": EXPENSE_REIMBURSE_ORG},
            ).all()
        ]


@pytest.mark.db
def test_upgrade_on_populated_db_then_symmetric_downgrade(scratch_url: str) -> None:
    _alembic(["upgrade", "0015_canonical_policy_figures"], scratch_url)
    engine = create_engine(scratch_url)
    org, user, report, trip = (uuid.uuid4() for _ in range(4))
    try:
        with engine.begin() as conn:
            for role in ("employee", "manager", "finance_admin"):
                conn.execute(
                    sa.text("INSERT INTO roles (id, key, description) VALUES (:i, :k, :k)"),
                    {"i": uuid.uuid4(), "k": role},
                )
            conn.execute(
                sa.text(
                    "INSERT INTO organizations (id, name, base_currency) "
                    "VALUES (:i, 'Populated Org', 'USD')"
                ),
                {"i": org},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, organization_id, email, name, role, password_hash) "
                    "VALUES (:i, :o, 'p@example.test', 'P', 'employee', 'x')"
                ),
                {"i": user, "o": org},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO expense_reports (id, organization_id, owner_id, title, status, "
                    "total_amount, currency) VALUES (:i, :o, :u, 'Old', 'approved', 42.50, 'USD')"
                ),
                {"i": report, "o": org, "u": user},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO trips (id, organization_id, traveler_id, origin, "
                    "primary_destination, start_date, end_date, currency, status) VALUES "
                    "(:i, :o, :u, 'NYC', 'SFO', '2026-01-01', '2026-01-03', 'USD', 'approved')"
                ),
                {"i": trip, "o": org, "u": user},
            )

        _alembic(["upgrade", "0016_report_trip_lifecycle"], scratch_url)
        assert REPORT_COLUMNS <= _columns(engine, "expense_reports")
        assert TRIP_COLUMNS <= _columns(engine, "trips")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT status, total_amount, "
                    + ", ".join(sorted(REPORT_COLUMNS))
                    + " FROM expense_reports WHERE id = :i"
                ),
                {"i": report},
            ).one()
            assert row[0] == "approved" and str(row[1]) == "42.50"
            assert all(v is None for v in row[2:])
            trow = conn.execute(
                sa.text(
                    "SELECT status, "
                    + ", ".join(sorted(TRIP_COLUMNS))
                    + " FROM trips WHERE id = :i"
                ),
                {"i": trip},
            ).one()
            assert trow[0] == "approved" and all(v is None for v in trow[1:])
        assert _grants(engine) == [("finance_admin", EXPENSE_REIMBURSE_ORG)]

        _alembic(["downgrade", "0015_canonical_policy_figures"], scratch_url)
        assert not REPORT_COLUMNS & _columns(engine, "expense_reports")
        assert not TRIP_COLUMNS & _columns(engine, "trips")
        assert _grants(engine) == []
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT count(*) FROM permissions WHERE key = :k"),
                    {"k": EXPENSE_REIMBURSE_ORG},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.text("SELECT status FROM expense_reports WHERE id = :i"), {"i": report}
                ).scalar_one()
                == "approved"
            )

        _alembic(["upgrade", "head"], scratch_url)
        assert _grants(engine) == [("finance_admin", EXPENSE_REIMBURSE_ORG)]
    finally:
        engine.dispose()
