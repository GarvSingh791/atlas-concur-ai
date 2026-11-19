"""Product gap 2 — entry-level policy rules (hermetic, deterministic evaluators).

LOD-02 hotel tolerance, MEAL-02 partial days, MEAL-04 / per-diem exclusions,
GRND-01 rail class, GRND-03 airport parking, GRND-05 tips — boundary values,
fail-safe "could not verify" outcomes, and the flag each produces.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest

from ai_agents.agents.expense_agent.presubmit import is_justifiable, verdicts_to_flags
from ai_agents.tools import entry_rules
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.corpus import figures as F
from database.seeds.policy_content import (
    ENTRY_RULES,
    HOTEL_CAP_DEFINITION,
    MEALS_PD_DEFINITION,
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


HOTEL = _rule("amount_cap", "hotel", HOTEL_CAP_DEFINITION, "HOTEL-CAP")
MEALS = _rule("per_diem", "meals", MEALS_PD_DEFINITION, "MEALS-PD")
RAIL = _rule("category_allow", "all", RAIL_CLASS_DEFINITION, "GRND-01")
PARKING = _rule("amount_cap", "all", PARKING_DAYS_DEFINITION, "GRND-03")
TIPS = _rule("amount_cap", "all", TIP_PCT_DEFINITION, "GRND-05")


def _verdicts(rule: PolicyRule, item: EvalItem) -> tuple[RuleVerdict, ...]:
    if not TOOL.rule_applies(rule.rule_type, rule.applies_to, rule.definition, item):
        return ()
    return TOOL._evaluate_all([rule], item)


def _one(rule: PolicyRule, item: EvalItem) -> RuleVerdict:
    (v,) = _verdicts(rule, item)
    return v


def _flags(verdicts: tuple[RuleVerdict, ...], category: str, justification: str | None = None):
    flags, _ = verdicts_to_flags(
        list(verdicts),
        entry_ref="x",
        has_receipt=True,
        category=category,
        justification=justification,
    )
    return flags


def _hotel(amount: float, *, booked: bool, city: str = "New York") -> EvalItem:
    return EvalItem(
        item_type="hotel", amount_usd=amount, city=city, nights=2, booked_through_tool=booked
    )


def _ground(amount: float, subtype: str | None, **kw) -> EvalItem:
    # Review round 3: a fixture with a sub-type stands for a line whose sub-type TRUSTED
    # evidence verified (receipt / card / segment); unverified lines are tested in
    # test_pg2_review_fixes_r3.py.
    classified = subtype in ("rideshare_taxi", "car_rental", "rail", "transit", "parking")
    kw.setdefault("ground_verified", classified)
    kw.setdefault("ground_candidates", (subtype,) if classified else ())
    return EvalItem(
        item_type="receipt",
        amount_usd=amount,
        expense_category="ground_transport",
        ground_subtype=subtype,
        **kw,
    )


# ---------------------------------------------------------------------------
# LOD-02 — hotel tolerance
# ---------------------------------------------------------------------------


class TestLod02:
    cap = F.LODGING_CAP["tier1"]  # 300

    def test_exactly_at_cap_is_in_policy_without_flag(self) -> None:
        v = _one(HOTEL, _hotel(float(self.cap), booked=True))
        assert v.in_policy and v.source_clause_ref == "HOTEL-CAP"
        assert _flags((v,), "lodging") == []

    def test_15_00_pct_over_booked_through_tool_is_auto_approved_advisory(self) -> None:
        v = _one(HOTEL, _hotel(345.00, booked=True))  # exactly 15.00% over 300
        assert v.in_policy and v.requires_approver is None
        assert v.source_clause_ref == "LOD-02"
        assert v.observed["over_cap_pct"] == 15.0 and v.observed["within_tolerance"]
        assert "auto-approved" in v.reason and "15.00%" in v.reason
        (flag,) = _flags((v,), "lodging")
        assert flag.flag_type == "policy_advisory" and flag.severity == "advisory"
        assert flag.source_clause_ref == "LOD-02"

    def test_15_01_pct_over_needs_manager(self) -> None:
        v = _one(HOTEL, _hotel(345.03, booked=True))  # 15.01% over 300
        assert not v.in_policy and v.requires_approver == "manager"
        (flag,) = _flags((v,), "lodging")
        assert flag.flag_type == "policy_violation" and flag.severity == "blocking"
        assert flag.justifiable  # LOD-02: > 15% requires manager approval with justification

    def test_within_tolerance_but_not_booked_through_tool_stays_exception(self) -> None:
        v = _one(HOTEL, _hotel(330.00, booked=False))
        assert not v.in_policy and v.requires_approver == "manager"
        assert "only for stays booked through the Atlas travel tool" in v.reason
        assert v.observed["within_tolerance"] and not v.observed["booked_through_tool"]
        assert v.observed["over_cap_pct"] == 10.0

    def test_beyond_tolerance_verdict_is_unchanged(self) -> None:
        """> 15% keeps the pre-existing HOTEL-CAP verdict byte-for-byte (no new keys)."""
        legacy = {k: v for k, v in HOTEL_CAP_DEFINITION.items() if not k.startswith("auto_")}
        before = TOOL.evaluate_candidate("amount_cap", "hotel", legacy, _hotel(400.0, booked=True))
        after = _one(HOTEL, _hotel(400.0, booked=True))
        assert (after.in_policy, after.reason, after.observed) == (
            before.in_policy,
            before.reason,
            before.observed,
        )

    def test_rule_without_tolerance_keys_never_auto_approves(self) -> None:
        legacy = {k: v for k, v in HOTEL_CAP_DEFINITION.items() if not k.startswith("auto_")}
        v = TOOL.evaluate_candidate("amount_cap", "hotel", legacy, _hotel(310.0, booked=True))
        assert not v.in_policy

    def test_property_boundary_matches_exact_decimal_arithmetic(self) -> None:
        rng = random.Random(20261114)
        for _ in range(300):
            cap = rng.choice([160, 225, 300])
            amount = round(cap * rng.uniform(1.0, 1.3), 2)
            t = entry_rules.lodging_tolerance(
                HOTEL_CAP_DEFINITION, amount=amount, cap=cap, booked_through_tool=True
            )
            a = Decimal(str(amount))
            if a <= cap:
                assert t is None
                continue
            assert t is not None
            assert t.within == ((a - cap) * 100 <= 15 * cap)


# ---------------------------------------------------------------------------
# MEAL-02 — partial days
# ---------------------------------------------------------------------------


class TestMeal02:
    rate = F.PER_DIEM["tier1"]  # 85 -> 63.75 on travel days

    def _pooled(self, total: float, days: int, travel: int) -> EvalItem:
        return EvalItem(
            item_type="meals",
            amount_usd=total / days,
            city="New York",
            days=days,
            travel_days=travel,
            stay_amount_usd=total,
        )

    def test_allowance_formula(self) -> None:
        assert entry_rules.per_diem_allowance(85, days=3, travel_days=2, pct=75) == Decimal(
            "212.50"
        )
        assert entry_rules.per_diem_allowance(85, days=1, travel_days=1, pct=75) == Decimal("63.75")
        assert entry_rules.travel_day_count(1) == 1  # same-day trip: one travel day
        assert entry_rules.travel_day_count(2) == 2
        assert entry_rules.travel_day_count(5) == 2

    def test_three_day_trip_exactly_at_allowance(self) -> None:
        v = _one(MEALS, self._pooled(212.50, 3, 2))
        assert v.in_policy
        assert v.observed["allowance_usd"] == 212.5 and v.observed["full_days"] == 1
        assert "MEAL-02" in v.reason

    def test_three_day_trip_one_cent_over_cites_meal02(self) -> None:
        v = _one(MEALS, self._pooled(212.51, 3, 2))
        assert not v.in_policy
        # Within 3 x 85 = 255 at the full rate: the breach is the partial-day rule's.
        assert v.source_clause_ref == "MEAL-02"

    def test_over_full_rate_cites_the_per_diem_rule(self) -> None:
        v = _one(MEALS, self._pooled(300.0, 3, 2))
        assert not v.in_policy and v.source_clause_ref == "MEALS-PD"

    def test_same_day_trip_gets_one_partial_day(self) -> None:
        assert _one(MEALS, self._pooled(63.75, 1, 1)).in_policy
        v = _one(MEALS, self._pooled(63.76, 1, 1))
        assert not v.in_policy and "same-day trip" in v.reason

    def test_unlinked_meal_above_partial_rate_could_not_verify(self) -> None:
        item = EvalItem(item_type="meals", amount_usd=70.0, city="New York")
        v = _one(MEALS, item)
        assert v.in_policy and v.source_clause_ref == "MEAL-02"
        assert v.observed[entry_rules.COULD_NOT_VERIFY] == "travel_day"
        (flag,) = _flags((v,), "meals")
        assert flag.flag_type == "not_evaluated" and flag.severity == "advisory"

    def test_unlinked_meal_at_or_below_partial_rate_has_no_flag(self) -> None:
        v = _one(MEALS, EvalItem(item_type="meals", amount_usd=63.75, city="New York"))
        assert v.in_policy and entry_rules.COULD_NOT_VERIFY not in v.observed
        assert _flags((v,), "meals") == []


# ---------------------------------------------------------------------------
# Exclusions (MEAL-04 alcohol)
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_itemized_alcohol_is_excluded_and_flagged(self) -> None:
        item = EvalItem(
            item_type="meals",
            amount_usd=80.0,
            city="New York",
            line_items=(("Steak frites", 50.0), ("Glass of Malbec wine", 30.0)),
        )
        per_diem, exclusion = _verdicts(MEALS, item)
        assert per_diem.in_policy and per_diem.observed["amount_usd"] == 50.0  # net of wine
        assert not exclusion.in_policy and exclusion.source_clause_ref == "MEAL-04"
        assert exclusion.observed["excluded_usd"] == 30.0
        assert exclusion.observed["reimbursable_usd"] == 50.0
        flags = _flags((per_diem, exclusion), "meals")
        (pv,) = [f for f in flags if f.flag_type == "policy_violation"]
        assert pv.severity == "blocking" and pv.justifiable
        justified = _flags((per_diem, exclusion), "meals", justification="Client dinner")
        assert [f.severity for f in justified if f.flag_type == "policy_violation"] == ["advisory"]

    def test_exclusion_is_taken_out_before_the_per_diem_compare(self) -> None:
        item = EvalItem(
            item_type="meals",
            amount_usd=120.0,
            city="New York",
            line_items=(("Dinner", 60.0), ("Bottle of wine", 60.0)),
        )
        per_diem, _ = _verdicts(MEALS, item)
        assert per_diem.in_policy  # 60 <= 85 once the wine is excluded

    def test_keyword_without_itemization_is_could_not_verify(self) -> None:
        item = EvalItem(item_type="meals", amount_usd=40.0, item_text="Dinner and beers")
        _, exclusion = _verdicts(MEALS, item)
        assert exclusion.in_policy and exclusion.observed["could_not_verify"] == "exclusion_amount"
        (flag,) = [f for f in _flags((exclusion,), "meals")]
        assert flag.flag_type == "not_evaluated" and flag.source_clause_ref == "MEAL-04"

    @pytest.mark.parametrize("text", ["Root beer float", "Ginger ale", "Non-alcoholic beer"])
    def test_not_excluded_phrases(self, text: str) -> None:
        item = EvalItem(item_type="meals", amount_usd=10.0, item_text=text)
        assert len(_verdicts(MEALS, item)) == 1

    def test_generic_exclusion_term_works_for_any_rule(self) -> None:
        defn = {**MEALS_PD_DEFINITION, "exclusions": ["minibar", "spa"]}
        rule = _rule("per_diem", "meals", defn, "MEALS-PD")
        item = EvalItem(item_type="meals", amount_usd=30.0, line_items=(("Spa treatment", 30.0),))
        _, exclusion = _verdicts(rule, item)
        assert exclusion.observed["excluded_terms"] == ["spa"]


# ---------------------------------------------------------------------------
# GRND-01 — rail class
# ---------------------------------------------------------------------------


class TestGrnd01Rail:
    def test_reaches_only_classified_rail_lines(self) -> None:
        for subtype in ("rideshare_taxi", "parking", "ground_transport", None):
            assert _verdicts(RAIL, _ground(90.0, subtype, travel_class="first")) == ()
        assert _verdicts(RAIL, _ground(90.0, "rail")) != ()

    def test_standard_class_is_in_policy(self) -> None:
        v = _one(RAIL, _ground(90.0, "rail", travel_class="standard", duration_hours=1.0))
        assert v.in_policy and v.source_clause_ref == "GRND-01" and not _flags((v,), "g")

    def test_first_class_under_3h_is_a_justifiable_violation(self) -> None:
        v = _one(RAIL, _ground(200.0, "rail", travel_class="first", duration_hours=2.99))
        assert not v.in_policy and v.requires_approver == "manager"
        assert "under 3 hours" in v.reason
        (flag,) = _flags((v,), "ground_transport")
        assert flag.flag_type == "policy_violation" and flag.justifiable

    def test_first_class_at_exactly_3h_is_conditional(self) -> None:
        v = _one(RAIL, _ground(200.0, "rail", travel_class="first", duration_hours=3.0))
        assert not v.in_policy and v.observed["condition"]
        assert "cheaper than or comparable to flying" in v.reason

    def test_first_class_unknown_duration_is_not_cleared(self) -> None:
        v = _one(RAIL, _ground(200.0, "rail", travel_class="business"))
        assert not v.in_policy and "could not be verified" in v.reason

    def test_unknown_class_could_not_verify(self) -> None:
        v = _one(RAIL, _ground(90.0, "rail"))
        assert v.in_policy and v.observed["could_not_verify"] == "travel_class"
        (flag,) = _flags((v,), "ground_transport")
        assert flag.flag_type == "not_evaluated" and flag.source_clause_ref == "GRND-01"

    def test_justifiable_guard_is_what_unblocks_rail(self) -> None:
        """Guard: category_allow is not a JUSTIFIABLE rule type — only the explicit
        ``justifiable_exception`` marker lets a manager accept a rail-class breach."""
        v = _one(RAIL, _ground(200.0, "rail", travel_class="first", duration_hours=1.0))
        assert is_justifiable(v)
        stripped = RuleVerdict(**{**v.__dict__, "observed": {}})
        assert not is_justifiable(stripped)


# ---------------------------------------------------------------------------
# GRND-03 — airport parking
# ---------------------------------------------------------------------------


def _airport_parking(amount: float, **kw) -> EvalItem:
    return _ground(amount, "parking", airport_parking=True, **kw)


class TestGrnd03Parking:
    # Review fix: GRND-03 reaches AIRPORT parking only — the fixtures say so explicitly
    # (``airport_parking=True``); the plain-parking cases are below.
    def test_7_days_is_within(self) -> None:
        v = _one(PARKING, _airport_parking(140.0, parking_days=7))
        assert v.in_policy and not _flags((v,), "ground_transport")

    def test_8_days_is_over_with_pro_rata_share(self) -> None:
        v = _one(PARKING, _airport_parking(160.0, parking_days=8))
        assert not v.in_policy and v.requires_approver == "manager"
        assert v.observed["reimbursable_usd"] == 140.0
        assert "7 of 8 days ($140.00 of $160.00)" in v.reason

    def test_premium_lot_is_an_exception(self) -> None:
        v = _one(PARKING, _airport_parking(60.0, parking_days=2, item_text="SFO valet parking"))
        assert not v.in_policy and v.observed["premium_lot"] == "valet"

    def test_unknown_days_could_not_verify(self) -> None:
        v = _one(PARKING, _airport_parking(60.0))
        assert v.in_policy and v.observed["could_not_verify"] == "parking_days"

    def test_trip_length_is_context_never_parking_days(self) -> None:
        """Guard: the linked trip's length is NOT how long the car was parked."""
        v = _one(PARKING, _airport_parking(225.0, trip_days=9))
        assert v.in_policy and v.observed["could_not_verify"] == "parking_days"
        assert "parking_days" not in v.observed and "reimbursable_usd" not in v.observed
        assert "trip spans 9 days" in v.reason
        (flag,) = _flags((v,), "ground_transport")
        assert flag.flag_type == "not_evaluated" and flag.severity == "advisory"

    def test_claimed_days_never_clear_but_count_against(self) -> None:
        within = _one(PARKING, _airport_parking(60.0, claimed_days=3))
        assert within.in_policy and within.observed["could_not_verify"] == "parking_days"
        over = _one(PARKING, _airport_parking(200.0, claimed_days=10))
        assert not over.in_policy and over.observed["days_source"] == "stated on the entry"

    @pytest.mark.parametrize(
        "kw",
        [
            {"item_text": "Marriott Downtown | Hotel valet parking - client visit"},
            {"item_text": "City Garage | Hourly parking near client office"},
            {"parking_days": 10, "item_text": "Hilton parking"},
            {"claimed_days": 12},
        ],
    )
    def test_plain_parking_is_not_reached(self, kw: dict) -> None:
        """Guard: GRND-03 is an airport-parking clause — hotel valet, a city garage and a
        long city stay are parking, but the clause never reaches them."""
        assert _verdicts(PARKING, _ground(60.0, "parking", **kw)) == ()


# ---------------------------------------------------------------------------
# GRND-05 — tips
# ---------------------------------------------------------------------------


def _ride(amount: float, **kw) -> EvalItem:
    """A rideshare line claiming the whole receipt: the OCR total equals the claim (review
    round 3 — the fare is the RECEIPT total minus the tip, never the claim minus it)."""
    return _ground(amount, "rideshare_taxi", receipt_total_usd=amount, **kw)


class TestGrnd05Tips:
    def test_tip_exactly_20_pct_is_within(self) -> None:
        v = _one(TIPS, _ride(12.0, tip_usd=2.0))  # 2 / 10 = 20.00%
        assert v.in_policy and "20.00%" in v.reason

    def test_tip_20_01_pct_is_over(self) -> None:
        # fare 99.95, tip 20.00 -> 20.01%
        v = _one(TIPS, _ride(119.95, tip_usd=20.0))
        assert not v.in_policy and "20.01%" in v.reason
        assert v.observed["reimbursable_tip_usd"] == 19.99

    def test_tip_from_line_items(self) -> None:
        item = _ground(30.0, "rideshare_taxi", line_items=(("Fare", 20.0), ("Tip", 10.0)))
        v = _one(TIPS, item)
        assert not v.in_policy and v.observed["tip_usd"] == 10.0

    def test_no_tip_is_not_applicable(self) -> None:
        v = _one(TIPS, _ground(30.0, "rideshare_taxi"))
        assert v.in_policy and v.observed["tip_identified"] is False
        assert _flags((v,), "ground_transport") == []

    def test_tip_mentioned_without_amount_could_not_verify(self) -> None:
        v = _one(TIPS, _ground(30.0, "rideshare_taxi", item_text="Uber incl. tip"))
        assert v.in_policy and v.observed["could_not_verify"] == "tip_amount"

    def test_tips_rule_does_not_reach_other_ground_lines(self) -> None:
        assert _verdicts(TIPS, _ground(30.0, "rail", tip_usd=20.0)) == ()

    def test_property_tip_boundary(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            fare = Decimal(rng.randint(100, 20000)) / 100
            tip = Decimal(rng.randint(0, 800)) / 100
            v = _one(TIPS, _ride(float(fare + tip), tip_usd=float(tip)))
            assert v.in_policy == (tip * 100 <= 20 * fare)


# ---------------------------------------------------------------------------
# Generic sub-type caps (gap 8) and the seeds
# ---------------------------------------------------------------------------


def test_rideshare_cap_does_not_reach_rail_or_parking_but_checks_unclassified() -> None:
    defn = {
        "currency": "USD",
        "per": "item",
        "expense_categories": ["ground_transport"],
        "ground_subtypes": ["rideshare_taxi"],
        "tiers": [{"tier": "standard", "cap": 50}],
        "exception_approver": "manager",
    }
    rule = _rule("amount_cap", "all", defn, "GRND-02")
    assert _verdicts(rule, _ground(90.0, "rail")) == ()
    assert _verdicts(rule, _ground(90.0, "parking")) == ()
    assert not _one(rule, _ground(90.0, "rideshare_taxi")).in_policy
    broad = _one(rule, _ground(90.0, "ground_transport"))
    assert not broad.in_policy and "could not tell which kind" in broad.reason


def test_entry_rules_seed_shape() -> None:
    assert [r["clause_ref"] for r in ENTRY_RULES] == ["GRND-01", "GRND-03", "GRND-05"]
    for r in ENTRY_RULES:
        assert entry_rules.KIND_RULE_TYPES[r["definition"]["kind"]] == r["rule_type"]
        assert r["applies_to"] == "all"
    assert HOTEL_CAP_DEFINITION["auto_approve_over_cap_pct"] == F.LODGING_CAP_AUTO_EXCEPTION_PCT
    assert MEALS_PD_DEFINITION["partial_day_pct"] == F.PER_DIEM_PARTIAL_PCT
    assert PARKING_DAYS_DEFINITION["max_days"] == F.AIRPORT_PARKING_MAX_DAYS
    assert TIP_PCT_DEFINITION["max_tip_pct"] == F.TIP_MAX_PCT
    assert RAIL_CLASS_DEFINITION["premium_min_hours"] == F.RAIL_FIRST_MIN_HOURS


def test_malformed_kind_rule_is_an_honest_skip() -> None:
    bad = {**PARKING_DAYS_DEFINITION, "max_days": "seven"}
    v = TOOL.evaluate_candidate(
        "amount_cap", "all", bad, _ground(10.0, "parking", airport_parking=True)
    )
    assert v.observed == {} and v.in_policy  # -> not_evaluated advisory, never a pass
    (flag,) = _flags((v,), "ground_transport")
    assert flag.flag_type == "not_evaluated"
