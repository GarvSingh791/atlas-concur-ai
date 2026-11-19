"""Deterministic OCR core — resolve image bytes, extract fields, route by confidence.

Responsibility
--------------
1. Resolve `image_url` (an object-store key) to bytes via StorageProvider.get_object.
2. Call OcrProvider.extract(bytes) to get OcrResult.
3. Deterministic total/subtotal cross-check (C7-18): when the provider signals the
   TOTAL line is unreadable, or the extracted amount equals the printed subtotal
   while a non-zero tax is printed, the amount's confidence is capped below the
   threshold so it is never asserted, and the amount is WITHHELD (C7R-18): it is
   persisted as null for the user to enter and no later stage may fill it.
4. Per-field confidence routing at threshold DEFAULT_CITATION_THRESHOLD (0.70):
   - fields with confidence >= 0.70 → deterministic_fields (done; provenance=deterministic)
   - fields with confidence < 0.70 or missing confidence → gap_fill_fields (needs LLM)
5. Return a RoutingDecision.  NEVER calls the LLM.

ProviderError from StorageProvider or OcrProvider propagates to the caller (pipeline.py),
which maps it to ocr_status=failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ai_agents.guardrails.citations import DEFAULT_CITATION_THRESHOLD
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS, OcrProvider, OcrResult
from services.storage_provider.interface import StorageProvider

logger = logging.getLogger(__name__)

# C7-18: confidence an ambiguous total is capped to. Strictly below the 0.70
# deterministic threshold so the amount can never be asserted deterministically.
AMBIGUOUS_TOTAL_CONFIDENCE_CAP = 0.30

# Provider signal keys (see services/ocr_provider/openrouter.py).
_TOTAL_LEGIBLE_KEY = "total_legible"
_TOTAL_AMBIGUOUS_KEY = "total_ambiguous"
_SUBTOTAL_KEY = "subtotal"
_TAX_KEY = "tax"
_TAX_INCLUDED_KEY = "tax_included"
_CENT = Decimal("0.01")

REVIEW_TOTAL_ILLEGIBLE = "total_illegible"
REVIEW_TOTAL_AMBIGUOUS = "total_ambiguous"
REVIEW_AMOUNT_EQUALS_SUBTOTAL = "amount_equals_subtotal"
REVIEW_TOTAL_UNREAD = "total_unread"


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return dec if dec.is_finite() else None


def detect_total_ambiguity(ocr_result: OcrResult) -> str | None:
    """Return a review reason when the extracted amount may not be the grand total.

    Deterministic, provider-agnostic checks (C7-18):
      * the provider says the TOTAL line is unreadable (``total_legible`` False)
        or explicitly flags ambiguity (``total_ambiguous`` True), while still
        returning an amount;
      * the extracted amount equals the printed subtotal and the provider did
        NOT affirm a legible total (``total_legible`` missing/None) — the staging
        R5 shape (blurred total, readable subtotal, unreadable or absent tax,
        legibility not reported) must never be asserted deterministically;
      * the extracted amount equals the printed subtotal while a positive tax is
        printed and that tax is not reported as already included in the total
        (``tax_included`` True — VAT-inclusive receipts, where total == subtotal
        is correct) — a tax-exclusive total cannot equal the pre-tax subtotal.
    Returns None when no ambiguity is detected or no amount was extracted.
    """
    fields = ocr_result.fields
    amount = _to_decimal(fields.get("amount"))
    if amount is None:
        return None
    legible = fields.get(_TOTAL_LEGIBLE_KEY)
    if legible is False:
        return REVIEW_TOTAL_ILLEGIBLE
    if fields.get(_TOTAL_AMBIGUOUS_KEY) is True:
        return REVIEW_TOTAL_AMBIGUOUS
    subtotal = _to_decimal(fields.get(_SUBTOTAL_KEY))
    if subtotal is None or abs(amount - subtotal) >= _CENT:
        return None
    if legible is not True:
        return REVIEW_AMOUNT_EQUALS_SUBTOTAL
    tax = _to_decimal(fields.get(_TAX_KEY))
    if tax is not None and tax > 0 and fields.get(_TAX_INCLUDED_KEY) is not True:
        return REVIEW_AMOUNT_EQUALS_SUBTOTAL
    return None


def detect_amount_withhold_reason(ocr_result: OcrResult) -> str | None:
    """Return a reason when the amount must be WITHHELD (left null for the user).

    C7R-18: an ambiguous / illegible total is never filled by any later stage
    (LLM gap-fill, fallback, heuristics). This extends
    :func:`detect_total_ambiguity` (which needs an extracted amount) to the
    shape where the provider returned NO amount but still said the TOTAL line is
    unreadable or ambiguous — otherwise the routed-but-empty amount would be
    gap-filled from the readable subtotal.
    """
    reason = detect_total_ambiguity(ocr_result)
    if reason is not None:
        return reason
    fields = ocr_result.fields
    if _to_decimal(fields.get("amount")) is not None:
        return None
    if fields.get(_TOTAL_LEGIBLE_KEY) is False:
        return REVIEW_TOTAL_ILLEGIBLE
    if fields.get(_TOTAL_AMBIGUOUS_KEY) is True:
        return REVIEW_TOTAL_AMBIGUOUS
    # C7R-18 (review round): no amount, legibility NOT affirmed, but the
    # subtotal or tax line was read — the total line is evidently there and
    # unreadable. The gap-fill LLM never sees the image, so any amount it
    # returned would be invented; withhold for the user instead.
    if fields.get(_TOTAL_LEGIBLE_KEY) is not True and (
        _to_decimal(fields.get(_SUBTOTAL_KEY)) is not None
        or _to_decimal(fields.get(_TAX_KEY)) is not None
    ):
        return REVIEW_TOTAL_UNREAD
    return None


@dataclass(frozen=True)
class RoutingDecision:
    """Result of the deterministic OCR core step.

    ocr_result          — raw OcrResult from the provider.
    deterministic_fields — field keys whose confidence >= threshold (provenance=deterministic).
    gap_fill_fields      — field keys with low/missing confidence (needs LLM gap-fill).
    image_bytes          — resolved bytes (kept for audit/debug; NOT passed to LLM).
    withheld_fields      — subset of gap_fill_fields that must stay NULL: no later
                           stage (LLM gap-fill included) may fill them (C7R-18).
    """

    ocr_result: OcrResult
    deterministic_fields: frozenset[str]
    gap_fill_fields: frozenset[str]
    image_bytes: bytes
    # C7-18: deterministic cross-check reasons that forced a field to review.
    review_reasons: dict[str, str] = field(default_factory=dict)
    # C7R-18: fields withheld for the user — never gap-filled, persisted as null.
    withheld_fields: frozenset[str] = frozenset()

    @property
    def needs_gap_fill(self) -> bool:
        return bool(self.gap_fill_fields)


def run_ocr_core(
    image_url: str,
    *,
    ocr: OcrProvider,
    storage: StorageProvider,
    confidence_threshold: float = DEFAULT_CITATION_THRESHOLD,
) -> RoutingDecision:
    """Resolve image bytes, run OCR, route fields by confidence.

    Args:
        image_url:           Object-store key (e.g. 'receipts/seed/…png').
        ocr:                 Injected OcrProvider (real or stub).
        storage:             Injected StorageProvider; resolves bytes (NFR-SEC4).
        confidence_threshold: Fields below this threshold route to gap-fill.
                             Default: DEFAULT_CITATION_THRESHOLD = 0.70.

    Returns:
        RoutingDecision with deterministic_fields, gap_fill_fields, ocr_result.

    Raises:
        services.exceptions.ProviderError: from storage.get_object or ocr.extract.
    """
    image_bytes: bytes = storage.get_object(image_url)
    ocr_result: OcrResult = ocr.extract(image_bytes)

    # C7-18: never assert an ambiguous total deterministically — cap its
    # confidence (copy; the provider's object is not mutated) so it leaves the
    # deterministic path and is flagged for review.
    # C7R-18: and WITHHOLD it — the amount stays null for the user to enter; no
    # later stage may fill it (the LLM never sees the image, so any amount it
    # "infers" is the readable subtotal or a guess).
    review_reasons: dict[str, str] = {}
    withheld: frozenset[str] = frozenset()
    reason = detect_amount_withhold_reason(ocr_result)
    if reason is not None:
        capped = dict(ocr_result.field_confidence)
        capped["amount"] = min(capped.get("amount", 0.0), AMBIGUOUS_TOTAL_CONFIDENCE_CAP)
        ocr_result = OcrResult(fields=dict(ocr_result.fields), field_confidence=capped)
        review_reasons["amount"] = reason
        withheld = frozenset({"amount"})
        logger.info("receipt_pipeline: amount withheld for review (%s)", reason)

    deterministic: set[str] = set()
    gap_fill: set[str] = set()

    for key in RECEIPT_FIELD_KEYS:
        conf = ocr_result.field_confidence.get(key)
        if conf is not None and conf >= confidence_threshold:
            deterministic.add(key)
        else:
            gap_fill.add(key)

    return RoutingDecision(
        ocr_result=ocr_result,
        deterministic_fields=frozenset(deterministic),
        gap_fill_fields=frozenset(gap_fill),
        image_bytes=image_bytes,
        review_reasons=review_reasons,
        withheld_fields=withheld,
    )
