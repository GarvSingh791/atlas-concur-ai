"""Gap-fill step — LLM inference for low-confidence / missing receipt fields.

Responsibility
--------------
1. Filter the ItineraryResult to the receipt's date window (±1 day around receipt date).
2. Build a PII-minimal LLM prompt using only partial fields + itinerary context.
   Image bytes NEVER sent to the LLM (NFR-SEC4).
3. Call LlmProvider.complete and parse inferred field values from the response.
4. On ProviderError: return GapFillResult with inferred_fields={}, needs_review=True
   (preserves deterministic fields; gap-fill fields stay null).

The grounding_context written to the receipt row (audit lineage) comes from here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ai_agents.tools.itinerary_calendar import CalendarEntry, ItineraryResult, ItinerarySegment
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

_DATE_WINDOW_DAYS = 1
"""Itinerary window around the receipt date (days before and after)."""


@dataclass(frozen=True)
class GapFillResult:
    """Output of the LLM gap-fill step.

    inferred_fields   — field key → inferred string value (or None if inference failed).
    inferred_conf     — per-field confidence for inferred values (0.0–1.0).
    needs_review      — True when LLM degraded (ProviderError) or inference was uncertain.
    grounding_context — audit snapshot of itinerary facts used (written to receipt row).
    """

    inferred_fields: dict[str, str | float | None] = field(default_factory=dict)
    inferred_conf: dict[str, float] = field(default_factory=dict)
    needs_review: bool = False
    grounding_context: dict = field(default_factory=dict)


def _parse_receipt_date(fields: dict) -> date | None:
    """Parse 'date' from OCR fields; return None if absent or unparseable."""
    raw = fields.get("date")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            if "-" in str(raw):
                return date.fromisoformat(str(raw))
            return datetime.strptime(str(raw), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _filter_itinerary_window(
    itinerary: ItineraryResult,
    receipt_date: date | None,
    window_days: int = _DATE_WINDOW_DAYS,
) -> tuple[list[ItinerarySegment], list[CalendarEntry]]:
    """Filter itinerary segments and calendar events to a date window around receipt_date.

    If receipt_date is None, returns all segments and events.
    """
    if receipt_date is None:
        return list(itinerary.segments), list(itinerary.calendar_events)

    lo = receipt_date - timedelta(days=window_days)
    hi = receipt_date + timedelta(days=window_days)

    def _seg_in_window(seg: ItinerarySegment) -> bool:
        if seg.start_datetime is None:
            return True  # include undated segments as a fallback
        try:
            seg_date = date.fromisoformat(seg.start_datetime[:10])
            return lo <= seg_date <= hi
        except (ValueError, AttributeError):
            return True

    def _evt_in_window(evt: CalendarEntry) -> bool:
        try:
            evt_date = date.fromisoformat(evt.start_datetime[:10])
            return lo <= evt_date <= hi
        except (ValueError, AttributeError):
            return True

    return (
        [s for s in itinerary.segments if _seg_in_window(s)],
        [e for e in itinerary.calendar_events if _evt_in_window(e)],
    )


def _build_grounding_context(
    segments: list[ItinerarySegment],
    events: list[CalendarEntry],
) -> dict:
    """Serialise the itinerary window used for grounding (stored on receipt row)."""
    return {
        "segments": [
            {
                "id": s.segment_id,
                "type": s.segment_type,
                "location": s.location,
                "start": s.start_datetime,
                "end": s.end_datetime,
            }
            for s in segments
        ],
        "calendar_events": [
            {
                "id": e.event_id,
                "title": e.title,
                "location": e.location,
                "start": e.start_datetime,
                "end": e.end_datetime,
            }
            for e in events
        ],
    }


# C7R-18: OCR cross-check signals and pipeline bookkeeping are never shown to
# the LLM — a readable subtotal next to "Fields to infer: ['amount']" is an
# invitation to copy it as the total.
_PROMPT_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {"subtotal", "tax", "tax_included", "total_legible", "total_ambiguous", "review_flags"}
)


def _build_prompt(
    gap_fields: frozenset[str],
    known_fields: dict,
    segments: list[ItinerarySegment],
    events: list[CalendarEntry],
) -> str:
    """Build a PII-minimal LLM prompt.  Image bytes are NEVER included (NFR-SEC4)."""
    known_summary = {
        k: v for k, v in known_fields.items() if v is not None and k not in _PROMPT_EXCLUDED_KEYS
    }
    seg_summary = [
        f"  - {s.segment_type} at {s.location or 'unknown'} "
        f"({s.start_datetime or '?'} to {s.end_datetime or '?'})"
        for s in segments
    ]
    evt_summary = [
        f"  - {e.title or 'event'} at {e.location or 'unknown'} ({e.start_datetime})"
        for e in events
    ]

    itinerary_block = ""
    if seg_summary:
        itinerary_block += "Trip segments:\n" + "\n".join(seg_summary) + "\n"
    if evt_summary:
        itinerary_block += "Calendar events:\n" + "\n".join(evt_summary) + "\n"
    if not itinerary_block:
        itinerary_block = "No itinerary context available.\n"

    return (
        "You are a receipt data extraction assistant. "
        "Given the partial receipt fields already extracted and the traveler's itinerary, "
        "infer the missing fields. Reply ONLY with a JSON object — no prose.\n\n"
        f"Already extracted fields: {json.dumps(known_summary)}\n\n"
        f"Itinerary context:\n{itinerary_block}\n"
        f"Fields to infer: {sorted(gap_fields)}\n\n"
        "Reply with a JSON object mapping each field name to its inferred value "
        "(string for text fields, number for amount, null if you cannot infer). "
        'Example: {{"amount": 42.50, "vendor": "Marriott", "location": "New York, NY", '
        '"expense_type": "hotel", "currency": "USD", "date": "2026-06-11"}}'
    )


def _parse_llm_response(text: str, gap_fields: frozenset[str]) -> dict[str, str | float | None]:
    """Parse the LLM's JSON response into a field → value mapping."""
    text = text.strip()
    # Trim markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(ln for ln in lines if not ln.strip().startswith("```")).strip()
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {}
        return {k: v for k, v in parsed.items() if k in gap_fields}
    except (json.JSONDecodeError, ValueError):
        logger.warning("gap_fill: failed to parse LLM JSON response; treating as empty")
        return {}


def run_gap_fill(
    *,
    gap_fields: frozenset[str],
    ocr_fields: dict,
    receipt_date_str: str | None,
    itinerary: ItineraryResult,
    llm: LlmProvider,
) -> GapFillResult:
    """Infer missing/low-confidence fields via LLM grounded in itinerary context.

    Args:
        gap_fields:       Field keys that need LLM inference.
        ocr_fields:       Already-extracted fields (high-confidence; for context only).
        receipt_date_str: The receipt date string from OCR fields (may be None).
        itinerary:        Full ItineraryResult; will be filtered to date window.
        llm:              Injected LlmProvider (real or stub).

    Returns:
        GapFillResult with inferred_fields, needs_review flag, grounding_context.
        On ProviderError: inferred_fields={}, needs_review=True.
    """
    if not gap_fields:
        return GapFillResult()

    receipt_date = _parse_receipt_date(ocr_fields) if not receipt_date_str else None
    if receipt_date_str:
        try:
            receipt_date = date.fromisoformat(receipt_date_str[:10])
        except (ValueError, AttributeError):
            receipt_date = None

    segments, events = _filter_itinerary_window(itinerary, receipt_date)
    grounding_context = _build_grounding_context(segments, events)
    prompt = _build_prompt(gap_fields, ocr_fields, segments, events)

    try:
        response = llm.complete([LlmMessage(role="user", content=prompt)])
    except ProviderError as exc:
        logger.warning(
            "gap_fill: LlmProvider raised ProviderError (%s); "
            "gap-fill fields will be null + needs_review (NFR-R1).",
            exc.code,
        )
        return GapFillResult(
            inferred_fields={},
            inferred_conf={},
            needs_review=True,
            grounding_context=grounding_context,
        )

    inferred = _parse_llm_response(response.text, gap_fields)

    # Assign a moderate default confidence to LLM-inferred fields.
    # Fields that were not parseable from the LLM response get None.
    inferred_conf: dict[str, float] = {}
    for fld in gap_fields:
        if fld in inferred and inferred[fld] is not None:
            inferred_conf[fld] = 0.60
        else:
            inferred_conf[fld] = 0.0

    return GapFillResult(
        inferred_fields=inferred,
        inferred_conf=inferred_conf,
        needs_review=False,
        grounding_context=grounding_context,
    )
