"""Product gap 8 (fxcard track): foreign-currency amounts vs USD policy caps.

Before: a EUR receipt was compared against USD caps unconverted, and an unknown
currency silently passed as if it were USD.  Pinned (hermetic, no DB):

  * ``config.constants.fx_rates``: Decimal conversion, ROUND_HALF_UP to the
    target's minor unit (JPY has none), explicit ``UnknownCurrency``, round trip;
  * ``PolicyEvalTool`` (the one choke point, via ``evaluate_candidate`` which
    dispatches through ``_evaluate_rule``): converts to the rule currency before
    the cap, quotes "EUR 220.00 ≈ USD 238.70 at 1.085" in the reason, and returns
    an advisory "currency not supported" verdict instead of a pass;
  * USD / currency-less items are byte-for-byte unchanged;
  * presubmit ``verdicts_to_flags``: one escalating ``currency_unverified`` flag;
  * ``build_eval_item_from_fields`` tags the receipt's currency.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from ai_agents.agents.approval_agent.evaluate import _is_blocking
from ai_agents.agents.expense_agent.assemble import assemble_report
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    ESCALATING_FLAG_TYPES,
    build_eval_item_from_fields,
    verdicts_to_flags,
)
from ai_agents.tools.fx_policy import CURRENCY_UNVERIFIED_FLAG, FX_STATUS_KEY, FX_UNSUPPORTED
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from config.constants.fx_rates import (
    RATES_AS_OF,
    SUPPORTED_CURRENCIES,
    UnknownCurrency,
    convert,
    display_rate,
    format_money,
    is_supported,
    normalize_currency,
)

# ---------------------------------------------------------------------------
# Pure conversion
# ---------------------------------------------------------------------------


class TestFxRates:
    def test_supported_list(self) -> None:
        assert set(SUPPORTED_CURRENCIES) == {
            "USD",
            "EUR",
            "GBP",
            "CAD",
            "JPY",
            "INR",
            "AUD",
            "CHF",
            "MXN",
            "SGD",
            "CNY",
        }
        assert RATES_AS_OF  # documented as-of date

    def test_eur_to_usd_example(self) -> None:
        assert convert(Decimal("220.00"), "EUR", "USD") == Decimal("238.70")
        assert display_rate("EUR", "USD") == "1.085"

    def test_result_is_decimal_quantized_to_cents(self) -> None:
        out = convert(Decimal("10"), "EUR", "USD")
        assert isinstance(out, Decimal)
        assert out == Decimal("10.85") and out.as_tuple().exponent == -2

    def test_round_half_up_not_bankers(self) -> None:
        # 0.50 CAD x 0.73 = 0.365 exactly: half-up -> 0.37 (half-even would be 0.36).
        exact = Decimal("0.50") * Decimal("0.73")
        assert exact == Decimal("0.365")
        assert exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) == Decimal("0.36")
        assert convert(Decimal("0.50"), "CAD", "USD") == Decimal("0.37")

    def test_rounding_just_below_half(self) -> None:
        # 0.10 EUR x 1.085 = 0.1085 -> 0.11; 0.04 EUR -> 0.0434 -> 0.04.
        assert convert(Decimal("0.10"), "EUR", "USD") == Decimal("0.11")
        assert convert(Decimal("0.04"), "EUR", "USD") == Decimal("0.04")

    def test_jpy_zero_decimal_target(self) -> None:
        out = convert(Decimal("10.00"), "USD", "JPY")  # 10 / 0.0067 = 1492.537...
        assert out == Decimal("1493")
        assert out.as_tuple().exponent == 0
        assert format_money(out, "JPY") == "JPY 1493"

    def test_jpy_source_to_usd(self) -> None:
        assert convert(Decimal("25000"), "JPY", "USD") == Decimal("167.50")
        assert convert(Decimal("1"), "JPY", "USD") == Decimal("0.01")

    def test_same_currency_is_unchanged_not_rerounded(self) -> None:
        assert convert(Decimal("12.345"), "USD", "USD") == Decimal("12.345")
        assert convert(Decimal("100"), "usd", " USD ") == Decimal("100")

    def test_float_input_uses_its_shortest_repr(self) -> None:
        assert convert(0.1, "EUR", "USD") == Decimal("0.11")

    @pytest.mark.parametrize("code", ["XYZ", "BRL", "", "US", None, 42, "dollars"])
    def test_unknown_currency_raises(self, code: object) -> None:
        with pytest.raises(UnknownCurrency):
            convert(Decimal("1"), code, "USD")  # type: ignore[arg-type]
        with pytest.raises(UnknownCurrency):
            convert(Decimal("1"), "USD", code)  # type: ignore[arg-type]
        assert not is_supported(code)

    def test_normalize_upper_cases(self) -> None:
        assert normalize_currency(" eur ") == "EUR"

    @pytest.mark.parametrize("ccy", [c for c in SUPPORTED_CURRENCIES if c != "USD"])
    @pytest.mark.parametrize("amount", ["0.01", "1.00", "19.99", "238.70", "1234.56", "99999.99"])
    def test_round_trip_within_one_minor_unit(self, ccy: str, amount: str) -> None:
        start = Decimal(amount)
        back = convert(convert(start, "USD", ccy), ccy, "USD")
        # Two roundings: at most half a target unit each way, re-expressed in USD.
        tolerance = Decimal("0.01") + Decimal("0.5") * convert(Decimal("1"), ccy, "USD").max(
            Decimal("0.01")
        )
        assert abs(back - start) <= tolerance

    def test_cross_rate_goes_through_usd(self) -> None:
        # 100 EUR -> GBP = 100 x 1.085 / 1.27 = 85.433... -> 85.43
        assert convert(Decimal("100"), "EUR", "GBP") == Decimal("85.43")
        assert display_rate("EUR", "GBP") == "0.854331"


# ---------------------------------------------------------------------------
# Policy evaluation (the choke point)
# ---------------------------------------------------------------------------

_CAP_DEFN = {"tiers": [{"tier": "default", "cap": 230}], "currency": "USD"}
_PER_DIEM_DEFN = {"rates": [{"region": "domestic", "amount": 75}], "currency": "USD"}
_RECEIPT_DEFN = {"threshold_usd": 25, "always_required_categories": []}


def _cap(item: EvalItem):
    return PolicyEvalTool().evaluate_candidate("amount_cap", "hotel", dict(_CAP_DEFN), item)


class TestPolicyEvalConversion:
    def test_eur_over_usd_cap_after_conversion(self) -> None:
        # 220 < 230 unconverted (the old bug would PASS); 238.70 USD > 230 -> violation.
        v = _cap(EvalItem(item_type="hotel", amount_usd=220.0, currency="EUR"))
        assert v.in_policy is False
        assert "EUR 220.00 ≈ USD 238.70 at 1.085" in v.reason
        assert v.observed["amount_usd"] == pytest.approx(238.70)
        fx = v.observed["fx"]
        assert fx["original_amount"] == "220.0" and fx["original_currency"] == "EUR"
        assert fx["converted_amount"] == "238.70" and fx["rate"] == "1.085"
        assert fx["rates_as_of"] == RATES_AS_OF

    def test_jpy_within_cap_after_conversion(self) -> None:
        # 30000 JPY = 201.00 USD <= 230 (unconverted 30000 would be a violation).
        v = _cap(EvalItem(item_type="hotel", amount_usd=30000.0, currency="JPY"))
        assert v.in_policy is True
        assert "JPY 30000 ≈ USD 201.00 at 0.0067" in v.reason

    def test_usd_item_is_byte_for_byte_unchanged(self) -> None:
        plain = _cap(EvalItem(item_type="hotel", amount_usd=240.0))
        tagged = _cap(EvalItem(item_type="hotel", amount_usd=240.0, currency="USD"))
        lower = _cap(EvalItem(item_type="hotel", amount_usd=240.0, currency="usd"))
        assert plain.reason == tagged.reason == lower.reason
        assert plain.observed == tagged.observed == lower.observed
        assert "fx" not in plain.observed and "≈" not in plain.reason

    def test_unknown_currency_is_advisory_not_a_pass(self) -> None:
        v = _cap(EvalItem(item_type="hotel", amount_usd=50.0, currency="BRL"))
        assert v.observed[FX_STATUS_KEY] == FX_UNSUPPORTED
        assert "currency not supported" in v.reason
        assert v.observed["currency"] == "BRL"
        assert "cap" not in v.observed  # nothing was compared

    def test_per_diem_converts(self) -> None:
        v = PolicyEvalTool().evaluate_candidate(
            "per_diem",
            "meals",
            dict(_PER_DIEM_DEFN),
            EvalItem(item_type="meals", amount_usd=60.0, currency="GBP"),
        )
        # 60 GBP = 76.20 USD > 75
        assert v.in_policy is False
        assert "GBP 60.00 ≈ USD 76.20 at 1.27" in v.reason

    def test_receipt_threshold_converts(self) -> None:
        v = PolicyEvalTool().evaluate_candidate(
            "receipt_required",
            "all",
            dict(_RECEIPT_DEFN),
            EvalItem(item_type="receipt", amount_usd=3000.0, currency="JPY"),
        )
        # 3000 JPY = 20.10 USD < 25 -> not required (unconverted 3000 would require one)
        assert v.observed["receipt_required"] is False

    def test_receipt_threshold_unknown_currency_fails_closed(self) -> None:
        v = PolicyEvalTool().evaluate_candidate(
            "receipt_required",
            "all",
            dict(_RECEIPT_DEFN),
            EvalItem(item_type="receipt", amount_usd=1.0, currency="XYZ"),
        )
        assert v.observed["receipt_required"] is True
        assert v.observed[FX_STATUS_KEY] == FX_UNSUPPORTED

    def test_non_money_rules_ignore_currency(self) -> None:
        defn = {"allowed_classes": ["economy"]}
        a = PolicyEvalTool().evaluate_candidate(
            "cabin_class",
            "flight",
            defn,
            EvalItem(item_type="flight", cabin_class="economy", currency="XYZ"),
        )
        b = PolicyEvalTool().evaluate_candidate(
            "cabin_class", "flight", defn, EvalItem(item_type="flight", cabin_class="economy")
        )
        assert a.reason == b.reason and a.observed == b.observed

    def test_hotel_stay_total_is_converted_too(self) -> None:
        v = _cap(
            EvalItem(
                item_type="hotel",
                amount_usd=220.0,
                amount_is_nightly_rate=True,
                stay_amount_usd=660.0,
                currency="EUR",
            )
        )
        assert "716.10 USD" in v.reason  # 660 EUR x 1.085


# ---------------------------------------------------------------------------
# Flags + receipt items
# ---------------------------------------------------------------------------


class TestFlags:
    def test_unsupported_currency_becomes_one_escalating_flag(self) -> None:
        cap = _cap(EvalItem(item_type="hotel", amount_usd=50.0, currency="BRL"))
        receipt = PolicyEvalTool().evaluate_candidate(
            "receipt_required",
            "all",
            dict(_RECEIPT_DEFN),
            EvalItem(item_type="receipt", amount_usd=50.0, currency="BRL"),
        )
        flags, failing = verdicts_to_flags(
            [cap, receipt],
            entry_ref="Hotel Rio",
            has_receipt=True,
            category="lodging",
            entry_id="e-1",
        )
        currency_flags = [f for f in flags if f.flag_type == CURRENCY_UNVERIFIED_FLAG]
        assert len(currency_flags) == 1
        flag = currency_flags[0]
        assert flag.severity == "advisory"
        assert "BRL is not supported" in flag.reason
        assert flag.entry_id == "e-1"
        assert failing == 0
        assert CURRENCY_UNVERIFIED_FLAG in ESCALATING_FLAG_TYPES
        assert _is_blocking(flag.to_dict()) is True  # never auto-approved

    def test_unsupported_currency_still_requires_a_missing_receipt(self) -> None:
        receipt = PolicyEvalTool().evaluate_candidate(
            "receipt_required",
            "all",
            dict(_RECEIPT_DEFN),
            EvalItem(item_type="receipt", amount_usd=5.0, currency="BRL"),
        )
        flags, _ = verdicts_to_flags(
            [receipt], entry_ref="Taxi", has_receipt=False, category="ground_transport"
        )
        types = sorted(f.flag_type for f in flags)
        assert types == [CURRENCY_UNVERIFIED_FLAG, "missing_receipt"]

    def test_receipt_fields_currency_is_tagged(self) -> None:
        item = build_eval_item_from_fields(
            {"amount": 220.0, "currency": "eur"}, category="meals", ref_id="r"
        )
        assert item.currency == "EUR"
        none = build_eval_item_from_fields({"amount": 20.0}, category="meals", ref_id="r")
        assert none.currency is None


def _draft_entry(amount: float, currency: str) -> DraftEntry:
    return DraftEntry(
        category="meals",
        amount=amount,
        currency=currency,
        expense_date="2026-06-10",
        vendor=f"{currency} vendor",
        description=None,
        status="draft",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


class TestAssembleTotals:
    """Review fix: a mixed-currency agent report total converts, never drops."""

    def test_foreign_entries_are_converted_into_the_report_currency(self) -> None:
        report = assemble_report(
            [_draft_entry(50.0, "USD"), _draft_entry(12.5, "USD"), _draft_entry(40.0, "EUR")],
            trip_id=None,
            today="2026-06-14",
        )
        assert report.mixed_currency is True and report.currency == "USD"
        assert report.total_amount == pytest.approx(105.90)  # 50 + 12.50 + EUR 40 at 1.085

    def test_report_currency_is_a_supported_one_and_unsupported_is_left_out(self) -> None:
        report = assemble_report(
            [_draft_entry(10.0, "BRL"), _draft_entry(20.0, "BRL"), _draft_entry(30.0, "USD")],
            trip_id=None,
            today="2026-06-14",
        )
        assert report.currency == "USD"
        assert report.total_amount == pytest.approx(30.0)

    def test_single_currency_total_unchanged(self) -> None:
        report = assemble_report(
            [_draft_entry(0.1, "USD"), _draft_entry(0.2, "USD")], trip_id=None, today="2026-06-14"
        )
        assert report.total_amount == round(0.1 + 0.2, 2) and report.mixed_currency is False
