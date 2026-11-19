"""Receipt Analysis Pipeline — main orchestrator (P4-T11, FR-R1..R5).

run_receipt_pipeline
--------------------
Ingest → OCR core → (route) → gap_fill → reflection → UPDATE receipt row → decision log.

Session is caller-owned (flush-only; caller commits — P4-T13 owns the commit).

ocr_status lifecycle
    pending → processed (on success)
    pending → failed    (on unrecoverable OCR / storage error)

Processing path
    deterministic   — all fields above threshold; no LLM involved.
    agent_gap_fill  — at least one field left the deterministic path (routed to LLM
                      gap-fill, or withheld for the user).

Withheld fields (C7R-18)
    When the provider says the TOTAL is illegible / ambiguous (or the amount is the
    subtotal), the amount is WITHHELD: never sent to nor accepted from gap-fill,
    persisted as null with confidence 0.0, provenance ai_inferred, and its reason
    code in extracted_fields["review_flags"] (surfaced as needs_review /
    review_reasons on the API). A gap-filled amount equal to the printed subtotal
    is withheld the same way.

Reflection write mapping (location field only)
    outcome=passed   → keep inferred location; provenance=ai_inferred; conf from grounded.
    outcome=revised  → write corrected location; provenance=ai_inferred; needs_review per asserted.
    outcome=rejected → set location=null; provenance=ai_inferred; low conf; needs_review=True.

Decision log
    One write_decision_log row per run_receipt_pipeline call.
    route=deterministic when no gap-fill; route=llm when gap-fill ran.
    reflection_outcome only set when reflection ran (location was gap-filled).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.decision_log.writer import latency_timer, write_decision_log
from ai_agents.receipt_pipeline.core import (
    RoutingDecision,
    detect_total_ambiguity,
    run_ocr_core,
)
from ai_agents.receipt_pipeline.evidence import (
    SHARED_EXPENSES_KEY,
    build_ocr_snapshot,
    with_ocr_snapshot,
)
from ai_agents.receipt_pipeline.gap_fill import GapFillResult, run_gap_fill
from ai_agents.receipt_pipeline.reflection import run_location_reflection
from ai_agents.receipt_pipeline.review import REVIEW_FLAGS_KEY
from ai_agents.tools.itinerary_calendar import (
    ItineraryCalendarTool,
    ItineraryIntent,
    ItineraryResult,
)
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from database.models.receipt import OcrStatus, ProcessingPath, Receipt
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmProvider
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS, OcrProvider, OcrResult
from services.storage_provider.interface import StorageProvider

logger = logging.getLogger(__name__)

_PROVENANCE_DETERMINISTIC = "deterministic"
_PROVENANCE_AI_INFERRED = "ai_inferred"

_LOW_CONFIDENCE_SENTINEL = 0.10
"""Confidence written for a rejected/null gap-filled field."""

_WITHHELD_CONFIDENCE = 0.0
"""Confidence written for a WITHHELD field (C7R-18): no value is asserted."""


def run_receipt_pipeline(
    session: Session,
    *,
    context: AgentContext,
    receipt_id: str | uuid.UUID,
    ocr: OcrProvider,
    llm: LlmProvider,
    storage: StorageProvider,
    itinerary_tool: ItineraryCalendarTool = None,  # type: ignore[assignment]
) -> Receipt:
    """Run the full receipt analysis pipeline for one receipt row.

    Loads the receipt (org + user scoped), runs OCR → routing → gap-fill → reflection,
    updates the row, flushes.  Caller commits.

    Args:
        session:        Active SQLAlchemy session (caller owns commit).
        context:        AgentContext (organization_id + user_id required).
        receipt_id:     UUID of the pending receipt to process.
        ocr:            Injected OcrProvider (real or stub for tests).
        llm:            Injected LlmProvider (real or stub for tests).
        storage:        Injected StorageProvider; resolves image bytes (NFR-SEC4).
        itinerary_tool: ItineraryCalendarTool instance (default: new instance).

    Returns:
        The updated Receipt ORM object (flushed, not committed).

    Raises:
        ValueError: Receipt not found or ownership mismatch.
    """
    if itinerary_tool is None:
        itinerary_tool = ItineraryCalendarTool()

    receipt_uuid = receipt_id if isinstance(receipt_id, uuid.UUID) else uuid.UUID(str(receipt_id))

    receipt = session.scalar(
        sa.select(Receipt).where(
            Receipt.id == receipt_uuid,
            Receipt.organization_id == context.organization_id,
            Receipt.uploaded_by == context.user_id,
        )
    )
    if receipt is None:
        raise ValueError(
            f"Receipt {receipt_uuid} not found or not owned by user {context.user_id} "
            f"in org {context.organization_id}."
        )

    with latency_timer() as ms:
        result = _run_pipeline_inner(
            receipt=receipt,
            context=context,
            session=session,
            ocr=ocr,
            llm=llm,
            storage=storage,
            itinerary_tool=itinerary_tool,
        )

    # Write decision log (one row per run)
    _write_log(session, context=context, receipt=receipt, result=result, latency_ms=ms[0])

    session.flush()
    return receipt


# ---------------------------------------------------------------------------
# Inner pipeline (mutable receipt state; returns structured result for logging)
# ---------------------------------------------------------------------------


class _PipelineResult:
    __slots__ = (
        "route",
        "reflection_outcome",
        "confidence",
        "gap_fill_ran",
        "needs_review",
        "ocr_failed",
        "fields_written",
        "gap_fields_used",
        "review_reasons",
        "withheld_fields",
    )

    def __init__(self) -> None:
        self.route: AiRoute = AiRoute.deterministic
        self.reflection_outcome: ReflectionOutcome | None = None
        self.confidence: float | None = None
        self.gap_fill_ran: bool = False
        self.needs_review: bool = False
        self.ocr_failed: bool = False
        self.fields_written: list[str] = []
        self.gap_fields_used: list[str] = []
        self.review_reasons: dict[str, str] = {}
        self.withheld_fields: list[str] = []


def _run_pipeline_inner(
    *,
    receipt: Receipt,
    context: AgentContext,
    session: Session,
    ocr: OcrProvider,
    llm: LlmProvider,
    storage: StorageProvider,
    itinerary_tool: ItineraryCalendarTool,
) -> _PipelineResult:
    result = _PipelineResult()

    # ── 1. OCR core (may fail with ProviderError) ─────────────────────────────
    try:
        routing: RoutingDecision = run_ocr_core(
            receipt.image_url,
            ocr=ocr,
            storage=storage,
        )
    except ProviderError as exc:
        logger.error(
            "receipt_pipeline: OCR failed for receipt=%s error=%s",
            receipt.id,
            exc.code,
        )
        receipt.ocr_status = OcrStatus.failed
        result.ocr_failed = True
        result.needs_review = True
        session.flush()
        return result

    # C7-18: a deterministic cross-check (e.g. amount == subtotal) demoted a
    # field — the receipt needs human review whatever gap-fill returns.
    if routing.review_reasons:
        result.review_reasons = dict(routing.review_reasons)
        result.needs_review = True

    # ── 2. Build extracted_fields / field_confidence / provenance dicts ────────
    extracted_fields: dict = {}
    field_confidence: dict = {}
    field_provenance: dict = {}

    for key in routing.ocr_result.fields:
        if key == REVIEW_FLAGS_KEY:
            continue  # pipeline-owned key; a provider-supplied one is never trusted
        val = routing.ocr_result.fields[key]
        conf = routing.ocr_result.field_confidence.get(key, 0.0)
        if key in routing.deterministic_fields:
            extracted_fields[key] = val
            field_confidence[key] = conf
            field_provenance[key] = _PROVENANCE_DETERMINISTIC
            result.fields_written.append(key)
        elif key not in RECEIPT_FIELD_KEYS and val is not None:
            # EXTRA OCR keys (e.g. lodging nights / per_night_rate) are not part
            # of the 6-key confidence routing, but downstream policy evaluation
            # needs the PER-NIGHT rate for HOTEL-CAP. Persist them additively into
            # extracted_fields without confidence/provenance (they are not routed
            # fields). The 6-key routing above is untouched.
            extracted_fields[key] = val

    # ── 3. Gap-fill (only if needed) ──────────────────────────────────────────
    gap_fill_result: GapFillResult | None = None
    grounding_context: dict | None = None
    itinerary: ItineraryResult | None = None

    # C7R-18: withheld fields (an illegible / ambiguous total) are never sent to
    # — nor accepted from — the LLM gap-fill; they are persisted as null below.
    withheld: dict[str, str] = {
        fld: routing.review_reasons.get(fld, "withheld") for fld in routing.withheld_fields
    }
    llm_fields = frozenset(routing.gap_fill_fields - routing.withheld_fields)

    if llm_fields:
        result.gap_fill_ran = True
        result.route = AiRoute.llm
        result.gap_fields_used = sorted(llm_fields)

        # Fetch itinerary (tool call)
        try:
            itinerary = itinerary_tool.get_itinerary(
                ItineraryIntent(trip_id=None, include_calendar=True),
                context=context,
                session=session,
            )
        except Exception as exc:
            logger.warning(
                "receipt_pipeline: ItineraryCalendarTool failed (%s); gap-fill without context.",
                exc,
            )
            from ai_agents.tools.itinerary_calendar import ItineraryResult as _IR

            itinerary = _IR(trips=(), segments=(), calendar_events=())

        receipt_date_str = str(extracted_fields.get("date") or "")

        gap_fill_result = run_gap_fill(
            gap_fields=llm_fields,
            ocr_fields=extracted_fields,
            receipt_date_str=receipt_date_str or None,
            itinerary=itinerary,
            llm=llm,
        )
        grounding_context = gap_fill_result.grounding_context

        if gap_fill_result.needs_review:
            result.needs_review = True

        # Write gap-filled fields (all except location — handled by reflection)
        location_inferred: str | None = None

        for fld in sorted(llm_fields):
            if fld == "location":
                location_inferred = gap_fill_result.inferred_fields.get("location")  # type: ignore[assignment]
                continue  # handled below after reflection
            val = gap_fill_result.inferred_fields.get(fld)
            conf = gap_fill_result.inferred_conf.get(fld, 0.0)
            extracted_fields[fld] = val
            field_confidence[fld] = conf
            field_provenance[fld] = _PROVENANCE_AI_INFERRED
            if conf < 0.70:
                result.needs_review = True
            result.fields_written.append(fld)

        # C7R-18: a gap-filled amount that equals the printed subtotal (with no
        # affirmed-legible total) is the subtotal-as-total failure — withhold it.
        if "amount" in llm_fields and extracted_fields.get("amount") is not None:
            reason = detect_total_ambiguity(
                OcrResult(
                    fields={**routing.ocr_result.fields, "amount": extracted_fields["amount"]}
                )
            )
            if reason is not None:
                withheld["amount"] = reason
                result.review_reasons["amount"] = reason

        # ── 4. Reflection (location-only, deterministic GroundingCritic) ──────
        if itinerary is not None and "location" in llm_fields:
            reflection = run_location_reflection(location_inferred, itinerary=itinerary)
            result.reflection_outcome = reflection.outcome

            if reflection.outcome == ReflectionOutcome.passed:
                extracted_fields["location"] = location_inferred
                field_confidence["location"] = reflection.grounded.confidence
                field_provenance["location"] = _PROVENANCE_AI_INFERRED
                result.needs_review = result.needs_review or (not reflection.asserted)

            elif reflection.outcome == ReflectionOutcome.revised:
                # Write the corrected location (LocationClaim → string)
                from ai_agents.guardrails.reflection import LocationClaim

                corrected = reflection.grounded.value
                corrected_str: str | None
                if isinstance(corrected, LocationClaim):
                    parts = [corrected.city]
                    if corrected.region:
                        parts.append(corrected.region)
                    if corrected.country:
                        parts.append(corrected.country)
                    corrected_str = ", ".join(parts)
                else:
                    corrected_str = str(corrected) if corrected else None
                extracted_fields["location"] = corrected_str
                field_confidence["location"] = reflection.grounded.confidence
                field_provenance["location"] = _PROVENANCE_AI_INFERRED
                result.needs_review = result.needs_review or (not reflection.asserted)

            else:  # rejected
                extracted_fields["location"] = None
                field_confidence["location"] = _LOW_CONFIDENCE_SENTINEL
                field_provenance["location"] = _PROVENANCE_AI_INFERRED
                result.needs_review = True

            result.fields_written.append("location")
            result.confidence = reflection.grounded.confidence
        elif "location" in llm_fields and gap_fill_result is not None:
            # No itinerary available — write without reflection
            val = gap_fill_result.inferred_fields.get("location")
            conf = gap_fill_result.inferred_conf.get("location", 0.0)
            extracted_fields["location"] = val
            field_confidence["location"] = conf
            field_provenance["location"] = _PROVENANCE_AI_INFERRED
            result.needs_review = True
            result.fields_written.append("location")

    # ── 4b. Withheld fields (C7R-18): null, no confidence, flagged for review ──
    for fld in sorted(withheld):
        extracted_fields[fld] = None
        field_confidence[fld] = _WITHHELD_CONFIDENCE
        field_provenance[fld] = _PROVENANCE_AI_INFERRED
        result.needs_review = True
        if fld not in result.fields_written:
            result.fields_written.append(fld)
    if withheld:
        extracted_fields[REVIEW_FLAGS_KEY] = dict(sorted(withheld.items()))
        result.withheld_fields = sorted(withheld)

    # ── 5. Write results to receipt row ───────────────────────────────────────
    receipt.extracted_fields = extracted_fields
    receipt.field_confidence = field_confidence
    receipt.field_provenance = field_provenance
    # UAT2 hardening: freeze what OCR READ (deterministic amount / date / vendor)
    # next to the gap-fill lineage.  Corrections never write this key, so the
    # receipt-reuse check always compares the machine reading, never a retyped
    # value.  A share list written earlier (itemized siblings) is preserved.
    previous = receipt.grounding_context if isinstance(receipt.grounding_context, dict) else {}
    context_out = with_ocr_snapshot(
        grounding_context, build_ocr_snapshot(extracted_fields, field_provenance)
    )
    if SHARED_EXPENSES_KEY in previous:
        context_out[SHARED_EXPENSES_KEY] = previous[SHARED_EXPENSES_KEY]
    receipt.grounding_context = context_out
    receipt.ocr_status = OcrStatus.processed
    receipt.processing_path = (
        ProcessingPath.agent_gap_fill if routing.needs_gap_fill else ProcessingPath.deterministic
    )

    # Derive overall confidence from deterministic fields when no reflection ran
    if result.confidence is None and field_confidence:
        confs = [v for v in field_confidence.values() if isinstance(v, int | float)]
        result.confidence = sum(confs) / len(confs) if confs else None

    return result


# ---------------------------------------------------------------------------
# Decision log writer
# ---------------------------------------------------------------------------


def _write_log(
    session: Session,
    *,
    context: AgentContext,
    receipt: Receipt,
    result: _PipelineResult,
    latency_ms: int,
) -> None:
    if result.ocr_failed:
        task_type = "ocr_extraction"
        output_summary: dict = {"ocr_failed": True, "ocr_status": "failed"}
    elif result.gap_fill_ran:
        task_type = "gap_fill"
        output_summary = {
            "gap_fields": result.gap_fields_used,
            "fields_written": result.fields_written,
            "needs_review": result.needs_review,
            "reflection_outcome": (
                result.reflection_outcome.value if result.reflection_outcome else None
            ),
        }
    else:
        task_type = "ocr_extraction"
        output_summary = {
            "fields_written": result.fields_written,
            "needs_review": result.needs_review,
        }
    if not result.ocr_failed:
        if result.review_reasons:
            output_summary["review_reasons"] = result.review_reasons
        if result.withheld_fields:
            output_summary["withheld_fields"] = result.withheld_fields

    record = DecisionRecord(
        agent=AiAgent.receipt_analysis,
        task_type=task_type,
        route=result.route,
        model_provider=None,
        input_summary={"receipt_id": str(receipt.id), "image_url": receipt.image_url},
        tools_used={"ItineraryCalendarTool": result.gap_fill_ran},
        output_summary=output_summary,
        confidence=result.confidence,
        reflection_outcome=result.reflection_outcome,
        latency_ms=latency_ms,
    )
    write_decision_log(session, context=context, record=record)
