"""Smart-match receipts, card transactions, and itinerary segments (P4-T10, FR-X2).

Match key: (round(amount, 2), currency.upper(), normalized_vendor, date_bucket).
- Vendor normalization: lowercase, strip punct/whitespace/suffixes; token-containment.
- Date bucket: day-level string; card↔receipt ±1 day; txn↔segment [start-1, end+1].
- Amount: exact to cent (epsilon ≤ $0.01).
- DUPLICATE: receipt + card_txn same charge → ONE MatchCandidate.
- UNMATCHED: card_txn-only / receipt-only → still become entries.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

from ai_agents.agents.expense_agent.ingest import CardTxnFact, IngestedData, ReceiptFact

logger = logging.getLogger(__name__)

_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "plc", "gmbh"}

# Category compatibility: which segment_type values are compatible with which receipt expense_types
_SEGMENT_CATEGORY_COMPAT: dict[str, set[str]] = {
    "flight": {"airfare", "flight", "air"},
    "hotel": {"lodging", "hotel", "accommodation"},
    # C7-21: "transportation" is the OCR prompt's own ground-travel expense_type.
    "rail": {"rail", "train", "ground_transport", "transportation", "transport"},
    "car": {"car", "ground_transport", "rideshare", "taxi", "transportation", "transport"},
}


@dataclass(frozen=True)
class MatchCandidate:
    """One matched (or unmatched) candidate expense entry."""

    key: str
    amount: float
    currency: str
    date: str
    vendor: str | None
    receipt: ReceiptFact | None
    card_txn: CardTxnFact | None
    segment: object | None  # ItinerarySegment
    match_source: str  # "receipt" | "card_txn" | "trip"
    match_confidence: float
    is_duplicate_resolved: bool


def _normalize_vendor(v: str | None) -> str:
    if not v:
        return ""
    v = v.lower()
    v = _PUNCT_RE.sub(" ", v)
    v = _WHITESPACE_RE.sub(" ", v).strip()
    tokens = [t for t in v.split() if t not in _SUFFIXES]
    return " ".join(tokens)


def _vendor_match(a: str | None, b: str | None) -> tuple[bool, float]:
    """Returns (matched, confidence). Token-containment gives 0.85; equality gives 1.0."""
    na = _normalize_vendor(a)
    nb = _normalize_vendor(b)
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    # Token containment: check if any token from shorter string is in longer
    tokens_a = set(na.split())
    tokens_b = set(nb.split())
    if tokens_a & tokens_b:
        return True, 0.85
    return False, 0.0


def _parse_date(d: str | None) -> date | None:
    if not d:
        return None
    try:
        return date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return None


def _amounts_match(a: float, b: float) -> bool:
    return abs(a - b) <= 0.01


def _dates_in_range(d: date | None, start: date | None, end: date | None) -> bool:
    """Check if d is within [start-1, end+1] range."""
    if d is None or start is None or end is None:
        return False
    return (start - timedelta(days=1)) <= d <= (end + timedelta(days=1))


def _dates_within_one_day(d1: date | None, d2: date | None) -> bool:
    if d1 is None or d2 is None:
        return False
    return abs((d1 - d2).days) <= 1


def _segment_compatible_with_category(segment_type: str, expense_type: str | None) -> bool:
    """Check if a segment type is compatible with a receipt expense_type."""
    if expense_type is None:
        return True  # no type info, allow
    compat = _SEGMENT_CATEGORY_COMPAT.get(segment_type, set())
    return expense_type.lower() in compat


def smart_match(data: IngestedData) -> list[MatchCandidate]:
    """Match receipts, card transactions, and segments into MatchCandidates.

    Algorithm:
    1. For each receipt, find matching card_txns (same amount+currency, date ±1).
    2. For each matched pair (or receipt-only / card_txn-only), find compatible segment.
    3. Segment-only planned entries are NOT auto-created.
    4. Include already_linked receipts and already_matched txns as markers (for idempotency).
    """
    candidates: list[MatchCandidate] = []

    # Filter to active (non-already-linked/matched) for new matching. A receipt
    # linked only to its own editable inbox draft is NOT already_linked (C7-22).
    active_receipts = [r for r in data.receipts if not r.already_linked]
    active_txns = [t for t in data.card_txns if not t.already_matched]

    used_txn_ids: set[str] = set()
    used_receipt_ids: set[str] = set()

    # ── Step 1: Receipt + CardTxn matching ───────────────────────────────────
    for receipt in active_receipts:
        r_date = _parse_date(receipt.date)
        matched_txn: CardTxnFact | None = None
        best_conf = 0.0

        for txn in active_txns:
            if txn.txn_id in used_txn_ids:
                continue
            if not _amounts_match(receipt.amount, txn.amount):
                continue
            if receipt.currency.upper() != txn.currency.upper():
                continue
            t_date = _parse_date(txn.txn_date)
            if not _dates_within_one_day(r_date, t_date):
                continue
            # Vendor match is a tiebreaker, not required
            _, conf = _vendor_match(receipt.vendor, txn.merchant)
            # Even if vendor doesn't match, amount+currency+date is sufficient
            effective_conf = max(conf, 0.80)
            if effective_conf > best_conf:
                best_conf = effective_conf
                matched_txn = txn

        # C7-22: a receipt that already sits in the Receipt Inbox as its own
        # draft entry only produces a candidate when it matches a card
        # transaction — otherwise the inbox entry already represents it and a
        # second receipt-only entry would double-claim the charge.
        if receipt.inbox_expense_id is not None and matched_txn is None:
            continue

        # Find best segment match
        segment = _find_segment(
            receipt.amount, receipt.currency, r_date, receipt.expense_type, data
        )

        is_dup = matched_txn is not None
        if is_dup:
            used_txn_ids.add(matched_txn.txn_id)  # type: ignore[union-attr]
        used_receipt_ids.add(receipt.receipt_id)

        key = f"receipt:{receipt.receipt_id}"
        candidates.append(
            MatchCandidate(
                key=key,
                amount=receipt.amount,
                currency=receipt.currency,
                date=receipt.date,
                vendor=receipt.vendor or (matched_txn.merchant if matched_txn else None),
                receipt=receipt,
                card_txn=matched_txn,
                segment=segment,
                match_source="receipt",
                match_confidence=0.9 if segment else (0.85 if is_dup else 0.75),
                is_duplicate_resolved=is_dup,
            )
        )

    # ── Step 2: Unmatched CardTxns ────────────────────────────────────────────
    for txn in active_txns:
        if txn.txn_id in used_txn_ids:
            continue
        t_date = _parse_date(txn.txn_date)
        segment = _find_segment(txn.amount, txn.currency, t_date, None, data)
        key = f"card_txn:{txn.txn_id}"
        candidates.append(
            MatchCandidate(
                key=key,
                amount=txn.amount,
                currency=txn.currency,
                date=txn.txn_date,
                vendor=txn.merchant,
                receipt=None,
                card_txn=txn,
                segment=segment,
                match_source="card_txn",
                match_confidence=0.7 if segment else 0.6,
                is_duplicate_resolved=False,
            )
        )

    return candidates


def _find_segment(
    amount: float,
    currency: str,
    entry_date: date | None,
    expense_type: str | None,
    data: IngestedData,
) -> object | None:
    """Find a matching itinerary segment by amount, currency, and date range."""
    for seg in data.segments:
        if seg.amount is None:
            continue
        if not _amounts_match(amount, seg.amount):
            continue
        if seg.currency is None or currency.upper() != seg.currency.upper():
            continue
        start = _parse_date(seg.start_datetime)
        end = _parse_date(seg.end_datetime) or start
        if entry_date is None or not _dates_in_range(entry_date, start, end):
            continue
        # Gate by category compatibility
        if not _segment_compatible_with_category(seg.segment_type, expense_type):
            continue
        return seg
    return None
