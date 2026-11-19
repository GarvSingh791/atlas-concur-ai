"""Evidence for the entry-level policy rules (product gap 2 / gap 8).

Small, pure helpers that turn an entry's own words and its receipt's OCR fields into
the EvalItem fields the entry-level rules read (``ai_agents/tools/entry_rules.py``):

* ``item_text``      — vendor / description / card merchant (detection & wording only);
* ``line_items``     — receipt line items READ by OCR (exclusions, tips);
* ``tip_usd``        — a tip READ by OCR;
* ``ground_*``       — the ground-transport sub-type, rail class and journey hours.

Trust boundary (same as presubmit): only values the receipt READ itself
(field_provenance absent or 'deterministic') are evidence; a value typed by the
claimant ('user_edited') or guessed by gap-fill ('ai_inferred') is not.  Words the
claimant typed may DETECT something (a rail class, a tip, an alcohol keyword) but a
missing number is then reported as "could not verify", never assumed.
"""

from __future__ import annotations

from typing import Any

from ai_agents.receipt_pipeline.evidence import ocr_snapshot
from ai_agents.tools.ground_transport import (
    PARKING,
    RAIL,
    airport_named,
    classify_with_trust,
    premium_rail_class,
    rail_class,
    stated_hours,
)

_DETERMINISTIC = "deterministic"
_MAX_LINE_ITEMS = 100


def _read(provenance: Any, key: str) -> bool:
    """True when *key* was read by OCR (no provenance entry, or 'deterministic')."""
    if not isinstance(provenance, dict):
        return True
    return provenance.get(key, _DETERMINISTIC) == _DETERMINISTIC


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def join_text(*parts: Any) -> str | None:
    texts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    return " | ".join(dict.fromkeys(texts)) or None


def receipt_line_items(fields: Any, provenance: Any) -> tuple[tuple[str, float], ...] | None:
    """((description, amount), ...) from OCR ``line_items``, or None when absent / untrusted."""
    if not isinstance(fields, dict) or not _read(provenance, "line_items"):
        return None
    raw = fields.get("line_items")
    if not isinstance(raw, list) or not raw:
        return None
    out: list[tuple[str, float]] = []
    for li in raw[:_MAX_LINE_ITEMS]:
        if not isinstance(li, dict):
            continue
        desc = next(
            (li[k] for k in ("description", "name", "item", "label") if isinstance(li.get(k), str)),
            None,
        )
        amount = _num(li.get("amount"))
        if desc and desc.strip() and amount is not None:
            out.append((desc.strip(), amount))
    return tuple(out) or None


def receipt_tip(fields: Any, provenance: Any) -> float | None:
    """A tip / gratuity the receipt printed and OCR read (>= 0), else None."""
    if not isinstance(fields, dict):
        return None
    for key in ("tip", "gratuity"):
        if key in fields and _read(provenance, key):
            value = _num(fields.get(key))
            if value is not None and value >= 0:
                return value
    return None


def receipt_parking_days(fields: Any, provenance: Any) -> int | None:
    """Parking days the receipt printed and OCR read (1..366), else None."""
    if not isinstance(fields, dict):
        return None
    for key in ("parking_days", "days"):
        if key in fields and _read(provenance, key):
            value = _num(fields.get(key))
            if value is not None and value.is_integer() and 1 <= value <= 366:
                return int(value)
    return None


def receipt_total(fields: Any, provenance: Any) -> float | None:
    """The receipt's total READ by OCR (> 0), else None.

    A total the claimant typed ('user_edited') or gap-fill guessed ('ai_inferred') is
    not the receipt's: partial-claim checks (MEAL-04, GRND-05) then fall back to the line
    items or say "could not verify".
    """
    if not isinstance(fields, dict) or not _read(provenance, "amount"):
        return None
    value = _num(fields.get("amount"))
    return value if value is not None and value > 0 else None


# Keys ``with_receipt_evidence`` adds; ``build_eval_item_from_fields`` reads them.
RECEIPT_TOTAL_KEY = "_receipt_total"
RECEIPT_WORDS_KEY = "_receipt_words"
RECEIPT_SHARED_KEY = "_receipt_shared"
RECEIPT_VENDOR_LOST_KEY = "_receipt_vendor_lost"


def with_receipt_evidence(
    fields: Any, provenance: Any, *, shared: bool = False, grounding_context: Any = None
) -> dict[str, Any]:
    """A copy of a receipt's OCR *fields* carrying what the RECEIPT itself read — its total
    and its words (``receipt_total`` / ``receipt_texts``) — under private keys, captured
    BEFORE a caller overwrites ``amount`` / ``vendor`` with the entry's (claimed) values.
    ``shared``: the receipt also backs other lines (itemized siblings).
    ``grounding_context``: the receipt row's, whose OCR snapshot still holds the vendor
    OCR read when the claimant edited it; with neither, the vendor evidence is LOST.
    """
    out: dict[str, Any] = dict(fields) if isinstance(fields, dict) else {}
    prov = provenance if isinstance(provenance, dict) else {}
    out[RECEIPT_TOTAL_KEY] = receipt_total(fields, provenance)
    out[RECEIPT_WORDS_KEY] = receipt_texts(fields, provenance, grounding_context=grounding_context)
    out[RECEIPT_SHARED_KEY] = bool(shared)
    out[RECEIPT_VENDOR_LOST_KEY] = (
        prov.get("vendor") == "user_edited" and snapshot_vendor(grounding_context) is None
    )
    return out


def snapshot_vendor(grounding_context: Any) -> str | None:
    """The vendor OCR read at processing time (``receipts.grounding_context`` snapshot,
    written once by the pipeline, never by a correction), else None."""
    snap = ocr_snapshot(grounding_context)
    vendor = snap.get("vendor") if snap is not None else None
    return vendor.strip() if isinstance(vendor, str) and vendor.strip() else None


def receipt_texts(
    fields: Any, provenance: Any, *, grounding_context: Any = None
) -> tuple[str, ...]:
    """Words the RECEIPT itself printed: the OCR vendor (provenance 'deterministic' only —
    a typed / AI-inferred vendor is the claimant's word; when the claimant edited it, the
    frozen OCR snapshot in *grounding_context* is still the machine reading) and the OCR
    line-item descriptions.  Fed to the ground-transport classifier with the claimant's words so a
    renamed vendor cannot move a line out of a sub-type cap: contradicting words make
    the line ambiguous -> the broad category, which every sub-type cap still reaches.
    """
    if not isinstance(fields, dict):
        return ()
    out: list[str] = []
    vendor = fields.get("vendor")
    prov = provenance if isinstance(provenance, dict) else {}
    if prov.get("vendor") == _DETERMINISTIC and isinstance(vendor, str) and vendor.strip():
        out.append(vendor.strip())
    elif (snap := snapshot_vendor(grounding_context)) is not None:
        out.append(snap)
    out.extend(desc for desc, _ in receipt_line_items(fields, provenance) or ())
    return tuple(out)


def ground_fields(
    *texts: str | None,
    trusted_texts: tuple[str | None, ...] = (),
    segment_type: str | None = None,
    segment_hours: float | None = None,
    segment_class: str | None = None,
    vendor_edited: bool = False,
) -> dict[str, Any]:
    """EvalItem updates for a ground-transport line.

    ``texts`` are the claimant's own words (entry vendor / description); ``trusted_texts``
    what the receipt / card printed (deterministic OCR vendor, OCR line items, card
    merchant).  ``classify_with_trust`` decides the sub-type: VERIFIED only when trusted
    evidence (or the trusted segment type) names exactly one family the typed words agree
    with; otherwise every named family is a candidate and the rules of each candidate
    apply (``entry_rules.ground_subtype_reach``).  For rail candidates, the class named in
    the words / the rail segment, and the journey hours from the (trusted) rail segment,
    else stated in the words.  ``vendor_edited``: the receipt's vendor reading was
    overwritten by the claimant and no OCR snapshot of it was found — the line is then
    unconfirmed (every sub-type a candidate).
    """
    trusted = tuple(t for t in trusted_texts if isinstance(t, str) and t.strip())
    every = (*texts, *trusted)
    cls = classify_with_trust(
        texts, trusted, segment_type=segment_type, evidence_destroyed=vendor_edited
    )
    out: dict[str, Any] = {
        "ground_subtype": cls.subtype,
        "ground_candidates": cls.families,
        "ground_verified": cls.verified,
        # Known limits (ground): the families the words / segment name; None when the
        # receipt's vendor reading was destroyed (nothing may then clear the line).
        "ground_named": None if vendor_edited else cls.named,
        "ground_trusted_texts": trusted,
        "item_text": join_text(*every),
    }
    if PARKING in cls.families:
        # GRND-03 is an AIRPORT-parking clause: only a parking line whose words name an
        # airport, or that is linked to a flight segment, is airport parking.  Hotel
        # valet / a city garage stays plain parking and GRND-03 never reaches it.
        seg = str(segment_type or "").strip().lower()
        out["airport_parking"] = seg == "flight" or airport_named(*every) is not None
    if RAIL in cls.families:
        # Review round 5: the itinerary is a plan, the receipt is what was bought.  A
        # premium class named by the receipt / card or by the claimant (against their
        # own interest) wins over the segment, even a segment that says "Standard".  The
        # segment's class is used only when no word names a premium class; typed
        # "standard" alone never clears GRND-01 (the class stays unverified).
        travel_class = premium_rail_class(*every) or (
            rail_class(segment_class) if segment_class else None
        )
        out["travel_class"] = travel_class
        hours = segment_hours if segment_hours and segment_hours > 0 else None
        out["duration_hours"] = hours or stated_hours(*every)
    return out
