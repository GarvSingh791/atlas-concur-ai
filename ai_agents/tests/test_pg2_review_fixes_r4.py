"""Product gap 2 — review fixes, round 4 (hermetic).

LOD-02 at TRIP approval: "booked through the travel tool" means the PLANNER booked the
hotel segment — the trip carries the server-set ``recommendation_context`` (POST /trips
always writes None) and the segment's ``details.option_id`` is one of that trip's
selected planner options.  A hotel segment the traveler typed in through POST /trips
(details are client-writable) is not a tool booking: 12.5% over the nightly cap stays a
blocking HOTEL-CAP violation that escalates to a manager.

Entry-rule "could not verify" advisories whose text promises an approver will check
(MEAL-04 keyword only, GRND-01 class / GRND-03 days / GRND-05 tip or fare unverified)
carry ``requires_approver`` on their not_evaluated flag, so the approval agent never
auto-approves past them; MEAL-02's unverified travel day promises no check and stays a
plain advisory.

Reuses the seeded-rule FakeSession harness of ``test_c7_05_15_review_fixes``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item,
    is_planner_booked_hotel,
    verdicts_to_flags,
)
from ai_agents.tests.test_c7_05_15_review_fixes import (  # noqa: F401  (autouse fixture)
    _assess_trip,
    _seeded_rules,
    _trip,
    _trip_session,
)
from ai_agents.tests.test_pg2_entry_rules import MEALS, PARKING, RAIL, TIPS, _ground, _verdicts
from ai_agents.tools import entry_rules
from ai_agents.tools.policy_eval import EvalItem
from database.models.trip import TripStatus
from database.models.trip_segment import SegmentType, TripSegment

_OPTION = "hotel-opt-denver-1"


def _denver_hotel(s: object, trip: object, *, details: dict) -> TripSegment:
    return s.add_obj(  # type: ignore[attr-defined, no-any-return]
        TripSegment(
            id=uuid.uuid4(),
            trip_id=trip.id,  # type: ignore[attr-defined]
            segment_type=SegmentType.hotel,
            supplier="Marriott International",
            start_datetime=datetime(2026, 11, 1, 15, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 11, 2, 11, tzinfo=timezone.utc),
            location="Denver",
            amount=Decimal("180.00"),  # 1 night, $160 Tier 3 cap -> 12.5% over
            currency="USD",
            in_policy=True,  # client-claimed
            details=details,
        )
    )


def _planner_context(*option_ids: str) -> dict:
    return {
        "agent_task": "trip_planning",
        "selected_option_ids": list(option_ids),
        "selected_options": [{"option_id": oid} for oid in option_ids],
        "reflection_outcome": "pass",
    }


def _clause_flags(a: object, ref: str) -> list[dict]:
    return [f for f in a.flags_snapshot if f.get("source_clause_ref") == ref]  # type: ignore[attr-defined]


class TestTripApprovalLod02:
    def test_manual_hotel_segment_over_cap_escalates(self) -> None:
        """POST /trips-style trip (recommendation_context None) — even a client-typed
        option_id in details does not make it a tool booking."""
        for details in ({"nights": 1}, {"nights": 1, "option_id": _OPTION}):
            s = _trip_session()
            t = _trip(s, status=TripStatus.pending_approval, estimated_cost="180.00")
            _denver_hotel(s, t, details=details)

            a, route = _assess_trip(s, t)

            (cap,) = _clause_flags(a, "HOTEL-CAP")
            assert cap["flag_type"] == "policy_violation", details
            assert cap["severity"] == "blocking"
            assert not _clause_flags(a, "LOD-02")
            assert route == "escalate"

    def test_planner_booked_hotel_keeps_the_lod02_advisory(self) -> None:
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="180.00")
        t.recommendation_context = _planner_context(_OPTION)
        _denver_hotel(s, t, details={"nights": 1, "option_id": _OPTION})

        a, route = _assess_trip(s, t)

        (adv,) = _clause_flags(a, "LOD-02")
        assert adv["flag_type"] == "policy_advisory" and adv["severity"] == "advisory"
        assert not _clause_flags(a, "HOTEL-CAP")
        assert route == "auto_approve"

    def test_planner_trip_with_a_foreign_option_id_escalates(self) -> None:
        """A planner trip whose hotel segment names an option the planner did not
        select for THIS trip is not a tool booking."""
        s = _trip_session()
        t = _trip(s, status=TripStatus.pending_approval, estimated_cost="180.00")
        t.recommendation_context = _planner_context("flight-opt-1")
        _denver_hotel(s, t, details={"nights": 1, "option_id": _OPTION})

        a, route = _assess_trip(s, t)

        (cap,) = _clause_flags(a, "HOTEL-CAP")
        assert cap["flag_type"] == "policy_violation"
        assert route == "escalate"


def _lodging() -> DraftEntry:
    return DraftEntry(
        category="lodging",
        amount=180.0,
        currency="USD",
        expense_date="2026-11-01",
        vendor="Marriott",
        description=None,
        status="planned",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def test_build_eval_item_never_grants_the_tool_booking_itself() -> None:
    """Only the two trusted call sites (planner ranking; build_entry_eval_item /
    trip approval after the planner check) may set booked_through_tool."""
    seg = SimpleNamespace(
        segment_type=SegmentType.hotel, details={"nights": 1, "option_id": _OPTION}, location="X"
    )
    assert build_eval_item(_lodging(), segment=seg).booked_through_tool is False


@pytest.mark.parametrize(
    ("ctx", "seg_type", "option_id", "same_trip", "expected"),
    [
        (_planner_context(_OPTION), SegmentType.hotel, _OPTION, True, True),
        (_planner_context(_OPTION), "hotel", _OPTION, True, True),
        (None, SegmentType.hotel, _OPTION, True, False),  # POST /trips
        ({"selected_option_ids": [_OPTION]}, SegmentType.hotel, _OPTION, True, False),
        (_planner_context(_OPTION), SegmentType.hotel, None, True, False),
        (_planner_context(_OPTION), SegmentType.hotel, "other", True, False),
        (_planner_context(_OPTION), SegmentType.flight, _OPTION, True, False),
        (_planner_context(_OPTION), SegmentType.hotel, _OPTION, False, False),
        ({**_planner_context(), "selected_option_ids": _OPTION}, "hotel", _OPTION, True, False),
    ],
)
def test_is_planner_booked_hotel(
    ctx: dict | None, seg_type: object, option_id: str | None, same_trip: bool, expected: bool
) -> None:
    trip = SimpleNamespace(id=uuid.uuid4(), recommendation_context=ctx)
    details = {"nights": 1} | ({"option_id": option_id} if option_id else {})
    seg = SimpleNamespace(
        trip_id=trip.id if same_trip else uuid.uuid4(), segment_type=seg_type, details=details
    )
    assert is_planner_booked_hotel(seg, trip) is expected
    assert is_planner_booked_hotel(seg, None) is False


# ---------------------------------------------------------------------------
# Could-not-verify advisories that promise an approver check escalate
# ---------------------------------------------------------------------------


def _unverified_flags(rule: object, item: EvalItem, category: str) -> list:
    verdicts = [v for v in _verdicts(rule, item) if v.observed.get(entry_rules.COULD_NOT_VERIFY)]  # type: ignore[arg-type]
    assert verdicts, "expected a could-not-verify verdict"
    flags, _ = verdicts_to_flags(
        verdicts, entry_ref="x", has_receipt=True, category=category, justification=None
    )
    return flags


@pytest.mark.parametrize(
    ("rule", "item", "category", "clause"),
    [
        (
            MEALS,
            EvalItem(item_type="meals", amount_usd=60.0, item_text="Dinner with wine"),
            "meals",
            "MEAL-04",
        ),
        (RAIL, _ground(90.0, "rail"), "ground_transport", "GRND-01"),
        (
            PARKING,
            _ground(60.0, "parking", airport_parking=True, trip_days=9),
            "ground_transport",
            "GRND-03",
        ),
        (
            TIPS,
            _ground(30.0, "rideshare_taxi", item_text="Uber incl. tip"),
            "ground_transport",
            "GRND-05",
        ),
        (
            TIPS,
            _ground(30.0, "rideshare_taxi", tip_usd=30.0),
            "ground_transport",
            "GRND-05",
        ),
    ],
    ids=["meal04-keyword", "grnd01-class", "grnd03-days", "grnd05-tip", "grnd05-fare"],
)
def test_approver_check_advisories_require_an_approver(
    rule: object, item: EvalItem, category: str, clause: str
) -> None:
    (flag,) = [f for f in _unverified_flags(rule, item, category) if f.source_clause_ref == clause]
    assert flag.flag_type == "not_evaluated" and flag.severity == "advisory"  # submit OK
    assert flag.requires_approver == "manager"  # ... but never auto-approved
    assert "approver should check" in flag.reason


def test_meal02_unverified_travel_day_stays_a_plain_advisory() -> None:
    """MEAL-02's could-not-verify wording promises no approver check — unchanged."""
    (flag,) = _unverified_flags(
        MEALS, EvalItem(item_type="meals", amount_usd=70.0, city="New York"), "meals"
    )
    assert flag.source_clause_ref == "MEAL-02" and flag.requires_approver is None
    assert "approver" not in flag.reason
