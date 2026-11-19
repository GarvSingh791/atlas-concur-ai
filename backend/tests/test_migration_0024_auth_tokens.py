"""Migration 0024 (auth tokens + users.email_verified_at / password_changed_at).

Static part (hermetic): revision id / parent and what it owns.

Live part (``db``, a throwaway database of its own derived from the configured
test DB name): upgrade to the parent, insert a populated slice (org + user),
upgrade to 0024 and check that
  * ``auth_tokens`` exists, is empty, and its CHECKs refuse an unknown purpose
    and a non-SHA-256 token hash;
  * the two ``users`` columns exist, are nullable and NULL on the existing user
    (no backfill: a legacy account is "not verified", its tokens stay valid);
  * the existing user row is otherwise untouched;
then downgrade (table and columns gone, the user untouched) and upgrade again.
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
from sqlalchemy.exc import IntegrityError

from config.settings import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "versions" / "0024_auth_tokens.py"
USER_COLUMNS = {"email_verified_at", "password_changed_at"}
HEAD = "0024_auth_tokens"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0024", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_ids_and_owned_columns() -> None:
    module = _load_module()
    assert module.revision == HEAD
    assert module.down_revision == "0023_entry_release_lineage"
    assert set(module._USER_COLUMNS) == USER_COLUMNS


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
    db_name = f"{make_url(raw).database}_auth_0024"
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


def _user_columns(engine: sa.Engine) -> dict[str, Any]:
    return {
        c["name"]: c for c in sa.inspect(engine).get_columns("users") if c["name"] in USER_COLUMNS
    }


@pytest.mark.db
def test_upgrade_on_populated_db_then_symmetric_downgrade(scratch_url: str) -> None:
    parent = _load_module().down_revision
    _alembic(["upgrade", parent], scratch_url)
    engine = create_engine(scratch_url)
    org, user = uuid.uuid4(), uuid.uuid4()
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
                    "VALUES (:i, :o, 'legacy@example.test', 'Legacy', 'employee', 'hash-x')"
                ),
                {"i": user, "o": org},
            )

        def _user() -> tuple[Any, ...]:
            with engine.connect() as conn:
                return tuple(
                    conn.execute(
                        sa.text(
                            "SELECT email, name, role::text, status::text, password_hash, "
                            "updated_at FROM users WHERE id = :i"
                        ),
                        {"i": user},
                    ).one()
                )

        before = _user()

        _alembic(["upgrade", HEAD], scratch_url)
        cols = _user_columns(engine)
        assert set(cols) == USER_COLUMNS
        assert all(c["nullable"] for c in cols.values())
        with engine.connect() as conn:
            values = conn.execute(
                sa.text("SELECT email_verified_at, password_changed_at FROM users WHERE id = :i"),
                {"i": user},
            ).one()
            assert values == (None, None)
            assert conn.execute(sa.text("SELECT count(*) FROM auth_tokens")).scalar_one() == 0
        assert _user() == before

        insert = sa.text(
            "INSERT INTO auth_tokens (organization_id, user_id, purpose, token_hash, expires_at) "
            "VALUES (:o, :u, :p, :h, now() + interval '30 minutes')"
        )
        good_hash = "a" * 64
        with engine.begin() as conn:
            conn.execute(insert, {"o": org, "u": user, "p": "password_reset", "h": good_hash})
        for purpose, token_hash in (("magic_link", "b" * 64), ("invite", "not-a-sha256")):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {"o": org, "u": user, "p": purpose, "h": token_hash})
        with pytest.raises(IntegrityError), engine.begin() as conn:  # unique token_hash
            conn.execute(insert, {"o": org, "u": user, "p": "invite", "h": good_hash})

        _alembic(["downgrade", parent], scratch_url)
        assert _user_columns(engine) == {}
        assert "auth_tokens" not in sa.inspect(engine).get_table_names()
        assert _user() == before

        _alembic(["upgrade", HEAD], scratch_url)
        assert set(_user_columns(engine)) == USER_COLUMNS
        assert "auth_tokens" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
