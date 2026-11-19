"""Product gap 2 / 8 — review fixes, round 3 (hermetic).

* Partial claims: a claimant may claim only part of a receipt (the food without the wine,
  the fare without the tip).  MEAL-04 and GRND-05 compare the CLAIM with what the RECEIPT
  read, never the claim with itself, and never subtract the wine twice.
* Ground-transport trust: the sub-type is not decided by the claimant's words alone.
"""

from __future__ import annotations

import uuid

from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item,
    build_eval_item_from_fields,
    verdicts_to_flags,
)
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import (
    ENTRY_RULES,
    MEALS_PD_DEFINITION,
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


MEALS = _rule("per_diem", "meals", MEALS_PD_DEFINITION, "MEALS-PD")
TIPS = _rule("amount_cap", "all", TIP_PCT_DEFINITION, "GRND-05")
RAIL = _rule("category_allow", "all", RAIL_CLASS_DEFINITION, "GRND-01")
RIDESHARE_CAP = _rule(
    "amount_cap",
    "all",
    {
        "currency": "USD",
        "per": "item",
        "expense_categories": ["ground_transport"],
        "ground_subtypes": ["rideshare_taxi"],
        "tiers": [{"tier": "standard", "cap": 50}],
        "exception_approver": "manager",
    },
    "GRND-02",
)


def _verdicts(rule: PolicyRule, item: EvalItem) -> tuple[RuleVerdict, ...]:
    if not TOOL.rule_applies(rule.rule_type, rule.applies_to, rule.definition, item):
        return ()
    return TOOL._evaluate_all([rule], item)


def _flags(verdicts: tuple[RuleVerdict, ...]) -> list:
    flags, _ = verdicts_to_flags(
        list(verdicts), entry_ref="x", has_receipt=True, category="meals", justification=None
    )
    return flags


def _meal(claim: float, *lines: tuple[str, float], **kw) -> EvalItem:
    return EvalItem(item_type="meals", amount_usd=claim, line_items=tuple(lines) or None, **kw)


def _by_clause(verdicts: tuple[RuleVerdict, ...], clause: str) -> list[RuleVerdict]:
    return [v for v in verdicts if v.source_clause_ref == clause]


# ---------------------------------------------------------------------------
# MEAL-04 — the excluded amount inside the claim
# ---------------------------------------------------------------------------


class TestMeal04PartialClaims:
    def test_claim_without_the_wine_gets_no_meal04_and_the_full_compare(self) -> None:
        """(a) claim 50 of a pasta-50 + wine-20 receipt: nothing alcoholic is claimed."""
        vs = _verdicts(MEALS, _meal(50.0, ("Pasta", 50.0), ("House red wine", 20.0)))
        per_diem = vs[0]
        assert per_diem.observed["amount_usd"] == 50.0  # compared on the full claim
        (meal04,) = _by_clause(vs, "MEAL-04")
        assert meal04.in_policy and meal04.observed["excluded_usd"] == 0.0
        assert meal04.observed["receipt_excluded_usd"] == 20.0
        assert not [f for f in _flags((meal04,))]  # no flag at all

    def test_claim_of_the_food_over_the_per_diem_still_breaches(self) -> None:
        """(b) claim 85 of a steak-85 + wine-20 receipt: the per-diem overage is NOT hidden
        by subtracting wine that was never claimed."""
        vs = _verdicts(MEALS, _meal(85.0, ("Steak dinner", 85.0), ("Glass of wine", 20.0)))
        per_diem = vs[0]
        assert not per_diem.in_policy and per_diem.observed["amount_usd"] == 85.0
        assert all(v.in_policy for v in _by_clause(vs, "MEAL-04"))

    def test_claim_including_the_wine_is_flagged_and_compared_net(self) -> None:
        """(c) claim 105 = steak 85 + wine 20: MEAL-04 violation, reimbursable 85, per
        diem compared on 85."""
        vs = _verdicts(MEALS, _meal(105.0, ("Steak dinner", 85.0), ("Glass of wine", 20.0)))
        assert vs[0].observed["amount_usd"] == 85.0
        (meal04,) = _by_clause(vs, "MEAL-04")
        assert not meal04.in_policy
        assert meal04.observed["excluded_usd"] == 20.0
        assert meal04.observed["reimbursable_usd"] == 85.0
        assert "$20.00 of the claimed amount" in meal04.reason

    def test_reviewer_probe_claim_40_of_steak_40_wine_20(self) -> None:
        vs = _verdicts(MEALS, _meal(40.0, ("Steak", 40.0), ("Glass of wine", 20.0)))
        assert vs[0].observed["amount_usd"] == 40.0
        assert all(v.in_policy for v in _by_clause(vs, "MEAL-04"))

    def test_part_of_the_wine_claimed(self) -> None:
        vs = _verdicts(MEALS, _meal(95.0, ("Steak dinner", 85.0), ("Glass of wine", 20.0)))
        (meal04,) = _by_clause(vs, "MEAL-04")
        assert not meal04.in_policy and meal04.observed["excluded_usd"] == 10.0
        assert meal04.observed["reimbursable_usd"] == 85.0

    def test_receipt_total_with_tax_is_the_non_excluded_base(self) -> None:
        """Food 40 + beer 20 + tax 6 = 66 read by OCR.

        Integration (security review, intended change): the tax is split PRO RATA — 4 on
        the food, 2 on the beer — so the food's own share is 44.  A claim of 44 (food +
        its tax) holds no alcohol; the line items alone (40) would wrongly see 4 of beer
        in it.  The round-3 reading (total - beer = 46) let the beer's 2 of tax hide in
        the claim: a claim of 46 now holds 2 of excluded money."""
        item = _meal(44.0, ("Burger", 40.0), ("Craft beer", 20.0), receipt_total_usd=66.0)
        (meal04,) = _by_clause(_verdicts(MEALS, item), "MEAL-04")
        assert meal04.in_policy and meal04.observed["excluded_usd"] == 0.0
        taxed = _meal(46.0, ("Burger", 40.0), ("Craft beer", 20.0), receipt_total_usd=66.0)
        (meal04,) = _by_clause(_verdicts(MEALS, taxed), "MEAL-04")
        assert not meal04.in_policy and meal04.observed["excluded_usd"] == 2.0
        assert meal04.observed["reimbursable_usd"] == 44.0
        full = _meal(66.0, ("Burger", 40.0), ("Craft beer", 20.0), receipt_total_usd=66.0)
        (meal04,) = _by_clause(_verdicts(MEALS, full), "MEAL-04")
        assert not meal04.in_policy and meal04.observed["reimbursable_usd"] == 44.0
        assert meal04.observed["receipt_excluded_with_tax_tip_usd"] == 22.0

    def test_pooled_trip_total_subtracts_only_what_the_line_claimed(self) -> None:
        lines = (("Steak dinner", 85.0), ("Glass of wine", 20.0))
        base = {"city": "New York", "days": 3, "travel_days": 2, "stay_amount_usd": 300.0}
        food_only = _meal(100.0, *lines, line_claim_usd=85.0, **base)
        assert _verdicts(MEALS, food_only)[0].observed["total_usd"] == 300.0
        with_wine = _meal(100.0, *lines, line_claim_usd=105.0, **base)
        assert _verdicts(MEALS, with_wine)[0].observed["total_usd"] == 280.0

    def test_pooled_line_claim_unknown_fails_closed(self) -> None:
        """Without the line's own claim the whole excluded amount counts as claimed:
        flagged (never hidden)."""
        item = _meal(
            100.0,
            ("Steak dinner", 85.0),
            ("Glass of wine", 20.0),
            city="New York",
            days=3,
            travel_days=2,
            stay_amount_usd=300.0,
        )
        (meal04,) = _by_clause(_verdicts(MEALS, item), "MEAL-04")
        assert not meal04.in_policy and meal04.observed["excluded_usd"] == 20.0


# ---------------------------------------------------------------------------
# GRND-05 — the fare comes from the receipt, the tip must be in the claim
# ---------------------------------------------------------------------------


def _ride(claim: float, **kw) -> EvalItem:
    kw.setdefault("ground_subtype", "rideshare_taxi")
    kw.setdefault("ground_candidates", ("rideshare_taxi",))
    kw.setdefault("ground_verified", True)
    return EvalItem(
        item_type="receipt", amount_usd=claim, expense_category="ground_transport", **kw
    )


def _tip(item: EvalItem) -> RuleVerdict:
    (v,) = _verdicts(TIPS, item)
    return v


class TestGrnd05PartialClaims:
    def test_reviewer_probe_fare_only_claim_with_a_20pct_tip(self) -> None:
        """Fare 50 claimed, a 10 tip (20%) on a 60 receipt: within — the fare is NOT
        claim - tip (= 40, which made it 25%)."""
        v = _tip(_ride(50.0, tip_usd=10.0, receipt_total_usd=60.0))
        assert v.in_policy and v.observed["fare_usd"] == 50.0 and "20.00%" in v.reason

    def test_receipt_total_unknown_is_could_not_verify(self) -> None:
        v = _tip(_ride(50.0, tip_usd=10.0))
        assert v.in_policy and v.observed["could_not_verify"] == "fare"

    def test_over_limit_tip_not_claimed_is_in_policy(self) -> None:
        v = _tip(_ride(50.0, tip_usd=15.0, receipt_total_usd=65.0))  # 30% tip, fare claimed
        assert v.in_policy and v.observed["tip_claimed_usd"] == 0.0
        flags, _ = verdicts_to_flags(
            [v], entry_ref="x", has_receipt=True, category="ground_transport"
        )
        assert flags == []

    def test_over_limit_tip_claimed_up_to_the_allowance_is_in_policy(self) -> None:
        v = _tip(_ride(60.0, tip_usd=15.0, receipt_total_usd=65.0))
        assert v.in_policy and v.observed["tip_claimed_usd"] == 10.0

    def test_over_limit_tip_claimed_beyond_the_allowance_is_a_violation(self) -> None:
        v = _tip(_ride(65.0, tip_usd=15.0, receipt_total_usd=65.0))
        assert not v.in_policy and v.observed["reimbursable_tip_usd"] == 10.0
        assert "30.00%" in v.reason and "$60.00 with the fare" in v.reason

    def test_tip_line_items_give_the_total(self) -> None:
        item = _ride(20.0, line_items=(("Fare", 20.0), ("Tip", 10.0)))
        v = _tip(item)  # fare-only claim of a 50% tip ride
        assert v.in_policy and v.observed["fare_usd"] == 20.0
        assert not _tip(_ride(30.0, line_items=(("Fare", 20.0), ("Tip", 10.0)))).in_policy


# ---------------------------------------------------------------------------
# Wiring: the receipt total reaches the EvalItem (never the claim)
# ---------------------------------------------------------------------------


def test_receipt_total_helper_respects_provenance() -> None:
    assert entry_context.receipt_total({"amount": 60.0}, {}) == 60.0
    assert entry_context.receipt_total({"amount": 60.0}, {"amount": "deterministic"}) == 60.0
    assert entry_context.receipt_total({"amount": 60.0}, {"amount": "user_edited"}) is None
    assert entry_context.receipt_total({"amount": 60.0}, {"amount": "ai_inferred"}) is None
    assert entry_context.receipt_total({"amount": 0}, {}) is None


def test_from_fields_reads_the_receipt_evidence_not_the_overwritten_amount() -> None:
    """The approval refresh path overwrites ``fields['amount']`` with the claim; the
    receipt total and words are captured BEFORE that (``with_receipt_evidence``)."""
    ocr = {"amount": 60.0, "vendor": "Uber", "tip": 10.0}
    prov = {"vendor": "deterministic", "amount": "deterministic"}
    fields = entry_context.with_receipt_evidence(ocr, prov)
    fields["amount"] = 50.0  # the claim
    fields["vendor"] = "Amtrak"  # what the claimant typed on the entry
    item = build_eval_item_from_fields(
        fields, category="ground_transport", ref_id="r", provenance=prov
    )
    assert item.receipt_total_usd == 60.0 and item.amount_usd == 50.0
    assert "rideshare_taxi" in item.ground_candidates and not item.ground_verified
    meal = build_eval_item_from_fields(
        entry_context.with_receipt_evidence({"amount": 66.0}, {}),
        category="meals",
        ref_id="m",
    )
    assert meal.receipt_total_usd == 66.0


# ---------------------------------------------------------------------------
# Security carry-over: ground sub-type is not decided by typed words alone
# ---------------------------------------------------------------------------


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


class TestGroundTrust:
    def test_typed_words_alone_do_not_verify_the_subtype(self) -> None:
        item = build_eval_item(_entry("Amtrak", "Train to client"))
        assert item.ground_subtype == "rail" and not item.ground_verified
        # an authored rideshare cap still reaches a typed-only line (it may be a ride)
        assert _verdicts(RIDESHARE_CAP, _with(item, amount_usd=90.0)) != ()

    def test_trusted_evidence_verifies_the_subtype(self) -> None:
        fields = entry_context.ground_fields("Amtrak", trusted_texts=("AMTRAK 1234 NEW YORK",))
        assert fields["ground_subtype"] == "rail" and fields["ground_verified"] is True
        item = EvalItem(
            item_type="receipt",
            amount_usd=90.0,
            expense_category="ground_transport",
            **fields,
        )
        assert _verdicts(RIDESHARE_CAP, item) == ()  # a verified train is not a rideshare

    def test_disagreeing_evidence_applies_every_candidate_kind_rule(self) -> None:
        """Typed "Amtrak" over an Uber card charge with a 50% tip on the receipt: the line
        is ambiguous, and BOTH the rail rule and the tip rule run (never skipped)."""
        fields = entry_context.ground_fields("Amtrak train", trusted_texts=("UBER *TRIP",))
        assert fields["ground_verified"] is False
        assert set(fields["ground_candidates"]) == {"rail", "rideshare_taxi"}
        item = EvalItem(
            item_type="receipt",
            amount_usd=30.0,
            expense_category="ground_transport",
            tip_usd=10.0,
            receipt_total_usd=30.0,
            **fields,
        )
        assert _verdicts(TIPS, item) != () and not _verdicts(TIPS, item)[0].in_policy
        assert _verdicts(RAIL, item) != ()
        assert _verdicts(RIDESHARE_CAP, item) != ()

    def test_typed_only_line_with_a_receipt_tip_still_gets_grnd05(self) -> None:
        """A typed "Train" with a tip read from the receipt: the tip is trusted evidence
        for GRND-05, so the tip rule runs although the words say rail."""
        fields = entry_context.ground_fields("Train ticket")
        item = EvalItem(
            item_type="receipt",
            amount_usd=30.0,
            expense_category="ground_transport",
            tip_usd=10.0,
            receipt_total_usd=30.0,
            **fields,
        )
        assert _verdicts(TIPS, item) != ()

    def test_verified_lines_are_not_given_other_kind_rules(self) -> None:
        fields = entry_context.ground_fields("Uber", trusted_texts=("UBER *TRIP",))
        item = EvalItem(
            item_type="receipt", amount_usd=30.0, expense_category="ground_transport", **fields
        )
        assert fields["ground_verified"] is True
        assert _verdicts(RAIL, item) == ()

    def test_seeded_kind_rules_cover_the_candidates(self) -> None:
        kinds = {r["definition"]["kind"] for r in ENTRY_RULES}
        assert kinds == {"rail_class", "parking_days", "tip_pct"}


def _with(item: EvalItem, **kw) -> EvalItem:
    import dataclasses

    return dataclasses.replace(item, **kw)


def test_shared_receipt_never_assumes_the_rest_is_left_out() -> None:
    """Itemized siblings split ONE receipt: the wine-only sibling must still be flagged
    (its food is claimed by the other line), and so must a tip-only sibling."""
    wine_line = _meal(
        20.0,
        ("Steak dinner", 85.0),
        ("Glass of wine", 20.0),
        receipt_total_usd=105.0,
        receipt_shared=True,
    )
    (meal04,) = _by_clause(_verdicts(MEALS, wine_line), "MEAL-04")
    assert not meal04.in_policy and meal04.observed["excluded_usd"] == 20.0
    tip_line = _ride(15.0, tip_usd=15.0, receipt_total_usd=65.0, receipt_shared=True)
    assert not _tip(tip_line).in_policy
