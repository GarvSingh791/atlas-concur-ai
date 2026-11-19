"""Product gap 2 (policy2) — MEAL-05 / ENT-02 / ENT-04 / MIL-01..03 evaluators (hermetic).

Pure, deterministic, Decimal.  Every rule is the SEEDED definition
(``database/seeds/spend_rules_content.py``) run through the real engine
(``PolicyEvalTool``) on items built by the real builder
(``presubmit.build_eval_item``), and the outcome through the real flag mapping
(``presubmit.verdicts_to_flags``).  Boundaries: exactly at a cap / cap + $0.01,
gift $100.00 vs $100.01, 300.0 vs 300.1 miles each way, 0.1 mile.
"""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import build_eval_item, verdicts_to_flags
from ai_agents.agents.expense_agent.rule_categorizer import is_valid_category
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from ai_agents.tools.spend_policy import (
    compute_mileage,
    restore_spend,
    spend_facts,
    to_miles,
)
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.corpus import figures as F
from database.seeds.policy_content import RULES
from database.seeds.spend_rules_content import SPEND_RULES

_BY_REF = {r["clause_ref"]: r for r in RULES}
TOOL = PolicyEvalTool()
TRIP = {"distance": "10", "unit": "mi", "commute_offset": "0", "origin": "A", "destination": "B"}


def _entry(category: str, amount: float, **kw: Any) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency=kw.pop("currency", "USD"),
        expense_date="2026-03-02",
        vendor=kw.pop("vendor", None),
        description=kw.pop("description", None),
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
        **kw,
    )


def _item(category: str, amount: float, **kw: Any) -> EvalItem:
    city = kw.pop("city", None)
    item = build_eval_item(_entry(category, amount, **kw), segment=None)
    return dataclasses.replace(item, city=city) if city else item


def _verdict(ref: str, item: EvalItem) -> RuleVerdict:
    rule = _BY_REF[ref]
    assert PolicyEvalTool.rule_applies(
        rule["rule_type"], rule["applies_to"], rule["definition"], item
    ), f"{ref} must reach this line"
    v = TOOL.evaluate_candidate(rule["rule_type"], rule["applies_to"], rule["definition"], item)
    return dataclasses.replace(v, source_clause_ref=ref)


def _flags(v: RuleVerdict, category: str, justification: str | None = None) -> list[Any]:
    flags, _ = verdicts_to_flags(
        [v], entry_ref="x", has_receipt=True, category=category, justification=justification
    )
    return flags


def _applies(ref: str, item: EvalItem) -> bool:
    rule = _BY_REF[ref]
    return PolicyEvalTool.rule_applies(
        rule["rule_type"], rule["applies_to"], rule["definition"], item
    )


# ---------------------------------------------------------------------------
# Seeded rules: figures and clause citations
# ---------------------------------------------------------------------------


def test_seeded_rules_quote_the_canonical_figures() -> None:
    meal = _BY_REF["MEAL-05"]["definition"]
    client = _BY_REF["ENT-02"]["definition"]
    caps = {t["tier"]: t["cap"] for t in meal["tiers"]}
    assert caps == F.TEAM_DINNER_CAP
    assert {t["tier"]: t["cap"] for t in client["tiers"]} == F.CLIENT_MEAL_CAP
    assert meal["tiers"][0]["tier"] == "tier3" and meal["tiers"][0]["default"] is True
    assert _BY_REF["ENT-04"]["definition"]["threshold_usd"] == F.GIFT_DISCLOSURE_THRESHOLD
    assert _BY_REF["MIL-01"]["definition"]["rate_per_mile"] == F.MILEAGE_RATE_USD_PER_MILE
    assert _BY_REF["MIL-03"]["definition"]["max_one_way_miles"] == F.MILEAGE_AIR_CAP_MILES
    assert {r["clause_ref"] for r in SPEND_RULES} == {
        "MEAL-05",
        "ENT-02",
        "ENT-04",
        "MIL-01",
        "MIL-02",
        "MIL-03",
    }


# ---------------------------------------------------------------------------
# MEAL-05 team meals
# ---------------------------------------------------------------------------


def test_meal05_exactly_at_cap_is_in_policy_and_one_cent_over_is_a_justifiable_violation() -> None:
    at_cap = _verdict("MEAL-05", _item("meals", 240.00, attendee_count=4))  # Tier 3 $60 x 4
    assert at_cap.in_policy and at_cap.observed["limit_usd"] == 240.0
    over = _verdict("MEAL-05", _item("meals", 240.01, attendee_count=4))
    assert not over.in_policy and over.requires_approver == "manager"
    assert "MEAL-05" in over.reason and "$60/head x 4 attendees = $240" in over.reason
    [flag] = _flags(over, "meals")
    assert flag.flag_type == "policy_violation" and flag.severity == "blocking"
    assert flag.justifiable is True and flag.source_clause_ref == "MEAL-05"
    [justified] = _flags(over, "meals", justification="Offsite with the client team.")
    assert justified.severity == "advisory" and justified.justification


def test_meal05_uses_the_verified_city_tier() -> None:
    ny = _verdict("MEAL-05", _item("meals", 180.00, attendee_count=2, city="New York, NY"))
    assert ny.in_policy and ny.observed["tier"] == "tier1" and ny.observed["per_head_cap"] == 90
    over = _verdict("MEAL-05", _item("meals", 180.01, attendee_count=2, city="New York"))
    assert not over.in_policy


def test_meal05_claimed_city_never_clears_but_marks_pending_verification() -> None:
    item = _item("meals", 150.0, attendee_count=2, description="Team dinner in Chicago")
    item = dataclasses.replace(item, claimed_city_text="Team dinner in Chicago")
    v = _verdict("MEAL-05", item)
    assert not v.in_policy  # Tier 3: $120 for two
    assert v.observed["within_cap_if_verified"] is True and v.observed["unverified"] == ["city"]
    [flag] = _flags(v, "meals")
    assert flag.severity == "advisory" and flag.pending_verification == "city"
    assert flag.flag_type == "policy_violation"  # still reaches the approver


def _rule(ref: str, **definition: Any) -> PolicyRule:
    seeded = _BY_REF[ref]
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.UUID(int=0),
        rule_type=PolicyRuleType(seeded["rule_type"]),
        applies_to=PolicyAppliesTo(seeded["applies_to"]),
        definition={**seeded["definition"], **definition},
        source_clause_ref=ref,
        status=PolicyRuleStatus.active,
    )


def _evaluated_refs(rules: list[PolicyRule], item: EvalItem) -> set[str]:
    """Clause refs the engine evaluates for *item* against the rule SET *rules*."""
    return {str(r.source_clause_ref) for r in TOOL._filter_applicable(rules, item)}


def test_team_meal_is_governed_by_meal05_not_the_per_diem() -> None:
    rules = [_rule("MEALS-PD"), _rule("MEAL-05")]
    team = _item("meals", 200.0, attendee_count=3)
    solo = _item("meals", 200.0)
    one = _item("meals", 200.0, attendee_count=1)
    assert _evaluated_refs(rules, team) == {"MEAL-05"}
    assert _evaluated_refs(rules, solo) == {"MEALS-PD"}
    assert _evaluated_refs(rules, one) == {"MEALS-PD"}


def test_a_claimed_attendee_count_never_drops_the_per_diem_without_an_active_meal05() -> None:
    """Review fix (fail closed): with MEAL-05 archived / never seeded, a meals line
    typing attendees=2 keeps the per diem — an attendee count alone never leaves a
    meal with no meal rule at all."""
    team = _item("meals", 400.0, attendee_count=2)
    assert _evaluated_refs([_rule("MEALS-PD")], team) == {"MEALS-PD"}
    # ENT-02 is a per-head cap too, but it reaches entertainment lines, not meals.
    assert _evaluated_refs([_rule("MEALS-PD"), _rule("ENT-02")], team) == {"MEALS-PD"}
    # A MEAL-05 that only covers 3+ people leaves a 2-person meal on the per diem.
    three_plus = [_rule("MEALS-PD"), _rule("MEAL-05", min_attendees=3)]
    assert _evaluated_refs(three_plus, team) == {"MEALS-PD"}
    assert _evaluated_refs(three_plus, _item("meals", 400.0, attendee_count=3)) == {"MEAL-05"}


def test_a_team_meal_within_the_cap_still_needs_a_human_to_confirm_the_attendees() -> None:
    """Review fix: the claimant-typed attendee count switches the per diem off and
    multiplies the cap, and nothing verifies it — an in-policy team meal is an
    approval_required advisory (never auto-approved), never a silent pass."""
    v = _verdict("MEAL-05", _item("meals", 240.00, attendee_count=4))
    assert v.in_policy and v.requires_approver == "manager"
    assert v.observed["could_not_verify"] == "attendees"
    assert "MEAL-05" in v.reason and "attendee" in v.reason
    [flag] = _flags(v, "meals")
    assert flag.flag_type == "approval_required" and flag.severity == "advisory"
    assert flag.requires_approver == "manager"


# ---------------------------------------------------------------------------
# ENT-02 client entertainment
# ---------------------------------------------------------------------------


def test_ent02_per_head_boundaries_and_missing_attendees_fail_closed() -> None:
    at = _verdict("ENT-02", _item("entertainment", 300.00, attendee_count=3))
    assert at.in_policy
    over = _verdict("ENT-02", _item("entertainment", 300.01, attendee_count=3))
    assert not over.in_policy and "ENT-02" in over.reason and "pre-approval" in over.reason
    # No attendee count -> checked as ONE person (the most restrictive limit).
    lone_ok = _verdict("ENT-02", _item("entertainment", 100.00))
    assert lone_ok.in_policy
    lone = _verdict("ENT-02", _item("entertainment", 100.01))
    assert not lone.in_policy and lone.observed["attendee_count_missing"] is True
    assert "No attendee count" in lone.reason
    # Without an attendee claim nothing is unverified; with one the approver confirms it.
    assert lone_ok.requires_approver is None and "could_not_verify" not in lone_ok.observed
    assert at.requires_approver == "manager" and at.observed["could_not_verify"] == "attendees"
    assert "ENT-01" in at.reason
    claimed_one = _verdict("ENT-02", _item("entertainment", 50.00, attendee_count=1))
    assert claimed_one.in_policy and claimed_one.requires_approver == "manager"


def test_ent02_only_reaches_entertainment_lines() -> None:
    assert not _applies("ENT-02", _item("meals", 500.0, attendee_count=2))
    assert not _applies("ENT-02", _item("other", 500.0))
    assert not _applies("ENT-02", EvalItem(item_type="hotel", amount_usd=500.0))


# ---------------------------------------------------------------------------
# ENT-04 gifts
# ---------------------------------------------------------------------------

_DISCLOSED = {
    "recipient": "Pat Client",
    "recipient_organization": "Acme",
    "business_purpose": "Contract renewal thank-you",
    "government_official": False,
}


def test_ent04_gift_at_100_needs_no_disclosure_but_100_01_blocks_until_disclosed() -> None:
    at = _verdict("ENT-04", _item("gift", 100.00))
    assert at.in_policy and at.observed["disclosure_required"] is False
    assert _flags(at, "gift") == []
    over = _verdict("ENT-04", _item("gift", 100.01))
    assert not over.in_policy and "ENT-04" in over.reason
    [flag] = _flags(over, "gift", justification="It was a small token.")
    # A justification never replaces the disclosure.
    assert flag.severity == "blocking" and flag.justifiable is False
    done = _verdict("ENT-04", _item("gift", 100.01, disclosure=_DISCLOSED))
    assert done.in_policy and _flags(done, "gift") == []


def test_ent04_partial_disclosure_lists_what_is_missing() -> None:
    partial = {"recipient": "Pat Client", "business_purpose": "  "}
    v = _verdict("ENT-04", _item("gift", 250.0, disclosure=partial))
    assert not v.in_policy
    assert v.observed["missing_fields"] == [
        "business purpose",
        "whether the recipient is a government official",
    ]


def test_ent04_government_official_needs_legal_compliance_and_never_auto_approves() -> None:
    gift = _verdict("ENT-04", _item("gift", 50.0, disclosure={"government_official": True}))
    assert gift.in_policy and gift.requires_approver == "Legal/Compliance"
    [flag] = _flags(gift, "gift")
    assert flag.flag_type == "approval_required" and flag.severity == "advisory"
    ent = _verdict("ENT-04", _item("entertainment", 80.0, disclosure={"government_official": True}))
    assert ent.requires_approver == "Legal/Compliance" and "pre-approval" in ent.reason


def test_ent04_unknown_amount_requires_the_disclosure() -> None:
    item = _item("gift", 10.0)
    assert item.spend is not None
    item = dataclasses.replace(item, spend=dataclasses.replace(item.spend, amount=None))
    v = _verdict("ENT-04", item)
    assert not v.in_policy and "an unknown amount" in v.reason


# ---------------------------------------------------------------------------
# MIL-01..03 mileage
# ---------------------------------------------------------------------------


def test_compute_mileage_is_decimal_and_rounds_half_up() -> None:
    rate = Decimal("0.70")
    one = compute_mileage(
        distance=Decimal("1"),
        unit="mi",
        round_trip=False,
        commute_offset=Decimal("0"),
        rate_per_mile=rate,
    )
    assert one.amount == Decimal("0.70") and isinstance(one.amount, Decimal)
    tenth = compute_mileage(
        distance=Decimal("0.1"),
        unit="mi",
        round_trip=False,
        commute_offset=Decimal("0"),
        rate_per_mile=Decimal("0.725"),
    )
    assert tenth.amount == Decimal("0.07")  # 0.0725 -> 0.07
    half = compute_mileage(
        distance=Decimal("0.2"),
        unit="mi",
        round_trip=False,
        commute_offset=Decimal("0"),
        rate_per_mile=Decimal("0.725"),
    )
    assert half.amount == Decimal("0.15")  # 0.145 -> 0.15 (HALF_UP, not banker's)
    trip = compute_mileage(
        distance=Decimal("120.5"),
        unit="mi",
        round_trip=True,
        commute_offset=Decimal("20"),
        rate_per_mile=rate,
    )
    assert trip.claimed_miles == Decimal("241.0") and trip.billable_miles == Decimal("221.0")
    assert trip.amount == Decimal("154.70")


def test_kilometres_convert_to_miles() -> None:
    assert to_miles(Decimal("100"), "km") == Decimal("62.1")
    assert to_miles(Decimal("482.8"), "km") == Decimal("300.0")
    assert to_miles(Decimal("483"), "km") == Decimal("300.1")
    with pytest.raises(ValueError):
        to_miles(Decimal("1"), "furlongs")


@pytest.mark.parametrize(
    ("distance", "offset", "rate"),
    [("0", "0", "0.70"), ("10", "-1", "0.70"), ("10", "10", "0.70"), ("10", "0", "0")],
)
def test_compute_mileage_refuses_nothing_to_reimburse(
    distance: str, offset: str, rate: str
) -> None:
    with pytest.raises(ValueError):
        compute_mileage(
            distance=Decimal(distance),
            unit="mi",
            round_trip=False,
            commute_offset=Decimal(offset),
            rate_per_mile=Decimal(rate),
        )


def test_mil01_amount_must_equal_billable_miles_times_rate() -> None:
    ok = _verdict("MIL-01", _item("mileage", 7.00, mileage=TRIP))
    assert ok.in_policy and ok.observed["computed_amount_usd"] == 7.0
    off = _verdict("MIL-01", _item("mileage", 7.01, mileage=TRIP))
    assert not off.in_policy and "MIL-01" in off.reason
    [flag] = _flags(off, "mileage", justification="trust me")
    assert flag.severity == "blocking" and flag.justifiable is False


def test_mil01_blocks_a_mileage_line_not_in_usd() -> None:
    """Review fix (defence in depth): the MIL-01 amount is computed in USD, so the
    same figure labelled in another currency is a blocking violation, never an
    in-policy advisory — even when the number matches the USD computation."""
    v = _verdict("MIL-01", _item("mileage", 7.00, mileage=TRIP, currency="EUR"))
    assert not v.in_policy and "USD" in v.reason and "MIL-01" in v.reason
    assert v.observed["currency"] == "EUR"
    [flag] = _flags(v, "mileage", justification="trust me")
    assert flag.severity == "blocking" and flag.justifiable is False
    restored = restore_spend(
        {
            "spend": dataclasses.asdict(
                spend_facts(category="mileage", amount="7.00", mileage=TRIP, currency="EUR")
            )
        }
    )
    assert restored["spend"].currency == "EUR"


def test_mil01_names_a_stale_rate() -> None:
    stored = {**TRIP, "rate_per_mile": "0.655"}
    v = _verdict("MIL-01", _item("mileage", 6.55, mileage=stored))
    assert not v.in_policy and "$0.655/mi" in v.reason and "$0.70/mi" in v.reason


def test_mil01_a_mileage_line_without_trip_details_is_blocked() -> None:
    v = _verdict("MIL-01", _item("mileage", 50.0))
    assert not v.in_policy and v.observed["mileage_details_missing"] is True


def test_mil02_commute_offset_must_be_recorded() -> None:
    missing = {k: v for k, v in TRIP.items() if k != "commute_offset"}
    v = _verdict("MIL-02", _item("mileage", 7.0, mileage=missing))
    assert not v.in_policy and "MIL-02" in v.reason
    zero = _verdict("MIL-02", _item("mileage", 7.0, mileage=TRIP))
    assert zero.in_policy


def test_a_correct_mileage_line_still_needs_a_human_to_confirm_distance_and_commute() -> None:
    """Review fix: distance and commute offset are the claimant's word (Atlas has no
    route or home / office data), so an in-policy MIL-01 / MIL-02 outcome is an
    approval_required advisory — a mileage line never auto-approves silently."""
    rate = _verdict("MIL-01", _item("mileage", 7.00, mileage=TRIP))
    assert rate.in_policy and rate.requires_approver == "manager"
    assert rate.observed["could_not_verify"] == "distance"
    assert "cannot verify the route distance" in rate.reason
    commute = _verdict("MIL-02", _item("mileage", 7.00, mileage=TRIP))
    assert commute.in_policy and commute.requires_approver == "manager"
    assert commute.observed["could_not_verify"] == "commute_offset"
    for v in (rate, commute):
        [flag] = _flags(v, "mileage")
        assert flag.flag_type == "approval_required" and flag.severity == "advisory"
        assert flag.requires_approver == "manager"


def test_mil03_300_miles_is_within_and_300_1_needs_the_approver() -> None:
    at = _verdict("MIL-03", _item("mileage", 210.0, mileage={**TRIP, "distance": "300"}))
    assert at.in_policy and at.requires_approver is None
    assert _flags(at, "mileage") == []
    over = _verdict("MIL-03", _item("mileage", 210.07, mileage={**TRIP, "distance": "300.1"}))
    assert over.in_policy and over.requires_approver == "manager"
    assert over.observed["could_not_verify"] == "lowest_logical_airfare"
    [flag] = _flags(over, "mileage")
    assert flag.flag_type == "approval_required" and flag.source_clause_ref == "MIL-03"
    impractical = _verdict(
        "MIL-03",
        _item("mileage", 400.0, mileage={**TRIP, "distance": "500", "air_impractical": True}),
    )
    assert "impractical" in impractical.reason and impractical.requires_approver == "manager"
    # Round trip doubles the claim, not the one-way distance MIL-03 measures.
    rt = _verdict(
        "MIL-03",
        _item("mileage", 420.0, mileage={**TRIP, "distance": "300", "round_trip": True}),
    )
    assert rt.in_policy


def test_mileage_needs_no_receipt_but_a_gift_does() -> None:
    receipt = _BY_REF["RECEIPT-REQ"]
    mileage = TOOL.evaluate_candidate(
        "receipt_required", "all", receipt["definition"], _item("mileage", 210.0, mileage=TRIP)
    )
    assert mileage.observed["receipt_required"] is False
    assert mileage.observed["exemption_clause"] == "REF-08"
    flags, _ = verdicts_to_flags([mileage], entry_ref="x", has_receipt=False, category="mileage")
    assert flags == []
    gift = TOOL.evaluate_candidate(
        "receipt_required", "all", receipt["definition"], _item("gift", 30.0)
    )
    assert gift.observed["receipt_required"] is True


# ---------------------------------------------------------------------------
# Reach: the new rules never touch any other line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["MEAL-05", "ENT-02", "ENT-04", "MIL-01", "MIL-02", "MIL-03"])
@pytest.mark.parametrize(
    "item",
    [
        EvalItem(item_type="flight", amount_usd=900.0),
        EvalItem(item_type="hotel", amount_usd=900.0),
        EvalItem(item_type="car", amount_usd=900.0, vehicle_class="Economy"),
        EvalItem(item_type="meals", amount_usd=900.0),
        EvalItem(item_type="receipt", amount_usd=900.0, expense_category="other"),
        EvalItem(item_type="receipt", amount_usd=900.0, expense_category="ground_transport"),
    ],
)
def test_spend_rules_do_not_reach_pre_existing_lines(ref: str, item: EvalItem) -> None:
    assert not _applies(ref, item)


def test_new_categories_are_manual_only() -> None:
    for category in ("mileage", "entertainment", "gift"):
        assert not is_valid_category(category)
    for category in ("airfare", "lodging", "meals", "ground_transport", "other"):
        assert is_valid_category(category)


def test_spend_facts_round_trip_through_asdict() -> None:
    item = _item("mileage", 7.0, mileage=TRIP, attendee_count=None)
    restored = EvalItem(**restore_spend(dataclasses.asdict(item)))
    assert restored.spend == item.spend
    junk = restore_spend({"item_type": "receipt", "spend": "not-a-dict"})
    assert junk["spend"] is None


def test_spend_facts_drop_junk() -> None:
    facts = spend_facts(
        category="meals", amount="abc", attendee_count=True, disclosure=[], mileage="x"
    )
    assert facts.amount is None and facts.attendee_count is None and facts.mileage is None


# ---------------------------------------------------------------------------
# Review fix: the exception list alone carries what the approver must confirm
# ---------------------------------------------------------------------------

_ROUTE_TRIP = {
    **TRIP,
    "origin": "Boston office",
    "destination": "Client site, Providence",
}


@pytest.mark.parametrize(
    ("ref", "amount", "trip"),
    [
        ("MIL-01", 7.00, _ROUTE_TRIP),
        ("MIL-02", 7.00, _ROUTE_TRIP),
        ("MIL-03", 210.07, {**_ROUTE_TRIP, "distance": "300.1"}),
        ("MIL-03", 350.0, {**_ROUTE_TRIP, "distance": "500", "air_impractical": True}),
    ],
)
def test_mileage_human_checks_name_the_route(ref: str, amount: float, trip: dict) -> None:
    """An approver asked to confirm a distance / commute / airfare sees WHICH trip
    in the flag itself (reason + observed), never just an amount."""
    v = _verdict(ref, _item("mileage", amount, mileage=trip))
    assert v.requires_approver == "manager", v
    assert "Boston office → Client site, Providence" in v.reason
    assert v.observed["origin"] == "Boston office"
    assert v.observed["destination"] == "Client site, Providence"


def test_mil01_out_of_policy_also_names_the_route() -> None:
    v = _verdict("MIL-01", _item("mileage", 9.99, mileage=_ROUTE_TRIP))
    assert not v.in_policy
    assert "Boston office → Client site, Providence" in v.reason
    assert v.observed["origin"] == "Boston office"


def test_a_mileage_check_without_a_recorded_route_says_so() -> None:
    bare = {k: v for k, v in TRIP.items() if k not in ("origin", "destination")}
    v = _verdict("MIL-01", _item("mileage", 7.00, mileage=bare))
    assert "route not recorded" in v.reason
    assert v.observed["origin"] is None and v.observed["destination"] is None


def test_government_official_check_names_the_recipient_and_purpose() -> None:
    disclosure = {
        "recipient": "Jane Roe",
        "recipient_organization": "Port Authority",
        "business_purpose": "Customs facility tour",
        "government_official": True,
    }
    v = _verdict("ENT-04", _item("gift", 250.0, disclosure=disclosure))
    assert v.in_policy and v.requires_approver == "Legal/Compliance"
    assert "Jane Roe (Port Authority)" in v.reason
    assert "Customs facility tour" in v.reason
    # Under the threshold the disclosure is partial — still name what is known.
    small = _verdict("ENT-04", _item("gift", 20.0, disclosure={"government_official": True}))
    assert "recipient not recorded" in small.reason
