"""Live-DB: one planning conversation -> one draft trip (UAT-2 Issue 4).

UAT: selecting a flight and then a hotel in the planner created TWO trips (each
approved on its own path).  The TravelAgent runs un-patched (real inventory, real
policy evaluation) inside a rolled-back session, except the concurrency test,
which commits and cleans up after itself.

Pinned:
  * flight then hotel -> ONE trip, two segments, cost = fare + nights x rate,
    purpose kept, conversation.context_ref_id -> the trip;
  * re-picking a leg of the same type replaces it; the same option twice is a no-op;
  * a trip that is no longer a draft (or was returned for changes) is never
    mutated — a NEW draft is started;
  * a different route or departure date is never silently rewritten — a NEW draft
    is started; a change of ONLY the return date updates the same draft in place
    (UAT2 defect E: flight kept, re-picked hotel re-priced, stale hotel flagged);
  * the pointer is only trusted for the caller's own planner trip of THIS
    conversation (never a teammate's trip / a hand-made draft / a payload value);
  * changing a trip needs trip.update.own;
  * two concurrent selections serialise on the conversation row (no 2nd trip).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.travel_agent import TravelAgent
from ai_agents.agents.travel_agent.intent import TripIntent
from ai_agents.agents.travel_agent.persistence import apply_trip_selection
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.context import AgentContext
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from config.constants.permissions import TRIP_CREATE_OWN, TRIP_UPDATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.conversation import Conversation, ConversationContextType
from database.models.conversation_message import ConversationMessage
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse
from services.llm_provider.local import LocalLlmProvider

TODAY = date(2026, 11, 11)

_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.deterministic,
    task_type="trip_planning",
    confidence=0.95,
    rationale="context_type=travel_planning",
)

_PLAN = {
    "origin": "ORD",
    "destination": "JFK",
    "departure_date": "2026-11-17",
    "return_date": "2026-11-20",
    "travelers": 1,
    "currency": "USD",
    "purpose": "Client meeting",
    "source": "chips",
}

_KEYS = frozenset({TRIP_CREATE_OWN, TRIP_UPDATE_OWN})


@pytest.fixture(scope="module")
def db_engine():
    try:
        from backend.app.core.database import get_engine
    except ImportError:
        pytest.skip("backend not importable — DB tests require the full stack")
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")
    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine) -> dict[str, Any]:
    from database.seeds.seed_all import seed

    seed()
    from backend.app.core.database import get_sessionmaker
    from database.models.identity import Organization, User

    with get_sessionmaker()() as s:
        org = s.scalar(sa.select(Organization).where(Organization.name == "Atlas Demo Corp"))
        emp = s.scalar(sa.select(User).where(User.email == "employee@atlas-demo.internal"))
        mgr = s.scalar(sa.select(User).where(User.email == "manager@atlas-demo.internal"))
    return {"org_id": org.id, "employee_id": emp.id, "manager_id": mgr.id}


@pytest.fixture()
def db_session(db_engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctx(seeded: dict, who: str = "employee_id", keys: frozenset[str] = _KEYS) -> AgentContext:
    return AgentContext(
        organization_id=seeded["org_id"],
        user_id=seeded[who],
        granted_permission_keys=keys,
        correlation_id="issue4-single-trip",
    )


def _conversation(
    session: Session, seeded: dict, who: str = "employee_id", **kw: Any
) -> Conversation:
    conv = Conversation(
        organization_id=seeded["org_id"],
        user_id=seeded[who],
        context_type=ConversationContextType.travel_planning,
        **kw,
    )
    session.add(conv)
    session.flush()
    return conv


class _Planner:
    """Drives the TravelAgent over one conversation, like the planner UI does."""

    def __init__(self, session: Session, ctx: AgentContext, conv: Conversation) -> None:
        self.session, self.ctx, self.conv = session, ctx, conv
        self.agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)

    def turn(self, **payload: Any) -> dict[str, Any]:
        body = {**_PLAN, "conversation_id": str(self.conv.id), **payload}
        result = self.agent.handle(
            Task(context_type="travel_planning", payload=body),
            _DECISION,
            context=self.ctx,
            session=self.session,
        )
        return dict(result.output)

    def options(self, **payload: Any) -> tuple[list[dict], list[dict]]:
        shortlist = self.turn(**payload)["shortlist"]
        flights = [o for o in shortlist if o["segment_type"] == "flight"]
        hotels = [o for o in shortlist if o["segment_type"] == "hotel"]
        assert flights and hotels, shortlist
        return flights, hotels

    def select(self, option_id: str, **payload: Any) -> dict[str, Any]:
        return self.turn(selected_option_ids=[option_id], question="Plan my trip", **payload)


def _segments(session: Session, trip_id: uuid.UUID) -> list[TripSegment]:
    return list(
        session.scalars(
            sa.select(TripSegment)
            .where(TripSegment.trip_id == trip_id)
            .order_by(TripSegment.segment_type)
        ).all()
    )


def _trips_of(session: Session, conv: Conversation) -> list[Trip]:
    return list(
        session.scalars(
            sa.select(Trip).where(
                Trip.recommendation_context["conversation_id"].astext == str(conv.id)
            )
        ).all()
    )


def _frozen(session: Session, trip: Trip) -> tuple:
    session.refresh(trip)
    segs = _segments(session, trip.id)
    return (
        trip.status,
        trip.version,
        trip.estimated_cost,
        trip.origin,
        trip.primary_destination,
        trip.start_date,
        trip.end_date,
        tuple((s.id, s.segment_type, s.amount, (s.details or {}).get("option_id")) for s in segs),
    )


# ---------------------------------------------------------------------------
# The UAT flow
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_flight_then_hotel_same_conversation_one_trip(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, hotels = p.options()
    flight = next(o for o in flights if o["in_policy"])
    hotel = next(o for o in hotels if o["in_policy"])

    out1 = p.select(flight["option_id"])
    assert out1["trip_change"]["action"] == "created", out1.get("trip_persist_error")
    out2 = p.select(hotel["option_id"])
    assert out2["trip_id"] == out1["trip_id"]
    assert out2["trip_change"]["action"] == "updated"
    assert out2["trip_change"]["segment_types_added"] == ["hotel"]
    assert out2["trip_notice"] is None
    assert "Added the" in out2["trip_message"] and "hotel" in out2["trip_message"]

    trips = _trips_of(db_session, conv)
    assert len(trips) == 1
    trip = trips[0]
    segs = _segments(db_session, trip.id)
    assert sorted(s.segment_type for s in segs) == [SegmentType.flight, SegmentType.hotel]
    hotel_seg = next(s for s in segs if s.segment_type == SegmentType.hotel)
    assert (hotel_seg.details or {})["nights"] == 3
    expected = (
        Decimal(str(flight["amount_usd"])) + Decimal(str(hotel["nightly_rate_usd"])) * 3
    ).quantize(Decimal("0.01"))
    assert trip.estimated_cost == expected
    assert sum(Decimal(str(s.amount)) for s in segs) == expected
    assert trip.purpose == "Client meeting"
    assert trip.status == TripStatus.draft
    assert trip.version == 2
    rec = trip.recommendation_context or {}
    assert set(rec["selected_option_ids"]) == {flight["option_id"], hotel["option_id"]}
    # The flight's snapshot survives even though it is not this turn's selection.
    assert {s["option_id"] for s in rec["selected_options"]} == set(rec["selected_option_ids"])
    db_session.refresh(conv)
    assert conv.context_ref_id == trip.id

    # The assistant reply of the selection turn says what happened.
    last = db_session.scalars(
        sa.select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.sequence.desc())
        .limit(1)
    ).one()
    assert last.content == out2["trip_message"]


@pytest.mark.db
def test_reselect_flight_replaces_segment_and_recomputes_cost(
    seeded: dict, db_session: Session
) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, hotels = p.options()
    first, second = flights[0], flights[1]
    p.select(first["option_id"])
    p.select(hotels[0]["option_id"])
    out = p.select(second["option_id"])
    assert out["trip_change"]["action"] == "updated"
    assert out["trip_change"]["segment_types_replaced"] == ["flight"]
    trip = db_session.get(Trip, uuid.UUID(out["trip_id"]))
    assert trip is not None
    segs = _segments(db_session, trip.id)
    flight_segs = [s for s in segs if s.segment_type == SegmentType.flight]
    assert [(s.details or {})["option_id"] for s in flight_segs] == [second["option_id"]]
    assert trip.estimated_cost == sum(Decimal(str(s.amount)) for s in segs)
    assert trip.version == 3
    assert len(_trips_of(db_session, conv)) == 1


@pytest.mark.db
def test_same_option_twice_is_unchanged(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, _ = p.options()
    out1 = p.select(flights[0]["option_id"])
    out2 = p.select(flights[0]["option_id"])
    assert out2["trip_change"]["action"] == "unchanged"
    assert out2["trip_id"] == out1["trip_id"]
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None and trip.version == 1
    assert len(_segments(db_session, trip.id)) == 1


# ---------------------------------------------------------------------------
# Never rewrite a draft into another trip; never touch a non-draft trip
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    "status",
    [
        TripStatus.pending_approval,
        TripStatus.approved,
        TripStatus.cancelled,
        TripStatus.rejected,
    ],
)
def test_non_draft_trip_never_mutated_new_draft_started(
    seeded: dict, db_session: Session, status: TripStatus
) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, hotels = p.options()
    out1 = p.select(flights[0]["option_id"])
    old = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert old is not None
    old.status = status
    db_session.flush()
    before = _frozen(db_session, old)

    out2 = p.select(hotels[0]["option_id"])
    assert out2["trip_change"]["action"] == "created"
    assert out2["trip_id"] != out1["trip_id"]
    prev = out2["trip_change"]["previous_trip"]
    assert prev["id"] == str(old.id) and prev["status"] == status.value
    assert out2["trip_notice"].startswith("Started a new draft trip ORD → JFK")
    assert "It is still in Trips." in out2["trip_notice"]
    assert _frozen(db_session, old) == before

    new = db_session.get(Trip, uuid.UUID(out2["trip_id"]))
    assert new is not None and new.status == TripStatus.draft
    assert (new.recommendation_context or {})["related_trip_id"] == str(old.id)
    db_session.refresh(conv)
    assert conv.context_ref_id == new.id


@pytest.mark.db
def test_returned_trip_never_mutated_new_draft_started(seeded: dict, db_session: Session) -> None:
    """Owner decision 3: a trip RETURNED for changes is not editable by the planner.

    A returned trip keeps ``status=draft``; the only sign is its approval in
    ``changes_requested``.  A new selection must start a NEW draft and say so.
    """
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, hotels = p.options()
    out1 = p.select(flights[0]["option_id"])
    old = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert old is not None and old.status == TripStatus.draft
    db_session.add(
        Approval(
            organization_id=seeded["org_id"],
            subject_type=ApprovalSubjectType.trip,
            subject_id=old.id,
            submitter_id=seeded["employee_id"],
            current_status=ApprovalStatus.changes_requested,
        )
    )
    db_session.flush()
    before = _frozen(db_session, old)

    out2 = p.select(hotels[0]["option_id"])
    assert out2["trip_change"]["action"] == "created"
    assert out2["trip_id"] != out1["trip_id"]
    prev = out2["trip_change"]["previous_trip"]
    assert prev["id"] == str(old.id)
    assert prev["reason"] == "not_draft" and prev["status"] == "returned"
    assert out2["trip_notice"].startswith("Started a new draft trip ORD → JFK")
    assert "returned for changes" in out2["trip_notice"]
    assert _frozen(db_session, old) == before
    db_session.refresh(old)
    assert old.status == TripStatus.draft  # untouched, still resubmittable from Trips

    new = db_session.get(Trip, uuid.UUID(out2["trip_id"]))
    assert new is not None and new.status == TripStatus.draft
    assert [s.segment_type for s in _segments(db_session, new.id)] == [SegmentType.hotel]
    db_session.refresh(conv)
    assert conv.context_ref_id == new.id


@pytest.mark.db
def test_route_change_starts_new_trip_old_draft_untouched(
    seeded: dict, db_session: Session
) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, hotels = p.options()
    out1 = p.select(flights[0]["option_id"])
    p.select(hotels[0]["option_id"])
    old = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert old is not None
    before = _frozen(db_session, old)

    sfo_flights, _ = p.options(origin="SFO")
    out3 = p.select(sfo_flights[0]["option_id"], origin="SFO")
    assert out3["trip_change"]["action"] == "created"
    assert out3["trip_change"]["previous_trip"]["reason"] == "different_trip"
    assert "your earlier draft ORD → JFK, 2026-11-17 – 2026-11-20" in out3["trip_notice"]
    assert _frozen(db_session, old) == before
    new = db_session.get(Trip, uuid.UUID(out3["trip_id"]))
    assert new is not None and new.origin == "SFO"
    assert len(_segments(db_session, new.id)) == 1


@pytest.mark.db
def test_departure_date_change_starts_new_trip(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, _ = p.options()
    out1 = p.select(flights[0]["option_id"])
    old = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert old is not None
    before = _frozen(db_session, old)
    _, hotels = p.options(departure_date="2026-11-18")
    out2 = p.select(hotels[0]["option_id"], departure_date="2026-11-18")
    assert out2["trip_change"]["action"] == "created"
    assert out2["trip_change"]["previous_trip"]["reason"] == "different_trip"
    assert out2["trip_id"] != out1["trip_id"]
    assert _frozen(db_session, old) == before


# ---------------------------------------------------------------------------
# UAT2 defect E: only the RETURN date changed -> the same draft, in place
# ---------------------------------------------------------------------------


def _flight_and_hotel(p: _Planner) -> tuple[dict, dict, dict]:
    flights, hotels = p.options()
    flight = next(o for o in flights if o["in_policy"])
    hotel = next(o for o in hotels if o["in_policy"])
    out1 = p.select(flight["option_id"])
    assert out1["trip_change"]["action"] == "created", out1.get("trip_persist_error")
    out2 = p.select(hotel["option_id"])
    assert out2["trip_id"] == out1["trip_id"]
    return flight, hotel, out1


@pytest.mark.db
def test_return_date_only_change_updates_draft_in_place(seeded: dict, db_session: Session) -> None:
    """Browser UAT: flight + Hilton, "come back Friday instead", re-pick the Hilton.

    Before: a SECOND draft trip holding only the hotel (the flight was lost).
    Now: the same draft gets the new end date, keeps its flight leg, and the
    re-selected hotel replaces the hotel leg with the nights of the new stay.
    """
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flight, hotel, out1 = _flight_and_hotel(p)
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None
    flight_seg_before = next(
        (s.id, s.amount, (s.details or {}).get("option_id"))
        for s in _segments(db_session, trip.id)
        if s.segment_type == SegmentType.flight
    )
    version_before = trip.version

    _, hotels2 = p.options(return_date="2026-11-21")
    same_hotel = next(o for o in hotels2 if o["supplier"] == hotel["supplier"])
    out3 = p.select(same_hotel["option_id"], return_date="2026-11-21")

    assert out3["trip_id"] == out1["trip_id"], out3.get("trip_notice")
    change = out3["trip_change"]
    assert change["action"] == "updated"
    assert change["previous_trip"] is None
    assert change["end_date_change"] == {"from": "2026-11-20", "to": "2026-11-21"}
    assert change["segment_types_replaced"] == ["hotel"]
    assert change["stale_hotel"] is False
    assert out3["trip_notice"] is None
    assert len(_trips_of(db_session, conv)) == 1

    db_session.refresh(trip)
    assert trip.status == TripStatus.draft
    assert trip.start_date == date(2026, 11, 17)
    assert trip.end_date == date(2026, 11, 21)
    assert trip.version == version_before + 1
    segs = _segments(db_session, trip.id)
    assert sorted(s.segment_type for s in segs) == [SegmentType.flight, SegmentType.hotel]
    flight_seg = next(s for s in segs if s.segment_type == SegmentType.flight)
    assert (flight_seg.id, flight_seg.amount, (flight_seg.details or {})["option_id"]) == (
        flight_seg_before
    )
    hotel_seg = next(s for s in segs if s.segment_type == SegmentType.hotel)
    details = hotel_seg.details or {}
    assert details["nights"] == 4
    assert details["check_in_date"] == "2026-11-17"
    assert details["check_out_date"] == "2026-11-21"
    rate = Decimal(str(same_hotel["nightly_rate_usd"]))
    assert hotel_seg.amount == (rate * 4).quantize(Decimal("0.01"))
    expected = (Decimal(str(flight["amount_usd"])) + rate * 4).quantize(Decimal("0.01"))
    assert trip.estimated_cost == expected

    msg = out3["trip_message"]
    assert "Updated the return date of your draft trip" in msg
    assert "from 2026-11-20 to 2026-11-21" in msg
    assert "4 nights" in msg
    assert "Started a new draft trip" not in msg


@pytest.mark.db
def test_return_date_change_without_hotel_reselect_flags_stale_hotel(
    seeded: dict, db_session: Session
) -> None:
    """Re-picking only a flight after the return date moved: the end date moves,
    the hotel leg is NOT silently kept as if it covered the new stay — the reply
    says it still covers the old dates (it is not re-priced behind the user's back).
    """
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    flight, hotel, out1 = _flight_and_hotel(p)
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None
    hotel_before = next(
        (s.id, s.amount, (s.details or {}).get("nights"))
        for s in _segments(db_session, trip.id)
        if s.segment_type == SegmentType.hotel
    )

    p.options(return_date="2026-11-21")
    # The SAME flight again: no leg changes, but the trip's end date still moves.
    out3 = p.select(flight["option_id"], return_date="2026-11-21")
    assert out3["trip_id"] == out1["trip_id"]
    change = out3["trip_change"]
    assert change["action"] == "updated"
    assert change["end_date_change"] == {"from": "2026-11-20", "to": "2026-11-21"}
    assert change["segment_types_added"] == [] and change["segment_types_replaced"] == []
    assert change["stale_hotel"] is True
    db_session.refresh(trip)
    assert trip.end_date == date(2026, 11, 21)
    hotel_seg = next(
        s for s in _segments(db_session, trip.id) if s.segment_type == SegmentType.hotel
    )
    assert (hotel_seg.id, hotel_seg.amount, (hotel_seg.details or {}).get("nights")) == (
        hotel_before
    )
    assert trip.estimated_cost == sum(
        Decimal(str(s.amount)) for s in _segments(db_session, trip.id)
    )
    msg = out3["trip_message"]
    assert "Updated the return date of your draft trip" in msg
    assert "still covers 2026-11-17 – 2026-11-20 (3 nights)" in msg
    assert "select a hotel" in msg
    assert len(_trips_of(db_session, conv)) == 1


@pytest.mark.db
def test_return_date_change_on_returned_draft_still_starts_new_trip(
    seeded: dict, db_session: Session
) -> None:
    """A draft RETURNED for changes is never edited, even for a return-date change."""
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    _, _, out1 = _flight_and_hotel(p)
    old = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert old is not None
    db_session.add(
        Approval(
            organization_id=seeded["org_id"],
            subject_type=ApprovalSubjectType.trip,
            subject_id=old.id,
            submitter_id=seeded["employee_id"],
            current_status=ApprovalStatus.changes_requested,
        )
    )
    db_session.flush()
    before = _frozen(db_session, old)
    _, hotels2 = p.options(return_date="2026-11-21")
    out3 = p.select(hotels2[0]["option_id"], return_date="2026-11-21")
    assert out3["trip_change"]["action"] == "created"
    assert out3["trip_change"]["previous_trip"]["status"] == "returned"
    assert out3["trip_change"]["end_date_change"] is None
    assert _frozen(db_session, old) == before


@pytest.mark.db
def test_return_date_change_needs_trip_update_permission(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    ctx = _ctx(seeded, keys=frozenset({TRIP_CREATE_OWN}))
    p = _Planner(db_session, ctx, conv)
    flights, _ = p.options()
    out1 = p.select(flights[0]["option_id"])
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None
    before = _frozen(db_session, trip)
    p.options(return_date="2026-11-21")
    out2 = p.select(flights[0]["option_id"], return_date="2026-11-21")
    assert out2["trip_id"] is None
    assert TRIP_UPDATE_OWN in out2["trip_persist_error"]
    assert _frozen(db_session, trip) == before


# ---------------------------------------------------------------------------
# UAT2 defect K: "Actually I'll come back a day later" -> return +1, same draft
# ---------------------------------------------------------------------------


class _WrongDateLlm(LlmProvider):
    """A real-provider stand-in answering "a day later" with a same-day return."""

    model = "google/gemini-2.5-flash"

    def complete(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> LlmResponse:
        reply = {"return_date": "2026-11-18", "trip_length_days": 1}
        return LlmResponse(text=json.dumps(reply), model=self.model)

    def stream(
        self, messages: list[LlmMessage], *, tools: Any = None, tool_choice: Any = None
    ) -> Iterator[LlmChunk]:
        yield from []


@pytest.mark.db
@pytest.mark.parametrize("llm", ["local", "wrong-date-llm"])
def test_a_day_later_moves_return_and_updates_draft_in_place(
    seeded: dict, db_session: Session, llm: str
) -> None:
    """Browser UAT: ORD->JFK 2026-11-18 -> 2026-11-19, flight + hotel on a draft,
    then "Actually I'll come back a day later".

    Before: the return became 2026-11-18 (a 0-night stay), only flights were
    re-searched and the trip was not updated.  Now: the return is 2026-11-20,
    hotels are searched for the 2-night stay, and re-picking the hotel moves the
    SAME draft's end date in place.
    """
    dates = {"departure_date": "2026-11-18", "return_date": "2026-11-19"}
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    if llm != "local":
        p.agent = TravelAgent(provider=_WrongDateLlm(), today=lambda: TODAY)

    flights, hotels = p.options(**dates)
    flight = next(o for o in flights if o["in_policy"])
    hotel = next(o for o in hotels if o["in_policy"])
    out1 = p.select(flight["option_id"], **dates)
    assert out1["trip_change"]["action"] == "created", out1.get("trip_persist_error")
    out2 = p.select(hotel["option_id"], **dates)
    assert out2["trip_id"] == out1["trip_id"]
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None and trip.end_date == date(2026, 11, 19)

    # The follow-up message (not a chip edit): the chips still carry the old dates.
    out = p.turn(question="Actually I'll come back a day later", source=None, **dates)
    assert out["clarify"] is False
    assert out["intent"]["departure_date"] == "2026-11-18"
    assert out["intent"]["return_date"] == "2026-11-20"
    hotels2 = [o for o in out["shortlist"] if o["segment_type"] == "hotel"]
    assert hotels2, "hotels must be re-searched for the longer stay"

    # The planner applies the server intent to the chips, then the hotel is re-picked.
    new_dates = {"departure_date": "2026-11-18", "return_date": out["intent"]["return_date"]}
    same_hotel = next(o for o in hotels2 if o["supplier"] == hotel["supplier"])
    out3 = p.select(same_hotel["option_id"], **new_dates)

    assert out3["trip_id"] == out1["trip_id"], out3.get("trip_notice")
    change = out3["trip_change"]
    assert change["action"] == "updated"
    assert change["end_date_change"] == {"from": "2026-11-19", "to": "2026-11-20"}
    assert change["segment_types_replaced"] == ["hotel"]
    assert len(_trips_of(db_session, conv)) == 1
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft
    assert (trip.start_date, trip.end_date) == (date(2026, 11, 18), date(2026, 11, 20))
    segs = _segments(db_session, trip.id)
    assert sorted(s.segment_type for s in segs) == [SegmentType.flight, SegmentType.hotel]
    hotel_seg = next(s for s in segs if s.segment_type == SegmentType.hotel)
    assert (hotel_seg.details or {})["nights"] == 2
    assert (hotel_seg.details or {})["check_out_date"] == "2026-11-20"


# ---------------------------------------------------------------------------
# Which trip the pointer may name
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_context_ref_to_foreign_trip_never_mutated(seeded: dict, db_session: Session) -> None:
    """A manager's assistant conversation opened on an employee's draft trip."""
    emp_conv = _conversation(db_session, seeded)
    emp = _Planner(db_session, _ctx(seeded), emp_conv)
    flights, hotels = emp.options()
    out = emp.select(flights[0]["option_id"])
    emp_trip = db_session.get(Trip, uuid.UUID(out["trip_id"]))
    assert emp_trip is not None
    before = _frozen(db_session, emp_trip)

    mgr_conv = _conversation(db_session, seeded, "manager_id", context_ref_id=emp_trip.id)
    mgr = _Planner(db_session, _ctx(seeded, "manager_id"), mgr_conv)
    mgr.options()
    out1 = mgr.select(hotels[0]["option_id"])
    assert out1["trip_change"]["action"] == "created"
    mgr_trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert mgr_trip is not None and mgr_trip.traveler_id == seeded["manager_id"]
    assert _frozen(db_session, emp_trip) == before
    db_session.refresh(mgr_conv)
    assert mgr_conv.context_ref_id == emp_trip.id  # entity scope is never rewritten

    # A later selection in that conversation joins the manager's OWN planner trip.
    out2 = mgr.select(flights[0]["option_id"])
    assert out2["trip_id"] == out1["trip_id"]
    assert out2["trip_change"]["action"] == "updated"
    assert _frozen(db_session, emp_trip) == before


@pytest.mark.db
def test_ref_to_handmade_owned_draft_not_appended(seeded: dict, db_session: Session) -> None:
    handmade = Trip(
        organization_id=seeded["org_id"],
        traveler_id=seeded["employee_id"],
        origin="ORD",
        primary_destination="JFK",
        start_date=date(2026, 11, 17),
        end_date=date(2026, 11, 20),
        currency="USD",
        status=TripStatus.draft,
        created_by=seeded["employee_id"],
        updated_by=seeded["employee_id"],
    )
    db_session.add(handmade)
    db_session.flush()
    conv = _conversation(db_session, seeded, context_ref_id=handmade.id)
    p = _Planner(db_session, _ctx(seeded), conv)
    flights, _ = p.options()
    out = p.select(flights[0]["option_id"])
    assert out["trip_id"] != str(handmade.id)
    assert _segments(db_session, handmade.id) == []
    db_session.refresh(conv)
    assert conv.context_ref_id == handmade.id


@pytest.mark.db
def test_payload_context_ref_id_is_ignored(seeded: dict, db_session: Session) -> None:
    other_conv = _conversation(db_session, seeded)
    other = _Planner(db_session, _ctx(seeded), other_conv)
    flights, hotels = other.options()
    out = other.select(flights[0]["option_id"])
    other_trip = db_session.get(Trip, uuid.UUID(out["trip_id"]))
    assert other_trip is not None
    before = _frozen(db_session, other_trip)

    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded), conv)
    p.options()
    out2 = p.select(hotels[0]["option_id"], context_ref_id=str(other_trip.id))
    assert out2["trip_id"] != str(other_trip.id)
    assert out2["trip_change"]["action"] == "created"
    assert _frozen(db_session, other_trip) == before


@pytest.mark.db
def test_update_requires_trip_update_permission(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    p = _Planner(db_session, _ctx(seeded, keys=frozenset({TRIP_CREATE_OWN})), conv)
    flights, hotels = p.options()
    out1 = p.select(flights[0]["option_id"])
    assert out1["trip_change"]["action"] == "created"
    out2 = p.select(hotels[0]["option_id"])
    assert out2["trip_id"] is None
    assert TRIP_UPDATE_OWN in out2["trip_persist_error"]
    trip = db_session.get(Trip, uuid.UUID(out1["trip_id"]))
    assert trip is not None and len(_segments(db_session, trip.id)) == 1


# ---------------------------------------------------------------------------
# Concurrency: a double-click must not create two trips
# ---------------------------------------------------------------------------


def _ranked(option_id: str, seg: str, amount: float) -> RankedOption:
    base = RankedOption(
        option_id=option_id,
        segment_type=seg,
        supplier="Delta Air Lines" if seg == "flight" else "Hilton",
        amount_usd=amount,
        sustainability_score=3,
        cabin_class="economy" if seg == "flight" else None,
        duration_hours=2.5 if seg == "flight" else None,
        is_direct=True if seg == "flight" else None,
        city=None if seg == "flight" else "New York",
        nightly_rate_usd=None if seg == "flight" else amount,
        room_type=None if seg == "flight" else "standard",
        in_policy=True,
        policy_reason="",
        clause_refs=(),
        requires_approver=None,
        rule_in_policy=True,
        rank_score=1.0,
        explanation="ok",
    )
    return replace(base)


@pytest.mark.db
def test_concurrent_selection_serializes_on_conversation_lock(seeded: dict, db_engine) -> None:
    from backend.app.core.database import get_sessionmaker

    factory = get_sessionmaker()
    ctx = _ctx(seeded)
    with factory() as s0:
        conv = _conversation(s0, seeded)
        conv_id = conv.id
        s0.commit()
    intent = TripIntent(
        origin="ORD",
        destination="JFK",
        departure_date="2026-11-17",
        return_date="2026-11-20",
        travelers=1,
        currency="USD",
        conversation_id=str(conv_id),
    )
    flight, hotel = _ranked("CC-F1", "flight", 147.0), _ranked("CC-H1", "hotel", 218.0)
    outcome: dict[str, Any] = {}

    def second() -> None:
        with factory() as sb:
            res = apply_trip_selection(
                intent=intent,
                shortlist=[flight, hotel],
                selected_ids=[hotel.option_id],
                reflection_outcome_value=None,
                context=ctx,
                session=sb,
            )
            outcome["action"], outcome["trip_id"] = res.action, res.trip.id
            sb.commit()

    sa_ = factory()
    try:
        first = apply_trip_selection(
            intent=intent,
            shortlist=[flight, hotel],
            selected_ids=[flight.option_id],
            reflection_outcome_value=None,
            context=ctx,
            session=sa_,
        )
        worker = threading.Thread(target=second)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive(), "second selection did not wait for the conversation lock"
        sa_.commit()
        worker.join(timeout=20)
        assert not worker.is_alive()
        assert outcome["action"] == "updated"
        assert outcome["trip_id"] == first.trip.id
        with factory() as check:
            trips = check.scalars(
                sa.select(Trip).where(
                    Trip.recommendation_context["conversation_id"].astext == str(conv_id)
                )
            ).all()
            assert len(trips) == 1
            assert len(_segments(check, trips[0].id)) == 2
    finally:
        sa_.close()
        with factory() as cleanup:
            ids = cleanup.scalars(
                sa.select(Trip.id).where(
                    Trip.recommendation_context["conversation_id"].astext == str(conv_id)
                )
            ).all()
            if ids:
                cleanup.execute(sa.delete(TripSegment).where(TripSegment.trip_id.in_(ids)))
                cleanup.execute(sa.delete(Trip).where(Trip.id.in_(ids)))
            cleanup.execute(sa.delete(Conversation).where(Conversation.id == conv_id))
            cleanup.commit()


@pytest.mark.db
def test_missing_trip_update_key_raises_on_update_path(seeded: dict, db_session: Session) -> None:
    conv = _conversation(db_session, seeded)
    ctx = _ctx(seeded, keys=frozenset({TRIP_CREATE_OWN}))
    intent = TripIntent(
        origin="ORD",
        destination="JFK",
        departure_date="2026-11-17",
        return_date="2026-11-20",
        travelers=1,
        currency="USD",
        conversation_id=str(conv.id),
    )
    flight, hotel = _ranked("PF-F1", "flight", 147.0), _ranked("PF-H1", "hotel", 218.0)
    kw: dict[str, Any] = {
        "intent": intent,
        "shortlist": [flight, hotel],
        "reflection_outcome_value": None,
        "context": ctx,
        "session": db_session,
    }
    created = apply_trip_selection(selected_ids=[flight.option_id], **kw)
    assert created.action == "created"
    # Re-picking the same option changes nothing, so needs no update right.
    assert apply_trip_selection(selected_ids=[flight.option_id], **kw).action == "unchanged"
    with pytest.raises(GuardrailBlocked):
        apply_trip_selection(selected_ids=[hotel.option_id], **kw)
