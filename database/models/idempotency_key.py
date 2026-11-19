"""IdempotencyKey support table.

Per DATABASE_SCHEMA.md §idempotency_keys.  Conventions applied:
  UUID PK, created_at only (append-only / lookup-only — no updated_at),
  tenant (organization_id).  No authorship, no version, no soft-delete.

entity_id has NO database FK — the reference is polymorphic (can point to
any entity type: expense_report, trip, etc.).  Application-layer integrity
matches the approach used in approvals.subject_id.

Unique constraint: (organization_id, scope, idempotency_key) — one entry
per client-supplied key within a scope+org namespace.  organization_id
leads the constraint, so it also serves as the implicit FK coverage index
(matches the 0005 FK-coverage rule — no extra index needed).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin


class IdempotencyKey(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """A client-supplied idempotency key, scoped to an org+scope namespace.

    Records which entity was created for a given key so that retried requests
    can return the original entity instead of creating a duplicate.
    entity_id has no FK constraint — polymorphic, consistent with the approach
    in approvals.subject_id (DATABASE_SCHEMA.md §Relationships).
    """

    __tablename__ = "idempotency_keys"

    # e.g. "expense_report.create", "trip.create"
    scope: Mapped[str] = mapped_column(Text, nullable=False)

    # The client-supplied idempotency key (opaque string)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    # The PK of the entity that was created for this key — no FK (polymorphic)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_keys_organization_id_scope_idempotency_key",
        ),
    )
