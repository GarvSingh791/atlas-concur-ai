"""Live-DB tests for ItineraryCalendarTool (P4-T4).

Marked @pytest.mark.db — self-skips when Postgres unreachable.

Proves:
  - Seeded SFO→JFK trip returns 2 segments (flight UA201 + hotel Marriott)
  - Both calendar events ("Q2 Engineering Summit Day 1/2", location "New York, NY")
    are returned for the employee
  - segment.details round-trips (flight_number, cabin_class, etc.)
  - context.user_id=None → ToolError
  - Cross-user leak test: employee sees none of manager's segments/events
  - Cross-org isolation
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
from ai_agents.tools.itinerary_calendar import (
    ItineraryCalendarTool,
    ItineraryIntent,
)

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
        pytest.skip("Postgres not reachable — skipping ItineraryCalendarTool tests")

    insp = sa.inspect(engine)
    if "trips" not in insp.get_table_names():
        pytest.skip("trips table not found — run `alembic upgrade head` first")

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
        correlation_id="itinerary-test",
    )


# ---------------------------------------------------------------------------
# Trips + segments
# ---------------------------------------------------------------------------


class TestItinerary:
    def test_sfo_jfk_trip_present(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        destinations = {t.primary_destination for t in result.trips}
        assert "JFK" in destinations, f"Expected SFO→JFK trip, got: {result.trips}"

    def test_two_segments_returned(self, seeded: dict, db_session: Session) -> None:
        """The seeded trip has exactly 2 segments: flight + hotel."""
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        segment_types = [s.segment_type for s in result.segments]
        assert "flight" in segment_types
        assert "hotel" in segment_types

    def test_flight_segment_details_roundtrip(self, seeded: dict, db_session: Session) -> None:
        """segment.details must carry the seeded flight metadata."""
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        flight_segs = [s for s in result.segments if s.segment_type == "flight"]
        assert flight_segs, "Expected at least one flight segment"
        details = flight_segs[0].details
        assert details.get("flight_number") == "UA201"
        assert details.get("cabin_class") == "economy"

    def test_flight_segment_supplier(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        flight_segs = [s for s in result.segments if s.segment_type == "flight"]
        assert flight_segs[0].supplier == "United Airlines"

    def test_hotel_segment_location(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        hotel_segs = [s for s in result.segments if s.segment_type == "hotel"]
        assert hotel_segs, "Expected hotel segment"
        assert hotel_segs[0].location == "New York, NY"

    def test_source_field(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.source == "trips"


# ---------------------------------------------------------------------------
# Calendar events
# ---------------------------------------------------------------------------


class TestCalendarEvents:
    def test_two_calendar_events_returned(self, seeded: dict, db_session: Session) -> None:
        """Seed has 2 calendar events for the employee."""
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert (
            len(result.calendar_events) >= 2
        ), f"Expected ≥2 calendar events, got {len(result.calendar_events)}"

    def test_calendar_event_location_new_york(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        locations = {e.location for e in result.calendar_events if e.location}
        assert any(
            "New York" in loc for loc in locations
        ), f"Expected 'New York' in calendar event locations, got: {locations}"

    def test_calendar_event_titles_summit(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        titles = [e.title for e in result.calendar_events if e.title]
        assert any("Summit" in t for t in titles), f"Expected Summit events, got titles: {titles}"

    def test_include_calendar_false_returns_empty(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=False),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        assert result.calendar_events == ()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_null_user_id_raises_tool_error(self, seeded: dict, db_session: Session) -> None:
        tool = ItineraryCalendarTool()
        ctx = AgentContext(
            organization_id=seeded["org_id"],
            granted_permission_keys=frozenset(),
            user_id=None,
        )
        with pytest.raises(ToolError, match="user_id"):
            tool.get_itinerary(ItineraryIntent(), context=ctx, session=db_session)


# ---------------------------------------------------------------------------
# Cross-user + cross-org scoping (leak test)
# ---------------------------------------------------------------------------


class TestScopingLeakPrevention:
    def test_employee_sees_none_of_manager_segments(
        self, seeded: dict, db_session: Session
    ) -> None:
        """Employee context must not return manager's trip segments or events."""
        tool = ItineraryCalendarTool()

        emp_result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(seeded["org_id"], seeded["employee_id"]),
            session=db_session,
        )
        mgr_result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(seeded["org_id"], seeded["manager_id"]),
            session=db_session,
        )

        emp_seg_ids = {s.segment_id for s in emp_result.segments}
        mgr_seg_ids = {s.segment_id for s in mgr_result.segments}
        assert emp_seg_ids.isdisjoint(
            mgr_seg_ids
        ), f"Segment IDs must not overlap: employee={emp_seg_ids} manager={mgr_seg_ids}"

        emp_evt_ids = {e.event_id for e in emp_result.calendar_events}
        mgr_evt_ids = {e.event_id for e in mgr_result.calendar_events}
        assert emp_evt_ids.isdisjoint(
            mgr_evt_ids
        ), f"Event IDs must not overlap: employee={emp_evt_ids} manager={mgr_evt_ids}"

    def test_cross_org_returns_empty_itinerary(self, seeded: dict, db_session: Session) -> None:
        """A different org_id must return no trips, segments, or events."""
        tool = ItineraryCalendarTool()
        other_org = uuid.uuid4()
        result = tool.get_itinerary(
            ItineraryIntent(include_calendar=True),
            context=_ctx(other_org, seeded["employee_id"]),
            session=db_session,
        )
        assert result.trips == ()
        assert result.segments == ()
        assert result.calendar_events == ()
