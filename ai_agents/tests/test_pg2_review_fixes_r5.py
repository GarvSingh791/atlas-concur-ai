"""Product gap 2 — review fixes, round 5 (hermetic).

* GRND-01: the itinerary is a plan, the receipt is what was bought.  A premium class read
  from the receipt / card (or stated by the claimant against their own interest) wins over
  a rail segment that says "Standard"; the segment's standard class is used only when no
  word names a premium class.
* MEAL-04: the usual itemized ways a receipt prints alcohol (grape varietals, beer brands,
  "Gin & tonic") are excluded and flagged; food cooked with wine / beer is not.
* Ground sub-type trust: a line with no trusted family (no receipt / card naming one, or a
  receipt vendor the claimant edited with no OCR snapshot left) is UNCONFIRMED — every
  sub-type is a candidate, so every kind rule (GRND-01 / GRND-03 / GRND-05) reaches it and
  gives its could-not-verify (approver check) verdict instead of being skipped.
"""

from __future__ import annotations

import uuid

import pytest

from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.expense_agent.entry_context import ground_fields
from ai_agents.tools import entry_rules
from ai_agents.tools.ground_transport import GROUND_SUBTYPES
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import (
    PARKING_DAYS_DEFINITION,
    RAIL_CLASS_DEFINITION,
    TIP_PCT_DEFINITION,
)

TOOL = PolicyEvalTool()


def _rule(rule_type: str, applies_to: str, definition: dict, ref: str) -> PolicyRule:
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType(rule_type),
        applies_to=PolicyAppliesTo(applies_to),
        definition=definition,
        source_clause_ref=ref,
    )


RAIL = _rule("category_allow", "all", RAIL_CLASS_DEFINITION, "GRND-01")
PARKING = _rule("amount_cap", "all", PARKING_DAYS_DEFINITION, "GRND-03")
TIPS = _rule("amount_cap", "all", TIP_PCT_DEFINITION, "GRND-05")
ALCOHOL = {"exclusions": ["alcohol"], "exclusions_clause_ref": "MEAL-04"}


def _verdicts(rule: PolicyRule, item: EvalItem) -> tuple[RuleVerdict, ...]:
    if not TOOL.rule_applies(rule.rule_type, rule.applies_to, rule.definition, item):
        return ()
    return TOOL._evaluate_all([rule], item)


def _line(amount: float, *typed: str, **kw) -> EvalItem:
    return EvalItem(
        item_type="receipt",
        amount_usd=amount,
        expense_category="ground_transport",
        **ground_fields(*typed, **kw),
    )


# ---------------------------------------------------------------------------
# GRND-01 — the receipt's class wins over the planned segment class
# ---------------------------------------------------------------------------


class TestRailClassEvidenceWins:
    def test_reviewer_probe_receipt_first_class_beats_segment_standard(self) -> None:
        out = ground_fields(
            "Amtrak",
            "train to DC",
            trusted_texts=("AMTRAK", "Acela First Class seat 2C"),
            segment_type="rail",
            segment_hours=2.0,
            segment_class="standard",
        )
        assert out["ground_verified"] is True
        assert out["travel_class"] == "first"
        assert out["duration_hours"] == 2.0

    def test_first_class_receipt_on_a_standard_segment_violates_grnd01(self) -> None:
        item = _line(
            180.0,
            "Amtrak",
            "Acela",
            trusted_texts=("AMTRAK", "First Class Seat 2A"),
            segment_type="rail",
            segment_hours=2.0,
            segment_class="Standard",
        )
        (v,) = _verdicts(RAIL, item)
        assert not v.in_policy and v.source_clause_ref == "GRND-01"
        assert "under 3 hours" in v.reason

    def test_claimant_typed_first_class_beats_segment_standard(self) -> None:
        out = ground_fields("Amtrak", "first class", segment_type="rail", segment_class="Standard")
        assert out["travel_class"] == "first"

    def test_business_on_the_receipt_beats_segment_standard(self) -> None:
        out = ground_fields(
            "Amtrak",
            trusted_texts=("AMTRAK", "Business class"),
            segment_type="rail",
            segment_class="standard",
        )
        assert out["travel_class"] == "business"

    def test_mixed_receipt_words_still_premium_never_segment_standard(self) -> None:
        out = ground_fields(
            "Amtrak",
            trusted_texts=("AMTRAK", "Coach upgrade to First Class"),
            segment_type="rail",
            segment_class="standard",
        )
        assert out["travel_class"] == "first"

    def test_segment_standard_used_when_no_word_names_a_premium_class(self) -> None:
        out = ground_fields(
            "Amtrak",
            trusted_texts=("AMTRAK", "Seat 14C"),
            segment_type="rail",
            segment_class="Standard",
        )
        assert out["travel_class"] == "standard"

    def test_segment_premium_class_is_kept(self) -> None:
        out = ground_fields(
            "Amtrak", trusted_texts=("AMTRAK",), segment_type="rail", segment_class="First"
        )
        assert out["travel_class"] == "first"

    def test_typed_standard_alone_still_does_not_clear(self) -> None:
        assert ground_fields("Amtrak", "standard class")["travel_class"] is None


# ---------------------------------------------------------------------------
# MEAL-04 — common itemized alcohol
# ---------------------------------------------------------------------------


def _meal(*lines: tuple[str, float]) -> EvalItem:
    claim = sum(a for _, a in lines)
    return EvalItem(item_type="meals", amount_usd=claim, line_items=tuple(lines))


class TestAlcoholVocabulary:
    @pytest.mark.parametrize(
        "line",
        [
            ("Cabernet Sauvignon", 14.0),
            ("Pinot Noir glass", 14.0),
            ("Heineken", 7.0),
            ("Gin & tonic", 12.0),
        ],
    )
    def test_reviewer_probes_are_excluded_and_flagged(self, line: tuple[str, float]) -> None:
        out = entry_rules.exclusion_outcome(ALCOHOL, _meal(("Burger", 20.0), line))
        assert out is not None and not out.in_policy
        assert out.clause_ref == "MEAL-04"
        assert out.observed["excluded_usd"] == line[1]
        assert out.observed["reimbursable_usd"] == 20.0

    @pytest.mark.parametrize(
        "desc",
        [
            "Merlot",
            "Chardonnay 6oz",
            "Sauvignon Blanc",
            "Pinot Grigio",
            "Malbec",
            "Riesling",
            "Rosé",
            "Shiraz",
            "Zinfandel",
            "Cab Sauv btl",
            "Glass of house red",
            "Pint of Guinness",
            "Stella Artois",
            "Corona Extra",
            "Bud Light",
            "Modelo Especial",
            "Peroni",
            "Asahi",
            "Sapporo",
            "Pale ale",
            "Dark rum",
            "Mezcal",
            "Soju",
            "Highball",
            "Old Fashioned",
            "Aperol Spritz",
            "Tawny port",
            "Dry sherry",
            "Gin and tonic",
        ],
    )
    def test_common_drinks_match(self, desc: str) -> None:
        assert entry_rules.exclusion_matches(desc, "alcohol", line_item=True), desc

    @pytest.mark.parametrize(
        "desc",
        [
            "Ginger ale",
            "Glass of water",
            "Glass of orange juice",
            "Pint of milk",
            "Beer-battered cod",
            "Red wine braised short rib",
            "Duck with port reduction",
            "Sherry vinaigrette salad",
            "Rum cake",
            "Rosemary chicken",
            "Airport sandwich",
        ],
    )
    def test_food_and_soft_drinks_do_not_match(self, desc: str) -> None:
        assert entry_rules.exclusion_matches(desc, "alcohol", line_item=True) is None, desc

    @pytest.mark.parametrize("vendor", ["Corona Cafe", "Port City Grill", "Stella's Diner"])
    def test_ambiguous_brand_words_are_not_matched_in_a_vendor_name(self, vendor: str) -> None:
        item = EvalItem(item_type="meals", amount_usd=30.0, item_text=vendor)
        assert entry_rules.exclusion_outcome(ALCOHOL, item) is None


# ---------------------------------------------------------------------------
# Unconfirmed ground sub-type — every kind rule reaches the line
# ---------------------------------------------------------------------------


class TestUnconfirmedSubtype:
    def test_typed_only_uber_reaches_every_kind_rule(self) -> None:
        item = _line(180.0, "Uber", "Ride")
        assert item.ground_verified is False
        assert set(item.ground_candidates) == set(GROUND_SUBTYPES)
        (rail,) = _verdicts(RAIL, item)
        assert rail.in_policy and rail.observed["could_not_verify"] == "travel_class"
        assert rail.observed["approver_check"] == "manager"
        assert "could not confirm" in rail.reason
        (park,) = _verdicts(PARKING, item)
        assert park.observed["could_not_verify"] == "airport_parking"
        assert park.observed["approver_check"] == "manager"
        (tip,) = _verdicts(TIPS, item)
        assert tip.in_policy  # no tip anywhere: nothing to check

    def test_no_words_at_all_is_unconfirmed(self) -> None:
        item = _line(80.0, "Ride")
        assert set(item.ground_candidates) == set(GROUND_SUBTYPES)
        assert _verdicts(RAIL, item)

    def test_receipt_vendor_naming_the_family_verifies_and_skips_other_kinds(self) -> None:
        item = _line(30.0, "Uber", "Ride", trusted_texts=("UBER",))
        assert item.ground_verified is True and item.ground_candidates == ("rideshare_taxi",)
        assert _verdicts(RAIL, item) == ()
        assert _verdicts(PARKING, item) == ()

    def test_contradicting_receipt_keeps_its_candidates_only(self) -> None:
        item = _line(180.0, "Uber", "Ride", trusted_texts=("AMTRAK",))
        assert set(item.ground_candidates) == {"rideshare_taxi", "rail"}
        assert _verdicts(RAIL, item)
        assert _verdicts(PARKING, item) == ()

    def test_edited_vendor_without_snapshot_is_unconfirmed(self) -> None:
        item = _line(180.0, "Uber", "Ride", trusted_texts=("UBER",), vendor_edited=True)
        assert item.ground_verified is False
        assert set(item.ground_candidates) == set(GROUND_SUBTYPES)


class TestReceiptTextsSnapshot:
    def test_edited_vendor_falls_back_to_the_ocr_snapshot(self) -> None:
        words = entry_context.receipt_texts(
            {"vendor": "Uber"},
            {"vendor": "user_edited"},
            grounding_context={"ocr_snapshot": {"vendor": "AMTRAK"}},
        )
        assert words == ("AMTRAK",)

    def test_deterministic_vendor_is_kept(self) -> None:
        words = entry_context.receipt_texts(
            {"vendor": "AMTRAK"},
            {"vendor": "deterministic"},
            grounding_context={"ocr_snapshot": {"vendor": "AMTRAK"}},
        )
        assert words == ("AMTRAK",)

    def test_edited_vendor_without_snapshot_gives_no_vendor(self) -> None:
        assert entry_context.receipt_texts({"vendor": "Uber"}, {"vendor": "user_edited"}) == ()
