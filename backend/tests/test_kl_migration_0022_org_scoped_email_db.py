"""Migration 0022 — organizations.slug backfill and per-organization email uniqueness.

On a POPULATED database at 0022's parent revision:

* every existing organization gets a deterministic, unique, well-formed slug
  derived from its name (collisions -> -2, -3 in created_at order; reserved
  words -> "-org"; empty -> "org-unnamed"); the demo org gets "atlas-demo-corp";
* no user row or other organization column is modified;
* the global uq_users_email_lower is replaced by uq_users_org_email_lower;
* downgrade REFUSES (count only, no email) while an email exists in two orgs,
  and succeeds (restoring the global index) once resolved;
* re-upgrading produces the same slugs (deterministic).

Own throwaway database (derived from the configured test DB name).  NOT
hermetic — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from config.settings import get_settings

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "versions" / "0022_org_scoped_email.py"
_TARGET = "0022_org_scoped_email"
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHARED = "Twin.Person@example.test"


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("_mig_0022", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_migration()
_BEFORE: str = _MOD.down_revision  # type: ignore[attr-defined]


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
    db_name = f"{make_url(raw_url).database}_org_email"
    admin_url = _url_with_db(raw_url, "postgres")
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping migration 0022 test")
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


def _indexes(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return {
            str(r[0])
            for r in conn.execute(
                sa.text("SELECT indexname FROM pg_indexes WHERE tablename='users'")
            )
        }


def _slugs(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            str(r[0]): str(r[1])
            for r in conn.execute(sa.text("SELECT id, slug FROM organizations"))
        }


def _users_snapshot(engine: sa.Engine) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                sa.text(
                    "SELECT id, organization_id, email, name, status, password_hash "
                    "FROM users ORDER BY id"
                )
            )
        ]


def test_0022_backfills_slugs_and_moves_email_uniqueness_to_the_org(scratch_url: str) -> None:
    up = _alembic(["upgrade", _BEFORE], scratch_url)
    assert up.returncode == 0, up.stderr

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    names = [
        ("Atlas Demo Corp", "atlas-demo-corp"),
        ("Acme Corp", "acme-corp"),
        ("ACME corp!!", "acme-corp-2"),
        ("  Acme -- Corp ", "acme-corp-3"),
        ("Admin", "admin-org"),
        ("!!!", "org-unnamed"),
        ("Zürich Reisen GmbH", "z-rich-reisen-gmbh"),
        ("x" * 80, "x" * 48),
        # One-character stems would break the CHECK (2..63 chars) and abort
        # the whole upgrade on a populated database.
        ("Q", "q-org"),
        ("X", "x-org"),
        ("é1", "1-org"),
        ("7", "7-org"),
    ]
    org_ids: list[uuid.UUID] = []
    engine = create_engine(scratch_url)
    try:
        with engine.begin() as conn:
            for i, (name, _expected) in enumerate(names):
                oid = uuid.uuid4()
                org_ids.append(oid)
                conn.execute(
                    sa.text(
                        "INSERT INTO organizations (id, name, base_currency, created_at) "
                        "VALUES (:id, :n, 'USD', :ts)"
                    ),
                    {"id": oid, "n": name, "ts": t0 + timedelta(minutes=i)},
                )
            for org in org_ids[:2]:
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, organization_id, name, email, role, status, "
                        "password_hash) VALUES (:id, :org, 'X', :email, 'employee', "
                        "'active', 'h')"
                    ),
                    {"id": uuid.uuid4(), "org": org, "email": f"{org.hex[:6]}@example.test"},
                )
        users_before = _users_snapshot(engine)

        ok = _alembic(["upgrade", _TARGET], scratch_url)
        assert ok.returncode == 0, ok.stderr
        slugs = _slugs(engine)
        for oid, (_name, expected) in zip(org_ids, names, strict=True):
            assert slugs[str(oid)] == expected
        assert len(set(slugs.values())) == len(slugs)
        assert all(_SLUG.match(s) for s in slugs.values())
        assert _users_snapshot(engine) == users_before  # no user row touched
        idx = _indexes(engine)
        assert "uq_users_org_email_lower" in idx
        assert "uq_users_email_lower" not in idx

        # Same email in two orgs is now legal; same org (any case) is not.
        with engine.begin() as conn:
            for org, email in ((org_ids[0], _SHARED), (org_ids[1], _SHARED.lower())):
                conn.execute(
                    sa.text(
                        "INSERT INTO users (id, organization_id, name, email, role, status, "
                        "password_hash) VALUES (:id, :org, 'T', :email, 'employee', "
                        "'active', 'h')"
                    ),
                    {"id": uuid.uuid4(), "org": org, "email": email},
                )
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, organization_id, name, email, role, status, "
                    "password_hash) VALUES (:id, :org, 'T', :email, 'employee', 'active', 'h')"
                ),
                {"id": uuid.uuid4(), "org": org_ids[0], "email": _SHARED.upper()},
            )

        # Downgrade cannot restore global uniqueness while the twin exists.
        refused = _alembic(["downgrade", _BEFORE], scratch_url)
        assert refused.returncode != 0
        output = refused.stdout + refused.stderr
        assert "more than one organization" in output
        assert _SHARED.lower() not in output.lower()
        assert "uq_users_org_email_lower" in _indexes(engine)

        with engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM users WHERE organization_id = :org AND name = 'T'"),
                {"org": org_ids[1]},
            )
        down = _alembic(["downgrade", _BEFORE], scratch_url)
        assert down.returncode == 0, down.stderr
        idx = _indexes(engine)
        assert "uq_users_email_lower" in idx and "uq_users_org_email_lower" not in idx
        with engine.connect() as conn:
            cols = {
                str(r[0])
                for r in conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'organizations'"
                    )
                )
            }
        assert "slug" not in cols

        # Deterministic: re-upgrading assigns exactly the same slugs.
        again = _alembic(["upgrade", _TARGET], scratch_url)
        assert again.returncode == 0, again.stderr
        assert _slugs(engine) == slugs
    finally:
        engine.dispose()


def test_slug_assignment_is_deterministic_and_never_reassigns_taken_slugs() -> None:
    assign = _MOD.assign_slugs  # type: ignore[attr-defined]
    rows = [(1, "Acme"), (2, "acme"), (3, "Login"), (4, None)]
    first = assign(rows, set())
    assert first == {1: "acme", 2: "acme-2", 3: "login-org", 4: "org-unnamed"}
    assert assign(rows, set()) == first
    # Rows that already hold a slug are not passed in; their slugs are only
    # avoided, never changed.
    assert assign([(5, "Acme")], set(first.values())) == {5: "acme-3"}


def test_migration_slug_rules_match_the_model_helper() -> None:
    from database.models.identity import RESERVED_ORG_SLUGS, org_slug_base

    for name in (
        "Atlas Demo Corp",
        "Admin",
        "",
        "  --  ",
        "Ünïcode & Co",
        "y" * 90,
        "WWW",
        "Q",
        "é1",
        "A!",
    ):
        assert _MOD.slug_base(name) == org_slug_base(name)  # type: ignore[attr-defined]
    for name in ("Q", "é1", "A!", "7"):
        base = org_slug_base(name)
        assert len(base) >= 2 and base.endswith("-org"), base
        assert _SLUG.match(base), base
    assert _MOD._RESERVED == RESERVED_ORG_SLUGS  # type: ignore[attr-defined]
