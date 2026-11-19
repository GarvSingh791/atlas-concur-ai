"""ItineraryCalendarTool — grounded trip + calendar facts (P4-T4).

Reads the context user's trips + trip_segments + calendar_events, org + user scoped.
TripSegment has no org/user column — scoped transitively via the parent trips' ids.

Raises ToolError if context.user_id is None.

Downstream seam: itinerary facts feed the Paris-paradox reflection (receipt pipeline
P4-T11) and the Travel Agent (P4-T9).
segment_id → Citation(kind=source_field, ref="trip_segments.id", source="trips").
event_id   → Citation(kind=source_field, ref="calendar_events.id", source="trips").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from ai_agents.tools.profile import TripSummary, _date_to_str
from database.models.calendar_event import CalendarEvent
from database.models.trip import Trip
from database.models.trip_segment import TripSegment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItineraryIntent:
    """Filter options for itinerary lookup."""

    trip_id: str | None = None  # None → all user's trips
    include_calendar: bool = True


@dataclass(frozen=True)
class ItinerarySegment:
    """One leg of a trip — core grounding fact for location/date context.

    segment_id → Citation(kind=source_field, ref="trip_segments.id", source="trips").
    """

    segment_id: str
    trip_id: str
    segment_type: str  # flight / hotel / rail / car
    supplier: str | None
    location: str | None
    start_datetime: str | None  # ISO-8601 with tz
    end_datetime: str | None  # ISO-8601 with tz
    amount: float | None
    currency: str | None
    details: dict  # raw JSONB (flight_number, cabin_class, etc.)


@dataclass(frozen=True)
class CalendarEntry:
    """One calendar event — grounding fact for location/time context.

    event_id → Citation(kind=source_field, ref="calendar_events.id", source="trips").
    """

    event_id: str
    title: str | None
    location: str | None
    start_datetime: str  # ISO-8601 with tz (NOT NULL on the model)
    end_datetime: str | None


@dataclass(frozen=True)
class ItineraryResult:
    """Combined itinerary + calendar grounding facts for the context user."""

    trips: tuple[TripSummary, ...]
    segments: tuple[ItinerarySegment, ...]
    calendar_events: tuple[CalendarEntry, ...]
    source: str = "trips"


def _dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class ItineraryCalendarTool:
    """Grounding tool: trip segments + calendar events (read-only, org+user scoped)."""

    def get_itinerary(
        self,
        intent: ItineraryIntent,
        *,
        context: AgentContext,
        session: Session,
    ) -> ItineraryResult:
        """Return the context user's trip segments and calendar events.

        Raises ToolError if context.user_id is None.
        """
        if context.user_id is None:
            raise ToolError(
                "ItineraryCalendarTool requires a user principal (context.user_id is None).",
                code="ITINERARY_CALENDAR_TOOL",
            )

        # Load trips (org + user scoped)
        trip_stmt = sa.select(Trip).where(
            Trip.traveler_id == context.user_id,
            Trip.organization_id == context.organization_id,
        )
        if intent.trip_id is not None:
            import uuid as _uuid

            trip_stmt = trip_stmt.where(Trip.id == _uuid.UUID(intent.trip_id))
        trips = list(session.scalars(trip_stmt).all())
        trip_ids = [t.id for t in trips]

        # Load segments (no org/user col — scoped via trip_id IN scoped trips)
        segments: list[TripSegment] = []
        if trip_ids:
            seg_stmt = sa.select(TripSegment).where(TripSegment.trip_id.in_(trip_ids))
            segments = list(session.scalars(seg_stmt).all())

        # Load calendar events (org + user scoped)
        calendar: list[CalendarEvent] = []
        if intent.include_calendar:
            cal_stmt = sa.select(CalendarEvent).where(
                CalendarEvent.user_id == context.user_id,
                CalendarEvent.organization_id == context.organization_id,
            )
            calendar = list(session.scalars(cal_stmt).all())

        return ItineraryResult(
            trips=tuple(
                TripSummary(
                    trip_id=str(t.id),
                    origin=t.origin,
                    primary_destination=t.primary_destination,
                    start_date=_date_to_str(t.start_date),  # type: ignore[arg-type]
                    end_date=_date_to_str(t.end_date),  # type: ignore[arg-type]
                    status=t.status.value if hasattr(t.status, "value") else str(t.status),
                    estimated_cost=(
                        float(t.estimated_cost)  # type: ignore[arg-type]
                        if t.estimated_cost is not None
                        else None
                    ),
                    budget_amount=(
                        float(t.budget_amount)  # type: ignore[arg-type]
                        if t.budget_amount is not None
                        else None
                    ),
                    currency=t.currency,
                )
                for t in trips
            ),
            segments=tuple(
                ItinerarySegment(
                    segment_id=str(s.id),
                    trip_id=str(s.trip_id),
                    segment_type=(
                        s.segment_type.value
                        if hasattr(s.segment_type, "value")
                        else str(s.segment_type)
                    ),
                    supplier=s.supplier,
                    location=s.location,
                    start_datetime=_dt_to_str(s.start_datetime),  # type: ignore[arg-type]
                    end_datetime=_dt_to_str(s.end_datetime),  # type: ignore[arg-type]
                    amount=(
                        float(s.amount)  # type: ignore[arg-type]
                        if s.amount is not None
                        else None
                    ),
                    currency=s.currency,
                    details=s.details or {},
                )
                for s in segments
            ),
            calendar_events=tuple(
                CalendarEntry(
                    event_id=str(e.id),
                    title=e.title,
                    location=e.location,
                    start_datetime=e.start_datetime.isoformat(),
                    end_datetime=_dt_to_str(e.end_datetime),
                )
                for e in calendar
            ),
        )
