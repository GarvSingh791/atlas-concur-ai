"""Pydantic request/response schemas for the travel module."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from database.models.trip import TripStatus
from database.models.trip_segment import SegmentType

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class SegmentCreate(BaseModel):
    segment_type: SegmentType
    supplier: str | None = None
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    location: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    in_policy: bool | None = None
    policy_notes: str | None = None
    details: dict | None = None


class TripCreate(BaseModel):
    origin: str
    primary_destination: str
    start_date: date
    end_date: date
    currency: str
    purpose: str | None = None
    estimated_cost: Decimal | None = None
    budget_amount: Decimal | None = None
    segments: list[SegmentCreate] = []

    @model_validator(mode="after")
    def end_date_gte_start_date(self) -> TripCreate:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be >= start_date")
        return self


class TripTransition(BaseModel):
    status: TripStatus


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trip_id: uuid.UUID
    segment_type: SegmentType
    supplier: str | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    location: str | None
    amount: Decimal | None
    currency: str | None
    in_policy: bool | None
    policy_notes: str | None
    details: dict | None
    created_at: datetime
    updated_at: datetime


class TripResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    traveler_id: uuid.UUID
    origin: str
    primary_destination: str
    start_date: date
    end_date: date
    purpose: str | None
    estimated_cost: Decimal | None
    budget_amount: Decimal | None
    currency: str
    status: TripStatus
    recommendation_context: dict | None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    version: int
    # C7R — the trip's approval as seen by its traveler (who has no
    # approval.read): current approval status (None = never sent for approval)
    # and, while the trip is returned (changes requested) or rejected, the
    # approver's reason.  A returned trip is a draft again; resubmitting it
    # (PATCH status=pending_approval) re-queues the same approval.
    approval_status: str | None = None
    approval_reason: str | None = None
    # UAT2 — the traveler's display name, so a non-owner reader (a manager via
    # the team read scope) sees whose trip it is.  Read-only, server-derived.
    traveler_name: str | None = None
    # Product gap 4 — the traveler withdrew this RETURNED trip (status cancelled).
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None


class TripListItem(BaseModel):
    """Lean list row for GET /trips."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    traveler_id: uuid.UUID
    origin: str
    primary_destination: str
    start_date: date
    end_date: date
    # UAT-2 Issue 4: the trip card title (route + dates + purpose) and its total.
    purpose: str | None = None
    estimated_cost: Decimal | None = None
    currency: str
    status: TripStatus
    version: int
    created_at: datetime
    updated_at: datetime
    # C7R — see TripResponse.
    approval_status: str | None = None
    approval_reason: str | None = None
    # Product gap 4 — see TripResponse.
    withdrawn_at: datetime | None = None
