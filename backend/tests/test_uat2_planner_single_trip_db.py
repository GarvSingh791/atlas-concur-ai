"""UAT-2 Issue 4 — planner flight + hotel selections make ONE trip (API level).

UAT: in one planning conversation the user selected a flight, then a hotel — two
draft trips were created, each with its own ``trip.created`` audit row, and each
was approved on its own path (the flight alone under the auto-approve line).

Pinned through POST /conversations/{id}/messages (real TravelAgent, deterministic
provider, shared rolled-back session):
  * flight then hotel -> ONE trip; audit ``trip.created`` then ``trip.updated``
    (with before/after segments); re-picking the same option writes no audit row;
    the internal audit snapshots are never streamed to the client;
  * the conversation points at its trip (GET /conversations/{id}.context_ref_id);
  * UAT2 defect E: changing ONLY the return date updates that same draft in place
    (audited ``trip.updated`` with ``end_date_change``) instead of a second trip;
  * after the trip is submitted a new selection starts a NEW draft trip (audited
    with previous_trip_id) and the submitted trip is untouched;
  * O1: a trip overlapping a live trip of the same traveler to the same
    destination gets a BLOCKING ``possible_split_trip`` flag -> escalated, never
    auto-approved ("JFK" and "New York" are the same destination).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.app.modules.ai.runtime import get_orchestrator
from database.models.approval import Approval, ApprovalStatus
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

pytestmark = pytest.mark.db

API = "/api/v1"
START = date.today() + timedelta(days=21)
END = START + timedelta(days=3)


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _travel_orchestrator() -> Any:
    from ai_agents.agents.travel_agent import register_travel
    from ai_agents.orchestrator.orchestrator import Orchestrator
    from ai_agents.orchestrator.registry import AgentRegistry
    from services.llm_provider.local import LocalLlmProvider

    registry = AgentRegistry()
    register_travel(registry, provider=LocalLlmProvider())
    return Orchestrator(registry)


@pytest.fixture()
def planner_app(app: Any) -> Any:
    orch = _travel_orchestrator()
    app.dependency_overrides[get_orchestrator] = lambda: orch
    yield app
    app.dependency_overrides.pop(get_orchestrator, None)


def _frames(body: str) -> dict[str, list[dict[str, Any]]]:
    frames: dict[str, list[dict[str, Any]]] = {}
    for block in body.strip().split("\n\n"):
        event: str | None = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None and data is not None:
            frames.setdefault(event, []).append(data)
    return frames


class _Planner:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        r = client.post(f"{API}/conversations", json={"context_type": "travel_planning"})
        assert r.status_code == 201, r.text
        self.conv_id = r.json()["id"]

    def send(self, content: str, **payload: Any) -> dict[str, Any]:
        body = {
            "content": content,
            "payload": {
                "origin": "ORD",
                "destination": "JFK",
                "departure_date": START.isoformat(),
                "return_date": END.isoformat(),
                "travelers": 1,
                "currency": "USD",
                "purpose": "Client meeting",
                "source": "chips",
                **payload,
            },
        }
        r = self.client.post(f"{API}/conversations/{self.conv_id}/messages", json=body)
        assert r.status_code == 200, r.text
        frames = _frames(r.text)
        return frames["result"][0]["output"]

    def options(self) -> tuple[list[dict], list[dict]]:
        out = self.send("Update my trip plan")
        flights = [o for o in out["shortlist"] if o["segment_type"] == "flight"]
        hotels = [o for o in out["shortlist"] if o["segment_type"] == "hotel"]
        assert flights and hotels, out
        return flights, hotels

    def select(self, option_id: str) -> dict[str, Any]:
        return self.send("Plan my trip", selected_option_ids=[option_id])


def _audits(db: Session, trip_id: Any) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog)
            .where(AuditLog.entity_id == uuid.UUID(str(trip_id)))
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
    )


def _approval_for(db: Session, subject_id: Any) -> Approval:
    approval = db.scalar(
        sa.select(Approval).where(Approval.subject_id == uuid.UUID(str(subject_id)))
    )
    assert approval is not None
    return approval


def test_two_selections_one_trip_audit_created_then_updated(
    planner_app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(planner_app, employee)
    p = _Planner(emp)
    flights, hotels = p.options()

    out1 = p.select(flights[0]["option_id"])
    assert out1["trip_change"]["action"] == "created", out1.get("trip_persist_error")
    assert "audit" not in out1["trip_change"]  # internal snapshots never streamed
    out2 = p.select(hotels[0]["option_id"])
    assert out2["trip_id"] == out1["trip_id"]
    assert out2["trip_change"]["action"] == "updated"
    assert "audit" not in out2["trip_change"]
    out3 = p.select(hotels[0]["option_id"])
    assert out3["trip_change"]["action"] == "unchanged"

    trips = db_session.scalars(sa.select(Trip).where(Trip.traveler_id == employee.id)).all()
    assert [str(t.id) for t in trips] == [out1["trip_id"]]
    segs = db_session.scalars(
        sa.select(TripSegment).where(TripSegment.trip_id == trips[0].id)
    ).all()
    assert sorted(s.segment_type for s in segs) == [SegmentType.flight, SegmentType.hotel]

    # One transaction: created_at ties, so compare as a multiset.
    audits = {a.action: a for a in _audits(db_session, out1["trip_id"])}
    assert sorted(a.action for a in _audits(db_session, out1["trip_id"])) == [
        "trip.created",
        "trip.updated",
    ]
    created, updated = audits["trip.created"], audits["trip.updated"]
    assert created.after_state is not None
    assert created.after_state["source"] == "planner"
    assert created.after_state["conversation_id"] == p.conv_id
    assert updated.before_state is not None and updated.after_state is not None
    assert len(updated.before_state["segments"]) == 1
    assert len(updated.after_state["segments"]) == 2
    assert updated.after_state["segments_added"] == ["hotel"]

    conv = emp.get(f"{API}/conversations/{p.conv_id}").json()
    assert conv["context_ref_id"] == out1["trip_id"]


def test_return_date_only_change_updates_same_draft_audited(
    planner_app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """UAT2 defect E (browser): flight + Hilton, then "Actually I'll come back Friday
    instead", re-pick the Hilton -> the SAME draft (flight kept, hotel re-priced for
    the new stay), audited ``trip.updated`` with the end-date change; the owner can
    still discard the draft (draft -> cancelled)."""
    employee = users_a["employee"]
    emp = _client(planner_app, employee)
    p = _Planner(emp)
    flights, hotels = p.options()
    out1 = p.select(flights[0]["option_id"])
    hotel = hotels[0]
    out2 = p.select(hotel["option_id"])
    assert out2["trip_id"] == out1["trip_id"]

    new_end = END + timedelta(days=1)
    moved = p.send("Actually I'll come back a day later", return_date=new_end.isoformat())
    same_hotel = next(o for o in moved["shortlist"] if o["supplier"] == hotel["supplier"])
    out3 = p.send(
        "Plan my trip",
        return_date=new_end.isoformat(),
        selected_option_ids=[same_hotel["option_id"]],
    )
    assert out3["trip_id"] == out1["trip_id"], out3.get("trip_notice")
    assert out3["trip_change"]["action"] == "updated"
    assert out3["trip_change"]["end_date_change"] == {
        "from": END.isoformat(),
        "to": new_end.isoformat(),
    }
    assert "audit" not in out3["trip_change"]
    assert out3["trip_notice"] is None
    assert "Updated the return date of your draft trip" in out3["trip_message"]

    trips = db_session.scalars(sa.select(Trip).where(Trip.traveler_id == employee.id)).all()
    assert [str(t.id) for t in trips] == [out1["trip_id"]]
    trip = trips[0]
    db_session.refresh(trip)
    assert trip.end_date == new_end and trip.start_date == START
    segs = db_session.scalars(sa.select(TripSegment).where(TripSegment.trip_id == trip.id)).all()
    assert sorted(s.segment_type for s in segs) == [SegmentType.flight, SegmentType.hotel]
    nights = (new_end - START).days
    hotel_seg = next(s for s in segs if s.segment_type == SegmentType.hotel)
    assert (hotel_seg.details or {})["nights"] == nights
    assert trip.estimated_cost == sum(Decimal(str(s.amount)) for s in segs)

    updates = [a for a in _audits(db_session, trip.id) if a.action == "trip.updated"]
    assert len(updates) == 2
    last = next(a for a in updates if (a.after_state or {}).get("end_date_change"))
    assert last.before_state is not None and last.after_state is not None
    assert last.before_state["end_date"] == END.isoformat()
    assert last.after_state["end_date"] == new_end.isoformat()
    assert last.after_state["end_date_change"] == {
        "from": END.isoformat(),
        "to": new_end.isoformat(),
    }
    assert last.after_state["segments_replaced"] == ["hotel"]

    # The owner can discard a never-submitted draft.
    r = emp.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


def test_selection_after_submit_creates_new_trip_and_split_is_escalated(
    planner_app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(planner_app, employee)
    p = _Planner(emp)
    flights, hotels = p.options()
    first = p.select(flights[0]["option_id"])
    r = emp.patch(f"{API}/trips/{first['trip_id']}", json={"status": "pending_approval"})
    assert r.status_code == 200, r.text
    first_trip = db_session.get(Trip, uuid.UUID(first["trip_id"]))
    assert first_trip is not None
    db_session.refresh(first_trip)
    first_status, first_version = first_trip.status, first_trip.version

    second = p.select(hotels[0]["option_id"])
    assert second["trip_change"]["action"] == "created"
    assert second["trip_id"] != first["trip_id"]
    assert second["trip_change"]["previous_trip"]["id"] == first["trip_id"]
    assert second["trip_notice"].startswith("Started a new draft trip")
    db_session.refresh(first_trip)
    assert (first_trip.status, first_trip.version) == (first_status, first_version)
    created = _audits(db_session, second["trip_id"])
    assert [a.action for a in created] == ["trip.created"]
    assert (created[0].after_state or {})["previous_trip_id"] == first["trip_id"]

    # O1: the hotel-only trip overlaps the live flight trip -> never auto-approved.
    r2 = emp.patch(f"{API}/trips/{second['trip_id']}", json={"status": "pending_approval"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "pending_approval"
    approval = _approval_for(db_session, second["trip_id"])
    assert approval.is_auto_approved is False
    flags = (approval.risk_summary or {}).get("flags") or []
    split = [fl for fl in flags if fl.get("flag_type") == "possible_split_trip"]
    assert split and split[0]["severity"] == "blocking"
    assert split[0]["related_subject_id"] == first["trip_id"]


def _cheap_trip(db: Session, traveler: User, destination: str, start: date) -> Trip:
    trip = f.make_trip(
        db,
        traveler=traveler,
        primary_destination=destination,
        start_date=start,
        end_date=start + timedelta(days=2),
        estimated_cost=Decimal("150.00"),
    )
    f.make_trip_segment(
        db,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="Delta",
        amount=Decimal("150.00"),
        currency="USD",
        in_policy=True,
    )
    db.flush()
    return trip


def test_overlapping_live_trip_same_destination_is_escalated(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(app, employee)

    flight_only = _cheap_trip(db_session, employee, "JFK", START)
    ok = emp.patch(f"{API}/trips/{flight_only.id}", json={"status": "pending_approval"})
    assert ok.json()["status"] == "approved", ok.text  # control: auto-approves alone

    # Same journey, other half, destination typed as the city -> escalated.
    hotel_only = _cheap_trip(db_session, employee, "New York", START + timedelta(days=1))
    r = emp.patch(f"{API}/trips/{hotel_only.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "pending_approval", r.text
    approval = _approval_for(db_session, hotel_only.id)
    assert approval.current_status == ApprovalStatus.pending
    flags = (approval.risk_summary or {}).get("flags") or []
    assert any(
        fl.get("flag_type") == "possible_split_trip"
        and fl.get("related_subject_id") == str(flight_only.id)
        for fl in flags
    ), flags

    # Different destination, or no date overlap: unaffected.
    other_city = _cheap_trip(db_session, employee, "Boston", START)
    assert (
        emp.patch(f"{API}/trips/{other_city.id}", json={"status": "pending_approval"}).json()[
            "status"
        ]
        == "approved"
    )
    later = _cheap_trip(db_session, employee, "JFK", START + timedelta(days=30))
    assert (
        emp.patch(f"{API}/trips/{later.id}", json={"status": "pending_approval"}).json()["status"]
        == "approved"
    )


def test_cancelled_or_draft_overlap_does_not_flag(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(app, employee)
    _cheap_trip(db_session, employee, "Denver", START)  # a draft — not a claim yet
    cancelled = _cheap_trip(db_session, employee, "Denver", START)
    cancelled.status = TripStatus.cancelled
    db_session.flush()
    trip = _cheap_trip(db_session, employee, "Denver", START)
    r = emp.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "approved", r.text


def test_completed_first_half_still_flags_split(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Review bypass: approve half 1, complete it, submit half 2.

    A completed trip is an approved claim that was spent — it stays "live" for
    the split check, so the overlapping second half is still escalated.
    (Product gap 8: completing EARLY by PATCH is now refused — TRIP_NOT_ENDED —
    so half 1 is moved to completed directly, as the elapsed-trip sweep would.)
    """
    employee = users_a["employee"]
    emp = _client(app, employee)

    flight_only = _cheap_trip(db_session, employee, "JFK", START)
    ok = emp.patch(f"{API}/trips/{flight_only.id}", json={"status": "pending_approval"})
    assert ok.json()["status"] == "approved", ok.text
    early = emp.patch(f"{API}/trips/{flight_only.id}", json={"status": "completed"})
    assert early.status_code == 409 and early.json()["error"]["code"] == "TRIP_NOT_ENDED"
    db_session.refresh(flight_only)
    flight_only.status = TripStatus.completed
    db_session.flush()

    hotel_only = _cheap_trip(db_session, employee, "JFK", START)
    r = emp.patch(f"{API}/trips/{hotel_only.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "pending_approval", r.text
    approval = _approval_for(db_session, hotel_only.id)
    assert approval.is_auto_approved is False
    flags = (approval.risk_summary or {}).get("flags") or []
    split = [fl for fl in flags if fl.get("flag_type") == "possible_split_trip"]
    assert split and split[0]["severity"] == "blocking", flags
    assert split[0]["related_subject_id"] == str(flight_only.id)
    assert "completed" in split[0]["reason"]
