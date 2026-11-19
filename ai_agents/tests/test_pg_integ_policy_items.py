"""Integration round 2 — open security items on the merged policy tracks (hermetic).

* MEAL-04 (i): the receipt's tax / tip is split PRO RATA over kept and excluded items,
  so alcohol (and the tax on it) cannot hide in the gap between the OCR total and the
  line items; an honest partial claim is never penalised or reduced twice; extras that
  cannot be attributed are a could-not-verify for the approver, never a silent pass.
* MEAL-04 (ii): the "cooked with" exemption is a NAMED drink directly followed by food
  phrasing, and only that span is stripped.
* Ground transport: ``verified`` never comes from the trip segment's type alone.
* PTA-02: the departure is the trusted one (planner-selected segment) or the EARLIER of
  the segment's and the trip's start dates.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ai_agents.agents import process_checks
from ai_agents.agents.expense_agent.presubmit import verdicts_to_flags
from ai_agents.tools import entry_rules, ground_transport
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import MEALS_PD_DEFINITION

TOOL = PolicyEvalTool()
MEALS = PolicyRule(
    id=uuid.uuid4(),
    policy_id=uuid.uuid4(),
    rule_type=PolicyRuleType.per_diem,
    applies_to=PolicyAppliesTo.meals,
    definition=MEALS_PD_DEFINITION,
    source_clause_ref="MEALS-PD",
)


def _meal04(claim: float, *lines: tuple[str, float], **kw: Any) -> RuleVerdict:
    item = EvalItem(item_type="meals", amount_usd=claim, line_items=tuple(lines) or None, **kw)
    (v,) = [x for x in TOOL._evaluate_all([MEALS], item) if x.source_clause_ref == "MEAL-04"]
    return v


# ---------------------------------------------------------------------------
# MEAL-04 (i) — the tax / tip gap
# ---------------------------------------------------------------------------


class TestAlcoholTaxTipGap:
    """Receipt: food 50 + wine 40 = 90 itemized; tax 9 + tip 18 -> OCR total 117.
    Pro rata: the wine carries 12 of the 27 extras (52 in all), the food 15 (65)."""

    LINES = (("Ribeye", 50.0), ("Bottle of Cabernet", 40.0))

    def test_reviewer_probe_total_minus_wine_hides_the_wines_tax_and_tip(self) -> None:
        """Claim 77 = total - wine: the round-3 base let 12 of wine tax / tip through."""
        v = _meal04(77.0, *self.LINES, receipt_total_usd=117.0)
        assert not v.in_policy
        assert v.observed["excluded_usd"] == 12.0
        assert v.observed["reimbursable_usd"] == 65.0
        assert v.observed["receipt_excluded_with_tax_tip_usd"] == 52.0

    @pytest.mark.parametrize("claim", [50.0, 65.0])
    def test_honest_partial_claims_are_not_penalised(self, claim: float) -> None:
        """The food alone, or the food with its own tax / tip: no flag, nothing
        subtracted from the per-diem compare."""
        v = _meal04(claim, *self.LINES, receipt_total_usd=117.0)
        assert v.in_policy and v.observed["excluded_usd"] == 0.0
        flags, _ = verdicts_to_flags(
            [v], entry_ref="x", has_receipt=True, category="meals", justification=None
        )
        assert flags == []
        item = EvalItem(
            item_type="meals",
            amount_usd=claim,
            line_items=self.LINES,
            receipt_total_usd=117.0,
        )
        assert TOOL._evaluate_all([MEALS], item)[0].observed["amount_usd"] == claim

    def test_full_claim_excludes_the_wine_with_its_tax_and_tip(self) -> None:
        v = _meal04(117.0, *self.LINES, receipt_total_usd=117.0)
        assert not v.in_policy
        assert v.observed["excluded_usd"] == 52.0 and v.observed["reimbursable_usd"] == 65.0

    def test_food_not_itemized_is_could_not_verify_never_a_silent_pass(self) -> None:
        """Only the wine is itemized (40) on a 117 bill: the rest may be food or tax.
        A claim of 77 passes the lenient reading but not the strict one -> the approver
        checks (escalating could-not-verify)."""
        v = _meal04(77.0, ("Bottle of Cabernet", 40.0), receipt_total_usd=117.0)
        assert v.in_policy
        assert v.observed[entry_rules.COULD_NOT_VERIFY] == "exclusion_share"
        assert v.observed[entry_rules.APPROVER_CHECK] == "manager"
        flags, _ = verdicts_to_flags(
            [v], entry_ref="x", has_receipt=True, category="meals", justification=None
        )
        (flag,) = flags
        assert flag.flag_type == "not_evaluated" and flag.requires_approver == "manager"

    def test_no_ocr_total_keeps_the_line_item_base(self) -> None:
        v = _meal04(50.0, *self.LINES)
        assert v.in_policy and v.observed["excluded_usd"] == 0.0
        v = _meal04(60.0, *self.LINES)
        assert not v.in_policy and v.observed["excluded_usd"] == 10.0


# ---------------------------------------------------------------------------
# MEAL-04 (ii) — the "cooked with" exemption
# ---------------------------------------------------------------------------


class TestCookedWith:
    @pytest.mark.parametrize(
        "text",
        [
            "Beer-battered cod",
            "Red wine braised short rib",
            "Port reduction duck",
            "Rum cake",
            "Bourbon glazed salmon",
            "Short rib, red wine jus",
        ],
    )
    def test_food_cooked_with_a_drink_is_not_alcohol(self, text: str) -> None:
        assert entry_rules.exclusion_matches(text, "alcohol", line_item=True) is None

    @pytest.mark.parametrize(
        "text",
        [
            # The old exemption swallowed up to two words before the food word.
            "Cabernet Sauvignon cake",
            "Whiskey flight butter",
            "Gin tonic sauce",
            "Merlot and cake",
            "Glass of wine with cake",
        ],
    )
    def test_arbitrary_words_before_a_food_word_never_hide_a_drink(self, text: str) -> None:
        assert entry_rules.exclusion_matches(text, "alcohol", line_item=True) is not None

    def test_only_the_matched_span_is_stripped(self) -> None:
        """'Rum cake' is food, the Heineken next to it is not."""
        assert (
            entry_rules.exclusion_matches("Rum cake + Heineken", "alcohol", line_item=True)
            is not None
        )


# ---------------------------------------------------------------------------
# Ground transport — the segment only corroborates
# ---------------------------------------------------------------------------


class TestSegmentNeverVerifiesAlone:
    def test_segment_type_alone_is_not_verified(self) -> None:
        cls = ground_transport.classify_with_trust(("Train",), (), segment_type="rail")
        assert not cls.verified
        # Nothing trusted names it -> UNCONFIRMED: every kind rule reaches it.
        assert ground_transport.unconfirmed(cls.families, cls.verified)

    def test_receipt_words_verify_and_the_segment_corroborates(self) -> None:
        cls = ground_transport.classify_with_trust(("Train",), ("AMTRAK",), segment_type="rail")
        assert cls.verified and cls.subtype == "rail"

    def test_a_segment_of_another_family_blocks_verification(self) -> None:
        cls = ground_transport.classify_with_trust((), ("AMTRAK",), segment_type="car")
        assert not cls.verified
        assert set(cls.families) >= {"rail", "car_rental"}


# ---------------------------------------------------------------------------
# PTA-02 — the departure date
# ---------------------------------------------------------------------------


def _trip(start: date, *, planner: bool, selected: tuple[str, ...] = ("opt-1",)) -> Any:
    ctx = (
        {"agent_task": "trip_planning", "selected_option_ids": list(selected)} if planner else None
    )
    return SimpleNamespace(start_date=start, recommendation_context=ctx)


def _seg(day: date, option_id: str | None = "opt-1") -> Any:
    return SimpleNamespace(
        start_datetime=datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc),
        details={"option_id": option_id} if option_id else {},
    )


class TestTrustedDeparture:
    def test_a_typed_far_future_segment_cannot_lengthen_the_lead_time(self) -> None:
        trip = _trip(date(2026, 11, 20), planner=False)
        seg = _seg(date(2027, 3, 1))
        assert process_checks.trusted_departure(trip, seg) == date(2026, 11, 20)

    def test_a_segment_before_the_trip_start_counts_from_its_own_date(self) -> None:
        trip = _trip(date(2026, 12, 20), planner=False)
        assert process_checks.trusted_departure(trip, _seg(date(2026, 11, 18))) == date(
            2026, 11, 18
        )

    def test_a_typed_option_id_is_not_a_planner_selection(self) -> None:
        trip = _trip(date(2026, 11, 20), planner=True, selected=("other",))
        assert process_checks.trusted_departure(trip, _seg(date(2027, 3, 1))) == date(2026, 11, 20)

    def test_a_planner_selected_segment_departs_when_the_planner_says(self) -> None:
        trip = _trip(date(2026, 11, 20), planner=True)
        assert process_checks.trusted_departure(trip, _seg(date(2026, 12, 15))) == date(
            2026, 12, 15
        )

    def test_missing_dates(self) -> None:
        trip = SimpleNamespace(start_date=None, recommendation_context=None)
        seg = SimpleNamespace(start_datetime=None, details={})
        assert process_checks.trusted_departure(trip, seg) is None
        assert process_checks.trusted_departure(
            _trip(date(2026, 11, 20), planner=False), seg
        ) == date(2026, 11, 20)
