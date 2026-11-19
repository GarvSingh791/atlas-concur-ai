"""Receipt review status — does a receipt need a human, and why (C7R-18).

Pure, deterministic derivation from the persisted receipt state, so every read
surface (upload response, GET, PATCH, idempotent replay) reports the same answer
and a user correction clears the matching reason without a pipeline re-run.

Inputs
    ocr_status        — failed -> the whole receipt needs manual entry.
    extracted_fields  — the six routed values plus ``review_flags``: the reason
                        codes the pipeline persisted for fields it WITHHELD
                        (e.g. ``{"amount": "total_illegible"}``).
    field_confidence  — below the 0.70 routing threshold -> confirm.
    field_provenance  — ``user_edited`` fields are settled by the user.

Output: ``(needs_review, review_reasons)`` — reasons are short, human-readable,
ordered by field (amount first: it is the money-critical one).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_agents.guardrails.citations import DEFAULT_CITATION_THRESHOLD
from config.constants.fx_rates import is_supported
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS

# Key under extracted_fields holding {field: reason_code} for withheld fields.
# Written ONLY by the pipeline (a provider-supplied key of this name is dropped).
REVIEW_FLAGS_KEY = "review_flags"

_PROVENANCE_USER_EDITED = "user_edited"

# Reason codes (kept identical to ai_agents.receipt_pipeline.core.REVIEW_*).
_TOTAL_ILLEGIBLE = "total_illegible"
_TOTAL_AMBIGUOUS = "total_ambiguous"
_AMOUNT_EQUALS_SUBTOTAL = "amount_equals_subtotal"
_TOTAL_UNREAD = "total_unread"

_WITHHELD_MESSAGES: dict[str, str] = {
    _TOTAL_ILLEGIBLE: "Total is not legible — enter the receipt total",
    _TOTAL_AMBIGUOUS: "Total is ambiguous — enter the receipt total",
    _AMOUNT_EQUALS_SUBTOTAL: "Only the subtotal could be read — enter the receipt total",
    _TOTAL_UNREAD: "Total could not be read (only subtotal/tax visible) — enter the receipt total",
}

MSG_OCR_FAILED = "Receipt could not be read — enter the details manually"
MSG_AMOUNT_MISSING = "Amount could not be read — enter the receipt total"
# C7R: the UI withholds a low-confidence amount (it is never drafted either), so
# the user is asked to ENTER the total, not to confirm a value they cannot see.
MSG_AMOUNT_LOW_CONFIDENCE = "Amount was not read with confidence — enter the receipt total"
# Product gap 8: an OCR currency outside the FX reference table.
MSG_CURRENCY_UNSUPPORTED = "Currency {code} is not supported — choose a supported currency"

_LABELS: dict[str, str] = {
    "amount": "Amount",
    "currency": "Currency",
    "date": "Date",
    "vendor": "Vendor",
    "location": "Location",
    "expense_type": "Expense type",
}

# C7R: the receipt location only feeds policy for lodging (the HOTEL-CAP city
# tier).  A rideshare / online / meal receipt without a city is complete, so a
# missing or low-confidence location is a review reason only for lodging — or
# when the expense type itself is unknown (conservative).
_LOCATION_RELEVANT_TYPES = frozenset({"lodging", "hotel"})


def _location_matters(expense_type: object) -> bool:
    if not isinstance(expense_type, str) or not expense_type.strip():
        return True
    key = expense_type.strip().lower().replace("-", "_").replace(" ", "_")
    return key in _LOCATION_RELEVANT_TYPES


# Review order: amount first, then the remaining routed keys in their usual order.
_ORDER: tuple[str, ...] = ("amount",) + tuple(k for k in RECEIPT_FIELD_KEYS if k != "amount")


def withheld_message(code: str) -> str:
    """Human message for a withheld-field reason code (generic fallback)."""
    return _WITHHELD_MESSAGES.get(code, MSG_AMOUNT_MISSING)


def _status_value(ocr_status: object) -> str:
    return str(getattr(ocr_status, "value", ocr_status) or "")


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _confidence(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def receipt_review_status(
    *,
    ocr_status: object,
    extracted_fields: Mapping[str, Any] | None,
    field_confidence: Mapping[str, Any] | None,
    field_provenance: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Return ``(needs_review, review_reasons)`` for a receipt's persisted state."""
    status = _status_value(ocr_status)
    if status == "failed":
        return True, [MSG_OCR_FAILED]
    if status != "processed":
        return False, []  # still pending: nothing to review yet

    fields = extracted_fields or {}
    confidence = field_confidence or {}
    provenance = field_provenance or {}
    flags_raw = fields.get(REVIEW_FLAGS_KEY)
    flags: Mapping[str, Any] = flags_raw if isinstance(flags_raw, Mapping) else {}

    reasons: list[str] = []
    for key in _ORDER:
        if provenance.get(key) == _PROVENANCE_USER_EDITED:
            continue  # the user has settled this field
        flag = flags.get(key)
        if isinstance(flag, str) and flag:
            reasons.append(withheld_message(flag))
            continue
        value = fields.get(key)
        conf = _confidence(confidence.get(key))
        if key == "amount":
            if _missing(value):
                reasons.append(MSG_AMOUNT_MISSING)
            elif conf is None or conf < DEFAULT_CITATION_THRESHOLD:
                reasons.append(MSG_AMOUNT_LOW_CONFIDENCE)
            continue
        if key == "location" and not _location_matters(fields.get("expense_type")):
            continue
        if key == "currency" and not _missing(value) and not is_supported(value):
            # Product gap 8: never drafted or converted until the user picks one.
            reasons.append(MSG_CURRENCY_UNSUPPORTED.format(code=str(value).strip().upper()))
            continue
        label = _LABELS.get(key, key)
        if _missing(value):
            reasons.append(f"{label} could not be read — enter it")
        elif conf is None or conf < DEFAULT_CITATION_THRESHOLD:
            reasons.append(f"{label} was inferred with low confidence — confirm it")
    return bool(reasons), reasons
