"""TripSegment model: individual legs of a trip.

Per DATABASE_SCHEMA.md §trip_segments.  Convention columns: UUID PK + timestamps
ONLY — no organization_id, no created_by/updated_by, no version, no deleted_at.
trip_id FK carries ON DELETE CASCADE so segments are true children of trips.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import BOOLEAN, CHAR, TIMESTAMP, CheckConstraint, ForeignKey, Index, Numeric, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class SegmentType(str, enum.Enum):
    """Transport/accommodation mode for a trip leg (PG type segment_type)."""

    flight = "flight"
    hotel = "hotel"
    rail = "rail"
    car = "car"


class TripSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One leg of a trip.  Only UUID PK + timestamps — no tenant/authorship/version."""

    __tablename__ = "trip_segments"

    # Parent trip — CASCADE: segments are deleted when the trip is deleted
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )

    segment_type: Mapped[SegmentType] = mapped_column(
        SAEnum(SegmentType, name="segment_type", create_type=True),
        nullable=False,
    )

    supplier: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_datetime: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    end_datetime: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    location: Mapped[str | None] = mapped_column(Text, nullable=True)

    amount: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)

    in_policy: Mapped[bool | None] = mapped_column(BOOLEAN, nullable=True)
    policy_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_trip_segments_trip_id", "trip_id"),
        CheckConstraint("end_datetime >= start_datetime", name="end_datetime_gte_start_datetime"),
    )

    trip: Mapped[object] = relationship("Trip", back_populates="segments")
