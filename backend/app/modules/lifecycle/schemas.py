"""Request / response schemas for the report & trip lifecycle actions (product gaps 1, 4, 8)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

NOTE_MAX_LENGTH = 500


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class ReimburseRequest(BaseModel):
    """POST /expense-reports/{id}/reimburse — optional finance note (kept in the audit row)."""

    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class WithdrawRequest(BaseModel):
    """POST /expense-reports/{id}/withdraw and /trips/{id}/withdraw — optional owner reason."""

    reason: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _blank_to_none(value)


class ReimbursementQueueItem(BaseModel):
    """One APPROVED report finance may mark reimbursed (GET /reimbursements/queue).

    ``can_reimburse`` is False for the caller's OWN report (segregation of
    duties) — the server refuses it too; the UI never offers the action.
    """

    id: uuid.UUID
    title: str
    owner_id: uuid.UUID
    owner_name: str | None = None
    total_amount: Decimal
    currency: str
    submitted_at: datetime | None = None
    approval_status: str | None = None
    version: int
    can_reimburse: bool


class ReimbursementQueue(BaseModel):
    items: list[ReimbursementQueueItem]
    total: int
