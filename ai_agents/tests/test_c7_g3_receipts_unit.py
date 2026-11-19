"""C7 G3 (receipts) — hermetic regression tests for C7-16, C7-18, C7-21, C7-22.

No DB, no network: the OCR provider and storage are in-memory stubs.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ai_agents.agents.expense_agent.categorize import (
    _categorize_single,
    category_for_expense_type,
)
from ai_agents.agents.expense_agent.ingest import CardTxnFact, IngestedData, ReceiptFact
from ai_agents.agents.expense_agent.matching import MatchCandidate, smart_match
from ai_agents.receipt_pipeline.core import (
    AMBIGUOUS_TOTAL_CONFIDENCE_CAP,
    REVIEW_AMOUNT_EQUALS_SUBTOTAL,
    REVIEW_TOTAL_ILLEGIBLE,
    detect_total_ambiguity,
    run_ocr_core,
)
from ai_agents.receipt_pipeline.correction import (
    CORRECTABLE_FIELDS,
    InvalidCorrectionError,
    normalize_correction,
)
from backend.app.modules.receipt.schemas import ReceiptCorrectionRequest
from database.models.expense import ExpenseCategory
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS, OcrProvider, OcrResult
from services.ocr_provider.openrouter import (
    _USER_INSTRUCTION,
    OCR_EXPENSE_TYPES,
    OpenRouterVisionOcrProvider,
)

_TODAY = date(2026, 6, 15)

# ---------------------------------------------------------------------------
# C7-16 — correction field + value validation
# ---------------------------------------------------------------------------


def test_correctable_fields_are_exactly_the_routed_ocr_keys() -> None:
    assert frozenset(RECEIPT_FIELD_KEYS) == CORRECTABLE_FIELDS


@pytest.mark.parametrize(
    "field",
    [
        "not_a_field",
        "expense_id",  # a real Receipt column, not a correctable field
        "ocr_status",
        "subtotal",  # an OCR extra, not a correctable field
        "per_night_rate",
        "",
    ],
)
def test_non_correctable_field_rejected(field: str) -> None:
    with pytest.raises(InvalidCorrectionError):
        normalize_correction(field, "x", today=_TODAY)


@pytest.mark.parametrize(
    "value",
    ["abc", -999999, -1, 0, "0", 1.234, "12.345", "NaN", "Infinity", True, 1e9, None, [1], {}],
)
def test_invalid_amount_rejected(value: object) -> None:
    with pytest.raises(InvalidCorrectionError):
        normalize_correction("amount", value, today=_TODAY)


@pytest.mark.parametrize(("value", "expected"), [(18.73, 18.73), ("18.73", 18.73), (20, 20.0)])
def test_valid_amount_normalized(value: object, expected: float) -> None:
    assert normalize_correction("amount", value, today=_TODAY) == expected


def test_currency_normalized_and_validated() -> None:
    assert normalize_correction("currency", " usd ", today=_TODAY) == "USD"
    for bad in ["US", "USDX", "U1D", 840, "", None]:
        with pytest.raises(InvalidCorrectionError):
            normalize_correction("currency", bad, today=_TODAY)


def test_date_validated() -> None:
    assert normalize_correction("date", "2026-06-11", today=_TODAY) == "2026-06-11"
    for bad in ["2999-01-01", "1990-01-01", "06/11/2026", "2026-13-01", "2026-06-11T10:00", 1]:
        with pytest.raises(InvalidCorrectionError):
            normalize_correction("date", bad, today=_TODAY)


def test_text_fields_validated() -> None:
    assert normalize_correction("vendor", "  Metro Cab ", today=_TODAY) == "Metro Cab"
    for bad in ["", "   ", "x" * 201, None, 5]:
        with pytest.raises(InvalidCorrectionError):
            normalize_correction("vendor", bad, today=_TODAY)
        with pytest.raises(InvalidCorrectionError):
            normalize_correction("location", bad, today=_TODAY)


@pytest.mark.parametrize(
    "value",
    ["a\x00b", " \x00 ", "line\nbreak", "tab\there", "\u200b", "\u200b\u200c", "x\ud800"],
)
@pytest.mark.parametrize("field", ["vendor", "location"])
def test_text_with_control_or_invisible_chars_rejected(field: str, value: str) -> None:
    """Review finding: NUL reached Postgres (TEXT/JSONB) → 500 instead of 422."""
    with pytest.raises(InvalidCorrectionError):
        normalize_correction(field, value, today=_TODAY)
    with pytest.raises(ValidationError):
        ReceiptCorrectionRequest.model_validate({"field": field, "value": value})


def test_text_with_format_char_inside_visible_text_is_allowed() -> None:
    # ZWNJ is a legitimate letter joiner in e.g. Persian names.
    assert normalize_correction("vendor", "\u0645\u06cc\u200c\u0647\u0627", today=_TODAY)


def test_expense_type_validated() -> None:
    assert normalize_correction("expense_type", "Transportation", today=_TODAY) == "transportation"
    with pytest.raises(InvalidCorrectionError):
        normalize_correction("expense_type", "bogus", today=_TODAY)


@pytest.mark.parametrize(
    "body",
    [
        {"field": "not_a_field", "value": "x"},
        {"field": "amount", "value": "abc"},
        {"field": "amount", "value": -999999},
        {"field": "amount", "value": 10, "extra": 1},
    ],
)
def test_schema_rejects_c7_16_payloads(body: dict) -> None:
    with pytest.raises(ValidationError):
        ReceiptCorrectionRequest.model_validate(body)


def test_schema_normalizes_value() -> None:
    req = ReceiptCorrectionRequest.model_validate({"field": "currency", "value": "eur"})
    assert req.value == "EUR"


# ---------------------------------------------------------------------------
# C7-21 — every OCR expense_type maps to a real category
# ---------------------------------------------------------------------------


def test_every_ocr_prompt_expense_type_maps_to_a_category() -> None:
    valid = {c.value for c in ExpenseCategory}
    for et in OCR_EXPENSE_TYPES:
        assert et in _USER_INSTRUCTION
        assert category_for_expense_type(et) in valid, et
    # Only "other" may legitimately map to "other".
    assert category_for_expense_type("transportation") == "ground_transport"
    assert category_for_expense_type(" Transportation ") == "ground_transport"
    assert category_for_expense_type("ground transport") == "ground_transport"
    assert category_for_expense_type("hotel") == "lodging"
    assert category_for_expense_type("bogus") is None
    assert category_for_expense_type(None) is None


def _receipt(**over: object) -> ReceiptFact:
    base: dict = dict(
        receipt_id="r-1",
        amount=93.44,
        currency="USD",
        date="2026-06-10",
        vendor="METRO YELLOW CAB 1731",
        location="Manhattan",
        expense_type="transportation",
        line_items=None,
        already_linked=False,
    )
    base.update(over)
    return ReceiptFact(**base)


def test_transportation_receipt_categorized_as_ground_transport() -> None:
    cand = MatchCandidate(
        key="receipt:r-1",
        amount=93.44,
        currency="USD",
        date="2026-06-10",
        vendor="ZZZ",  # no vendor keyword hit — the expense_type must decide
        receipt=_receipt(vendor="ZZZ"),
        card_txn=None,
        segment=None,
        match_source="receipt",
        match_confidence=0.75,
        is_duplicate_resolved=False,
    )
    assert _categorize_single(cand) == "ground_transport"


# ---------------------------------------------------------------------------
# C7-18 — total/subtotal cross-check + prompt
# ---------------------------------------------------------------------------


def test_prompt_forbids_subtotal_substitution() -> None:
    text = _USER_INSTRUCTION
    assert "NEVER substitute the subtotal" in text
    assert '"total_legible"' in text
    assert '"subtotal"' in text


class _StubStorage:
    def get_object(self, key: str) -> bytes:
        return b"img"


class _StubOcr(OcrProvider):
    def __init__(self, fields: dict, conf: dict) -> None:
        self._fields = fields
        self._conf = conf

    def extract(self, image: bytes | str) -> OcrResult:
        return OcrResult(fields=dict(self._fields), field_confidence=dict(self._conf))


_R5_FIELDS: dict[str, str | float | None] = {
    "amount": 17.20,
    "currency": "USD",
    "date": "2026-06-11",
    "vendor": "Corner Deli",
    "location": "New York, NY",
    "expense_type": "meals",
    "subtotal": 17.20,
    "tax": 1.53,
}
_HIGH = dict.fromkeys(RECEIPT_FIELD_KEYS, 0.9)


def test_r5_subtotal_as_total_is_demoted_to_gap_fill() -> None:
    routing = run_ocr_core(
        "k",
        ocr=_StubOcr(_R5_FIELDS, _HIGH),
        storage=_StubStorage(),  # type: ignore[arg-type]
    )
    assert "amount" in routing.gap_fill_fields
    assert "amount" not in routing.deterministic_fields
    assert routing.review_reasons == {"amount": REVIEW_AMOUNT_EQUALS_SUBTOTAL}
    assert routing.ocr_result.field_confidence["amount"] == AMBIGUOUS_TOTAL_CONFIDENCE_CAP
    assert AMBIGUOUS_TOTAL_CONFIDENCE_CAP < 0.70


def test_total_illegible_signal_demotes_amount() -> None:
    fields = {**_R5_FIELDS, "total_legible": False}
    fields.pop("tax")
    routing = run_ocr_core(
        "k",
        ocr=_StubOcr(fields, _HIGH),
        storage=_StubStorage(),  # type: ignore[arg-type]
    )
    assert "amount" in routing.gap_fill_fields
    assert routing.review_reasons == {"amount": REVIEW_TOTAL_ILLEGIBLE}


def test_clean_receipt_stays_deterministic() -> None:
    fields = {**_R5_FIELDS, "amount": 18.73, "total_legible": True}
    routing = run_ocr_core(
        "k",
        ocr=_StubOcr(fields, _HIGH),
        storage=_StubStorage(),  # type: ignore[arg-type]
    )
    assert "amount" in routing.deterministic_fields
    assert routing.review_reasons == {}


def test_subtotal_equal_without_tax_is_not_ambiguous_when_total_affirmed() -> None:
    # A tax-free receipt whose total the provider affirms legible is trusted.
    fields = {k: v for k, v in _R5_FIELDS.items() if k != "tax"}
    fields["total_legible"] = True
    assert detect_total_ambiguity(OcrResult(fields=fields)) is None
    assert (
        detect_total_ambiguity(OcrResult(fields={"amount": None, "total_legible": False})) is None
    )


@pytest.mark.parametrize("tax", [None, "absent"])
def test_r5_shape_without_tax_or_legibility_signal_is_demoted(tax: object) -> None:
    """Review finding: the staging R5 shape (blurred total, unreadable tax,
    legibility NOT reported) must still route the amount to gap-fill. On the
    pre-review rule (amount==subtotal only with tax>0) this returned None and the
    17.20 subtotal stayed deterministic at 0.9 — the exact C7-18 outcome."""
    fields = {k: v for k, v in _R5_FIELDS.items() if k != "tax"}
    if tax is None:
        fields["tax"] = None
    assert detect_total_ambiguity(OcrResult(fields=fields)) == REVIEW_AMOUNT_EQUALS_SUBTOTAL
    routing = run_ocr_core(
        "k",
        ocr=_StubOcr(fields, _HIGH),
        storage=_StubStorage(),  # type: ignore[arg-type]
    )
    assert "amount" in routing.gap_fill_fields
    assert "amount" not in routing.deterministic_fields


def test_r5_with_tax_is_demoted_even_if_model_affirms_legibility() -> None:
    # Tax added on top: total == subtotal is impossible, whatever the model says.
    fields = {**_R5_FIELDS, "total_legible": True}
    assert detect_total_ambiguity(OcrResult(fields=fields)) == REVIEW_AMOUNT_EQUALS_SUBTOTAL
    fields["tax_included"] = False
    assert detect_total_ambiguity(OcrResult(fields=fields)) == REVIEW_AMOUNT_EQUALS_SUBTOTAL


def test_vat_inclusive_receipt_with_affirmed_total_stays_deterministic() -> None:
    """Review finding: 'Subtotal 100.00 incl. VAT 16.67 / Total 100.00' — a legible,
    affirmed, VAT-inclusive total must not be demoted to review."""
    fields = {
        **_R5_FIELDS,
        "amount": 100.00,
        "subtotal": 100.00,
        "tax": 16.67,
        "currency": "EUR",
        "total_legible": True,
        "tax_included": True,
    }
    assert detect_total_ambiguity(OcrResult(fields=fields)) is None
    routing = run_ocr_core(
        "k",
        ocr=_StubOcr(fields, _HIGH),
        storage=_StubStorage(),  # type: ignore[arg-type]
    )
    assert "amount" in routing.deterministic_fields
    assert routing.review_reasons == {}


def test_prompt_asks_for_tax_included_and_provider_parses_it() -> None:
    assert '"tax_included"' in _USER_INSTRUCTION
    result = OpenRouterVisionOcrProvider()._build_result(
        {
            "amount": {"value": "100.00", "confidence": 0.9},
            "subtotal": {"value": "100.00", "confidence": 0.9},
            "tax": {"value": 16.67, "confidence": 0.9},
            "total_legible": True,
            "tax_included": {"value": "true", "confidence": 0.9},
        }
    )
    assert result.fields["tax_included"] is True
    assert detect_total_ambiguity(result) is None


def test_openrouter_parses_subtotal_tax_and_legibility() -> None:
    provider = OpenRouterVisionOcrProvider()
    result = provider._build_result(
        {
            "amount": {"value": "17.20", "confidence": 0.9},
            "subtotal": {"value": "$17.20", "confidence": 0.9},
            "tax": {"value": 1.53, "confidence": 0.8},
            "total_legible": {"value": False, "confidence": 0.9},
        }
    )
    assert result.fields["subtotal"] == 17.20
    assert result.fields["tax"] == 1.53
    assert result.fields["total_legible"] is False
    assert detect_total_ambiguity(result) == REVIEW_TOTAL_ILLEGIBLE


# ---------------------------------------------------------------------------
# C7-22 — inbox-linked receipts remain eligible for card smart-match
# ---------------------------------------------------------------------------


def _txn(**over: object) -> CardTxnFact:
    base: dict = dict(
        txn_id="t-1",
        amount=320.00,
        currency="USD",
        merchant="UNITED AIRLINES",
        txn_date="2026-06-10",
        last_four="4242",
        already_matched=False,
    )
    base.update(over)
    return CardTxnFact(**base)


def _data(receipts: list[ReceiptFact], txns: list[CardTxnFact]) -> IngestedData:
    return IngestedData(
        receipts=receipts, card_txns=txns, segments=[], trip_id=None, unprocessed_receipts=[]
    )


def _r8(**over: object) -> ReceiptFact:
    return _receipt(
        receipt_id="r-8",
        amount=320.00,
        vendor="UNITED",
        expense_type="airfare",
        **over,
    )


def test_inbox_linked_receipt_smart_matches_card_txn() -> None:
    cands = smart_match(_data([_r8(inbox_expense_id="e-inbox")], [_txn()]))
    assert len(cands) == 1
    (c,) = cands
    assert c.receipt is not None and c.receipt.receipt_id == "r-8"
    assert c.card_txn is not None and c.card_txn.txn_id == "t-1"
    assert c.is_duplicate_resolved is True


def test_inbox_linked_receipt_without_card_match_is_not_duplicated() -> None:
    cands = smart_match(_data([_r8(inbox_expense_id="e-inbox")], []))
    assert cands == []


def test_receipt_linked_elsewhere_is_still_excluded() -> None:
    cands = smart_match(_data([_r8(already_linked=True)], [_txn()]))
    assert len(cands) == 1
    assert cands[0].receipt is None  # card-only entry; the receipt stays where it is
