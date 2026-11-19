"""Data ingestion for the Expense Agent (P4-T10, FR-X1).

Reads receipts + card_transactions from the database (READ-ONLY, org+owner scoped).
Fetches itinerary segments via ItineraryCalendarTool.
Never silently drops unprocessed receipts — they are surfaced in unprocessed_receipts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.inbox import editable_inbox_expense_ids
from ai_agents.tools.base import ToolError
from ai_agents.tools.itinerary_calendar import (
    ItineraryCalendarTool,
    ItineraryIntent,
    ItinerarySegment,
)
from config.constants.fx_rates import is_supported, normalize_currency
from database.models.card_transaction import CardTransaction
from database.models.identity import Organization
from database.models.receipt import OcrStatus, Receipt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptFact:
    """Processed receipt facts extracted from OCR output."""

    receipt_id: str
    amount: float
    currency: str
    date: str  # YYYY-MM-DD
    vendor: str | None
    location: str | None
    expense_type: str | None
    line_items: list | None  # from extracted_fields
    already_linked: bool  # linked to an entry OTHER than its own editable inbox draft
    # C7-22: set when the receipt's only link is its own auto-drafted Receipt
    # Inbox entry on a still-draft report. Such a receipt stays eligible for
    # receipt↔card matching; when it matches, persistence releases the inbox entry.
    inbox_expense_id: str | None = None


@dataclass(frozen=True)
class CardTxnFact:
    """Card transaction facts."""

    txn_id: str
    amount: float
    currency: str
    merchant: str | None
    txn_date: str  # YYYY-MM-DD
    last_four: str | None
    already_matched: bool  # matched=True or expense_id is not None


@dataclass(frozen=True)
class IngestedData:
    """All data ingested for expense report assembly."""

    receipts: list[ReceiptFact]  # ocr_status==processed with non-null extracted_fields
    card_txns: list[CardTxnFact]
    segments: list  # ItinerarySegment objects
    trip_id: str | None
    unprocessed_receipts: list[str]  # receipt_ids that are pending/failed


def _org_base_currency(session: Session, org_id: object) -> str:
    """The organization's base currency (USD when it is missing or unsupported)."""
    base = session.scalar(sa.select(Organization.base_currency).where(Organization.id == org_id))
    return normalize_currency(base) if is_supported(base) else "USD"


def persistence_releases_inbox_drafts() -> bool:
    """True once persist_expense_report releases a receipt's inbox draft entry.

    Contract (C7-22): ``persistence.py`` calls
    ``ai_agents.receipt_pipeline.inbox.release_inbox_draft`` before re-pointing
    ``receipt.expense_id`` at a smart-matched entry (skipping the link when it
    returns False) and declares ``RELEASES_INBOX_DRAFTS_ON_RELINK = True``.
    Until then inbox-linked receipts stay ``already_linked`` (HEAD behaviour).
    """
    # Lazy: persistence → assemble → matching → ingest would cycle at import time.
    from ai_agents.agents.expense_agent import persistence

    return getattr(persistence, "RELEASES_INBOX_DRAFTS_ON_RELINK", False) is True


def ingest(
    intent: ExpenseIntent,
    *,
    context: AgentContext,
    session: Session,
) -> IngestedData:
    """Ingest receipts, card transactions, and itinerary segments.

    All reads are scoped to org + owner. Unprocessed receipts are tracked
    (never silently dropped). Already-linked/matched records are skipped for idempotency.
    """
    user_id = context.user_id
    org_id = context.organization_id

    # ── Load receipts ─────────────────────────────────────────────────────────
    receipt_rows = list(
        session.scalars(
            sa.select(Receipt).where(
                Receipt.organization_id == org_id,
                Receipt.uploaded_by == user_id,
            )
        ).all()
    )

    receipts: list[ReceiptFact] = []
    unprocessed_receipts: list[str] = []
    # Product gap 8: a receipt with no currency is in the org's base currency
    # (the same rule as the receipt upload's auto-draft), not a hard-coded USD.
    base_currency = _org_base_currency(session, org_id)

    # C7-22: a receipt linked only to its own auto-drafted inbox entry is NOT
    # "already linked" for smart-match purposes. Read-only lookup. Only enabled
    # once persistence releases the inbox entry before re-linking the receipt;
    # otherwise a match would re-point the receipt at the card entry and leave
    # an orphaned, receipt-less duplicate in the inbox (review finding).
    inbox_ids: set = set()
    if user_id is not None and persistence_releases_inbox_drafts():
        inbox_ids = editable_inbox_expense_ids(
            session,
            organization_id=org_id,
            user_id=user_id,
            expense_ids=[r.expense_id for r in receipt_rows if r.expense_id is not None],
        )

    for r in receipt_rows:
        rid = str(r.id)
        if r.ocr_status != OcrStatus.processed or r.extracted_fields is None:
            unprocessed_receipts.append(rid)
            continue
        ef = r.extracted_fields
        amount_raw = ef.get("amount")
        if amount_raw is None:
            unprocessed_receipts.append(rid)
            continue
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            unprocessed_receipts.append(rid)
            continue

        # Product gap 8: an amount in a currency Atlas cannot convert is never
        # drafted (no cap comparison, no total) — surfaced as unprocessed, like
        # the upload path that holds such a receipt for review.
        raw_currency = ef.get("currency")
        if raw_currency is None or (isinstance(raw_currency, str) and not raw_currency.strip()):
            currency = base_currency
        elif is_supported(raw_currency):
            currency = normalize_currency(raw_currency)
        else:
            unprocessed_receipts.append(rid)
            continue

        date_str = ef.get("date") or ""
        receipts.append(
            ReceiptFact(
                receipt_id=rid,
                amount=amount,
                currency=currency,
                date=date_str,
                vendor=ef.get("vendor") or None,
                location=ef.get("location") or None,
                expense_type=ef.get("expense_type") or None,
                line_items=ef.get("line_items") or None,
                already_linked=(r.expense_id is not None and r.expense_id not in inbox_ids),
                inbox_expense_id=(str(r.expense_id) if r.expense_id in inbox_ids else None),
            )
        )

    # ── Load card transactions ─────────────────────────────────────────────────
    txn_rows = list(
        session.scalars(
            sa.select(CardTransaction).where(
                CardTransaction.organization_id == org_id,
                CardTransaction.user_id == user_id,
            )
        ).all()
    )

    card_txns: list[CardTxnFact] = []
    for t in txn_rows:
        if not is_supported(t.currency):
            # Product gap 8: never drafted; the swipe stays unmatched in the feed.
            logger.warning("ExpenseAgent: card transaction %s in an unsupported currency.", t.id)
            continue
        already_matched = bool(t.matched) or (t.expense_id is not None)
        card_txns.append(
            CardTxnFact(
                txn_id=str(t.id),
                amount=float(t.amount),  # type: ignore[arg-type]
                currency=normalize_currency(t.currency),
                merchant=t.merchant or None,
                txn_date=str(t.txn_date),
                last_four=t.last_four or None,
                already_matched=already_matched,
            )
        )

    # ── Load itinerary segments ────────────────────────────────────────────────
    segments: list[ItinerarySegment] = []
    inferred_trip_id: str | None = intent.trip_id

    if user_id is not None:
        try:
            result = ItineraryCalendarTool().get_itinerary(
                ItineraryIntent(trip_id=intent.trip_id, include_calendar=False),
                context=context,
                session=session,
            )
            segments = list(result.segments)
            if inferred_trip_id is None and segments:
                # Infer trip_id if all segments share one trip
                trip_ids = {s.trip_id for s in segments}
                if len(trip_ids) == 1:
                    inferred_trip_id = next(iter(trip_ids))
        except ToolError as exc:
            logger.warning("ExpenseAgent: ItineraryCalendarTool failed (%s); no segments.", exc)

    return IngestedData(
        receipts=receipts,
        card_txns=card_txns,
        segments=segments,
        trip_id=inferred_trip_id,
        unprocessed_receipts=unprocessed_receipts,
    )
