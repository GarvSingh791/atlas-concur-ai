"""Integration (fxcard x policy tracks) — every amount the new rules compare goes through
the same currency conversion as the existing caps (hermetic).

fxcard converts a line's amounts to the rule's currency at the ONE policy-evaluation
choke point (``fx_policy.evaluate_with_fx``).  The policy tracks added rules that
compared a line in its OWN currency against USD figures: ENT-04's $100 gift threshold,
the MEAL-05 / ENT-02 per-head caps, EXP-04's $75 affidavit ceiling, and the receipt
figures MEAL-04 / GRND-05 compare with the (already converted) claim.  Reference rates
(``config.constants.fx_rates``): EUR 1.085, JPY 0.0067 USD per unit.

Boundaries (USD = amount x rate, cents ROUND_HALF_UP):
  EUR 92.17 -> 100.00 (not over $100)   EUR 92.18 -> 100.02 (over)
  JPY 14926 -> 100.00 (not over $100)   JPY 14927 -> 100.01 (over)
  EUR 110.60 -> 120.00 (= 2 x $60)      EUR 110.61 -> 120.01 (over)
  EUR 69.12 -> 75.00 (= $75 ceiling)    EUR 69.13 -> 75.01 (over)
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    CURRENCY_UNVERIFIED_FLAG,
    build_eval_item,
    verdicts_to_flags,
    with_item_currency,
)
from ai_agents.tools import process_rules as pr
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import MEALS_PD_DEFINITION, RULES, TIP_PCT_DEFINITION

TOOL = PolicyEvalTool()
_BY_REF = {r["clause_ref"]: r for r in RULES}
RECEIPT_REQ = _BY_REF["RECEIPT-REQ"]["definition"]


def _rule(ref: str) -> PolicyRule:
    r = _BY_REF[ref]
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType(r["rule_type"]),
        applies_to=PolicyAppliesTo(r["applies_to"]),
        definition=r["definition"],
        source_clause_ref=ref,
    )


def _raw_rule(rule_type: str, applies_to: str, definition: dict, ref: str) -> PolicyRule:
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType(rule_type),
        applies_to=PolicyAppliesTo(applies_to),
        definition=definition,
        source_clause_ref=ref,
    )


GIFT = _rule("ENT-04")
TEAM_MEAL = _rule("MEAL-05")
MEALS = _raw_rule("per_diem", "meals", MEALS_PD_DEFINITION, "MEALS-PD")
TIPS = _raw_rule("amount_cap", "all", TIP_PCT_DEFINITION, "GRND-05")


def _entry(category: str, amount: float, currency: str, **kw: Any) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency=currency,
        expense_date="2026-11-02",
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


def _item(category: str, amount: float, currency: str, **kw: Any) -> EvalItem:
    entry = _entry(category, amount, currency, **kw)
    # The same two steps presubmit takes: build the item, tag it with the currency.
    return with_item_currency(build_eval_item(entry, segment=None), entry.currency)


def _verdicts(rule: PolicyRule, item: EvalItem) -> tuple[RuleVerdict, ...]:
    assert TOOL.rule_applies(rule.rule_type, rule.applies_to, rule.definition, item)
    return TOOL._evaluate_all([rule], item)


def _gift(amount: float, currency: str, **disclosure: Any) -> RuleVerdict:
    (v,) = _verdicts(GIFT, _item("gift", amount, currency, disclosure=disclosure or None))
    return v


def _flags(verdicts: tuple[RuleVerdict, ...], category: str) -> list[Any]:
    flags, _ = verdicts_to_flags(
        list(verdicts), entry_ref="x", has_receipt=True, category=category, justification=None
    )
    return flags


# ---------------------------------------------------------------------------
# ENT-04 — the $100 gift threshold
# ---------------------------------------------------------------------------


class TestGiftThreshold:
    def test_reviewer_probe_eur_95_needs_the_disclosure(self) -> None:
        """EUR 95 ≈ USD 103.08 is over $100: the disclosure is required (blocking)."""
        v = _gift(95.0, "EUR")
        assert not v.in_policy and v.observed["disclosure_required"] is True
        assert v.observed["fx"]["converted_amount"] == "103.08"
        assert "EUR 95.00 ≈ USD 103.08 at 1.085" in v.reason

    @pytest.mark.parametrize(
        ("amount", "currency", "needed"),
        [
            (92.17, "EUR", False),  # 100.00
            (92.18, "EUR", True),  # 100.02
            (14926, "JPY", False),  # 100.00
            (14927, "JPY", True),  # 100.01
        ],
    )
    def test_boundaries(self, amount: float, currency: str, needed: bool) -> None:
        v = _gift(amount, currency)
        assert v.observed["disclosure_required"] is needed
        assert v.in_policy is (not needed)

    def test_usd_is_byte_for_byte_unchanged(self) -> None:
        for amount in (100.00, 100.01):
            tagged = _gift(amount, "USD")
            (untagged,) = _verdicts(
                GIFT, build_eval_item(_entry("gift", amount, "USD"), segment=None)
            )
            assert dataclasses.replace(tagged, rule_id="") == dataclasses.replace(
                untagged, rule_id=""
            )
            assert "fx" not in tagged.observed and "[" not in tagged.reason

    def test_unsupported_currency_requires_the_disclosure_and_escalates(self) -> None:
        """XYZ cannot be converted: the amount is treated as unknown (disclosure
        required — fail closed) and the line also gets the escalating
        ``currency_unverified`` flag.  Never a silent pass."""
        v = _gift(50.0, "XYZ")
        assert not v.in_policy and v.observed["disclosure_required"] is True
        flags = _flags((v,), "gift")
        assert {f.flag_type for f in flags} == {CURRENCY_UNVERIFIED_FLAG, "policy_violation"}
        complete = _gift(
            50.0,
            "XYZ",
            recipient="Ana",
            business_purpose="Thanks",
            government_official=False,
        )
        assert complete.in_policy
        assert [f.flag_type for f in _flags((complete,), "gift")] == [CURRENCY_UNVERIFIED_FLAG]


# ---------------------------------------------------------------------------
# MEAL-05 — per-head caps
# ---------------------------------------------------------------------------


class TestPerHeadCap:
    @pytest.mark.parametrize(("amount", "in_policy"), [(110.60, True), (110.61, False)])
    def test_eur_team_meal_at_the_tier3_cap(self, amount: float, in_policy: bool) -> None:
        (v,) = _verdicts(TEAM_MEAL, _item("meals", amount, "EUR", attendee_count=2))
        assert v.in_policy is in_policy, v.reason
        assert v.observed["limit_usd"] == 120.0

    def test_unsupported_currency_is_could_not_verify(self) -> None:
        (v,) = _verdicts(TEAM_MEAL, _item("meals", 10.0, "XYZ", attendee_count=2))
        assert v.in_policy and v.observed["fx_status"] == "currency_not_supported"
        assert [f.flag_type for f in _flags((v,), "meals")] == [CURRENCY_UNVERIFIED_FLAG]

    def test_usd_unchanged(self) -> None:
        (v,) = _verdicts(TEAM_MEAL, _item("meals", 120.01, "USD", attendee_count=2))
        assert not v.in_policy and "fx" not in v.observed


# ---------------------------------------------------------------------------
# EXP-04 — the $75 affidavit ceiling
# ---------------------------------------------------------------------------


class TestAffidavitCeiling:
    def _finding(self, amount: Any, currency: str | None) -> pr.Finding:
        return pr.affidavit(
            {**RECEIPT_REQ, "affidavit_max_usd": 75},
            amount=amount,
            signed=True,
            justification="Receipt lost",
            prior_in_quarter=0,
            quarter="Q4 2026",
            currency=currency,
        )

    @pytest.mark.parametrize(
        ("amount", "currency", "accepted"),
        [
            ("69.12", "EUR", True),  # 75.00
            ("69.13", "EUR", False),  # 75.01
            ("11194", "JPY", True),  # 75.00 (11194 x 0.0067 = 74.9998)
            ("11195", "JPY", False),  # 75.01 (75.0065)
            ("75.00", "USD", True),
            ("75.01", "USD", False),
        ],
    )
    def test_ceiling_in_usd(self, amount: str, currency: str, accepted: bool) -> None:
        f = self._finding(amount, currency)
        assert (f.severity == "advisory") is accepted, f.reason
        if not accepted:
            assert f.observed["affidavit_issue"] == "over_amount"

    def test_usd_observed_unchanged(self) -> None:
        assert self._finding("60.00", "USD") == self._finding("60.00", None)

    def test_unsupported_currency_is_never_accepted(self) -> None:
        f = self._finding("10.00", "XYZ")
        assert f.severity == "blocking"
        assert f.observed["affidavit_issue"] == "currency_not_supported"

    @pytest.mark.parametrize(
        ("amount", "currency", "category", "needed"),
        [
            ("23.03", "EUR", "meals", False),  # 24.98755 -> 24.99, under the $25 threshold
            ("23.04", "EUR", "meals", True),  # 24.9984 -> 25.00, at the threshold
            ("3", "EUR", "lodging", True),  # always-required category
            ("500", "USD", "mileage", False),  # REF-08
            ("1", "XYZ", "meals", True),  # unknown -> required (fail closed)
        ],
    )
    def test_receipt_needed_converts_the_threshold(
        self, amount: str, currency: str, category: str, needed: bool
    ) -> None:
        usd, ok = pr.amount_in_usd(amount, currency)
        assert pr.receipt_needed(
            RECEIPT_REQ, amount_usd=usd if ok else None, category=category
        ) is (needed)


# ---------------------------------------------------------------------------
# MEAL-04 / GRND-05 — the receipt's own figures convert with the claim
# ---------------------------------------------------------------------------


class TestReceiptFiguresConvert:
    def test_eur_food_only_claim_holds_no_wine(self) -> None:
        """EUR pasta 50 + wine 20, claim EUR 50: the claim converts to 54.25 and so must
        the line items — mixing them made 4.25 of 'wine' appear in an honest claim."""
        item = dataclasses.replace(
            _item("meals", 50.0, "EUR"),
            line_items=(("Pasta", 50.0), ("House red wine", 20.0)),
        )
        verdicts = _verdicts(MEALS, item)
        meal04 = [v for v in verdicts if v.source_clause_ref == "MEAL-04"]
        assert len(meal04) == 1 and meal04[0].in_policy, meal04[0].reason
        assert meal04[0].observed["excluded_usd"] == 0.0
        assert verdicts[0].observed["amount_usd"] == 54.25

    def test_eur_wine_inside_the_claim_is_reported_in_usd(self) -> None:
        item = dataclasses.replace(
            _item("meals", 70.0, "EUR"),
            line_items=(("Pasta", 50.0), ("House red wine", 20.0)),
        )
        (meal04,) = [v for v in _verdicts(MEALS, item) if v.source_clause_ref == "MEAL-04"]
        assert not meal04.in_policy
        assert meal04.observed["excluded_usd"] == 21.7  # EUR 20 x 1.085
        assert meal04.observed["reimbursable_usd"] == 54.25

    def test_eur_tip_at_20_percent_is_within(self) -> None:
        """Fare EUR 50 + tip EUR 10 (exactly 20%), claim EUR 60: within GRND-05 — the
        claim (65.10) and the receipt's total / tip convert together."""
        item = EvalItem(
            item_type="receipt",
            amount_usd=60.0,
            expense_category="ground_transport",
            currency="EUR",
            ground_subtype="rideshare_taxi",
            ground_candidates=("rideshare_taxi",),
            ground_verified=True,
            tip_usd=10.0,
            receipt_total_usd=60.0,
        )
        (v,) = _verdicts(TIPS, item)
        assert v.in_policy, v.reason
        assert v.observed["fare_usd"] == 54.25 and v.observed["tip_usd"] == 10.85
