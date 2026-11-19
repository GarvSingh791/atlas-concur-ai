"""NotificationRead — per-user read state for derived notifications (product gap 3).

Notifications are NOT stored: GET /notifications derives them on every call from
existing workflow state (approvals, approval steps, report / trip statuses).  Each
derived item carries a deterministic key (e.g. ``decision:<approval_step id>``),
and this table records which keys a user has read — nothing else.

Conventions: UUID PK, no tenant column (scoped through ``user_id``, whose user row
carries the organization), no timestamps beyond ``read_at`` (rows are only ever
inserted and pruned, never updated).  ``user_id`` CASCADEs: read state has no
meaning once the user is gone.  ``(user_id, notification_key)`` is unique so
marking twice is an idempotent no-op (INSERT ... ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import UUIDPrimaryKeyMixin

# Longest key the service derives is ~"approval_pending:<uuid>:<int>" (< 80 chars).
NOTIFICATION_KEY_MAX_LENGTH = 200


class NotificationRead(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notification_reads"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    notification_key: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "notification_key", name="uq_notification_reads_user_id_notification_key"
        ),
        CheckConstraint(
            f"char_length(notification_key) BETWEEN 1 AND {NOTIFICATION_KEY_MAX_LENGTH}",
            name="notification_key_length",
        ),
        # Pruning old read rows per user (read_at window) — the unique index above
        # already serves the (user_id, key) lookups.
        Index("ix_notification_reads_user_id_read_at", "user_id", "read_at"),
    )
