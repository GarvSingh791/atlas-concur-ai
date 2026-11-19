"""Migration 0023 (re-file lineage on expense lines) — safe on a POPULATED database, symmetric.

Static part (hermetic): the revision id / parent and the three columns it owns.

Live part (``db``, a throwaway database of its own derived from the configured
test DB name, so parallel worktrees never collide): upgrade to the parent,
insert a populated slice (org, user, report, one expense line), upgrade to 0023
and check that
  * the three columns exist, are nullable, and are NULL on the existing line
    (no backfill — a legacy line keeps today's matching);
  * the existing line is otherwise untouched;
then downgrade (exactly these columns gone, the line untouched) and upgrade again.
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

from config.settings import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "versions" / "0023_entry_release_lineage.py"
COLUMNS = {"released_from_report_id", "released_from_approval_id", "released_at"}


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0023", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_ids_and_owned_columns() -> None:
    module = _load_module()
    assert module.revision == "0023_entry_release_lineage"
    assert module.down_revision == "0022_org_scoped_email"
    assert set(module._COLUMNS) == COLUMNS


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
    db_name = f"{make_url(raw).database}_lineage_0023"
    admin_url = _url_with_db(raw, "postgres")
    url = _url_with_db(raw, db_name)
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
        conn.execute(sa.text(drop), {"db": db_name})
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(drop), {"db": db_name})
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()


def _lineage_columns(engine: sa.Engine) -> dict[str, Any]:
    return {
        c["name"]: c for c in sa.inspect(engine).get_columns("expenses") if c["name"] in COLUMNS
    }


@pytest.mark.db
def test_upgrade_on_populated_db_then_symmetric_downgrade(scratch_url: str) -> None:
    parent = _load_module().down_revision
    _alembic(["upgrade", parent], scratch_url)
    engine = create_engine(scratch_url)
    org, user, report, line = (uuid.uuid4() for _ in range(4))
    try:
        with engine.begin() as conn:
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
                    "VALUES (:i, :o, 'lineage@example.test', 'P', 'employee', 'x')"
                ),
                {"i": user, "o": org},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO expense_reports (id, organization_id, owner_id, title, status, "
                    "total_amount, currency) VALUES (:i, :o, :u, 'Old', 'draft', 24.50, 'USD')"
                ),
                {"i": report, "o": org, "u": user},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO expenses (id, organization_id, report_id, category, amount, "
                    "currency, expense_date, vendor, status) VALUES "
                    "(:i, :o, :r, 'meals', 24.50, 'USD', '2026-06-10', 'Cafe', 'draft')"
                ),
                {"i": line, "o": org, "r": report},
            )

        def _line() -> tuple[Any, ...]:
            with engine.connect() as conn:
                return tuple(
                    conn.execute(
                        sa.text(
                            "SELECT report_id, category::text, amount, currency, expense_date, "
                            "vendor, status::text FROM expenses WHERE id = :i"
                        ),
                        {"i": line},
                    ).one()
                )

        before = _line()

        _alembic(["upgrade", "0023_entry_release_lineage"], scratch_url)
        cols = _lineage_columns(engine)
        assert set(cols) == COLUMNS
        assert all(c["nullable"] for c in cols.values())
        with engine.connect() as conn:
            lineage = conn.execute(
                sa.text("SELECT " + ", ".join(sorted(COLUMNS)) + " FROM expenses WHERE id = :i"),
                {"i": line},
            ).one()
        assert all(v is None for v in lineage)
        assert _line() == before

        _alembic(["downgrade", parent], scratch_url)
        assert _lineage_columns(engine) == {}
        assert _line() == before

        _alembic(["upgrade", "0023_entry_release_lineage"], scratch_url)
        assert set(_lineage_columns(engine)) == COLUMNS
    finally:
        engine.dispose()
