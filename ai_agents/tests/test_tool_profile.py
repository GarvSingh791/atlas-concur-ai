"""Live-DB tests for ProfileTool (P4-T4).

Marked @pytest.mark.db — self-skips when Postgres unreachable.

Proves:
  - Seeded employee profile: email, role, manager_id present
  - preferences == {} (never None) — null-safe (all seed users have NULL preferences)
  - recent_trips includes the seeded SFO→JFK trip
  - context.user_id=None → ToolError
  - Cross-user: employee context returns NONE of manager's trips
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from ai_agents.tools.profile import ProfileIntent, ProfileTool

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping ProfileTool tests")

    insp = sa.inspect(engine)
    if "users" not in insp.get_table_names():
        pytest.skip("users table not found — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization, User  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        employee = session.query(User).filter(User.email == "employee@atlas-demo.internal").one()
        manager = session.query(User).filter(User.email == "manager@atlas-demo.internal").one()

    return {
        "org_id": org.id,
        "employee_id": employee.id,
        "manager_id": manager.id,
        "employee_email": "employee@atlas-demo.internal",
    }


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=frozenset(),
        user_id=user_id,
        correlation_id="profile-test",
    )


# ---------------------------------------------------------------------------
# Core profile
# ---------------------------------------------------------------------------


class TestProfileContent:
    def test_employee_email_present(self, seeded: dict, db_session: Session) -> None:
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.email == seeded["employee_email"]

    def test_employee_role_present(self, seeded: dict, db_session: Session) -> None:
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.role == "employee"

    def test_manager_id_set(self, seeded: dict, db_session: Session) -> None:
        """Employee has manager_id pointing to the manager user."""
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.manager_id == str(seeded["manager_id"])

    def test_preferences_null_safe_returns_empty_dict(
        self, seeded: dict, db_session: Session
    ) -> None:
        """All seed users have preferences=NULL — must return {}, never None."""
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert (
            result.preferences == {}
        ), f"preferences must be {{}} when DB has NULL, got {result.preferences!r}"
        assert result.preferences is not None

    def test_recent_trips_includes_sfo_jfk(self, seeded: dict, db_session: Session) -> None:
        """Employee has the seeded SFO→JFK trip."""
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(recent_trips_limit=10),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        destinations = {t.primary_destination for t in result.recent_trips}
        assert (
            "JFK" in destinations
        ), f"Expected SFO→JFK trip in recent_trips, got: {result.recent_trips}"

    def test_source_field(self, seeded: dict, db_session: Session) -> None:
        tool = ProfileTool()
        result = tool.get_profile(
            ProfileIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.source == "users"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestProfileErrors:
    def test_null_user_id_raises_tool_error(self, seeded: dict, db_session: Session) -> None:
        tool = ProfileTool()
        ctx = AgentContext(
            organization_id=seeded["org_id"],
            granted_permission_keys=frozenset(),
            user_id=None,
            correlation_id="profile-test",
        )
        with pytest.raises(ToolError, match="user_id"):
            tool.get_profile(ProfileIntent(), context=ctx, session=db_session)


# ---------------------------------------------------------------------------
# Cross-user isolation (scoping test)
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    def test_employee_context_never_returns_manager_trips(
        self, seeded: dict, db_session: Session
    ) -> None:
        """Employee's trips must not bleed into manager's profile and vice versa."""
        tool = ProfileTool()

        emp_result = tool.get_profile(
            ProfileIntent(recent_trips_limit=50),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        mgr_result = tool.get_profile(
            ProfileIntent(recent_trips_limit=50),
            context=_ctx(seeded["org_id"], seeded["manager_id"]),
            session=db_session,
        )

        emp_trip_ids = {t.trip_id for t in emp_result.recent_trips}
        mgr_trip_ids = {t.trip_id for t in mgr_result.recent_trips}

        # The seeded trip is for the employee — manager should not see it
        assert emp_trip_ids.isdisjoint(
            mgr_trip_ids
        ), f"Trip IDs must not overlap: employee={emp_trip_ids} manager={mgr_trip_ids}"

    def test_cross_org_returns_no_profile(self, seeded: dict, db_session: Session) -> None:
        """A user_id from a different org must raise ToolError (not found)."""
        tool = ProfileTool()
        other_org = uuid.uuid4()
        ctx = _ctx(other_org, seeded["employee_id"])
        with pytest.raises(ToolError, match="not found"):
            tool.get_profile(ProfileIntent(), context=ctx, session=db_session)
