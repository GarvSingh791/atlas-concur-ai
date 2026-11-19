"""UAT-2 Issue 4 — planner selections (hermetic: no DB, no network).

One planning conversation -> one draft trip.  The DB behaviour (append / replace /
new trip when not a draft or for another route) is pinned in
test_travel_selection_upsert_db.py; these tests pin the pure pieces: one option per
segment type, the reply wording per outcome, and an unknown option id never
creating an empty trip.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.agents.travel_agent.agent import TravelAgent, _trip_change, _trip_messages
from ai_agents.agents.travel_agent.persistence import (
    TripSelectionResult,
    _select_options,
    trip_label,
)
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from config.constants.permissions import TRIP_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.trip import Trip, TripStatus
from services.llm_provider.local import LocalLlmProvider


def _opt(option_id: str, seg: str = "flight", amount: float = 147.0, **kw: Any) -> RankedOption:
    base = RankedOption(
        option_id=option_id,
        segment_type=seg,
        supplier=kw.pop("supplier", "Delta Air Lines" if seg == "flight" else "Hilton"),
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
    return replace(base, **kw) if kw else base


def _trip(cost: str | None = "801.00", **kw: Any) -> Trip:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "origin": "ORD",
        "primary_destination": "JFK",
        "start_date": date(2026, 11, 17),
        "end_date": date(2026, 11, 20),
        "purpose": "Client meeting",
        "estimated_cost": Decimal(cost) if cost else None,
        "currency": "USD",
        "status": TripStatus.draft,
    }
    fields.update(kw)
    return Trip(**fields)


_HOTEL_ROWS = ({"segment_type": "hotel", "nights": 3},)


class TestSelectOptions:
    def test_one_option_per_segment_type_highest_ranked_wins(self) -> None:
        shortlist = [_opt("F1"), _opt("F2"), _opt("H1", "hotel", 218.0)]
        picked = _select_options(shortlist, ["F2", "F1", "H1"])
        assert [o.option_id for o in picked] == ["F1", "H1"]

    def test_unknown_ids_select_nothing(self) -> None:
        assert _select_options([_opt("F1")], ["nope"]) == []


class TestTripLabel:
    def test_route_dates_and_purpose(self) -> None:
        assert (
            trip_label("ORD", "JFK", date(2026, 11, 17), date(2026, 11, 20), "Client meeting")
            == "ORD → JFK, 2026-11-17 – 2026-11-20 (Client meeting)"
        )

    def test_one_day_trip_shows_one_date(self) -> None:
        assert trip_label("ORD", "JFK", "2026-11-17", "2026-11-17") == "ORD → JFK, 2026-11-17"


class TestTripMessages:
    def test_created(self) -> None:
        res = TripSelectionResult(trip=_trip("147.00"), action="created", added=("flight",))
        msg, notice = _trip_messages(res, [_opt("F1")])
        assert notice is None
        assert msg == (
            "Saved the Delta Air Lines flight ($147.00) to a new draft trip "
            "ORD → JFK, 2026-11-17 – 2026-11-20. Estimated cost $147.00."
        )

    def test_hotel_added_to_draft(self) -> None:
        res = TripSelectionResult(
            trip=_trip("801.00"), action="updated", added=("hotel",), segments=_HOTEL_ROWS
        )
        msg, notice = _trip_messages(res, [_opt("H1", "hotel", 218.0)])
        assert notice is None
        assert "Added the Hilton hotel (3 nights, $654.00) to your draft trip ORD → JFK" in msg
        assert msg.endswith("Estimated cost is now $801.00.")

    def test_flight_replaced(self) -> None:
        res = TripSelectionResult(trip=_trip("816.00"), action="updated", replaced=("flight",))
        msg, _ = _trip_messages(res, [_opt("F2", supplier="United Airlines", amount=162.0)])
        assert "Replaced the flight on your draft trip" in msg
        assert "United Airlines flight ($162.00)" in msg

    def test_unchanged(self) -> None:
        res = TripSelectionResult(trip=_trip(), action="unchanged")
        msg, notice = _trip_messages(res, [_opt("F1")])
        assert msg.startswith("That option is already on your draft trip ORD → JFK")
        assert notice is None

    def test_new_trip_after_route_change_names_the_old_draft(self) -> None:
        prev = {
            "reason": "different_trip",
            "id": str(uuid.uuid4()),
            "status": "draft",
            "label": "ORD → JFK, 2026-11-17 – 2026-11-20 (Client meeting)",
        }
        trip = _trip(
            "150.00",
            primary_destination="BOS",
            start_date=date(2026, 12, 3),
            end_date=date(2026, 12, 5),
        )
        res = TripSelectionResult(
            trip=trip, action="created", added=("flight",), previous_trip=prev
        )
        msg, notice = _trip_messages(res, [_opt("F9", amount=150.0)])
        assert notice == (
            "Started a new draft trip ORD → BOS, 2026-12-03 – 2026-12-05 — your earlier "
            "draft ORD → JFK, 2026-11-17 – 2026-11-20 (Client meeting) is still in Trips."
        )
        assert msg.startswith(notice)

    def test_new_trip_when_old_one_is_pending(self) -> None:
        prev = {"reason": "not_draft", "id": "x", "status": "pending_approval", "label": "L"}
        res = TripSelectionResult(trip=_trip(), action="created", previous_trip=prev)
        _, notice = _trip_messages(res, [_opt("H1", "hotel", 218.0)])
        assert notice is not None
        assert "your earlier trip L is awaiting approval, so it can't be changed" in notice

    def test_rejected_predecessor_sets_reviewer_expectation(self) -> None:
        prev = {"reason": "not_draft", "id": "x", "status": "rejected", "label": "L"}
        res = TripSelectionResult(trip=_trip(), action="created", previous_trip=prev)
        _, notice = _trip_messages(res, [_opt("F1")])
        assert notice is not None and "will go to a reviewer" in notice


class TestReturnDateChange:
    """UAT2 defect E: only the return date changed -> the same draft, in place."""

    def test_rehoteled_reply_says_return_date_updated(self) -> None:
        res = TripSelectionResult(
            trip=_trip("1019.00", end_date=date(2026, 11, 21)),
            action="updated",
            replaced=("hotel",),
            segments=({"segment_type": "flight"}, {"segment_type": "hotel", "nights": 4}),
            end_date_change=(date(2026, 11, 20), date(2026, 11, 21)),
        )
        msg, notice = _trip_messages(res, [_opt("H1", "hotel", 218.0)])
        assert notice is None
        assert msg.startswith(
            "Updated the return date of your draft trip ORD → JFK, 2026-11-17 – "
            "2026-11-21 from 2026-11-20 to 2026-11-21."
        )
        assert "Hilton hotel (4 nights, $872.00)" in msg
        assert "still covers" not in msg
        assert msg.endswith("Estimated cost is now $1,019.00.")

    def test_stale_hotel_is_flagged_not_hidden(self) -> None:
        res = TripSelectionResult(
            trip=_trip("801.00", end_date=date(2026, 11, 21)),
            action="updated",
            segments=(
                {"segment_type": "flight"},
                {
                    "segment_type": "hotel",
                    "supplier": "Hilton",
                    "nights": 3,
                    "check_in_date": "2026-11-17",
                    "check_out_date": "2026-11-20",
                },
            ),
            end_date_change=(date(2026, 11, 20), date(2026, 11, 21)),
            stale_hotel=True,
        )
        msg, notice = _trip_messages(res, [_opt("F1")])
        assert notice is None
        assert msg.startswith("Updated the return date of your draft trip")
        assert (
            "The Hilton hotel on it still covers 2026-11-17 – 2026-11-20 (3 nights) — "
            "select a hotel to cover the new dates."
        ) in msg

    def test_trip_change_carries_end_date_change(self) -> None:
        res = TripSelectionResult(
            trip=_trip("801.00", end_date=date(2026, 11, 21)),
            action="updated",
            end_date_change=(date(2026, 11, 20), date(2026, 11, 21)),
            stale_hotel=True,
        )
        tc = _trip_change(res)
        assert tc["end_date_change"] == {"from": "2026-11-20", "to": "2026-11-21"}
        assert tc["stale_hotel"] is True


def test_trip_change_payload_shape() -> None:
    res = TripSelectionResult(
        trip=_trip("801.00"),
        action="updated",
        added=("hotel",),
        before={"version": 1},
        after={"version": 2},
        segments=({"segment_type": "flight"}, {"segment_type": "hotel"}),
    )
    tc = _trip_change(res)
    assert tc["action"] == "updated"
    assert tc["segment_types_added"] == ["hotel"]
    assert tc["segment_count"] == 2
    assert tc["estimated_cost"] == "801.00"
    assert tc["audit"] == {"before": {"version": 1}, "after": {"version": 2}}
    assert tc["end_date_change"] is None
    assert tc["stale_hotel"] is False


def test_unknown_option_id_never_creates_a_trip() -> None:
    """A stale option id (e.g. after the dates changed) is an error, not an empty trip."""
    ctx = AgentContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        granted_permission_keys=frozenset({TRIP_CREATE_OWN}),
        correlation_id="t",
    )
    session = MagicMock()
    with (
        patch("ai_agents.agents.travel_agent.agent.write_decision_log", return_value=MagicMock()),
        patch(
            "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
            side_effect=lambda items, **_k: tuple(
                MagicMock(in_policy=True, verdicts=()) for _ in items
            ),
        ),
    ):
        result = TravelAgent(provider=LocalLlmProvider(), today=lambda: date(2026, 11, 11)).handle(
            Task(
                context_type="travel_planning",
                payload={
                    "origin": "SFO",
                    "destination": "JFK",
                    "departure_date": "2026-06-10",
                    "travelers": 1,
                    "currency": "USD",
                    "include_hotels": False,
                    "selected_option_ids": ["NOT-IN-SHORTLIST"],
                },
            ),
            RoutingDecision(
                target_agent=AiAgent.travel,
                route=AiRoute.deterministic,
                task_type="trip_planning",
                confidence=1.0,
                rationale="t",
            ),
            context=ctx,
            session=session,
        )
    assert result.output["trip_id"] is None
    assert "no longer available" in result.output["trip_persist_error"]
    assert "trip_change" not in result.output
    assert result.status == AgentResultStatus.needs_review
    session.add.assert_not_called()
