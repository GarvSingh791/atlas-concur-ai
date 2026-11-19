"""Product gap 2, stage 2 — process & timing rules (pure evaluators, hermetic).

Boundary values: EXP-01 day 30 / 31 / 60 / 61, EXP-04 $75.00 / $75.01 and the 3rd / 4th
affidavit of a quarter, PTA-02 13 / 14 (domestic) and 20 / 21 (international) days,
PTA-05 9.99% / 10.00% / 10.01%, AIR-06 $150.00 / $150.01 ($400 international),
AIR-04 6 h / 6.5 h / 10 h, CARD-03 day 30 / 31.  Every finding cites its clause.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ai_agents.agents import process_rule_semantics as sem
from ai_agents.agents.policy_rule_semantics import describe_rule, validate_rule_definition
from ai_agents.tools import process_rules as pr
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from database.seeds.policy_content import PROCESS_RULES, RULES

DEADLINE = {
    "kind": "submission_deadline",
    "corpus_clause_ref": "EXP-01",
    "deadline_days": 30,
    "stale_days": 60,
    "stale_approver": "VP",
}
BOOKING = {
    "kind": "advance_booking",
    "corpus_clause_ref": "PTA-02",
    "domestic_days": 14,
    "international_days": 21,
    "exception_approver": "manager",
}
CHANGE = {
    "kind": "trip_change_reapproval",
    "corpus_clause_ref": "PTA-05",
    "max_increase_pct": 10,
    "exception_approver": "manager",
}
FARE = {
    "kind": "lowest_fare",
    "corpus_clause_ref": "AIR-06",
    "domestic_tolerance_usd": 150,
    "international_tolerance_usd": 400,
    "exception_approver": "manager",
}
CARD = {"kind": "card_reconciliation", "corpus_clause_ref": "CARD-03", "deadline_days": 30}
RECEIPT = next(r["definition"] for r in RULES if r["clause_ref"] == "RECEIPT-REQ")
CABIN = next(r["definition"] for r in RULES if r["clause_ref"] == "AIR-CABIN")
TODAY = date(2026, 11, 14)


# ---------------------------------------------------------------------------
# EXP-01
# ---------------------------------------------------------------------------


class TestSubmissionDeadline:
    def _run(self, age: int) -> pr.Finding | None:
        return pr.submission_deadline(
            DEADLINE, completed_on=TODAY - timedelta(days=age), today=TODAY, basis="trip completion"
        )

    @pytest.mark.parametrize("age", [-3, 0, 1, 29, 30])
    def test_on_time(self, age: int) -> None:
        assert self._run(age) is None

    @pytest.mark.parametrize("age", [31, 45, 60])
    def test_late_is_an_advisory_without_approver(self, age: int) -> None:
        f = self._run(age)
        assert f is not None
        assert f.flag_type == pr.LATE_SUBMISSION and f.severity == "advisory"
        assert f.requires_approver is None and f.clause_ref == "EXP-01"
        assert f"{age} days after trip completion" in f.reason
        assert f.observed["age_days"] == age

    @pytest.mark.parametrize("age", [61, 120])
    def test_stale_requires_the_vp(self, age: int) -> None:
        f = self._run(age)
        assert f is not None
        assert f.flag_type == pr.APPROVAL_REQUIRED and f.requires_approver == "VP"
        assert "may be treated as taxable" in f.reason and f.clause_ref == "EXP-01"
        assert f.observed["stale"] is True

    def test_no_completion_date_no_finding(self) -> None:
        assert pr.submission_deadline(DEADLINE, completed_on=None, today=TODAY, basis="x") is None

    def test_malformed_setting_raises(self) -> None:
        with pytest.raises(ValueError):
            pr.submission_deadline(
                {**DEADLINE, "deadline_days": "thirty"},
                completed_on=TODAY - timedelta(days=40),
                today=TODAY,
                basis="x",
            )


# ---------------------------------------------------------------------------
# EXP-04
# ---------------------------------------------------------------------------


class TestAffidavit:
    def _run(
        self,
        amount: str,
        *,
        signed: bool = True,
        justification: str | None = "Receipt lost in transit",
        prior: int = 0,
    ) -> pr.Finding:
        return pr.affidavit(
            RECEIPT,
            amount=Decimal(amount),
            signed=signed,
            justification=justification,
            prior_in_quarter=prior,
            quarter="Q4 2026",
        )

    def test_accepted_at_exactly_75(self) -> None:
        f = self._run("75.00")
        assert f.severity == "advisory" and f.flag_type == "missing_receipt"
        assert f.clause_ref == "EXP-04" and f.observed["affidavit_accepted"] is True
        assert "affidavit 1 of 3 for Q4 2026" in f.reason

    def test_75_01_is_over_the_ceiling_even_signed(self) -> None:
        f = self._run("75.01")
        assert f.severity == "blocking" and f.observed["affidavit_issue"] == "over_amount"
        assert "only up to $75.00" in f.reason

    def test_unsigned_is_blocking_and_says_how(self) -> None:
        f = self._run("40.00", signed=False)
        assert f.severity == "blocking" and f.observed["affidavit_issue"] == "not_signed"
        assert "sign the missing-receipt affidavit" in f.reason

    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_signed_without_explanation_is_blocking(self, text: str | None) -> None:
        f = self._run("40.00", justification=text)
        assert f.severity == "blocking" and f.observed["affidavit_issue"] == "no_explanation"

    def test_third_affidavit_of_the_quarter_is_allowed(self) -> None:
        f = self._run("40.00", prior=2)
        assert f.severity == "advisory" and "affidavit 3 of 3" in f.reason

    def test_fourth_affidavit_of_the_quarter_is_blocking(self) -> None:
        f = self._run("40.00", prior=3)
        assert f.severity == "blocking" and f.observed["affidavit_issue"] == "over_count"
        assert "no more than 3" in f.reason

    def test_quarter_bounds(self) -> None:
        assert pr.quarter_bounds(date(2026, 3, 31)) == (date(2026, 1, 1), date(2026, 4, 1))
        assert pr.quarter_bounds(date(2026, 4, 1)) == (date(2026, 4, 1), date(2026, 7, 1))
        assert pr.quarter_bounds(date(2026, 12, 31)) == (date(2026, 10, 1), date(2027, 1, 1))
        assert pr.quarter_label(date(2026, 11, 14)) == "Q4 2026"


# ---------------------------------------------------------------------------
# PTA-02
# ---------------------------------------------------------------------------


class TestAdvanceBooking:
    def _run(self, lead: int, international: bool | None) -> pr.Finding | None:
        return pr.advance_booking(
            BOOKING,
            departure=TODAY + timedelta(days=lead),
            booked_on=TODAY,
            international=international,
        )

    def test_domestic_14_days_is_enough(self) -> None:
        assert self._run(14, False) is None

    def test_domestic_13_days_needs_a_business_reason(self) -> None:
        f = self._run(13, False)
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED
        assert f.requires_approver == "manager" and f.clause_ref == "PTA-02"
        assert "Booked 13 days ahead" in f.reason and "at least 14 days" in f.reason
        assert f.observed["lead_days"] == 13 and f.observed["required_days"] == 14

    def test_international_boundary(self) -> None:
        assert self._run(21, True) is None
        f = self._run(20, True)
        assert f is not None and "international flights" in f.reason
        assert "at least 21 days" in f.reason

    def test_unknown_route_between_the_two_is_could_not_verify(self) -> None:
        f = self._run(15, None)
        assert f is not None and f.flag_type == pr.NOT_EVALUATED
        assert f.requires_approver == "manager"
        assert self._run(21, None) is None
        short = self._run(10, None)
        assert short is not None and short.flag_type == pr.APPROVAL_REQUIRED

    def test_no_departure_is_could_not_verify(self) -> None:
        f = pr.advance_booking(BOOKING, departure=None, booked_on=TODAY, international=False)
        assert f is not None and f.flag_type == pr.NOT_EVALUATED

    def test_route_country(self) -> None:
        assert pr.is_international("SFO", "JFK") is False
        assert pr.is_international("Chicago", "London") is True
        assert pr.is_international("ORD", "Denver") is None


# ---------------------------------------------------------------------------
# PTA-05
# ---------------------------------------------------------------------------


class TestTripChange:
    def _run(self, old: str, new: str) -> pr.Finding | None:
        return pr.trip_change(
            CHANGE, approved_estimate=Decimal(old), new_estimate=Decimal(new), approved_label="T"
        )

    @pytest.mark.parametrize("new", ["1000.00", "1099.90", "1100.00", "900.00"])
    def test_within_ten_percent(self, new: str) -> None:
        assert self._run("1000.00", new) is None

    def test_10_01_percent_needs_reapproval(self) -> None:
        f = self._run("1000.00", "1100.10")
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED
        assert f.requires_approver == "manager" and f.clause_ref == "PTA-05"
        assert "10.01% more" in f.reason and "must be re-approved" in f.reason

    def test_9_99_percent_does_not(self) -> None:
        assert self._run("1000.00", "1099.90") is None  # 9.99%

    def test_unknown_baseline_is_could_not_verify(self) -> None:
        f = pr.trip_change(CHANGE, approved_estimate=None, new_estimate=1, approved_label="T")
        assert f is not None and f.flag_type == pr.NOT_EVALUATED
        f0 = pr.trip_change(CHANGE, approved_estimate=0, new_estimate=1, approved_label="T")
        assert f0 is not None and f0.flag_type == pr.NOT_EVALUATED


# ---------------------------------------------------------------------------
# AIR-06
# ---------------------------------------------------------------------------


class TestLowestFare:
    def test_domestic_tolerance_boundary(self) -> None:
        assert pr.lowest_fare(FARE, fare="450.00", lowest="300.00", international=False) is None
        f = pr.lowest_fare(FARE, fare="450.01", lowest="300.00", international=False)
        assert f is not None and f.flag_type == pr.POLICY_VIOLATION
        assert f.severity == "blocking" and f.clause_ref == "AIR-06"
        assert "$150.01 above" in f.reason and "within $150.00 (domestic)" in f.reason

    def test_international_tolerance(self) -> None:
        assert pr.lowest_fare(FARE, fare="1400", lowest="1000", international=True) is None
        f = pr.lowest_fare(FARE, fare="1400.01", lowest="1000", international=True)
        assert f is not None and "$400.00 (international)" in f.reason

    def test_unknown_route_uses_the_stricter_domestic_tolerance(self) -> None:
        f = pr.lowest_fare(FARE, fare="1200", lowest="1000", international=None)
        assert f is not None and f.flag_type == pr.POLICY_VIOLATION

    def test_no_search_baseline_is_could_not_verify(self) -> None:
        f = pr.lowest_fare(FARE, fare="500", lowest=None, international=False)
        assert f is not None and f.flag_type == pr.NOT_EVALUATED

    def test_comparable_is_the_same_cabin(self) -> None:
        lowest = pr.lowest_comparable_fares(
            [("a", "economy", 300), ("b", "economy", 280.5), ("c", "business", 1300)]
        )
        assert lowest == {"a": Decimal("280.50"), "b": Decimal("280.50"), "c": Decimal("1300.00")}


# ---------------------------------------------------------------------------
# AIR-04
# ---------------------------------------------------------------------------


class TestRoleCabinException:
    @pytest.mark.parametrize("hours", [7.0, 6.5, 9.9])
    def test_business_over_six_hours_needs_an_svp_approver(self, hours: float) -> None:
        # Known limits (AIR-04 grades): with NO grade on record the exception still goes
        # to an SVP-level approver; the reason now says the grade is unknown (it used to
        # say Atlas records no grade at all).
        f = pr.role_cabin_exception(CABIN, cabin="business", duration_hours=hours)
        assert f is not None and f.flag_type == pr.APPROVAL_REQUIRED
        assert f.requires_approver == "SVP" and f.clause_ref == "AIR-04"
        assert "No grade is recorded for the traveler" in f.reason

    @pytest.mark.parametrize("hours", [3.0, 6.0])
    def test_six_hours_or_less_is_not_air04(self, hours: float) -> None:
        assert pr.role_cabin_exception(CABIN, cabin="business", duration_hours=hours) is None

    def test_business_already_permitted_over_ten_hours(self) -> None:
        assert pr.role_cabin_exception(CABIN, cabin="business", duration_hours=10.5) is None

    def test_other_cabins_and_missing_data(self) -> None:
        assert pr.role_cabin_exception(CABIN, cabin="economy", duration_hours=8) is None
        assert pr.role_cabin_exception(CABIN, cabin="business", duration_hours=None) is None
        old = {k: v for k, v in CABIN.items() if not k.startswith("role_exception")}
        assert pr.role_cabin_exception(old, cabin="business", duration_hours=8) is None


# ---------------------------------------------------------------------------
# CARD-03
# ---------------------------------------------------------------------------


class TestCardReconciliation:
    def test_day_30_is_on_time_day_31_overdue(self) -> None:
        on_time = [(TODAY - timedelta(days=30), Decimal("12.00"), "USD")]
        assert pr.card_reconciliation(CARD, unreconciled=on_time, today=TODAY) is None
        overdue = [
            (TODAY - timedelta(days=31), Decimal("12.00"), "USD"),
            (TODAY - timedelta(days=45), Decimal("30.50"), "USD"),
            (TODAY - timedelta(days=2), Decimal("99.00"), "USD"),
        ]
        f = pr.card_reconciliation(CARD, unreconciled=overdue, today=TODAY)
        assert f is not None and f.flag_type == pr.UNRECONCILED_CARD
        assert f.severity == "advisory" and f.clause_ref == "CARD-03"
        assert f.observed["overdue_count"] == 2 and "42.50 USD" in f.reason
        assert f.observed["oldest_txn_date"] == (TODAY - timedelta(days=45)).isoformat()


# ---------------------------------------------------------------------------
# The engine never evaluates a process rule per line / option
# ---------------------------------------------------------------------------


class TestEngineReach:
    @pytest.mark.parametrize("rule", PROCESS_RULES, ids=lambda r: r["clause_ref"])
    def test_process_rule_reaches_no_item(self, rule: dict) -> None:
        for item in (
            EvalItem(item_type="flight", amount_usd=500.0, cabin_class="economy"),
            EvalItem(item_type="receipt", amount_usd=50.0, expense_category="meals"),
            EvalItem(item_type="hotel", amount_usd=200.0, city="Denver"),
        ):
            assert not PolicyEvalTool.rule_applies(
                rule["rule_type"], rule["applies_to"], rule["definition"], item
            )

    def test_receipt_and_cabin_rules_still_apply(self) -> None:
        item = EvalItem(item_type="flight", amount_usd=500.0, cabin_class="economy")
        assert PolicyEvalTool.rule_applies("cabin_class", "flight", CABIN, item)


# ---------------------------------------------------------------------------
# Semantics (policy page)
# ---------------------------------------------------------------------------


class TestSemantics:
    @pytest.mark.parametrize("rule", [*PROCESS_RULES, *RULES], ids=lambda r: r["clause_ref"])
    def test_seeded_rules_validate(self, rule: dict) -> None:
        assert (
            validate_rule_definition(rule["rule_type"], rule["applies_to"], rule["definition"])
            == ()
        )

    @pytest.mark.parametrize("rule", PROCESS_RULES, ids=lambda r: r["clause_ref"])
    def test_described_with_their_clause(self, rule: dict) -> None:
        text = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
        assert rule["clause_ref"] in text and "Warning" not in text

    def test_modifier_sentences(self) -> None:
        receipt = " ".join(describe_rule("receipt_required", "all", RECEIPT))
        assert "EXP-04" in receipt and "$75" in receipt and "3 per quarter" in receipt
        cabin = " ".join(describe_rule("cabin_class", "flight", CABIN))
        assert "AIR-04" in cabin and "SVP" in cabin

    def test_invalid_shapes_are_refused(self) -> None:
        bad = {**DEADLINE, "stale_days": 10}
        assert sem.validate_process_kind("category_allow", "all", bad)
        assert sem.validate_process_kind("amount_cap", "all", DEADLINE)  # wrong shape
        assert sem.validate_process_kind("category_allow", "all", {**DEADLINE, "x": 1})
        assert validate_rule_definition(
            "receipt_required", "all", {**RECEIPT, "affidavit_max_usd": "x"}
        )
        assert validate_rule_definition(
            "cabin_class", "flight", {**CABIN, "role_exception_class": ""}
        )
