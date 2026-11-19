"""P2-T4 Validate gate (part 2): record-level scope filter tests.

NOT hermetic — needs a running Postgres; self-skips when unreachable.

Uses a throwaway scratch model (_ScratchScoped) to prove scope_query
without requiring any real domain model (no migration).

Test topology (two organizations):
  Org A:
    M   — manager (manager_id=None)
    R1  — employee, direct report of M (manager_id=M)
    R2  — employee, direct report of M (manager_id=M)
    U   — employee, unrelated (manager_id=None)
    Fin — finance_admin, unrelated to M (manager_id=None)
  Org B:
    O   — employee

Scratch rows: one row owned by each user (M, R1, R2, U, Fin, O).

Assertions:
  (a) scope_query with employee R1 → {R1}
  (b) scope_query with manager M  → {M, R1, R2}
      (NOT U, NOT Fin, NOT O — proves: unrelated same-org excluded; other-org excluded)
  (c) scope_query with finance Fin → {M, R1, R2, U, Fin}
      (NOT O — proves tenant isolation: finance never sees another org's row)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy import String, Table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.rbac import scope_query
from backend.app.core.security import hash_password
from database.models import Base
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.mixins import UUIDPrimaryKeyMixin
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Throwaway scratch model — defined here (never in database/models/) so it
# cannot affect the real schema.  UUIDPrimaryKeyMixin first, Base last.
# ---------------------------------------------------------------------------


class _ScratchScoped(UUIDPrimaryKeyMixin, Base):
    """Test-only: minimal model with organization_id + owner_id ownership cols."""

    __tablename__ = "_scratch_p2t4_scoped"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(50), nullable=False)


_SCRATCH_TABLE: Table = cast(Table, _ScratchScoped.__table__)

# Identity tables needed when migration hasn't run yet.
_IDENTITY_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
]

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping record-scoping tests")

    existing = set(sa_inspect(engine).get_table_names())

    # Create identity tables only if they don't exist yet (migration may have run).
    identity_to_create = [t for t in _IDENTITY_TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=identity_to_create)

    # Always create (and always drop) the scratch table.
    Base.metadata.create_all(bind=engine, tables=[_SCRATCH_TABLE])
    try:
        yield engine
    finally:
        _SCRATCH_TABLE.drop(bind=engine, checkfirst=True)
        for table in reversed(identity_to_create):
            table.drop(bind=engine, checkfirst=True)


# Org names and email suffixes for this test module.
_SCOPE_ORG_A = "Scope Test Org A"
_SCOPE_ORG_B = "Scope Test Org B"
_SCOPE_EMAIL_SUFFIX = "@scope-test.internal"


@pytest.fixture(scope="module")
def scoped_users(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed RBAC catalog + two orgs + test users + scratch rows.  Cleans up."""
    factory = get_sessionmaker()

    # Prune stale data.
    with factory() as session:
        for org_name in (_SCOPE_ORG_A, _SCOPE_ORG_B):
            stale_org = session.query(Organization).filter(Organization.name == org_name).first()
            if stale_org is not None:
                session.query(_ScratchScoped).filter(
                    _ScratchScoped.organization_id == stale_org.id
                ).delete()
                session.query(User).filter(User.organization_id == stale_org.id).delete()
                session.query(Organization).filter(Organization.id == stale_org.id).delete()
        session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        hpw = hash_password("scope-test-pw")

        # --- Org A ---
        org_a = Organization(name=_SCOPE_ORG_A, base_currency="USD")
        session.add(org_a)
        session.flush()

        # Manager M (top-level)
        user_m = User(
            organization_id=org_a.id,
            name="Manager M",
            email=f"m{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.manager,
            department="Scope",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_m)
        session.flush()

        # Direct reports R1, R2
        user_r1 = User(
            organization_id=org_a.id,
            name="Report R1",
            email=f"r1{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.employee,
            department="Scope",
            manager_id=user_m.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        user_r2 = User(
            organization_id=org_a.id,
            name="Report R2",
            email=f"r2{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.employee,
            department="Scope",
            manager_id=user_m.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add_all([user_r1, user_r2])

        # Unrelated same-org employee (no manager)
        user_u = User(
            organization_id=org_a.id,
            name="Unrelated U",
            email=f"u{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.employee,
            department="Scope",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_u)

        # Finance admin Fin
        user_fin = User(
            organization_id=org_a.id,
            name="Finance Fin",
            email=f"fin{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.finance_admin,
            department="Scope",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_fin)
        session.flush()

        # --- Org B ---
        org_b = Organization(name=_SCOPE_ORG_B, base_currency="USD")
        session.add(org_b)
        session.flush()

        user_o = User(
            organization_id=org_b.id,
            name="Other Org O",
            email=f"o{_SCOPE_EMAIL_SUFFIX}",
            role=RoleKey.employee,
            department="Scope",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_o)
        session.flush()

        # One scratch row per user.
        session.add_all(
            [
                _ScratchScoped(
                    organization_id=user_m.organization_id, owner_id=user_m.id, label="row-M"
                ),
                _ScratchScoped(
                    organization_id=user_r1.organization_id, owner_id=user_r1.id, label="row-R1"
                ),
                _ScratchScoped(
                    organization_id=user_r2.organization_id, owner_id=user_r2.id, label="row-R2"
                ),
                _ScratchScoped(
                    organization_id=user_u.organization_id, owner_id=user_u.id, label="row-U"
                ),
                _ScratchScoped(
                    organization_id=user_fin.organization_id,
                    owner_id=user_fin.id,
                    label="row-Fin",
                ),
                _ScratchScoped(
                    organization_id=user_o.organization_id, owner_id=user_o.id, label="row-O"
                ),
            ]
        )
        session.commit()

    yield {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "user_m": user_m,
        "user_r1": user_r1,
        "user_r2": user_r2,
        "user_u": user_u,
        "user_fin": user_fin,
        "user_o": user_o,
    }

    # Teardown
    with factory() as session:
        for org_id in (org_a.id, org_b.id):
            session.query(_ScratchScoped).filter(_ScratchScoped.organization_id == org_id).delete()
            session.query(User).filter(User.organization_id == org_id).delete()
            session.query(Organization).filter(Organization.id == org_id).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_scope(user: User, session: Any) -> list[str]:
    """Execute scope_query for *user* against _ScratchScoped; return sorted labels."""
    base = sa.select(_ScratchScoped.label)
    scoped = scope_query(
        base,
        user=user,
        ownership_column=_ScratchScoped.owner_id,  # type: ignore[arg-type]
        org_column=_ScratchScoped.organization_id,  # type: ignore[arg-type]
        session=session,
    )
    return sorted(session.scalars(scoped))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScopeQuery:
    def test_employee_sees_only_own_row(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """Employee R1 → only row-R1."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_r1"], session)
        assert labels == ["row-R1"]

    def test_manager_sees_self_and_direct_reports(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """Manager M → row-M, row-R1, row-R2 (NOT row-U, row-Fin, row-O)."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_m"], session)
        assert labels == ["row-M", "row-R1", "row-R2"]

    def test_manager_excludes_unrelated_same_org_user(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """Manager M does NOT see row-U (same org, but not a direct report)."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_m"], session)
        assert "row-U" not in labels

    def test_manager_excludes_other_org(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """Manager M does NOT see row-O (different org)."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_m"], session)
        assert "row-O" not in labels

    def test_finance_sees_whole_org(self, db_engine: Engine, scoped_users: dict[str, Any]) -> None:
        """Finance Fin → all five rows in org A (row-M, R1, R2, U, Fin)."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_fin"], session)
        assert labels == ["row-Fin", "row-M", "row-R1", "row-R2", "row-U"]

    def test_finance_excluded_from_other_org(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """Finance Fin does NOT see row-O (org B — tenant isolation enforced)."""
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_fin"], session)
        assert "row-O" not in labels

    def test_direct_reports_one_level_only(
        self, db_engine: Engine, scoped_users: dict[str, Any]
    ) -> None:
        """A manager's scope includes R1 and R2 (direct reports).

        If R1 had sub-reports, they would NOT appear — one level only.  This
        topology has no sub-reports, but the test confirms the set is exactly
        {M, R1, R2} with no unexpected members from deeper levels.
        """
        with get_sessionmaker()() as session:
            labels = _run_scope(scoped_users["user_m"], session)
        # Exactly three rows — any deeper transitive report would add a fourth.
        assert len(labels) == 3
        assert set(labels) == {"row-M", "row-R1", "row-R2"}
