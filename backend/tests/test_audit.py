"""Validate gate (part 2): an audited test action writes exactly one audit_log row.

NOT hermetic — needs a running Postgres; self-skips when unreachable (mirrors
``test_db_conventions.py``), so hermetic CI stays green.  Creates the minimal
set of tables required by the now-resolved FKs (organizations, users, audit_log)
via scoped ``create_all`` and drops them on teardown.  A function-scoped cleanup
keeps each test's row count deterministic.

FK note (P2-T3): audit_log.organization_id → organizations and
audit_log.actor_id → users are now real constraints, so the fixture must first
insert a live Organization + User and the tests must reference those ids.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy import Table, delete, func, select
from sqlalchemy.engine import Engine

from backend.app.core.audit import record_audit
from backend.app.core.database import get_engine, get_sessionmaker
from database.models import AuditLog, Base
from database.models.identity import Organization, RoleKey, User, UserStatus

# Every test in this module needs a live Postgres.
pytestmark = pytest.mark.db

_AUDIT_TABLE: Table = cast(Table, AuditLog.__table__)
_ORG_TABLE: Table = cast(Table, Organization.__table__)
_USER_TABLE: Table = cast(Table, User.__table__)


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the shared engine if Postgres is reachable, else skip the module.

    Creates the required tables if they don't already exist (e.g. on a clean
    test DB); on a DB where the migration has already run, create_all is a
    no-op.  Tracks which tables were NEW and only drops those on teardown, so
    running against the production DB after a migration never drops real data.
    """
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    engine = get_engine()
    try:
        connection = engine.connect()
        connection.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping live audit test")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [
        t for t in [_ORG_TABLE, _USER_TABLE, _AUDIT_TABLE] if t.name not in existing
    ]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        # Only drop tables this fixture created (not pre-existing migration tables).
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


@pytest.fixture(scope="module")
def seed_ids(db_engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    """Insert one Organization + User; clean up on teardown."""
    factory = get_sessionmaker()
    with factory() as session:
        org = Organization(name="Audit Test Org", base_currency="USD")
        session.add(org)
        session.flush()

        actor = User(
            organization_id=org.id,
            name="Audit Actor",
            email="audit-actor@test.internal",
            role=RoleKey.employee,
            status=UserStatus.active,
            password_hash="$2b$12$placeholder",
        )
        session.add(actor)
        session.commit()
        org_id = org.id

    yield {"org_id": org_id, "actor_id": actor.id}

    # Teardown: remove test data (audit_log rows cleaned per-test; delete org+user here).
    with factory() as session:
        session.query(User).filter(User.organization_id == org_id).delete()
        session.query(Organization).filter(Organization.id == org_id).delete()
        session.commit()


@pytest.fixture(autouse=True)
def _clean_table(db_engine: Engine) -> None:
    """Empty audit_log before each test so row-count assertions are deterministic."""
    factory = get_sessionmaker()
    with factory() as session:
        session.execute(delete(AuditLog))
        session.commit()


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def test_audit_writes_exactly_one_row(db_engine: Engine, seed_ids: dict) -> None:
    factory = get_sessionmaker()
    org_id: uuid.UUID = seed_ids["org_id"]
    actor_id: uuid.UUID = seed_ids["actor_id"]
    entity_id = uuid.uuid4()

    with factory() as session:
        entry = record_audit(
            session,
            organization_id=org_id,
            action="test.performed",
            entity_type="test_entity",
            entity_id=entity_id,
            actor_id=actor_id,
            before_state=None,
            after_state={"status": "done"},
        )
        # flush (inside record_audit) populated the server-side defaults
        assert isinstance(entry.id, uuid.UUID)
        assert entry.created_at is not None
        session.commit()

    with factory() as session:
        assert _count(session) == 1
        row = session.execute(select(AuditLog)).scalar_one()
        assert row.organization_id == org_id
        assert row.actor_id == actor_id
        assert row.action == "test.performed"
        assert row.entity_type == "test_entity"
        assert row.entity_id == entity_id
        assert row.before_state is None
        assert row.after_state == {"status": "done"}
        assert row.created_at is not None


def test_audit_system_initiated_actor_is_null(db_engine: Engine, seed_ids: dict) -> None:
    factory = get_sessionmaker()
    org_id: uuid.UUID = seed_ids["org_id"]

    with factory() as session:
        record_audit(
            session,
            organization_id=org_id,
            action="system.cleanup",
            entity_type="job",
            entity_id=uuid.uuid4(),
            actor_id=None,  # system-initiated
        )
        session.commit()

    with factory() as session:
        assert _count(session) == 1
        row = session.execute(select(AuditLog)).scalar_one()
        assert row.actor_id is None


def test_audit_log_is_append_only() -> None:
    """The model carries created_at only — no updated_at / version / soft-delete."""
    for forbidden in ("updated_at", "version", "deleted_at"):
        assert not hasattr(AuditLog, forbidden), f"audit_log must not have {forbidden}"

    column_names = {col.name for col in _AUDIT_TABLE.columns}
    assert column_names == {
        "id",
        "organization_id",
        "actor_id",
        "action",
        "entity_type",
        "entity_id",
        "before_state",
        "after_state",
        "created_at",
    }
