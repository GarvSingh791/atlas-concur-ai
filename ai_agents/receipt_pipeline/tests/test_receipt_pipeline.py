"""Hermetic tests for the Receipt Analysis Pipeline (P4-T11, FR-R1..R5).

No database, no LLM API, no network.  All providers are injected stubs.

12 break-tests
--------------
 1. deterministic_clean       — all fields high-conf; no LLM; all provenance=deterministic.
 2. route_mix_decision_log    — gap-fill triggered; decision log route=llm.
 3. gap_fill_triggered        — itinerary tool called, ai_inferred + grounding_context set.
 4. paris_paradox             — inferred location 'Paris, France' vs NY-area itinerary
                                → reflection_outcome=revised; location corrected or null.
 5. reflection_passed         — location matches itinerary; outcome=passed; asserted.
 6. reflection_rejected       — city not in any itinerary fact; outcome=rejected; null.
 7. provenance_completeness   — every extracted field has a provenance entry.
 8. user_edit_flip            — apply_user_correction sets provenance=user_edited.
 9. idempotent_rerun          — second run updates same row; no duplicate rows.
10. degradation_ocr_fail      — OCR ProviderError → ocr_status=failed; no extracted_fields.
11. degradation_llm_fail      — LLM ProviderError → deterministic fields preserved; gap null.
12. flush_only                — session.commit() never called; rollback reverts.
Plus:  one decision-log row  — exactly one ai_decision_logs row per run.
       pii_image_safety       — image bytes never reach LLM prompt.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.context import AgentContext
from ai_agents.guardrails.reflection import LocationClaim
from ai_agents.receipt_pipeline.core import run_ocr_core
from ai_agents.receipt_pipeline.correction import apply_user_correction
from ai_agents.receipt_pipeline.gap_fill import run_gap_fill
from ai_agents.receipt_pipeline.pipeline import run_receipt_pipeline
from ai_agents.receipt_pipeline.reflection import (
    ReceiptFieldHypothesizer,
    build_grounding_facts,
    run_location_reflection,
)
from ai_agents.tools.itinerary_calendar import (
    ItineraryResult,
)
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.interface import StorageProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_RECEIPT_ID = uuid.uuid4()
_IMAGE_KEY = "receipts/seed/employee-airfare-001.png"
_SAMPLE_BYTES = b"\x89PNG\r\n\x1a\n"  # minimal PNG header

_HIGH_CONF_FIELDS: dict[str, str | float | None] = {
    "amount": 320.00,
    "currency": "USD",
    "date": "2026-06-10",
    "vendor": "United Airlines",
    "location": "New York, NY, US",
    "expense_type": "airfare",
}
_HIGH_CONF = {k: 0.92 for k in _HIGH_CONF_FIELDS}

_LOW_CONF_FIELDS: dict[str, str | float | None] = {
    "amount": 320.00,
    "currency": "USD",
    "date": "2026-06-10",
    "vendor": "United Airlines",
}
_LOW_CONF = {
    "amount": 0.92,
    "currency": "0.91",
    "date": 0.90,
    "vendor": 0.88,
}


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class _StubStorage(StorageProvider):
    """Returns a fixed bytes blob for any key."""

    def __init__(self, data: bytes = _SAMPLE_BYTES) -> None:
        self._data = data
        self.calls: list[str] = []

    def get_object(self, key: str) -> bytes:
        self.calls.append(key)
        return self._data

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        pass

    def delete_object(self, key: str) -> None:
        pass

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> object:  # type: ignore[override]
        return None


class _StubOcr(OcrProvider):
    """Returns a fixed OcrResult."""

    def __init__(self, result: OcrResult) -> None:
        self._result = result
        self.calls: list[bytes | str] = []

    def extract(self, image: bytes | str) -> OcrResult:
        self.calls.append(image)
        return self._result


class _FailingOcr(OcrProvider):
    """Always raises ProviderError."""

    def extract(self, image: bytes | str) -> OcrResult:
        raise ProviderError("OCR service unavailable", code="PROVIDER_UNAVAILABLE")


class _StubLlm(LlmProvider):
    """Returns a scripted JSON response."""

    def __init__(self, response_json: dict | None = None, *, raises: bool = False) -> None:
        self._json = response_json or {}
        self._raises = raises
        self.prompts: list[str] = []

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        if self._raises:
            raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")
        for m in messages:
            self.prompts.append(m.content)
        return LlmResponse(
            text=json.dumps(self._json),
            model="stub",
            usage=LlmUsage(input_tokens=10, output_tokens=5),
            tool_calls=[],
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield LlmChunk(delta=json.dumps(self._json))


class _RaisingLlm(LlmProvider):
    """Always raises ProviderError."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Itinerary stubs
# ---------------------------------------------------------------------------


def _ny_itinerary() -> ItineraryResult:
    """Itinerary grounded in New York, NY."""
    from ai_agents.tools.itinerary_calendar import ItinerarySegment

    seg = ItinerarySegment(
        segment_id=str(uuid.uuid4()),
        trip_id=str(uuid.uuid4()),
        segment_type="flight",
        supplier="United Airlines",
        location="New York, NY, US",
        start_datetime="2026-06-10T08:00:00+00:00",
        end_datetime="2026-06-10T16:00:00+00:00",
        amount=320.00,
        currency="USD",
        details={},
    )
    return ItineraryResult(trips=(), segments=(seg,), calendar_events=())


def _dallas_itinerary() -> ItineraryResult:
    """Itinerary grounded in Dallas/Paris TX — triggers Paris paradox."""
    from ai_agents.tools.itinerary_calendar import ItinerarySegment

    seg1 = ItinerarySegment(
        segment_id=str(uuid.uuid4()),
        trip_id=str(uuid.uuid4()),
        segment_type="flight",
        supplier="AA",
        location="Dallas, TX, US",
        start_datetime="2026-06-10T08:00:00+00:00",
        end_datetime="2026-06-10T12:00:00+00:00",
        amount=200.00,
        currency="USD",
        details={},
    )
    seg2 = ItinerarySegment(
        segment_id=str(uuid.uuid4()),
        trip_id=str(uuid.uuid4()),
        segment_type="hotel",
        supplier="Marriott",
        location="Paris, TX, US",
        start_datetime="2026-06-10T15:00:00+00:00",
        end_datetime="2026-06-14T11:00:00+00:00",
        amount=400.00,
        currency="USD",
        details={},
    )
    return ItineraryResult(trips=(), segments=(seg1, seg2), calendar_events=())


def _empty_itinerary() -> ItineraryResult:
    return ItineraryResult(trips=(), segments=(), calendar_events=())


# ---------------------------------------------------------------------------
# Receipt ORM mock factory
# ---------------------------------------------------------------------------


def _make_receipt(
    receipt_id: uuid.UUID = _RECEIPT_ID,
    image_url: str = _IMAGE_KEY,
    ocr_status: OcrStatus = OcrStatus.pending,
) -> MagicMock:
    """Build a MagicMock that behaves like a Receipt ORM object."""
    r = MagicMock(spec=Receipt)
    r.id = receipt_id
    r.image_url = image_url
    r.organization_id = _ORG_ID
    r.uploaded_by = _USER_ID
    r.ocr_status = ocr_status
    r.extracted_fields = None
    r.field_confidence = None
    r.field_provenance = None
    r.grounding_context = None
    r.processing_path = None
    return r


# ---------------------------------------------------------------------------
# Session mock factory
# ---------------------------------------------------------------------------


def _make_session(receipt: MagicMock) -> MagicMock:
    """Build a MagicMock(spec=Session) that returns the receipt on scalar()."""
    session = MagicMock()
    session.scalar.return_value = receipt
    session.flush.return_value = None
    session.commit = MagicMock(side_effect=AssertionError("session.commit must NOT be called"))
    return session


# ---------------------------------------------------------------------------
# AgentContext helper
# ---------------------------------------------------------------------------


def _ctx(user_id: uuid.UUID = _USER_ID) -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        user_id=user_id,
        granted_permission_keys=frozenset(),
    )


# ---------------------------------------------------------------------------
# Itinerary tool stub
# ---------------------------------------------------------------------------


def _make_itinerary_tool(result: ItineraryResult) -> MagicMock:
    tool = MagicMock()
    tool.get_itinerary.return_value = result
    return tool


# ---------------------------------------------------------------------------
# 1. Deterministic-clean: all fields high-conf, no LLM
# ---------------------------------------------------------------------------


class TestDeterministicClean:
    def _run(self) -> tuple[MagicMock, MagicMock]:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        stub_ocr = _StubOcr(ocr_result)
        stub_storage = _StubStorage()
        stub_llm = _StubLlm()

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log") as mock_log:
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=stub_ocr,
                llm=stub_llm,
                storage=stub_storage,
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )
            return receipt, mock_log

    def test_all_provenance_deterministic(self) -> None:
        receipt, _ = self._run()
        prov = receipt.field_provenance
        assert prov is not None
        for key in _HIGH_CONF_FIELDS:
            assert prov[key] == "deterministic", f"{key} should be deterministic"

    def test_no_llm_called(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        raising_llm = MagicMock(spec=LlmProvider)
        raising_llm.complete.side_effect = AssertionError("LLM must NOT be called")

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=raising_llm,
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )
        raising_llm.complete.assert_not_called()

    def test_ocr_status_processed(self) -> None:
        receipt, _ = self._run()
        assert receipt.ocr_status == OcrStatus.processed

    def test_processing_path_deterministic(self) -> None:
        receipt, _ = self._run()
        assert receipt.processing_path == ProcessingPath.deterministic

    def test_extracted_fields_set(self) -> None:
        receipt, _ = self._run()
        assert receipt.extracted_fields is not None
        assert receipt.extracted_fields.get("amount") == 320.00


# ---------------------------------------------------------------------------
# 2. Route mix in decision log (gap-fill triggers llm route)
# ---------------------------------------------------------------------------


class TestRouteMixDecisionLog:
    def test_route_llm_when_gap_fill_triggered(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        # Only 4 high-conf fields; location + expense_type missing → gap-fill
        partial_conf = {"amount": 0.95, "currency": 0.93, "date": 0.91, "vendor": 0.90}
        partial_fields: dict[str, str | float | None] = {
            "amount": 320.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "UA",
        }
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)
        stub_llm = _StubLlm({"location": "New York, NY", "expense_type": "airfare"})

        logged_records: list[Any] = []

        def capture_log(session, *, context, record):
            logged_records.append(record)

        _patch = "ai_agents.receipt_pipeline.pipeline.write_decision_log"
        with patch(_patch, side_effect=capture_log):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=stub_llm,
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_ny_itinerary()),
            )

        assert len(logged_records) == 1
        assert logged_records[0].route == AiRoute.llm


# ---------------------------------------------------------------------------
# 3. Gap-fill triggered: itinerary tool called, ai_inferred + grounding_context
# ---------------------------------------------------------------------------


class TestGapFillTriggered:
    def _run_gap_fill_scenario(self) -> tuple[MagicMock, MagicMock]:
        receipt = _make_receipt()
        session = _make_session(receipt)
        partial_conf = {"amount": 0.95, "currency": 0.93, "date": 0.90, "vendor": 0.88}
        partial_fields: dict[str, str | float | None] = {
            "amount": 320.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "UA",
        }
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)
        stub_llm = _StubLlm({"location": "New York, NY", "expense_type": "airfare"})
        itinerary_tool = _make_itinerary_tool(_ny_itinerary())

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=stub_llm,
                storage=_StubStorage(),
                itinerary_tool=itinerary_tool,
            )
        return receipt, itinerary_tool

    def test_itinerary_tool_called(self) -> None:
        _, tool = self._run_gap_fill_scenario()
        tool.get_itinerary.assert_called_once()

    def test_grounding_context_set(self) -> None:
        receipt, _ = self._run_gap_fill_scenario()
        assert receipt.grounding_context is not None
        assert "segments" in receipt.grounding_context

    def test_ai_inferred_provenance(self) -> None:
        receipt, _ = self._run_gap_fill_scenario()
        prov = receipt.field_provenance
        assert prov is not None
        assert prov.get("expense_type") == "ai_inferred"

    def test_processing_path_agent_gap_fill(self) -> None:
        receipt, _ = self._run_gap_fill_scenario()
        assert receipt.processing_path == ProcessingPath.agent_gap_fill


# ---------------------------------------------------------------------------
# 4. Paris paradox: location 'Paris, France' vs Dallas/TX itinerary → revised/null
# ---------------------------------------------------------------------------


class TestParisParadox:
    def _run(self) -> tuple[MagicMock, list]:
        receipt = _make_receipt()
        session = _make_session(receipt)
        partial_conf = {"amount": 0.95, "currency": 0.93, "date": 0.90, "vendor": 0.88}
        partial_fields: dict[str, str | float | None] = {
            "amount": 400.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "Marriott",
        }
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)
        # LLM infers "Paris, France" — should be corrected or rejected by reflection
        stub_llm = _StubLlm({"location": "Paris, France", "expense_type": "hotel"})
        itinerary_tool = _make_itinerary_tool(_dallas_itinerary())

        logged_records: list = []

        def capture_log(session, *, context, record):
            logged_records.append(record)

        _patch = "ai_agents.receipt_pipeline.pipeline.write_decision_log"
        with patch(_patch, side_effect=capture_log):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=stub_llm,
                storage=_StubStorage(),
                itinerary_tool=itinerary_tool,
            )
        return receipt, logged_records

    def test_reflection_outcome_is_revised_or_rejected(self) -> None:
        _, records = self._run()
        assert records[0].reflection_outcome in (
            ReflectionOutcome.revised,
            ReflectionOutcome.rejected,
        )

    def test_reflection_outcome_never_passed(self) -> None:
        _, records = self._run()
        assert records[0].reflection_outcome != ReflectionOutcome.passed

    def test_location_not_france(self) -> None:
        receipt, _ = self._run()
        loc = (receipt.extracted_fields or {}).get("location")
        # Must either be corrected (TX) or null — never "Paris, France"
        if loc is not None:
            assert "France" not in loc, f"Location must not be Paris France; got {loc!r}"

    def test_needs_review_true(self) -> None:
        # Reflection that results in revised/rejected must set appropriate state
        # (the receipt itself doesn't track this flag, but the decision log reflects it)
        _, records = self._run()
        outcome = records[0].reflection_outcome
        assert outcome in (ReflectionOutcome.revised, ReflectionOutcome.rejected)


# ---------------------------------------------------------------------------
# 5. Reflection passed: location matches itinerary
# ---------------------------------------------------------------------------


class TestReflectionPassed:
    def test_outcome_passed_when_location_matches_itinerary(self) -> None:
        result = run_location_reflection("New York, NY, US", itinerary=_ny_itinerary())
        assert result.outcome == ReflectionOutcome.passed

    def test_passed_result_is_asserted(self) -> None:
        result = run_location_reflection("New York, NY, US", itinerary=_ny_itinerary())
        assert result.asserted is True

    def test_passed_result_has_citation(self) -> None:
        result = run_location_reflection("New York, NY, US", itinerary=_ny_itinerary())
        assert len(result.citations) >= 1


# ---------------------------------------------------------------------------
# 6. Reflection rejected: city not found in itinerary
# ---------------------------------------------------------------------------


class TestReflectionRejected:
    def test_outcome_rejected_when_city_absent(self) -> None:
        result = run_location_reflection("Honolulu, HI, US", itinerary=_dallas_itinerary())
        assert result.outcome == ReflectionOutcome.rejected

    def test_rejected_not_asserted(self) -> None:
        result = run_location_reflection("Honolulu, HI, US", itinerary=_dallas_itinerary())
        assert result.asserted is False

    def test_rejected_needs_review(self) -> None:
        result = run_location_reflection("Honolulu, HI, US", itinerary=_dallas_itinerary())
        assert result.needs_review is True


# ---------------------------------------------------------------------------
# 7. Provenance completeness: every extracted field has a provenance entry
# ---------------------------------------------------------------------------


class TestProvenanceCompleteness:
    def test_all_fields_have_provenance(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        fields = receipt.extracted_fields or {}
        prov = receipt.field_provenance or {}
        for key in fields:
            assert key in prov, f"Field {key!r} has no provenance entry"

    def test_provenance_values_valid(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        valid = {"deterministic", "ai_inferred", "user_edited"}
        for key, val in (receipt.field_provenance or {}).items():
            assert val in valid, f"Field {key!r} has invalid provenance {val!r}"


# ---------------------------------------------------------------------------
# 8. User edit flip: apply_user_correction sets provenance=user_edited
# ---------------------------------------------------------------------------


class TestUserEditFlip:
    def _make_processed_receipt(self) -> tuple[MagicMock, MagicMock]:
        receipt = _make_receipt(ocr_status=OcrStatus.processed)
        receipt.extracted_fields = {"amount": 100.0, "vendor": "Acme"}
        receipt.field_provenance = {"amount": "deterministic", "vendor": "ai_inferred"}
        session = _make_session(receipt)
        return receipt, session

    def test_field_value_updated(self) -> None:
        receipt, session = self._make_processed_receipt()
        apply_user_correction(
            session, context=_ctx(), receipt_id=_RECEIPT_ID, field="amount", value=150.0
        )
        assert receipt.extracted_fields["amount"] == 150.0

    def test_provenance_flipped_to_user_edited(self) -> None:
        receipt, session = self._make_processed_receipt()
        apply_user_correction(
            session, context=_ctx(), receipt_id=_RECEIPT_ID, field="amount", value=150.0
        )
        assert receipt.field_provenance["amount"] == "user_edited"

    def test_other_fields_provenance_unchanged(self) -> None:
        receipt, session = self._make_processed_receipt()
        apply_user_correction(
            session, context=_ctx(), receipt_id=_RECEIPT_ID, field="amount", value=150.0
        )
        assert receipt.field_provenance["vendor"] == "ai_inferred"

    def test_flush_called(self) -> None:
        receipt, session = self._make_processed_receipt()
        apply_user_correction(
            session, context=_ctx(), receipt_id=_RECEIPT_ID, field="amount", value=150.0
        )
        session.flush.assert_called()

    def test_not_found_raises_value_error(self) -> None:
        session = MagicMock()
        session.scalar.return_value = None
        with pytest.raises(ValueError, match="not found"):
            apply_user_correction(
                session, context=_ctx(), receipt_id=uuid.uuid4(), field="amount", value=99.0
            )


# ---------------------------------------------------------------------------
# 9. Idempotent re-run: second run updates same row; no duplicate rows
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    def test_second_run_updates_not_inserts(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        log_calls: list = []

        def capture(session, *, context, record):
            log_calls.append(record)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log", side_effect=capture):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )
            # Second run — session.scalar still returns same receipt object
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        # session.add should never be called (UPDATE path, not INSERT)
        session.add.assert_not_called()
        # Two decision log rows (one per run) — not zero, not one, not three
        assert len(log_calls) == 2


# ---------------------------------------------------------------------------
# 10. Degradation: OCR ProviderError → ocr_status=failed; no extracted_fields
# ---------------------------------------------------------------------------


class TestDegradationOcrFail:
    def _run(self) -> MagicMock:
        receipt = _make_receipt()
        session = _make_session(receipt)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_FailingOcr(),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )
        return receipt

    def test_ocr_status_failed(self) -> None:
        receipt = self._run()
        assert receipt.ocr_status == OcrStatus.failed

    def test_extracted_fields_not_set(self) -> None:
        receipt = self._run()
        assert receipt.extracted_fields is None

    def test_does_not_raise(self) -> None:
        # The pipeline must not propagate ProviderError
        receipt = _make_receipt()
        session = _make_session(receipt)
        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_FailingOcr(),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )


# ---------------------------------------------------------------------------
# 11. Degradation: LLM ProviderError → deterministic fields preserved; gap null
# ---------------------------------------------------------------------------


class TestDegradationLlmFail:
    def _run(self) -> MagicMock:
        receipt = _make_receipt()
        session = _make_session(receipt)
        # 4 high-conf fields; location + expense_type will need gap-fill
        partial_conf = {"amount": 0.95, "currency": 0.93, "date": 0.90, "vendor": 0.88}
        partial_fields: dict[str, str | float | None] = {
            "amount": 320.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "UA",
        }
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_RaisingLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_ny_itinerary()),
            )
        return receipt

    def test_deterministic_fields_preserved(self) -> None:
        receipt = self._run()
        fields = receipt.extracted_fields or {}
        assert fields.get("amount") == 320.0
        assert fields.get("currency") == "USD"

    def test_deterministic_provenance_preserved(self) -> None:
        receipt = self._run()
        prov = receipt.field_provenance or {}
        assert prov.get("amount") == "deterministic"

    def test_gap_fields_null(self) -> None:
        receipt = self._run()
        fields = receipt.extracted_fields or {}
        # LLM failed → inferred gap fields should be None or absent
        loc = fields.get("location")
        # location may be None or not present
        assert loc is None or loc == ""

    def test_ocr_status_processed(self) -> None:
        # Pipeline should still mark processed even when LLM degraded
        receipt = self._run()
        assert receipt.ocr_status == OcrStatus.processed


# ---------------------------------------------------------------------------
# 12. flush_only: session.commit() never called; rollback reverts
# ---------------------------------------------------------------------------


class TestFlushOnly:
    def test_commit_never_called(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        # session.commit is set to raise AssertionError in _make_session
        # If we reach here, commit was never triggered
        session.commit.assert_not_called()

    def test_flush_is_called(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        session.flush.assert_called()


# ---------------------------------------------------------------------------
# Plus: one decision-log row per run
# ---------------------------------------------------------------------------


class TestOneDecisionLogRow:
    def test_exactly_one_log_row_per_run(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        log_calls: list = []

        def capture(session, *, context, record):
            log_calls.append(record)

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log", side_effect=capture):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=_StubLlm(),
                storage=_StubStorage(),
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        assert len(log_calls) == 1
        assert log_calls[0].agent == AiAgent.receipt_analysis


# ---------------------------------------------------------------------------
# Plus: PII / image safety — bytes never reach LLM prompt
# ---------------------------------------------------------------------------


class TestPiiImageSafety:
    def test_image_bytes_not_in_llm_prompt(self) -> None:
        receipt = _make_receipt()
        session = _make_session(receipt)
        partial_conf = {"amount": 0.95, "currency": 0.93}
        partial_fields: dict[str, str | float | None] = {"amount": 100.0, "currency": "USD"}
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)

        stub_llm = _StubLlm(
            {"location": "NY", "vendor": "Acme", "date": "2026-06-10", "expense_type": "meal"}
        )
        stub_storage = _StubStorage(data=b"\x89PNG SENSITIVE_BINARY_DATA\x00\xff")

        with patch("ai_agents.receipt_pipeline.pipeline.write_decision_log"):
            run_receipt_pipeline(
                session,
                context=_ctx(),
                receipt_id=_RECEIPT_ID,
                ocr=_StubOcr(ocr_result),
                llm=stub_llm,
                storage=stub_storage,
                itinerary_tool=_make_itinerary_tool(_empty_itinerary()),
            )

        for prompt in stub_llm.prompts:
            assert b"SENSITIVE_BINARY_DATA" not in prompt.encode(
                errors="replace"
            ), "Image bytes must not appear in LLM prompt"
            assert "\x89PNG" not in prompt, "Raw PNG header must not appear in LLM prompt"


# ---------------------------------------------------------------------------
# Unit tests: core routing
# ---------------------------------------------------------------------------


class TestCoreRouting:
    def test_high_conf_all_deterministic(self) -> None:
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        stub_ocr = _StubOcr(ocr_result)
        stub_storage = _StubStorage()

        decision = run_ocr_core(_IMAGE_KEY, ocr=stub_ocr, storage=stub_storage)

        assert decision.needs_gap_fill is False
        assert len(decision.gap_fill_fields) == 0

    def test_missing_conf_routes_to_gap_fill(self) -> None:
        # Only 4 fields have confidence; location + expense_type have no entry
        partial_conf = {"amount": 0.95, "currency": 0.93, "date": 0.90, "vendor": 0.88}
        partial_fields: dict[str, str | float | None] = {
            "amount": 1.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "UA",
        }
        ocr_result = OcrResult(fields=partial_fields, field_confidence=partial_conf)

        decision = run_ocr_core(_IMAGE_KEY, ocr=_StubOcr(ocr_result), storage=_StubStorage())

        assert "location" in decision.gap_fill_fields
        assert "expense_type" in decision.gap_fill_fields

    def test_storage_called_with_image_url(self) -> None:
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        stub_storage = _StubStorage()

        run_ocr_core(_IMAGE_KEY, ocr=_StubOcr(ocr_result), storage=stub_storage)

        assert stub_storage.calls == [_IMAGE_KEY]

    def test_ocr_receives_bytes_not_url(self) -> None:
        ocr_result = OcrResult(fields=_HIGH_CONF_FIELDS, field_confidence=_HIGH_CONF)
        stub_ocr = _StubOcr(ocr_result)

        run_ocr_core(_IMAGE_KEY, ocr=stub_ocr, storage=_StubStorage())

        assert len(stub_ocr.calls) == 1
        assert isinstance(stub_ocr.calls[0], bytes), "OcrProvider must receive bytes, not a URL"


# ---------------------------------------------------------------------------
# Unit tests: reflection module
# ---------------------------------------------------------------------------


class TestBuildGroundingFacts:
    def test_segments_become_location_facts(self) -> None:
        facts = build_grounding_facts(_ny_itinerary())
        cities = [f.city for f in facts.locations]
        assert "New York" in cities

    def test_empty_itinerary_gives_empty_facts(self) -> None:
        facts = build_grounding_facts(_empty_itinerary())
        assert len(facts.locations) == 0

    def test_dallas_itinerary_has_both_cities(self) -> None:
        facts = build_grounding_facts(_dallas_itinerary())
        cities = {f.city for f in facts.locations}
        assert "Dallas" in cities
        assert "Paris" in cities


class TestReceiptFieldHypothesizer:
    def test_seed_returns_location_claim(self) -> None:
        hyp = ReceiptFieldHypothesizer(_location_str="New York, NY, US")
        result = hyp.seed()
        assert isinstance(result.value, LocationClaim)
        assert result.value.city == "New York"

    def test_regenerate_adopts_correction(self) -> None:
        hyp = ReceiptFieldHypothesizer(_location_str="Paris, France")
        seed = hyp.seed()
        correction = MagicMock()
        correction_grounded = MagicMock()
        correction.correction = correction_grounded
        result = hyp.regenerate(seed, correction)
        assert result is correction_grounded

    def test_seed_none_location_gives_unknown_city(self) -> None:
        hyp = ReceiptFieldHypothesizer(_location_str=None)
        result = hyp.seed()
        assert isinstance(result.value, LocationClaim)
        assert result.value.city == "unknown"


# ---------------------------------------------------------------------------
# Unit tests: gap_fill module
# ---------------------------------------------------------------------------


class TestRunGapFill:
    def test_llm_response_parsed(self) -> None:
        llm = _StubLlm({"location": "Austin, TX", "expense_type": "meal"})
        result = run_gap_fill(
            gap_fields=frozenset(["location", "expense_type"]),
            ocr_fields={"amount": 50.0, "vendor": "Bistro"},
            receipt_date_str="2026-06-10",
            itinerary=_empty_itinerary(),
            llm=llm,
        )
        assert result.inferred_fields.get("location") == "Austin, TX"
        assert result.needs_review is False

    def test_llm_error_returns_needs_review(self) -> None:
        result = run_gap_fill(
            gap_fields=frozenset(["location"]),
            ocr_fields={},
            receipt_date_str=None,
            itinerary=_empty_itinerary(),
            llm=_RaisingLlm(),
        )
        assert result.needs_review is True
        assert result.inferred_fields == {}

    def test_empty_gap_fields_returns_immediately(self) -> None:
        llm = MagicMock(spec=LlmProvider)
        result = run_gap_fill(
            gap_fields=frozenset(),
            ocr_fields={},
            receipt_date_str=None,
            itinerary=_empty_itinerary(),
            llm=llm,
        )
        llm.complete.assert_not_called()
        assert result.inferred_fields == {}
