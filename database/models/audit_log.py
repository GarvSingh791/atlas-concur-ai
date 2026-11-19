"""The append-only ``audit_log`` model — Atlas's immutable compliance record.

Per ``DATABASE_SCHEMA.md`` (Audit Logging Strategy) and NFR-R4, ``audit_log``
captures who/what/when with before/after state for every meaningful business
event. Rows are **never updated or deleted** — the table is append-only, so it
composes ``UUIDPrimaryKeyMixin`` + ``CreatedAtMixin`` only (NO ``updated_at``,
``version`` or ``deleted_at``).

Single write path: rows are written exclusively through
``backend.app.core.audit.record_audit`` (Dev Rule 8). Nothing else inserts here.

FKs resolved (P2-T3): ``organization_id`` (via ``TenantMixin``) now references
``organizations`` and ``actor_id`` now references ``users``.  ``actor_id`` is
NULL for system-initiated events (e.g. an auto-approval by the Approval Agent).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, TenantMixin, CreatedAtMixin, Base):
    """Immutable record of a single meaningful business event.

    Field set is EXACTLY the ``DATABASE_SCHEMA.md`` definition:
    ``id``, ``organization_id``, ``actor_id``, ``action``, ``entity_type``,
    ``entity_id``, ``before_state``, ``after_state``, ``created_at``.
    """

    __tablename__ = "audit_log"

    # organization_id (UUID NOT NULL, FK → organizations) comes from TenantMixin.

    # NULL when the action was system-initiated; FK → users resolved in P2-T3.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # e.g. 'trip.created', 'report.submitted', 'approval.decided'.
    action: Mapped[str] = mapped_column(Text, nullable=False)

    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # before_state is NULL on create; after_state is NULL on delete.
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # created_at (TIMESTAMPTZ NOT NULL) comes from CreatedAtMixin. No updated_at.

    __table_args__ = (
        # DATABASE_SCHEMA.md Indexing Strategy — entity history lookups.
        # Explicit name: the convention's single-column ``ix`` pattern would not
        # capture all three columns, so name this composite index directly.
        Index(
            "ix_audit_log_entity_type_entity_id_created_at",
            "entity_type",
            "entity_id",
            "created_at",
        ),
        Index("ix_audit_log_organization_id", "organization_id"),
        Index("ix_audit_log_actor_id", "actor_id"),
    )
