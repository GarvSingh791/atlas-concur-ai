"""apply_user_correction — targeted single-field update for user-edited receipt fields.

Responsibility
--------------
Load the receipt row (org + user scoped), validate + normalize the correction
(C7-16: only the six extracted receipt fields, each with a per-field value
check), set extracted_fields[field]=value, flip field_provenance[field]=
'user_edited', session.flush().  No pipeline re-run.

Raises InvalidCorrectionError (a ValueError) for a non-correctable field or an
invalid value, ReceiptNotFoundError (a ValueError) if the receipt is not
found or not owned by the context user, and ReceiptLockedError (UAT2 hardening)
when the receipt is relied-on evidence: kept for audit, or backing an entry on a
report that is not a draft or has approval history.  A correction never writes
``grounding_context`` — the OCR snapshot there stays the machine reading.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.retention import correction_lock_reason
from config.constants.fx_rates import SUPPORTED_CURRENCIES, is_supported
from database.models.receipt import Receipt
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS

logger = logging.getLogger(__name__)

_PROVENANCE_USER_EDITED = "user_edited"

# C7-16: the ONLY keys a user may correct — the six routed OCR fields. Anything
# else (a real column name, an OCR extra such as "subtotal", a junk key) is
# rejected so a correction can never inject or overwrite arbitrary JSONB keys.
CORRECTABLE_FIELDS: frozenset[str] = frozenset(RECEIPT_FIELD_KEYS)

# Amount bounds: positive, at most 2 decimal places, within Numeric(14, 2) and a
# sane single-receipt ceiling.
MAX_CORRECTION_AMOUNT = Decimal("1000000.00")
_CENT = Decimal("0.01")
# Date bounds: not before this floor, not more than a few days in the future
# (timezone slack + pre-dated folios), never decades ahead.
MIN_CORRECTION_DATE = date(2000, 1, 1)
MAX_FUTURE_DAYS = 7
MAX_TEXT_LENGTH = 200
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidCorrectionError(ValueError):
    """The correction names a non-correctable field or carries an invalid value."""


class ReceiptNotFoundError(ValueError):
    """The receipt does not exist or is not owned by the caller."""


class ReceiptLockedError(ValueError):
    """The receipt is evidence a reviewer relied on and can no longer be corrected."""


def _normalize_amount(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise InvalidCorrectionError("amount must be a number")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidCorrectionError("amount must be a number") from exc
    if not dec.is_finite():
        raise InvalidCorrectionError("amount must be a finite number")
    if dec <= 0:
        raise InvalidCorrectionError("amount must be positive")
    if dec > MAX_CORRECTION_AMOUNT:
        raise InvalidCorrectionError("amount exceeds the maximum allowed")
    if dec != dec.quantize(_CENT):
        raise InvalidCorrectionError("amount must have at most 2 decimal places")
    # Stored as a JSON number, like OCR-extracted amounts.
    return float(dec.quantize(_CENT))


def _normalize_currency(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidCorrectionError("currency must be a 3-letter ISO-4217 code")
    code = value.strip().upper()
    if not _CURRENCY_RE.match(code):
        raise InvalidCorrectionError("currency must be a 3-letter ISO-4217 code")
    # Product gap 8: only a currency Atlas can convert (it is compared with caps
    # and summed into a report total).
    if not is_supported(code):
        raise InvalidCorrectionError(
            f"currency must be one of the supported currencies: {', '.join(SUPPORTED_CURRENCIES)}"
        )
    return code


def _normalize_date(value: object, *, today: date | None = None) -> str:
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value.strip()):
        raise InvalidCorrectionError("date must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidCorrectionError("date must be an ISO date (YYYY-MM-DD)") from exc
    ref = today or date.today()
    if parsed < MIN_CORRECTION_DATE or parsed > ref + timedelta(days=MAX_FUTURE_DAYS):
        raise InvalidCorrectionError("date is out of the allowed range")
    return parsed.isoformat()


def _normalize_text(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCorrectionError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_TEXT_LENGTH:
        raise InvalidCorrectionError(f"{field_name} is too long")
    # Control characters (NUL, C0/C1) and lone surrogates: Postgres rejects NUL
    # in TEXT/JSONB and a surrogate cannot be UTF-8 encoded — either would be a
    # 500 instead of a 422. A value made only of invisible format characters
    # (zero-width space etc.) is not a real vendor/location either; format
    # characters inside visible text (e.g. ZWNJ in Persian names) are allowed.
    categories = [unicodedata.category(ch) for ch in text]
    if any(cat in {"Cc", "Cs"} for cat in categories):
        raise InvalidCorrectionError(f"{field_name} contains control characters")
    if all(cat == "Cf" or ch.isspace() for cat, ch in zip(categories, text, strict=True)):
        raise InvalidCorrectionError(f"{field_name} must contain visible characters")
    return text


def _normalize_expense_type(value: object) -> str:
    # Lazy import: expense_agent.ingest imports receipt_pipeline.inbox, so a
    # module-level import here would create a package import cycle.
    from ai_agents.agents.expense_agent.categorize import category_for_expense_type

    if category_for_expense_type(value) is None:
        raise InvalidCorrectionError("expense_type is not a recognized expense type")
    assert isinstance(value, str)  # guaranteed by category_for_expense_type
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def normalize_correction(field: str, value: object, *, today: date | None = None) -> object:
    """Validate a (field, value) receipt correction and return the normalized value.

    Raises InvalidCorrectionError for a non-correctable field or invalid value.
    Single source of truth shared by the API schema and apply_user_correction.
    """
    if field not in CORRECTABLE_FIELDS:
        raise InvalidCorrectionError(f"field {field!r} is not a correctable receipt field")
    if field == "amount":
        return _normalize_amount(value)
    if field == "currency":
        return _normalize_currency(value)
    if field == "date":
        return _normalize_date(value, today=today)
    if field == "expense_type":
        return _normalize_expense_type(value)
    return _normalize_text(field, value)  # vendor, location


def apply_user_correction(
    session: Session,
    *,
    context: AgentContext,
    receipt_id: str | uuid.UUID,
    field: str,
    value: object,
) -> Receipt:
    """Apply a user correction to a single extracted field on a receipt.

    Loads the receipt (org + user scoped), mutates the JSONB columns, flushes.
    The caller commits.

    Args:
        session:    Active SQLAlchemy session (caller owns commit).
        context:    AgentContext carrying organization_id and user_id.
        receipt_id: UUID of the receipt to correct (str or UUID).
        field:      Field key to update (e.g. 'amount', 'vendor').
        value:      New field value (str, float, None, …).

    Returns:
        The mutated (flushed) Receipt ORM object.

    Raises:
        InvalidCorrectionError: field not correctable or value invalid (C7-16).
        ReceiptNotFoundError:   Receipt not found or ownership mismatch.
        ReceiptLockedError:     Receipt is relied-on evidence (UAT2 hardening).
    """
    # Validate BEFORE touching the row (defence in depth: the API schema runs
    # the same check, but a direct caller must not bypass it).
    normalized = normalize_correction(field, value)

    if isinstance(receipt_id, str):
        try:
            receipt_uuid = uuid.UUID(receipt_id)
        except ValueError as exc:
            raise ReceiptNotFoundError(f"Invalid receipt_id {receipt_id!r}") from exc
    else:
        receipt_uuid = receipt_id

    # Row lock (held until the caller commits) + fresh read: the JSONB columns
    # are rewritten copy-on-write below, so concurrent corrections must
    # serialise or one would silently drop the other's edit.
    receipt = session.scalar(
        sa.select(Receipt)
        .where(
            Receipt.id == receipt_uuid,
            Receipt.organization_id == context.organization_id,
            Receipt.uploaded_by == context.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if receipt is None:
        raise ReceiptNotFoundError(
            f"Receipt {receipt_uuid} not found or not owned by user {context.user_id} "
            f"in org {context.organization_id}."
        )
    # UAT2 hardening: evidence on a submitted / reviewed report, or kept for
    # audit, is frozen — retyping it could launder a double claim (the reuse
    # check would compare the retyped values).  Checked under the row lock.
    locked = correction_lock_reason(session, receipt=receipt)
    if locked is not None:
        raise ReceiptLockedError(locked)

    # Mutate extracted_fields (copy-on-write for JSONB)
    current_fields: dict = dict(receipt.extracted_fields or {})
    current_fields[field] = normalized
    receipt.extracted_fields = current_fields

    # Flip provenance to user_edited
    current_provenance: dict = dict(receipt.field_provenance or {})
    current_provenance[field] = _PROVENANCE_USER_EDITED
    receipt.field_provenance = current_provenance

    session.flush()

    logger.info(
        "apply_user_correction: receipt=%s field=%r user=%s org=%s",
        receipt_uuid,
        field,
        context.user_id,
        context.organization_id,
    )
    return receipt
