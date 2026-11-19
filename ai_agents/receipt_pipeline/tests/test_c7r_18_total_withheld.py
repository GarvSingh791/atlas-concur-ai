"""C7R-18 residual — an illegible / ambiguous TOTAL is never filled by any later stage.

Staging re-acceptance: a receipt whose TOTAL (18.73) is blurred but whose SUBTOTAL
(17.20) is readable came back with amount 17.2 @ 0.6 ``ai_inferred`` — the C7-18
guard demoted the OCR amount, then the LLM gap-fill re-inferred it from the
subtotal it was shown — and no review signal reached the client.

Contract pinned here (hermetic; no DB, no network):
  * total_legible False / total_ambiguous True / amount==subtotal ambiguity ->
    the amount is WITHHELD: value null, confidence 0.0, provenance ai_inferred,
    and it is never sent to (nor accepted from) the LLM gap-fill;
  * the same holds when the provider returns NO amount but says the total is
    illegible (the "other" staging run shape);
  * a gap-filled amount equal to the printed subtotal is withheld too;
  * the subtotal / tax / legibility signals never reach the gap-fill prompt;
  * the withheld reason is persisted (extracted_fields.review_flags) and surfaces
    as ReceiptResponse.needs_review / review_reasons, until the user corrects it;
  * an ordinary low-confidence amount with no subtotal is still gap-filled
    (eval-db M-A1 compatibility).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.core import (
    REVIEW_AMOUNT_EQUALS_SUBTOTAL,
    REVIEW_TOTAL_AMBIGUOUS,
    REVIEW_TOTAL_ILLEGIBLE,
    REVIEW_TOTAL_UNREAD,
    run_ocr_core,
)
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from ai_agents.receipt_pipeline.review import (
    REVIEW_FLAGS_KEY,
    receipt_review_status,
)
from ai_agents.tools.itinerary_calendar import ItineraryResult
from backend.app.modules.receipt.schemas import ReceiptResponse
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS, OcrProvider, OcrResult

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_RID = uuid.uuid4()
_HIGH = dict.fromkeys(RECEIPT_FIELD_KEYS, 0.92)

# The staging receipt: TOTAL 18.73 blurred, SUBTOTAL 17.20 + TAX 1.53 readable.
_BLURRED: dict[str, Any] = {
    "amount": 17.20,
    "currency": "USD",
    "date": "2026-06-11",
    "vendor": "Corner Deli",
    "location": "New York, NY",
    "expense_type": "meals",
    "subtotal": 17.20,
    "tax": 1.53,
    "total_legible": False,
}

TOTAL_ILLEGIBLE_MSG = "Total is not legible — enter the receipt total"


class _Storage:
    def get_object(self, key: str) -> bytes:
        return b"img"


class _Ocr(OcrProvider):
    def __init__(self, fields: dict, conf: dict) -> None:
        self._fields, self._conf = fields, conf

    def extract(self, image: bytes | str) -> OcrResult:
        return OcrResult(fields=dict(self._fields), field_confidence=dict(self._conf))


class _Llm(LlmProvider):
    """Gap-fill stub that ALWAYS answers with the subtotal as the amount."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.extend(m.content for m in messages)
        return LlmResponse(
            text=json.dumps(self._payload),
            model="stub",
            usage=LlmUsage(input_tokens=1, output_tokens=1),
            tool_calls=[],
        )

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _receipt() -> MagicMock:
    r = MagicMock(spec=Receipt)
    r.id = _RID
    r.image_url = "k"
    r.organization_id = _ORG
    r.uploaded_by = _USER
    r.ocr_status = OcrStatus.pending
    r.extracted_fields = None
    r.field_confidence = None
    r.field_provenance = None
    r.grounding_context = None
    r.processing_path = None
    return r


def _run(fields: dict, conf: dict, llm: _Llm) -> tuple[MagicMock, list[Any]]:
    receipt = _receipt()
    session = MagicMock()
    session.scalar.return_value = receipt
    tool = MagicMock()
    tool.get_itinerary.return_value = ItineraryResult(trips=(), segments=(), calendar_events=())
    logged: list[Any] = []
    with patch(
        "ai_agents.receipt_pipeline.pipeline.write_decision_log",
        side_effect=lambda s, *, context, record: logged.append(record),
    ):
        run_receipt_pipeline(
            session,
            context=AgentContext(
                organization_id=_ORG, user_id=_USER, granted_permission_keys=frozenset()
            ),
            receipt_id=_RID,
            ocr=_Ocr(fields, conf),
            llm=llm,
            storage=_Storage(),  # type: ignore[arg-type]
            itinerary_tool=tool,
        )
    return receipt, logged


def _assert_withheld(receipt: MagicMock, reason: str) -> None:
    assert receipt.extracted_fields["amount"] is None
    assert receipt.field_confidence["amount"] == 0.0
    assert receipt.field_provenance["amount"] == "ai_inferred"
    assert receipt.extracted_fields[REVIEW_FLAGS_KEY] == {"amount": reason}
    assert receipt.ocr_status == OcrStatus.processed


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_illegible_total_amount_is_withheld_at_routing() -> None:
    routing = run_ocr_core("k", ocr=_Ocr(_BLURRED, _HIGH), storage=_Storage())  # type: ignore[arg-type]
    assert routing.withheld_fields == frozenset({"amount"})
    assert routing.review_reasons == {"amount": REVIEW_TOTAL_ILLEGIBLE}


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        ({"total_legible": False}, REVIEW_TOTAL_ILLEGIBLE),
        ({"total_ambiguous": True}, REVIEW_TOTAL_AMBIGUOUS),
    ],
)
def test_missing_amount_with_illegible_signal_is_withheld(signal: dict, reason: str) -> None:
    fields = {**_BLURRED, "amount": None, "total_legible": None, **signal}
    conf = {**_HIGH, "amount": 0.0}
    routing = run_ocr_core("k", ocr=_Ocr(fields, conf), storage=_Storage())  # type: ignore[arg-type]
    assert routing.withheld_fields == frozenset({"amount"})
    assert routing.review_reasons == {"amount": reason}


def test_clean_receipt_withholds_nothing() -> None:
    fields = {**_BLURRED, "amount": 18.73, "total_legible": True}
    routing = run_ocr_core("k", ocr=_Ocr(fields, _HIGH), storage=_Storage())  # type: ignore[arg-type]
    assert routing.withheld_fields == frozenset()


# ---------------------------------------------------------------------------
# Pipeline — the staging defect
# ---------------------------------------------------------------------------


def test_gap_fill_never_refills_an_illegible_total_from_the_subtotal() -> None:
    llm = _Llm({"amount": 17.2, "location": "New York, NY"})
    receipt, logged = _run(_BLURRED, _HIGH, llm)
    _assert_withheld(receipt, REVIEW_TOTAL_ILLEGIBLE)
    # The only routed gap was the withheld amount -> the LLM is never consulted.
    assert llm.prompts == []
    # processing_path still says the receipt left the deterministic path (C7-18).
    assert receipt.processing_path == ProcessingPath.agent_gap_fill
    assert logged[0].output_summary["review_reasons"] == {"amount": REVIEW_TOTAL_ILLEGIBLE}
    assert logged[0].output_summary["needs_review"] is True


def test_missing_amount_with_illegible_total_stays_null_even_when_llm_guesses() -> None:
    fields = {**_BLURRED, "amount": None, "location": None}
    conf = {**_HIGH, "amount": 0.0, "location": 0.0}
    llm = _Llm({"amount": 17.2, "location": "New York, NY"})
    receipt, _ = _run(fields, conf, llm)
    _assert_withheld(receipt, REVIEW_TOTAL_ILLEGIBLE)
    # The LLM ran for the location only: amount was never requested, and the
    # subtotal / tax / legibility signals were never shown to it.
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "Fields to infer: ['location']" in prompt
    for leaked in ("subtotal", "17.2", "1.53", "total_legible", REVIEW_FLAGS_KEY):
        assert leaked not in prompt, leaked


def test_gap_filled_amount_equal_to_subtotal_is_withheld() -> None:
    # Provider (inconsistently) affirms a legible total yet returns no amount, so
    # routing does not withhold; the post-gap-fill subtotal check still must
    # (a positive, tax-exclusive tax is printed, so total cannot equal subtotal).
    fields = dict(_BLURRED)
    fields["amount"] = None
    fields["total_legible"] = True
    conf = {**_HIGH, "amount": 0.2}
    receipt, _ = _run(fields, conf, _Llm({"amount": 17.2}))
    _assert_withheld(receipt, REVIEW_AMOUNT_EQUALS_SUBTOTAL)


# Review round: legibility NOT reported (the openrouter parser drops a non-bool
# value), amount missing, subtotal and/or tax readable. The gap-fill LLM never
# sees the image, so any total it returns (here 19.0, not equal to the subtotal,
# so the post-check cannot catch it) is invented -> must be withheld.
@pytest.mark.parametrize(
    "printed",
    [
        {"subtotal": 17.20, "tax": 1.53},
        {"subtotal": 17.20},
        {"tax": 1.53},
    ],
)
def test_unreported_legibility_missing_amount_guess_is_withheld(printed: dict) -> None:
    fields = {k: v for k, v in _BLURRED.items() if k in RECEIPT_FIELD_KEYS}
    fields.update(printed)
    fields["amount"] = None
    conf = {**_HIGH, "amount": 0.0}
    routing = run_ocr_core("k", ocr=_Ocr(fields, conf), storage=_Storage())  # type: ignore[arg-type]
    assert routing.withheld_fields == frozenset({"amount"})
    assert routing.review_reasons == {"amount": REVIEW_TOTAL_UNREAD}
    llm = _Llm({"amount": 19.0})
    receipt, logged = _run(fields, conf, llm)
    _assert_withheld(receipt, REVIEW_TOTAL_UNREAD)
    assert llm.prompts == []  # amount was the only gap -> never asked
    assert logged[0].output_summary["needs_review"] is True


def test_ordinary_low_confidence_amount_is_still_gap_filled() -> None:
    # No subtotal / legibility signal -> pre-existing gap-fill behaviour (eval-db M-A1).
    fields = {k: v for k, v in _BLURRED.items() if k in RECEIPT_FIELD_KEYS}
    fields["amount"] = None
    conf = {**_HIGH, "amount": 0.2}
    receipt, _ = _run(fields, conf, _Llm({"amount": 42.5}))
    assert receipt.extracted_fields["amount"] == 42.5
    assert receipt.field_provenance["amount"] == "ai_inferred"
    assert REVIEW_FLAGS_KEY not in receipt.extracted_fields


def test_provider_supplied_review_flags_key_is_never_trusted() -> None:
    fields = {**_BLURRED, "amount": 18.73, "total_legible": True, REVIEW_FLAGS_KEY: {"x": "y"}}
    receipt, _ = _run(fields, _HIGH, _Llm({}))
    assert REVIEW_FLAGS_KEY not in receipt.extracted_fields
    assert receipt.extracted_fields["amount"] == 18.73


# ---------------------------------------------------------------------------
# Review status + ReceiptResponse exposure
# ---------------------------------------------------------------------------


def _response(**over: Any) -> ReceiptResponse:
    base: dict[str, Any] = dict(
        id=_RID,
        organization_id=_ORG,
        uploaded_by=_USER,
        expense_id=None,
        image_url="k",
        ocr_status="processed",
        processing_path="agent_gap_fill",
        extracted_fields={
            **{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS},
            "amount": None,
            REVIEW_FLAGS_KEY: {"amount": REVIEW_TOTAL_ILLEGIBLE},
        },
        field_confidence={**_HIGH, "amount": 0.0},
        field_provenance={
            **dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
            "amount": "ai_inferred",
        },
        created_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return ReceiptResponse.model_validate(base)


def test_response_exposes_needs_review_for_withheld_total() -> None:
    resp = _response()
    assert resp.needs_review is True
    assert resp.review_reasons == [TOTAL_ILLEGIBLE_MSG]
    dumped = resp.model_dump()
    assert dumped["needs_review"] is True
    assert dumped["review_reasons"] == [TOTAL_ILLEGIBLE_MSG]


def test_user_correction_clears_the_review() -> None:
    resp = _response(
        extracted_fields={
            **{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS},
            "amount": 18.73,
            REVIEW_FLAGS_KEY: {"amount": REVIEW_TOTAL_ILLEGIBLE},
        },
        field_provenance={
            **dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
            "amount": "user_edited",
        },
    )
    assert resp.needs_review is False
    assert resp.review_reasons == []


def test_clean_receipt_needs_no_review() -> None:
    resp = _response(
        extracted_fields={**{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS}, "amount": 18.73},
        field_confidence=_HIGH,
        field_provenance=dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
        processing_path="deterministic",
    )
    assert resp.needs_review is False
    assert resp.review_reasons == []


def test_review_reasons_cover_failed_pending_and_low_confidence() -> None:
    failed = receipt_review_status(
        ocr_status="failed", extracted_fields=None, field_confidence=None, field_provenance=None
    )
    assert failed[0] is True and failed[1] == [
        "Receipt could not be read — enter the details manually"
    ]
    pending = receipt_review_status(
        ocr_status="pending", extracted_fields=None, field_confidence=None, field_provenance=None
    )
    assert pending == (False, [])
    needs, reasons = receipt_review_status(
        ocr_status=OcrStatus.processed,
        extracted_fields={
            **{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS},
            "location": None,
            "expense_type": "lodging",  # the city feeds HOTEL-CAP
        },
        field_confidence={**_HIGH, "vendor": 0.6, "location": 0.0},
        field_provenance={
            **dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
            "vendor": "ai_inferred",
            "location": "ai_inferred",
        },
    )
    assert needs is True
    assert reasons == [
        "Vendor was inferred with low confidence — confirm it",
        "Location could not be read — enter it",
    ]


@pytest.mark.parametrize("expense_type", ["meals", "Transportation", "other"])
def test_missing_location_is_not_a_review_reason_outside_lodging(expense_type: str) -> None:
    """C7R review: a rideshare / online / meal receipt without a city is complete.

    The city only feeds HOTEL-CAP; flagging every non-hotel receipt without one
    put a needs-review banner on receipts the user cannot do anything about.
    """
    fields = {
        **{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS},
        "amount": 18.73,
        "location": None,
        "expense_type": expense_type,
    }
    assert receipt_review_status(
        ocr_status=OcrStatus.processed,
        extracted_fields=fields,
        field_confidence={**_HIGH, "location": 0.0},
        field_provenance=dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
    ) == (False, [])
    # Unknown expense type: conservative, the location is still asked for.
    needs, reasons = receipt_review_status(
        ocr_status=OcrStatus.processed,
        extracted_fields={**fields, "expense_type": None},
        field_confidence={**_HIGH, "location": 0.0, "expense_type": 0.0},
        field_provenance=dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
    )
    assert needs is True and "Location could not be read — enter it" in reasons


def test_low_confidence_amount_asks_the_user_to_enter_the_total() -> None:
    """C7R review: the UI withholds a < 0.70 amount, so the reason must not say
    'confirm' a value the user cannot see."""
    needs, reasons = receipt_review_status(
        ocr_status=OcrStatus.processed,
        extracted_fields={**{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS}, "amount": 980.0},
        field_confidence={**_HIGH, "amount": 0.6},
        field_provenance={
            **dict.fromkeys(RECEIPT_FIELD_KEYS, "deterministic"),
            "amount": "ai_inferred",
        },
    )
    assert needs is True
    assert reasons == ["Amount was not read with confidence — enter the receipt total"]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (REVIEW_TOTAL_ILLEGIBLE, TOTAL_ILLEGIBLE_MSG),
        (REVIEW_TOTAL_AMBIGUOUS, "Total is ambiguous — enter the receipt total"),
        (
            REVIEW_AMOUNT_EQUALS_SUBTOTAL,
            "Only the subtotal could be read — enter the receipt total",
        ),
        (
            REVIEW_TOTAL_UNREAD,
            "Total could not be read (only subtotal/tax visible) — enter the receipt total",
        ),
    ],
)
def test_every_withhold_reason_has_a_human_message(code: str, message: str) -> None:
    resp = _response(
        extracted_fields={
            **{k: _BLURRED[k] for k in RECEIPT_FIELD_KEYS},
            "amount": None,
            REVIEW_FLAGS_KEY: {"amount": code},
        }
    )
    assert resp.review_reasons == [message]
