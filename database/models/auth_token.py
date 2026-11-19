"""AuthToken — one-time account tokens (phase 12 follow-ups: account recovery).

One row per issued token: a password reset, an invitation or an email
verification.  The RAW token is never stored or logged — only its SHA-256
digest (``token_hash``, 64 lower-case hex chars, unique).  A token is usable
only while ``used_at`` is NULL and ``expires_at`` is in the future, only for
its ``purpose`` and only for the organisation it was issued in.  Using it,
revoking it, or issuing a newer token of the same purpose sets ``used_at``
(rows are never deleted by the application — they are the record of who asked
for what, and when).

Conventions: UUID PK, tenant column (``organization_id``), ``created_at`` only
(rows change exactly once, when consumed).  ``user_id`` CASCADEs (a token has
no meaning without its account); ``created_by_id`` (the inviting admin, or the
user themself) goes NULL when that account is deleted.  ``purpose`` is TEXT
with a CHECK, not a PG enum, so adding a purpose later is a one-line CHECK
swap rather than an ``ALTER TYPE``.  ``requested_ip_hash`` is a keyed hash of
the requesting address (optional; never the address itself).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import UUIDPrimaryKeyMixin


class AuthTokenPurpose(str, enum.Enum):
    password_reset = "password_reset"
    invite = "invite"
    email_verify = "email_verify"


_PURPOSES_SQL = ", ".join(f"'{p.value}'" for p in AuthTokenPurpose)


class AuthToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "auth_tokens"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    requested_ip_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
        CheckConstraint(f"purpose IN ({_PURPOSES_SQL})", name="purpose_known"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_sha256_hex"),
        # Invalidate-earlier / pending-invite lookups: (user, purpose) with used_at.
        Index("ix_auth_tokens_user_id_purpose", "user_id", "purpose"),
    )
