"""Categorization and itemization for the Expense Agent (P4-T10, FR-X3).

Priority: segment_type → receipt expense_type → keyword heuristics → other.
No LLM calls. Itemized receipts (line_items ≥ 2) → sibling DraftEntry rows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from ai_agents.agents.expense_agent.matching import MatchCandidate

logger = logging.getLogger(__name__)

# Keyword heuristics (containment on normalized vendor)
_LODGING_KEYWORDS = frozenset(
    [
        "hotel",
        "inn",
        "marriott",
        "hilton",
        "hyatt",
        "westin",
        "sheraton",
        "ritz",
        "suites",
        "resort",
    ]
)
_AIRFARE_KEYWORDS = frozenset(
    ["airline", "air", "flight", "united", "delta", "american", "southwest", "jetblue", "airways"]
)
_GROUND_KEYWORDS = frozenset(
    ["uber", "lyft", "taxi", "cab", "rideshare", "hertz", "avis", "enterprise", "rental"]
)
_MEALS_KEYWORDS = frozenset(
    ["restaurant", "cafe", "food", "dining", "bistro", "coffee", "starbucks", "mcdonald", "pizza"]
)

# Segment type → category mapping
_SEGMENT_TYPE_TO_CATEGORY: dict[str, str] = {
    "flight": "airfare",
    "hotel": "lodging",
    "rail": "ground_transport",
    "car": "ground_transport",
}

# Receipt expense_type → category mapping
_EXPENSE_TYPE_TO_CATEGORY: dict[str, str] = {
    "airfare": "airfare",
    "flight": "airfare",
    "lodging": "lodging",
    "hotel": "lodging",
    "meals": "meals",
    "food": "meals",
    "dining": "meals",
    "ground_transport": "ground_transport",
    # C7-21: the OCR prompt's own vocabulary uses "transportation" for ground
    # travel (taxi, rideshare, rail, car rental); without these keys every
    # ground-transport receipt fell through to "other".
    "transportation": "ground_transport",
    "transport": "ground_transport",
    "ground_transportation": "ground_transport",
    "rail": "ground_transport",
    "train": "ground_transport",
    "rideshare": "ground_transport",
    "car": "ground_transport",
    "taxi": "ground_transport",
    "other": "other",
}


def category_for_expense_type(expense_type: object) -> str | None:
    """Map a receipt/OCR ``expense_type`` to an ExpenseCategory value, or None.

    Normalizes case, surrounding whitespace and space/hyphen separators so
    "Transportation", " ground transport " and "ground-transport" all resolve.
    Returns None for an unknown or non-string value (callers choose the default).
    """
    if not isinstance(expense_type, str):
        return None
    key = expense_type.strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    return _EXPENSE_TYPE_TO_CATEGORY.get(key)


@dataclass(frozen=True)
class DraftEntry:
    """One draft expense entry, possibly itemized."""

    category: str  # ExpenseCategory value
    amount: float
    currency: str
    expense_date: str  # YYYY-MM-DD
    vendor: str | None
    description: str | None
    status: str  # "matched" or "flagged"
    match_source: str | None
    trip_segment_id: str | None
    is_itemized: bool
    primary_receipt_id: str | None
    source_card_txn_id: str | None
    sibling_group_id: str | None  # UUID str shared by itemized siblings
    # C7R: persisted Expense id (str) when the entry is a saved row; None for a
    # draft the Expense Agent has not written yet.  Stamped on policy flags.
    entry_id: str | None = None
    # C7R UAT: the persisted entry's written policy-exception justification
    # (None for agent drafts).  See presubmit.JUSTIFIABLE_RULE_TYPES.
    justification: str | None = None
    # UAT2: EVERY receipt linked to the persisted entry (newest first).  A
    # correct receipt attached after a wrong one must be able to satisfy
    # RECEIPT-REQ, so receipt support considers all of them.  Empty for agent
    # drafts (they carry only ``primary_receipt_id``).
    receipt_ids: tuple[str, ...] = ()
    # Product gap 2 stage 2 (EXP-04): when the owner signed the missing-receipt affidavit
    # for the persisted entry (ISO timestamp), None when not signed / an agent draft.
    receipt_affidavit_at: str | None = None
    # Product gap 2 (policy2): the persisted entry's policy fields (None for agent
    # drafts) — attendees (MEAL-05 / ENT-02), gift disclosure (ENT-04) and the
    # mileage trip (MIL-01..03).  See ai_agents/tools/spend_policy.py.
    attendee_count: int | None = None
    disclosure: dict | None = None
    mileage: dict | None = None


def _normalize_vendor_lower(v: str | None) -> str:
    if not v:
        return ""
    return v.lower()


def _keyword_category(vendor: str | None, merchant: str | None) -> str:
    combined = f"{vendor or ''} {merchant or ''}".lower()
    if any(k in combined for k in _LODGING_KEYWORDS):
        return "lodging"
    if any(k in combined for k in _AIRFARE_KEYWORDS):
        return "airfare"
    if any(k in combined for k in _GROUND_KEYWORDS):
        return "ground_transport"
    if any(k in combined for k in _MEALS_KEYWORDS):
        return "meals"
    return "other"


def _categorize_single(candidate: MatchCandidate) -> str:
    """Determine category for a single (non-itemized) candidate."""
    # Priority 1: segment type
    if candidate.segment is not None:
        seg_type = getattr(candidate.segment, "segment_type", None)
        if seg_type and seg_type in _SEGMENT_TYPE_TO_CATEGORY:
            return _SEGMENT_TYPE_TO_CATEGORY[seg_type]

    # Priority 2: receipt expense_type
    if candidate.receipt is not None and candidate.receipt.expense_type:
        mapped = category_for_expense_type(candidate.receipt.expense_type)
        if mapped is not None:
            return mapped

    # Priority 3: vendor keyword heuristics
    vendor = candidate.vendor
    merchant = candidate.card_txn.merchant if candidate.card_txn else None
    return _keyword_category(vendor, merchant)


def _categorize_line_item(item: dict, group_vendor: str | None) -> str:
    """Categorize a single line item from a hotel folio."""
    desc = str(item.get("description") or "").lower()
    cat = item.get("category") or ""
    combined = f"{desc} {cat} {group_vendor or ''}".lower()

    if any(k in combined for k in _LODGING_KEYWORDS) or "room" in combined:
        return "lodging"
    if any(k in combined for k in _MEALS_KEYWORDS) or "restaurant" in combined:
        return "meals"
    if any(k in combined for k in _GROUND_KEYWORDS):
        return "ground_transport"
    if any(k in combined for k in _AIRFARE_KEYWORDS):
        return "airfare"
    return "other"


def build_draft_entries(candidates: list[MatchCandidate]) -> list[DraftEntry]:
    """Convert MatchCandidates into DraftEntry rows, handling itemization."""
    entries: list[DraftEntry] = []

    for candidate in candidates:
        receipt = candidate.receipt
        segment = candidate.segment

        trip_segment_id: str | None = None
        if segment is not None:
            trip_segment_id = getattr(segment, "segment_id", None)

        source_card_txn_id: str | None = None
        if candidate.card_txn is not None:
            source_card_txn_id = candidate.card_txn.txn_id

        status = "matched" if candidate.match_confidence >= 0.75 else "flagged"

        # Check for itemized receipt (line_items ≥ 2)
        line_items = receipt.line_items if receipt else None
        if line_items and len(line_items) >= 2:
            entries.extend(
                _build_itemized_entries(
                    candidate=candidate,
                    line_items=line_items,
                    trip_segment_id=trip_segment_id,
                    source_card_txn_id=source_card_txn_id,
                    status=status,
                )
            )
        else:
            category = _categorize_single(candidate)
            primary_receipt_id = receipt.receipt_id if receipt else None
            entries.append(
                DraftEntry(
                    category=category,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    expense_date=candidate.date,
                    vendor=candidate.vendor,
                    description=None,
                    status=status,
                    match_source=candidate.match_source,
                    trip_segment_id=trip_segment_id,
                    is_itemized=False,
                    primary_receipt_id=primary_receipt_id,
                    source_card_txn_id=source_card_txn_id,
                    sibling_group_id=None,
                )
            )

    return entries


def _build_itemized_entries(
    *,
    candidate: MatchCandidate,
    line_items: list,
    trip_segment_id: str | None,
    source_card_txn_id: str | None,
    status: str,
) -> list[DraftEntry]:
    """Build sibling DraftEntry rows for an itemized receipt."""
    sibling_group_id = str(uuid.uuid4())
    receipt = candidate.receipt
    primary_receipt_id = receipt.receipt_id if receipt else None
    folio_total = candidate.amount
    currency = candidate.currency
    vendor = candidate.vendor
    date_str = candidate.date

    # Parse line items
    parsed_items: list[dict] = []
    for item in line_items:
        if isinstance(item, dict):
            try:
                amt = float(item.get("amount") or 0)
                parsed_items.append({"amount": amt, "raw": item})
            except (TypeError, ValueError):
                pass

    if not parsed_items:
        # Fallback: single entry
        category = _categorize_single(candidate)
        return [
            DraftEntry(
                category=category,
                amount=folio_total,
                currency=currency,
                expense_date=date_str,
                vendor=vendor,
                description=None,
                status=status,
                match_source=candidate.match_source,
                trip_segment_id=trip_segment_id,
                is_itemized=False,
                primary_receipt_id=primary_receipt_id,
                source_card_txn_id=source_card_txn_id,
                sibling_group_id=None,
            )
        ]

    # Sort by amount descending (largest = primary/room charge)
    parsed_items_sorted = sorted(parsed_items, key=lambda x: x["amount"], reverse=True)

    # Compute residual for sum invariant
    item_sum = sum(i["amount"] for i in parsed_items_sorted)
    residual = round(folio_total - item_sum, 2)

    entries: list[DraftEntry] = []
    for idx, item in enumerate(parsed_items_sorted):
        item_amount = item["amount"]
        if idx == 0:
            # First/largest item gets residual and primary_receipt_id
            item_amount = round(item_amount + residual, 2)
            this_primary_receipt_id = primary_receipt_id
        else:
            this_primary_receipt_id = None

        raw = item["raw"]
        desc = str(raw.get("description") or "")
        category = _categorize_line_item(raw, vendor)

        entries.append(
            DraftEntry(
                category=category,
                amount=item_amount,
                currency=currency,
                expense_date=date_str,
                vendor=vendor,
                description=desc or None,
                status=status,
                match_source=candidate.match_source,
                trip_segment_id=trip_segment_id,
                is_itemized=True,
                primary_receipt_id=this_primary_receipt_id,
                source_card_txn_id=source_card_txn_id if idx == 0 else None,
                sibling_group_id=sibling_group_id,
            )
        )

    return entries
