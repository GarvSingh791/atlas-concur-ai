"""Product gap 2 — review round 2 fixes (hermetic).

* the GRND-03 summary shown to finance says what the engine enforces: airport parking
  only, days only from the parking receipt (or a stated count above the limit), the
  linked trip's length is context and never counted as parking days;
* LOD-02's "booked through the travel tool" is a HOTEL segment of the trip — a lodging
  entry linked to a flight / car / rail segment is not a tool booking.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import build_eval_item
from ai_agents.agents.policy_rule_semantics import describe_rule
from database.models.trip_segment import SegmentType
from database.seeds.policy_content import HOTEL_CAP_DEFINITION, PARKING_DAYS_DEFINITION


def _parking_summary() -> str:
    return " ".join(describe_rule("amount_cap", "all", PARKING_DAYS_DEFINITION))


def test_grnd03_summary_matches_enforcement() -> None:
    summary = _parking_summary()
    # Scope: airport parking only, and what that means.
    assert "airport parking (an airport named on the receipt or entry, or a line linked " in (
        summary
    )
    assert "to a flight segment)" in summary
    assert "hotel valet, city garages and meters are not covered" in summary
    # Lots: the premium lots are AIRPORT lots (valet elsewhere is not reached at all).
    assert "premium/valet/short-term airport lots" in summary
    # Days: receipt only (or a stated count above the limit); never the trip length.
    assert "Days come only from the parking receipt (OCR)" in summary
    assert "above the limit" in summary
    assert "never counted as parking days" in summary


def test_grnd03_summary_never_claims_trip_days() -> None:
    summary = _parking_summary()
    assert "linked approved trip" not in summary
    assert "departure to return" not in summary
    assert "Atlas identifies as parking" not in summary


def test_lod02_summary_names_the_hotel_segment_condition() -> None:
    summary = " ".join(describe_rule("amount_cap", "hotel", HOTEL_CAP_DEFINITION))
    # Review round 4 (intended change): only a hotel segment the PLANNER booked.
    assert "linked to a hotel segment the trip planner booked on the traveler's approved" in (
        summary
    )
    assert "a hotel typed into a trip by hand" in summary
    # Security review round 3 (intended change): positive receipt evidence is required.
    assert "receipt or card names that hotel, with the receipt in the booked city" in summary
    assert "without that receipt evidence, it is a manager exception" in summary


def _lodging(amount: float = 690.0) -> DraftEntry:
    return DraftEntry(
        category="lodging",
        amount=amount,
        currency="USD",
        expense_date="2026-04-06",
        vendor="Some Indie Hotel",
        description=None,
        status="matched",
        match_source=None,
        trip_segment_id="seg",
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def _segment(kind: object) -> SimpleNamespace:
    return SimpleNamespace(segment_type=kind, details={"nights": 2}, location="New York")


@pytest.mark.parametrize(
    "kind", [SegmentType.flight, SegmentType.car, SegmentType.rail, "flight", "car", None]
)
def test_lodging_on_a_non_hotel_segment_is_not_a_tool_booking(kind: object) -> None:
    item = build_eval_item(_lodging(), segment=_segment(kind), idx=0)
    assert item.item_type == "hotel" and item.booked_through_tool is False


@pytest.mark.parametrize("kind", [SegmentType.hotel, "hotel"])
def test_lodging_on_a_hotel_segment_alone_is_not_a_tool_booking(kind: object) -> None:
    """Review round 4 (behaviour intentionally changed): a hotel segment alone no longer
    makes build_eval_item grant the tool booking — POST /trips segments are typed by the
    traveler.  Only a PLANNER-booked hotel segment (is_planner_booked_hotel, see
    test_pg2_review_fixes_r4) qualifies, and only at the trusted call sites."""
    item = build_eval_item(_lodging(), segment=_segment(kind), idx=0)
    assert item.booked_through_tool is False


def test_unlinked_lodging_is_not_a_tool_booking() -> None:
    assert build_eval_item(_lodging(), segment=None, idx=0).booked_through_tool is False
