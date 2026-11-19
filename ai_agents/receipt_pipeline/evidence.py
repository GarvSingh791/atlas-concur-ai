"""Receipt evidence bookkeeping (UAT2 hardening) — machine reading + shared links.

Two pieces of state live in ``receipts.grounding_context`` (JSONB, no migration):

``ocr_snapshot``
    What OCR read deterministically at processing time — ``{amount, date,
    vendor}``, only the fields whose provenance was ``deterministic`` (never a
    gap-filled ``ai_inferred`` guess, never a ``user_edited`` value).  Written
    ONCE by the receipt pipeline and never by a correction (PATCH /receipts only
    rewrites ``extracted_fields`` / ``field_provenance``).  The receipt-reuse check
    compares these immutable readings, so retyping a receipt's vendor / date /
    total cannot make two copies of one paper receipt look different.  A row
    processed before this existed has no snapshot ("legacy"); its trusted
    reading is then its current fields whose provenance is still deterministic.

``shared_expense_ids``
    Further entries (ids, as strings) that the SAME receipt row backs, beyond
    its ``expense_id`` — itemized siblings split from one folio.  A share is
    valid only while every backing entry is on the same report and their summed
    amounts are covered by the receipt's machine-read total; the pre-submit
    check re-validates that on every run (presubmit ``_receipt_reuse_scan``).

LAYERING LAW: never imports from backend/.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, TEXT
from sqlalchemy.orm import Session

from database.models.receipt import Receipt

OCR_SNAPSHOT_KEY = "ocr_snapshot"
SHARED_EXPENSES_KEY = "shared_expense_ids"
SNAPSHOT_FIELDS: tuple[str, ...] = ("amount", "date", "vendor")
_DETERMINISTIC = "deterministic"
_UNTRUSTED = frozenset({"user_edited", "ai_inferred"})


def build_ocr_snapshot(fields: Any, provenance: Any) -> dict[str, Any]:
    """The machine reading to freeze: deterministic amount / date / vendor only."""
    if not isinstance(fields, dict):
        return {}
    prov: dict[str, Any] = provenance if isinstance(provenance, dict) else {}
    out: dict[str, Any] = {}
    for key in SNAPSHOT_FIELDS:
        if prov.get(key) != _DETERMINISTIC:
            continue
        value = fields.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, date):
            value = value.isoformat()
        out[key] = value
    return out


def with_ocr_snapshot(grounding_context: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """*grounding_context* (copied) carrying *snapshot* — other keys are preserved."""
    base: dict[str, Any] = dict(grounding_context) if isinstance(grounding_context, dict) else {}
    base[OCR_SNAPSHOT_KEY] = dict(snapshot)
    return base


def ocr_snapshot(grounding_context: Any) -> dict[str, Any] | None:
    """The frozen machine reading, or None for a legacy row processed before snapshots."""
    if not isinstance(grounding_context, dict):
        return None
    snap = grounding_context.get(OCR_SNAPSHOT_KEY)
    return dict(snap) if isinstance(snap, dict) else None


def machine_total(receipt: Receipt) -> Decimal | None:
    """The receipt's total as READ by OCR (cents), never a typed / inferred one.

    The snapshot total when the row has a snapshot; a legacy row's current total
    while its provenance is not ``user_edited`` / ``ai_inferred``.  None = unknown.
    """
    snap = ocr_snapshot(receipt.grounding_context)
    if snap is not None:
        value = snap.get("amount")
    else:
        fields = receipt.extracted_fields if isinstance(receipt.extracted_fields, dict) else {}
        prov = receipt.field_provenance if isinstance(receipt.field_provenance, dict) else {}
        value = None if prov.get("amount") in _UNTRUSTED else fields.get("amount")
    if value is None or isinstance(value, bool):
        return None
    try:
        total = Decimal(str(value).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return total if total.is_finite() and total > 0 else None


def machine_reading(receipt: Receipt) -> dict[str, Any]:
    """The receipt's vendor / date / total as READ by OCR (UAT2 defect F).

    Same trust rule as ``machine_total``: the frozen snapshot when the row has one,
    else a legacy row's current fields whose provenance is not ``user_edited`` /
    ``ai_inferred``.  Returns ``{"vendor": str | None, "date": str | None,
    "amount": Decimal | None}`` — None for a field with no machine reading.
    """
    snap = ocr_snapshot(receipt.grounding_context)
    if snap is None:
        fields = receipt.extracted_fields if isinstance(receipt.extracted_fields, dict) else {}
        prov = receipt.field_provenance if isinstance(receipt.field_provenance, dict) else {}
        snap = {k: fields.get(k) for k in SNAPSHOT_FIELDS if prov.get(k) not in _UNTRUSTED}
    vendor = snap.get("vendor")
    raw_date = snap.get("date")
    if isinstance(raw_date, date):
        raw_date = raw_date.isoformat()
    day: str | None = None
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            day = date.fromisoformat(raw_date.strip()[:10]).isoformat()
        except ValueError:
            day = None
    return {
        "vendor": vendor.strip() if isinstance(vendor, str) and vendor.strip() else None,
        "date": day,
        "amount": machine_total(receipt),
    }


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def shared_expense_ids(receipt: Receipt) -> list[uuid.UUID]:
    """The entries *receipt* backs IN ADDITION to its ``expense_id`` (itemized siblings)."""
    gc = receipt.grounding_context
    raw = gc.get(SHARED_EXPENSES_KEY) if isinstance(gc, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[uuid.UUID] = []
    for value in raw:
        eid = _as_uuid(value)
        if eid is not None and eid != receipt.expense_id and eid not in out:
            out.append(eid)
    return out


def set_shared_expense_ids(receipt: Receipt, ids: Iterable[uuid.UUID]) -> None:
    """Rewrite the share list copy-on-write; ``ocr_snapshot`` is carried over untouched."""
    clean: list[str] = []
    for eid in ids:
        if eid is None or eid == receipt.expense_id or str(eid) in clean:
            continue
        clean.append(str(eid))
    base: dict[str, Any] = (
        dict(receipt.grounding_context) if isinstance(receipt.grounding_context, dict) else {}
    )
    if clean:
        base[SHARED_EXPENSES_KEY] = clean
    else:
        base.pop(SHARED_EXPENSES_KEY, None)
    receipt.grounding_context = base or None


def backing_expense_ids(receipt: Receipt) -> list[uuid.UUID]:
    """Every entry *receipt* backs: its ``expense_id`` first, then the shared ones."""
    ids: list[uuid.UUID] = [receipt.expense_id] if receipt.expense_id is not None else []
    return ids + shared_expense_ids(receipt)


def shared_with_any(entry_ids: Sequence[uuid.UUID]) -> sa.ColumnElement[bool]:
    """SQL predicate: the receipt's share list names any of *entry_ids* (JSONB ``?|``)."""
    return Receipt.grounding_context[SHARED_EXPENSES_KEY].has_any(
        sa.cast(sa.literal([str(e) for e in entry_ids], ARRAY(TEXT)), ARRAY(TEXT))
    )


def receipt_ids_by_entry(
    session: Session, *, organization_id: uuid.UUID, entry_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Entry id → every receipt backing it: its own (newest first), then shared ones."""
    if not entry_ids:
        return {}
    wanted = set(entry_ids)
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for r_id, r_exp in session.execute(
        sa.select(Receipt.id, Receipt.expense_id)
        .where(
            Receipt.expense_id.in_(list(entry_ids)),
            Receipt.organization_id == organization_id,
        )
        .order_by(Receipt.created_at.desc(), Receipt.id.desc())
    ).all():
        out.setdefault(r_exp, []).append(r_id)
    shared = session.scalars(
        sa.select(Receipt)
        .where(
            Receipt.organization_id == organization_id,
            shared_with_any(list(entry_ids)),
        )
        .order_by(Receipt.created_at.desc(), Receipt.id.desc())
    ).all()
    for receipt in shared:
        for eid in shared_expense_ids(receipt):
            if eid in wanted and receipt.id not in out.get(eid, []):
                out.setdefault(eid, []).append(receipt.id)
    return out
