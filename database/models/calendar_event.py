"""CalendarEvent domain model (simulated grounding source).

Per DATABASE_SCHEMA.md §calendar_events.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id).
  NO authorship — system-seeded grounding data.
  No version, no soft-delete.

start_datetime is NOT NULL; end_datetime is nullable (open-ended events).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CalendarEvent(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A seeded calendar entry used to ground the Receipt Analysis and Travel Agents.

    No authorship — grounding data is system-seeded, not user-authored.
    """

    __tablename__ = "calendar_events"

    # The calendar owner (RESTRICT: events are not orphaned).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Geographic context for receipt matching ("where was this receipt?").
    location: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_datetime: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )

    end_datetime: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_calendar_events_organization_id", "organization_id"),
        Index("ix_calendar_events_user_id", "user_id"),
    )
