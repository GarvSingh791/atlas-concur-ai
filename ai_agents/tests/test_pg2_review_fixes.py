"""Product gap 2 / 8 — review fixes (hermetic).

* GRND-03 reaches AIRPORT parking only: hotel valet, an hourly city garage and other
  plain parking are never flagged under an airport-only clause;
* the receipt's own OCR words (deterministic vendor, line items) classify a ground line
  together with the claimant's words — a typed vendor cannot hide a rideshare receipt.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item,
    build_eval_item_from_fields,
)
from ai_agents.tools.ground_transport import airport_named
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import PARKING_DAYS_DEFINITION

TOOL = PolicyEvalTool()
PARKING = PolicyRule(
    id=uuid.uuid4(),
    policy_id=uuid.uuid4(),
    rule_type=PolicyRuleType.amount_cap,
    applies_to=PolicyAppliesTo.all,
    definition=PARKING_DAYS_DEFINITION,
    source_clause_ref="GRND-03",
)


def _entry(vendor: str | None, description: str | None) -> DraftEntry:
    return DraftEntry(
        category="ground_transport",
        amount=60.0,
        currency="USD",
        expense_date="2026-05-02",
        vendor=vendor,
        description=description,
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def _grnd03(item: EvalItem) -> list:
    if not TOOL.rule_applies(PARKING.rule_type, PARKING.applies_to, PARKING.definition, item):
        return []
    return list(TOOL._evaluate_all([PARKING], item))


@pytest.mark.parametrize(
    ("vendor", "description"),
    [
        ("Marriott Downtown", "Hotel valet parking - client visit"),
        ("City Garage", "Hourly parking near client office"),
        ("SpotHero", "Parking near the conference center, 3 days"),
        ("Hilton parking", "one night"),
    ],
)
def test_plain_parking_gets_no_grnd03(vendor: str, description: str) -> None:
    item = build_eval_item(_entry(vendor, description))
    assert item.ground_subtype == "parking" and item.airport_parking is False
    # Review round 5 (behaviour intentionally changed): with no receipt / card nothing
    # trusted says this is NOT airport parking, so GRND-03 no longer skips it — but it is
    # never a violation either, only a could-not-verify the approver checks.
    (v,) = _grnd03(item)
    assert v.in_policy and v.observed["could_not_verify"] == "airport_parking"
    # Verified plain parking (the receipt names the garage) is not reached at all.
    verified = dataclasses.replace(item, ground_verified=True, ground_candidates=("parking",))
    assert _grnd03(verified) == []


@pytest.mark.parametrize(
    ("vendor", "description"),
    [
        ("SFO Long Term Parking", None),
        ("The Parking Spot", "economy lot"),
        ("Park 'N Fly", None),
        ("Airport Parking", "Lot C"),
        ("Joe's Lot", "Parking at JFK, 3 days"),
    ],
)
def test_airport_parking_is_reached(vendor: str, description: str | None) -> None:
    item = build_eval_item(_entry(vendor, description))
    assert item.ground_subtype == "parking" and item.airport_parking is True
    (v,) = _grnd03(item)
    assert v.source_clause_ref == "GRND-03"


def test_flight_segment_makes_parking_airport_parking() -> None:
    fields = entry_context.ground_fields("Lot 4 parking", segment_type="flight")
    assert fields["airport_parking"] is True
    hotel = entry_context.ground_fields("Lot 4 parking", segment_type="hotel")
    assert hotel["airport_parking"] is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Airport economy lot", "airport"),
        ("PARK N FLY", "park n fly"),
        ("SFO Long Term", "SFO"),
        ("sfo long term", None),  # codes are matched in capitals only
        ("SAN FRANCISCO PARKING", None),  # SAN is a city prefix, not taken as a code
        ("LAS VEGAS GARAGE", None),
        ("Downtown garage", None),
    ],
)
def test_airport_named(text: str, expected: str | None) -> None:
    assert airport_named(text) == expected


def test_receipt_texts_trust_boundary() -> None:
    fields = {
        "vendor": "Uber",
        "line_items": [{"description": "Trip fare", "amount": 40}, {"description": "Tip"}],
    }
    assert entry_context.receipt_texts(fields, {"vendor": "deterministic"}) == (
        "Uber",
        "Trip fare",
    )
    # A vendor the claimant typed over / gap-fill guessed is not the receipt's word.
    assert entry_context.receipt_texts(fields, {"vendor": "user_edited"}) == ("Trip fare",)
    assert entry_context.receipt_texts(fields, {"vendor": "ai_inferred"}) == ("Trip fare",)
    assert entry_context.receipt_texts(None, {}) == ()


def test_ocr_words_contradicting_typed_vendor_fall_back_to_broad() -> None:
    fields = entry_context.ground_fields("Amtrak", "train to client", "Uber")
    assert fields["ground_subtype"] == "ground_transport"


def test_upload_path_classifies_from_ocr_line_items() -> None:
    item = build_eval_item_from_fields(
        {
            "amount": 30.0,
            "vendor": "Receipt",
            "line_items": [{"description": "Airport parking 2 days", "amount": 30.0}],
        },
        category="ground_transport",
        ref_id="r1",
        provenance={"vendor": "deterministic"},
    )
    assert item.ground_subtype == "parking" and item.airport_parking is True
