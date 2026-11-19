"""Pydantic response schemas for the card module (product gap 8)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from backend.app.core.money import AmountConversion

CardTransactionStatus = Literal["matched", "unmatched"]


class CardTransactionItem(BaseModel):
    """One card swipe of the caller, with what it was matched to.

    ``status`` is ``matched`` once the swipe backs an expense entry (smart match),
    else ``unmatched`` (the UI offers "Upload receipt").  ``expense_id`` /
    ``report_id`` / ``receipt_id`` link the matched entry, its report and its
    first receipt — only when that report is the caller's own (never a link to
    someone else's record).  ``last_four`` is the card's last four digits only.
    """

    id: uuid.UUID
    merchant: str | None
    txn_date: date
    amount: Decimal
    currency: str
    last_four: str | None
    status: CardTransactionStatus
    expense_id: uuid.UUID | None = None
    report_id: uuid.UUID | None = None
    receipt_id: uuid.UUID | None = None
    # The amount in the org's base currency when the swipe is in another one.
    amount_conversion: AmountConversion | None = None
