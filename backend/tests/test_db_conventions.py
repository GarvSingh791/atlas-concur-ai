"""Live-DB Validate gate for P2-T1 — prove the conventions + engine round-trip.

This test is NOT hermetic: it needs a running Postgres. It self-skips when the
database is unreachable, so the hermetic CI (no Postgres service) stays green
while the gate is proven locally against a live pg16. Run it with a database up,
e.g. ``docker compose -f docker/docker-compose.yml up -d postgres`` (or any
Postgres matching the configured ``DATABASE_URL``).

It exercises the real ``Base`` + mixins and the shared engine/session from
``backend.app.core.database`` — the same objects Phase 3 models will use.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from sqlalchemy import String, Table
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import get_engine, get_sessionmaker
from database.models import (
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)

# Every test in this module needs a live Postgres.
pytestmark = pytest.mark.db


class _ScratchConventions(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    """Throwaway, TEST-ONLY model composing a subset of the convention mixins.

    Defined inside the test module (never in ``database/models/``) so it cannot
    leak into or pre-empt the real Phase 3 schema. ``Base`` is LAST in the bases
    list so the declarative metaclass resolves correctly. Exists solely to prove
    the mixins + engine round-trip against a live Postgres for the Validate gate.
    """

    __tablename__ = "_scratch_p2t1_conventions"

    label: Mapped[str] = mapped_column(String(50), nullable=False)


# ``Model.__table__`` is typed as ``FromClause``; the create/drop helpers want a
# concrete ``Table``. The cast is safe — declarative always sets a real Table.
_SCRATCH_TABLE: Table = cast(Table, _ScratchConventions.__table__)


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the shared engine if Postgres is reachable, else skip the module.

    Creates ONLY the scratch table (scoped — never an unscoped ``create_all``)
    and drops it on teardown, so no real schema is materialized and nothing is
    left behind.
    """
    engine = get_engine()
    try:
        connection = engine.connect()
        connection.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping live DB convention test")

    Base.metadata.create_all(bind=engine, tables=[_SCRATCH_TABLE])
    try:
        yield engine
    finally:
        # Drop just this table directly — avoid metadata.drop_all which can
        # also attempt to drop enum types from other models in Base.metadata.
        _SCRATCH_TABLE.drop(bind=engine, checkfirst=True)


def test_conventions_round_trip(db_engine: Engine) -> None:
    """Create a row, read it back, and assert every convention fired."""
    session_factory = get_sessionmaker()

    # ---- INSERT: server-side defaults populate the convention columns ----
    with session_factory() as session:
        row = _ScratchConventions(label="initial")
        session.add(row)
        session.commit()
        session.refresh(row)

        row_id = row.id
        assert isinstance(row_id, uuid.UUID)  # UUID PK auto-generated DB-side
        assert row.created_at is not None  # TimestampMixin
        assert row.updated_at is not None
        assert row.version == 1  # VersionMixin default
        assert row.deleted_at is None  # SoftDeleteMixin: live row
        assert row.is_deleted is False

        initial_created_at = row.created_at
        initial_updated_at = row.updated_at

    # ---- UPDATE in a SEPARATE transaction: updated_at advances ----
    with session_factory() as session:
        fetched = session.get(_ScratchConventions, row_id)
        assert fetched is not None
        fetched.label = "changed"
        session.commit()
        session.refresh(fetched)

        # created_at is immutable; updated_at moves forward (now() is the
        # transaction-start timestamp, and this is a new transaction).
        assert fetched.created_at == initial_created_at
        assert fetched.updated_at > initial_updated_at
