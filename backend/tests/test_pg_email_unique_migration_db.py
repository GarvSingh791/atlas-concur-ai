"""Product gap 3 — migration 0020 adds uq_users_email_lower and fails closed on duplicates.

Login resolves a user by email across all organizations, so 0020 makes the
email globally unique (case-insensitive).  On a populated database that already
holds a cross-org duplicate the migration must REFUSE (no guessing which account
to keep, no row modified) and its message must not contain the email (no PII in
logs).  Once the duplicate is resolved, the same upgrade succeeds.

Uses its own throwaway database (derived from the configured test DB name, so
parallel worktrees never collide); never touches the shared test schema.
NOT hermetic — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from config.settings import get_settings

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "versions" / "0020_notification_reads.py"


def _parent_revision() -> str:
    """0020's own parent — the chain position moves as other tracks are integrated."""
    spec = importlib.util.spec_from_file_location("_mig_0020", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parent = module.down_revision
    assert isinstance(parent, str)
    return parent


_BEFORE = _parent_revision()
# 0022 (organization-aware login) drops this global index again, so the test
# pins 0020 itself instead of head (updated in the known-limits identity track).
_TARGET = "0020_notification_reads"
_DUP_EMAIL = "Shared.Person@example.test"


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


def _alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["alembic", *args],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[str]:
    raw_url = get_settings().DATABASE_URL
    db_name = f"{make_url(raw_url).database}_email_guard"
    admin_url = _url_with_db(raw_url, "postgres")
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping migration guard test")
        return

    def _drop() -> None:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))

    _drop()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    url = _url_with_db(raw_url, db_name)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    try:
        yield url
    finally:
        _drop()
        admin.dispose()


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def _has_index(engine: sa.Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")
        ).first()
    return row is not None


def test_0020_refuses_cross_org_duplicates_then_succeeds_once_resolved(scratch_url: str) -> None:
    up = _alembic(["upgrade", _BEFORE], scratch_url)
    assert up.returncode == 0, up.stderr

    engine = create_engine(scratch_url)
    try:
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        dup_b = uuid.uuid4()
        with engine.begin() as conn:
            for org, name in ((org_a, "Org A"), (org_b, "Org B")):
                conn.execute(
                    sa.text(
                        "INSERT INTO organizations (id, name, base_currency) "
                        "VALUES (:id, :n, 'USD')"
                    ),
                    {"id": org, "n": name},
                )
            for uid, org, email in (
                (uuid.uuid4(), org_a, _DUP_EMAIL),
                (dup_b, org_b, _DUP_EMAIL.lower()),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, organization_id, name, email, role, status, "
                        "password_hash) VALUES (:id, :org, 'X', :email, 'employee', "
                        "'active', 'h')"
                    ),
                    {"id": uid, "org": org, "email": email},
                )

        refused = _alembic(["upgrade", _TARGET], scratch_url)
        assert refused.returncode != 0
        output = refused.stdout + refused.stderr
        assert "shared by more than one user" in output
        assert _DUP_EMAIL.lower() not in output.lower()  # no PII in the message
        assert _version(engine) == _BEFORE  # nothing applied
        assert not _has_index(engine)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 2

        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": dup_b})
        ok = _alembic(["upgrade", _TARGET], scratch_url)
        assert ok.returncode == 0, ok.stderr
        assert _has_index(engine)

        down = _alembic(["downgrade", _BEFORE], scratch_url)
        assert down.returncode == 0, down.stderr
        assert not _has_index(engine)
    finally:
        engine.dispose()
