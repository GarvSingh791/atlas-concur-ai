"""Trip domain model: Trip + TripStatus enum.

Per DATABASE_SCHEMA.md §trips.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id), authorship (created_by/updated_by),
  version (optimistic lock) — NO soft-delete (trips field table omits deleted_at;
  the schema's general soft-delete note applies only where records must be retained
  for audit via hard-delete; trips use hard-delete semantics).

The traveler_id FK (→ users) is the ownership column consumed by scope_query.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CHAR, CheckConstraint, Date, ForeignKey, Index, Numeric, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import (
    AuthorshipMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class TripStatus(str, enum.Enum):
    """Legal lifecycle states for a trip (PG type trip_status)."""

    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    completed = "completed"
    cancelled = "cancelled"


class Trip(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TenantMixin,
    AuthorshipMixin,
    VersionMixin,
    Base,
):
    """A planned/booked trip.  traveler_id is the ownership column for scope_query."""

    __tablename__ = "trips"

    # The employee traveling (ownership column for scope_query)
    traveler_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    origin: Mapped[str] = mapped_column(Text, nullable=False)
    primary_destination: Mapped[str] = mapped_column(Text, nullable=False)

    start_date: Mapped[object] = mapped_column(Date, nullable=False)
    end_date: Mapped[object] = mapped_column(Date, nullable=False)

    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)

    estimated_cost: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)
    budget_amount: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)

    # ISO 4217 three-character code — CHAR(3) per schema (not Text)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    status: Mapped[TripStatus] = mapped_column(
        SAEnum(TripStatus, name="trip_status", create_type=True),
        nullable=False,
    )

    # Populated by AI planner (Phase 4); NULL until then
    recommendation_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Product gap 4 (0016) — the traveler withdrew a RETURNED trip (status ->
    # cancelled).  Distinguishes a withdrawal from an ordinary cancellation.
    withdrawn_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    withdrawn_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # base.py requires explicit name= on every CheckConstraint
        CheckConstraint("end_date >= start_date", name="end_date_gte_start_date"),
        Index("ix_trips_traveler_id_status", "traveler_id", "status"),
        Index("ix_trips_start_date", "start_date"),
        Index("ix_trips_organization_id", "organization_id"),
    )

    segments: Mapped[list[object]] = relationship(
        "TripSegment",
        back_populates="trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TripSegment.created_at",
    )
