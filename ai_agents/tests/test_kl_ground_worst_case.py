"""Known limits — unconfirmed ground lines: worst case across every candidate (hermetic).

A ground line no trusted evidence identifies (typed words only) is UNCONFIRMED: every
kind rule reaches it (policy review round 5).  ``ground_worst_case.relax`` clears the
kind rules' could-not-verify when the line is within every limit WHATEVER its kind:
below the RECEIPT-REQ threshold (USD, via the engine's FX helper), no other limit failed,
and nothing the words / segment say puts the rule at risk.  Everything else is unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai_agents.agents.expense_agent.entry_context import ground_fields
from ai_agents.agents.expense_agent.presubmit import verdicts_to_flags
from ai_agents.tools import ground_worst_case as gwc
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import PolicyAppliesTo, PolicyRule, PolicyRuleType
from database.seeds.policy_content import (
    ENTRY_RULES,
    PARKING_DAYS_DEFINITION,
    RAIL_CLASS_DEFINITION,
    RULES,
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


SEEDED = [_rule(r["rule_type"], r["applies_to"], r["definition"], r["clause_ref"]) for r in RULES]
SEEDED += [
    _rule(r["rule_type"], r["applies_to"], r["definition"], r["clause_ref"]) for r in ENTRY_RULES
]
RAIL = _rule("category_allow", "all", RAIL_CLASS_DEFINITION, "GRND-01")
PARKING = _rule("amount_cap", "all", PARKING_DAYS_DEFINITION, "GRND-03")
TIPS = _rule("amount_cap", "all", TIP_PCT_DEFINITION, "GRND-05")
RECEIPT = next(r for r in SEEDED if r.source_clause_ref == "RECEIPT-REQ")
# An authored sub-type cap "rideshare/taxi up to $20" (it reaches unconfirmed lines).
RIDE_CAP_20 = _rule(
    "amount_cap",
    "all",
    {
        "currency": "USD",
        "per": "item",
        "expense_categories": ["ground_transport"],
        "ground_subtypes": ["rideshare_taxi"],
        "tiers": [{"tier": "standard", "cap": 20}],
        "exception_approver": "manager",
    },
    "GRND-02",
)
KIND_REFS = {"GRND-01", "GRND-03", "GRND-05"}


def _line(amount: float, *typed: str, currency: str | None = None, **kw: Any) -> EvalItem:
    return EvalItem(
        item_type="receipt",
        amount_usd=amount,
        expense_category="ground_transport",
        currency=currency,
        **ground_fields(*typed, **kw),
    )


def _eval(item: EvalItem, rules: list[PolicyRule] | None = None) -> tuple[RuleVerdict, ...]:
    return TOOL._evaluate_all(TOOL._filter_applicable(rules or SEEDED, item), item)


def _kind(verdicts: tuple[RuleVerdict, ...], ref: str) -> RuleVerdict:
    (v,) = [x for x in verdicts if x.source_clause_ref == ref]
    return v


def _escalates(item: EvalItem, rules: list[PolicyRule] | None = None) -> bool:
    return gwc.line_escalates(_eval(item, rules))


def _flags(item: EvalItem, rules: list[PolicyRule] | None = None) -> list:
    flags, _ = verdicts_to_flags(
        list(_eval(item, rules)),
        entry_ref="entry_0",
        has_receipt=False,
        category="ground_transport",
    )
    return [f for f in flags if f.source_clause_ref in KIND_REFS]


# ---------------------------------------------------------------------------
# Clearing a typed-only ride
# ---------------------------------------------------------------------------


class TestTypedOnlySmallRide:
    def test_14_dollar_typed_uber_does_not_escalate(self) -> None:
        item = _line(14.0, "Uber", "Ride to client")
        assert item.ground_verified is False and item.ground_named == ("rideshare_taxi",)
        out = _eval(item)
        rail, park = _kind(out, "GRND-01"), _kind(out, "GRND-03")
        for v in (rail, park):
            assert v.in_policy and v.requires_approver is None
            assert "could_not_verify" not in v.observed and "approver_check" not in v.observed
            assert v.observed[gwc.CLEARED] is True and v.observed[gwc.CONFIRM_HINT] is True
        assert not gwc.line_escalates(out)

    def test_one_low_key_advisory_with_the_entry_time_hint(self) -> None:
        flags = _flags(_line(14.0, "Uber"))
        (note,) = flags
        assert note.flag_type == "policy_advisory" and note.requires_approver is None
        assert note.severity == "advisory"
        assert "category unverified" in note.reason
        assert "within every ground-transport limit" in note.reason
        assert gwc.HINT_TEXT in note.reason
        assert "GRND-01" in note.reason and "GRND-03" in note.reason

    def test_no_words_at_all_small_ride_is_cleared(self) -> None:
        assert not _escalates(_line(9.5, "Ride to the office"))


class TestBoundaries:
    @pytest.mark.parametrize(
        ("amount", "escalates"),
        [(24.98, False), (24.99, False), (25.00, True), (25.01, True)],
    )
    def test_receipt_threshold_is_the_floor(self, amount: float, escalates: bool) -> None:
        """RECEIPT-REQ needs a receipt AT the threshold (amount >= $25), so $25.00 is the
        first amount that is no longer receipt-free."""
        assert _escalates(_line(amount, "Uber")) is escalates

    def test_at_threshold_reason_names_the_rules_at_risk(self) -> None:
        out = _eval(_line(25.0, "Uber"))
        for ref in ("GRND-01", "GRND-03"):
            v = _kind(out, ref)
            assert v.observed["approver_check"] == "manager"
            assert "not below the $25.00 receipt threshold" in v.reason
            assert v.observed[gwc.CONFIRM_HINT] is True and gwc.HINT_TEXT in v.reason

    @pytest.mark.parametrize(("amount", "escalates"), [(20.00, False), (20.01, True)])
    def test_exactly_at_the_strictest_authored_cap(self, amount: float, escalates: bool) -> None:
        rules = [*SEEDED, RIDE_CAP_20]
        out = _eval(_line(amount, "Uber"), rules)
        assert gwc.line_escalates(out) is escalates
        if escalates:
            assert "over another limit (GRND-02)" in _kind(out, "GRND-01").reason
            assert _kind(out, "GRND-01").observed["approver_check"] == "manager"

    def test_strictest_of_several_caps_wins(self) -> None:
        cap_15 = _rule(
            "amount_cap",
            "all",
            {
                "currency": "USD",
                "per": "item",
                "expense_categories": ["ground_transport"],
                "tiers": [{"tier": "standard", "cap": 15}],
                "exception_approver": "manager",
            },
            "GT-15",
        )
        rules = [*SEEDED, RIDE_CAP_20, cap_15]
        assert not _escalates(_line(15.0, "Uber"), rules)
        assert _escalates(_line(15.01, "Uber"), rules)


class TestMixedCurrency:
    def test_converted_below_the_threshold_is_cleared(self) -> None:
        # EUR 20.00 is ~USD 21.70 — under the USD 25 floor.
        item = _line(20.0, "Taxi", currency="EUR")
        out = _eval(item)
        assert not gwc.line_escalates(out)
        assert _kind(out, "GRND-01").observed["amount_checked_usd"] > 20.0

    def test_converted_over_the_threshold_escalates_though_the_number_is_small(self) -> None:
        # EUR 24.00 is ~USD 26 — the raw number is below 25, the converted one is not.
        out = _eval(_line(24.0, "Taxi", currency="EUR"))
        assert gwc.line_escalates(out)
        assert "receipt threshold" in _kind(out, "GRND-01").reason

    def test_weaker_currency_large_number_small_value_is_cleared(self) -> None:
        # GBP is stronger, JPY weaker: JPY 2000 is ~USD 13.
        assert not _escalates(_line(2000.0, "Taxi", currency="JPY"))

    def test_unsupported_currency_fails_closed(self) -> None:
        out = _eval(_line(5.0, "Taxi", currency="XYZ"))
        assert gwc.line_escalates(out)
        assert _kind(out, "GRND-01").observed["approver_check"] == "manager"


class TestWordsOnlyWidenScrutiny:
    def test_typed_rail_word_keeps_grnd01(self) -> None:
        out = _eval(_line(14.0, "Amtrak"))
        rail = _kind(out, "GRND-01")
        assert rail.observed["approver_check"] == "manager"
        assert "say it is rail" in rail.reason
        assert _kind(out, "GRND-03").observed.get(gwc.CLEARED) is True
        assert gwc.line_escalates(out)

    def test_ambiguous_words_naming_rail_keep_grnd01(self) -> None:
        item = _line(14.0, "Uber to the train station")
        assert set(item.ground_named or ()) == {"rideshare_taxi", "rail"}
        assert _escalates(item)

    def test_a_tip_mentioned_keeps_grnd05(self) -> None:
        out = _eval(_line(14.0, "Uber incl. tip"))
        assert _kind(out, "GRND-05").observed["approver_check"] == "manager"
        assert gwc.line_escalates(out)
        # The rules it cannot breach are cleared, without an extra advisory note.
        rail = _kind(out, "GRND-01")
        assert rail.observed[gwc.CLEARED] is True and "advisory" not in rail.observed

    def test_premium_class_word_is_never_cleared(self) -> None:
        out = _eval(_line(14.0, "Uber, first class"))
        assert gwc.line_escalates(out)

    def test_a_named_class_the_rule_does_not_list_is_never_cleared(self) -> None:
        """A rail rule listing only First as premium: "business" is a named class it
        cannot place (could not verify) — a class word is never cleared as low-value."""
        defn = {**RAIL_CLASS_DEFINITION, "premium_classes": ["first"]}
        rail = _rule("category_allow", "all", defn, "GRND-01")
        item = _line(14.0, "Uber business class")
        assert item.travel_class == "business"
        out = _eval(item, [rail, PARKING, TIPS, RECEIPT])
        assert _kind(out, "GRND-01").observed["could_not_verify"] == "travel_class"
        assert "ticket class is named" in _kind(out, "GRND-01").reason
        assert gwc.line_escalates(out)

    @pytest.mark.parametrize("words", ["Hotel valet", "short-term lot"])
    def test_premium_lot_word_keeps_grnd03(self, words: str) -> None:
        out = _eval(_line(14.0, words))
        park = _kind(out, "GRND-03")
        assert park.observed["approver_check"] == "manager"
        assert "lot" in park.reason and gwc.line_escalates(out)

    def test_airport_parking_words_keep_grnd03(self) -> None:
        out = _eval(_line(14.0, "Airport parking"))
        assert _kind(out, "GRND-03").observed["could_not_verify"] == "parking_days"
        assert gwc.line_escalates(out)

    def test_a_ride_to_the_airport_keeps_grnd03_at_risk(self) -> None:
        # Integration security fix (intent changed): the branch cleared this as "not
        # parking"; an airport-linked line now keeps GRND-03 whatever word was typed.
        item = _line(18.0, "Uber to the airport")
        assert item.airport_parking is True
        out = _eval(item)
        assert _kind(out, "GRND-03").observed.get(gwc.CLEARED) is not True
        assert gwc.line_escalates(out)

    def test_parking_with_a_flight_segment_keeps_grnd03(self) -> None:
        assert _escalates(_line(14.0, "Parking", segment_type="flight"))

    def test_claimed_days_over_the_limit_keep_grnd03(self) -> None:
        item = _line(14.0, "Parking")
        item = type(item)(**{**item.__dict__, "claimed_days": 9})
        assert _escalates(item)

    def test_more_typed_words_never_clear_what_fewer_words_kept(self) -> None:
        """Adding words to an escalating line never de-escalates it."""
        base = ["Amtrak", "Uber incl. tip", "valet", "Airport parking", "Uber first class"]
        for words in base:
            assert _escalates(_line(14.0, words))
            assert _escalates(_line(14.0, words, "Uber ride"))
            assert _escalates(_line(14.0, "Taxi", words))


class TestEvidenceAndSegments:
    def test_rail_segment_alone_keeps_grnd01(self) -> None:
        item = _line(14.0, "Uber", segment_type="rail")
        assert item.ground_verified is False
        assert _escalates(item)

    def test_flight_segment_makes_it_airport_parking_candidate(self) -> None:
        assert _escalates(_line(14.0, "Parking", segment_type="flight"))

    def test_overwritten_receipt_vendor_is_never_cleared(self) -> None:
        item = _line(14.0, "Uber", trusted_texts=("UBER",), vendor_edited=True)
        assert item.ground_named is None
        out = _eval(item)
        assert "vendor reading was overwritten" in _kind(out, "GRND-01").reason
        assert gwc.line_escalates(out)

    def test_receipt_naming_nothing_small_amount_is_cleared(self) -> None:
        assert not _escalates(_line(12.0, "Ride", trusted_texts=("PAYMENT RECEIPT",)))

    def test_verified_lines_are_untouched(self) -> None:
        """A receipt that names the family: the same verdicts as each rule alone."""
        for item in (
            _line(14.0, "Amtrak", trusted_texts=("AMTRAK",)),  # rail, no class: escalates
            _line(14.0, "Uber", trusted_texts=("UBER",)),
            _line(14.0, "Uber", trusted_texts=("AMTRAK",)),  # contradicting receipt
        ):
            applicable = TOOL._filter_applicable(SEEDED, item)
            alone = tuple(v for r in applicable for v in TOOL._evaluate_all([r], item))
            assert _eval(item) == alone
        assert _escalates(_line(14.0, "Amtrak", trusted_texts=("AMTRAK",)))

    def test_no_receipt_rule_in_the_set_clears_nothing(self) -> None:
        item = _line(5.0, "Uber")
        out = _eval(item, [RAIL, PARKING, TIPS])
        assert _kind(out, "GRND-01").observed["approver_check"] == "manager"
        assert "no receipt threshold applies" in _kind(out, "GRND-01").reason

    def test_always_required_category_clears_nothing(self) -> None:
        defn = {**RECEIPT.definition, "always_required_categories": ["ground_transport"]}
        strict = _rule("receipt_required", "all", defn, "RECEIPT-REQ")
        assert _escalates(_line(5.0, "Uber"), [RAIL, PARKING, TIPS, strict])

    def test_unknown_amount_clears_nothing(self) -> None:
        item = EvalItem(
            item_type="receipt",
            amount_usd=None,
            expense_category="ground_transport",
            **ground_fields("Uber"),
        )
        assert _escalates(item)

    def test_non_ground_lines_are_untouched(self) -> None:
        item = EvalItem(item_type="meals", amount_usd=14.0, expense_category="meals")
        applicable = TOOL._filter_applicable(SEEDED, item)
        alone = tuple(v for r in applicable for v in TOOL._evaluate_all([r], item))
        assert _eval(item) == alone


# ---------------------------------------------------------------------------
# Review-load metric — a realistic mix of 20 typed-only rides
# ---------------------------------------------------------------------------

TWENTY_TYPED_RIDES: list[tuple[float, tuple[str, ...], str | None, bool]] = [
    # (amount, typed words, currency, still escalates after the change)
    (14.00, ("Uber", "Ride to client"), None, False),
    (9.50, ("Lyft",), None, False),
    # Integration fix: an airport named keeps GRND-03 at risk (was cleared).
    (18.25, ("Taxi", "Airport hotel to office"), None, True),
    (6.00, ("Bus fare",), None, False),
    (3.25, ("Subway",), None, False),
    (22.40, ("Uber",), None, False),
    (12.00, ("Ride to dinner",), None, False),
    (24.99, ("Yellow cab",), None, False),
    (11.00, ("Taxi",), "EUR", False),
    (1500.0, ("Taxi",), "JPY", False),
    (16.75, ("Lyft", "office to venue"), None, False),
    (8.00, ("Metro card top-up",), None, False),
    # Still escalate: over the floor / words that put a rule at risk / ambiguity.
    (25.00, ("Uber",), None, True),
    (48.00, ("Uber", "airport run"), None, True),
    (96.00, ("Black car",), None, True),
    (14.00, ("Uber incl. tip",), None, True),
    (19.00, ("Amtrak",), None, True),
    (15.00, ("Uber to the train",), None, True),
    (21.00, ("Hotel valet",), None, True),
    (24.00, ("Taxi",), "GBP", True),
]


def test_review_load_on_twenty_typed_only_rides() -> None:
    items = [_line(amount, *words, currency=cur) for amount, words, cur, _ in TWENTY_TYPED_RIDES]
    assert all(not i.ground_verified for i in items)
    load = gwc.review_load(items, SEEDED)
    assert load.lines == 20
    assert load.escalated_before == 20  # round 5: every typed-only ride went to a human
    assert load.escalated_after == 9  # 11 small, plain rides no longer do
    for item, (_, words, _, expected) in zip(items, TWENTY_TYPED_RIDES, strict=True):
        assert _escalates(item) is expected, words


def test_review_load_is_deterministic() -> None:
    items = [_line(a, *w, currency=c) for a, w, c, _ in TWENTY_TYPED_RIDES]
    assert gwc.review_load(items, SEEDED) == gwc.review_load(list(items), list(SEEDED))


def test_finance_summary_states_the_low_value_clearing() -> None:
    from ai_agents.agents.entry_rule_semantics import describe_kind

    rail = " ".join(describe_kind(RAIL_CLASS_DEFINITION))
    park = " ".join(describe_kind(PARKING_DAYS_DEFINITION))
    tip = " ".join(describe_kind(TIP_PCT_DEFINITION))
    assert "below the receipt threshold" in rail and "rail, a ticket class" in rail
    assert "below the receipt threshold" in park and "premium lot" in park
    assert "below the receipt threshold" not in tip  # GRND-05 is never cleared


# ---------------------------------------------------------------------------
# Integration security fixes (automated review)
# ---------------------------------------------------------------------------


class TestAirportLinkKeepsGrnd03:
    """GRND-03 stays at risk whenever the line is airport-linked (an airport named
    or a flight segment linked), whether or not a parking word was typed: a
    typed "Uber" says nothing reliable about what the money paid for."""

    @pytest.mark.parametrize("words", [("Uber to the airport",), ("Ride", "JFK")])
    def test_airport_named_without_a_parking_word_keeps_grnd03(
        self, words: tuple[str, ...]
    ) -> None:
        item = _line(14.0, *words)
        assert item.airport_parking is True
        out = _eval(item)
        park = _kind(out, "GRND-03")
        assert park.observed.get(gwc.CLEARED) is not True
        assert park.observed.get("approver_check") and gwc.AT_RISK in park.observed
        assert gwc.line_escalates(out)

    def test_flight_segment_without_a_parking_word_keeps_grnd03(self) -> None:
        item = _line(14.0, "Uber", segment_type="flight")
        assert item.airport_parking is True
        out = _eval(item)
        assert _kind(out, "GRND-03").observed.get(gwc.CLEARED) is not True
        assert gwc.line_escalates(out)


def _many(
    items: list[EvalItem], monkeypatch: pytest.MonkeyPatch, rules: list[PolicyRule] | None = None
) -> list[tuple[RuleVerdict, ...]]:
    """``evaluate_many`` (one report) with the rule set injected instead of loaded."""
    tool = PolicyEvalTool()
    monkeypatch.setattr(tool, "_load_active_rules", lambda _context, _session: rules or SEEDED)
    return [pv.verdicts for pv in tool.evaluate_many(items, context=None, session=None)]  # type: ignore[arg-type]


class TestClearedTotalPerReport:
    """Clearing is bounded by a TOTAL per report: many small typed-only rides must
    not each pass.  Lines are cleared in report order while the sum of the cleared
    lines stays below the RECEIPT-REQ threshold; the rest escalate as before."""

    def test_two_small_rides_do_not_both_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first, second = _many([_line(14.0, "Uber"), _line(14.0, "Lyft")], monkeypatch)
        assert not gwc.line_escalates(first)
        assert gwc.line_escalates(second)
        park = _kind(second, "GRND-03")
        assert "receipt threshold" in park.observed[gwc.AT_RISK]
        assert park.observed[gwc.CONFIRM_HINT] is True

    def test_many_tiny_rides_clear_only_up_to_the_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _many([_line(6.0, "Taxi") for _ in range(6)], monkeypatch)
        # 6 + 6 + 6 + 6 = 24 < 25 clears four; the fifth would reach 30.
        assert [gwc.line_escalates(v) for v in out] == [False] * 4 + [True] * 2

    def test_escalating_lines_do_not_use_up_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _many([_line(19.0, "Amtrak"), _line(14.0, "Uber")], monkeypatch)
        assert gwc.line_escalates(out[0]) and not gwc.line_escalates(out[1])

    def test_a_single_line_is_judged_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        (only,) = _many([_line(24.99, "Uber")], monkeypatch)
        assert not gwc.line_escalates(only)

    def test_separate_evaluations_do_not_share_a_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not gwc.line_escalates(_many([_line(14.0, "Uber")], monkeypatch)[0])
        assert not gwc.line_escalates(_many([_line(14.0, "Uber")], monkeypatch)[0])
