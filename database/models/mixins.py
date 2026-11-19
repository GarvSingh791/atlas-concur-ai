"""Composable, single-responsibility model mixins (the table conventions).

Each mixin owns exactly ONE convention from ``DATABASE_SCHEMA.md`` and nothing
else, so a model composes only the subset it actually needs. The conventions are
deliberately NOT uniform across tables — e.g. ``audit_log`` is append-only
(``UUIDPrimaryKeyMixin`` + ``created_at`` only), while ``expense_reports`` uses
every mixin. A single monolithic base would force every column onto every table;
these mixins avoid that.

Usage (Phase 3): ``class ExpenseReport(UUIDPrimaryKeyMixin, TimestampMixin,
TenantMixin, AuthorshipMixin, VersionMixin, Base): ...`` — ``Base`` stays LAST in
the bases list so the declarative metaclass resolves correctly.

These mixins are plain classes: SQLAlchemy 2.0 picks up ``Mapped[]`` +
``mapped_column()`` attributes via inheritance, so ``@declarative_mixin`` /
``declared_attr`` are not needed for these scalar columns.

FKs resolved (P2-T3): ``TenantMixin.organization_id`` → organizations;
``AuthorshipMixin.created_by``/``updated_by`` → users.  The identity tables that
use these mixins (and ``audit_log``) now carry real FK constraints.
Optimistic-lock ENFORCEMENT (``version_id_col``) remains P3-T4.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Integer, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """``id`` UUID primary key, generated DB-side (``gen_random_uuid()``)."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class CreatedAtMixin:
    """``created_at`` ONLY (TIMESTAMPTZ, NOT NULL, server default ``now()``).

    For append-only / immutable tables such as ``audit_log`` that must record an
    insert timestamp but must NOT carry ``updated_at`` (NFR-R4: rows are never
    updated). Kept standalone — deliberately NOT folded into ``TimestampMixin`` —
    so the both-columns convention stays the single source of truth for mutable
    tables and this file's tested mixins are left undisturbed.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TimestampMixin:
    """``created_at`` / ``updated_at`` (TIMESTAMPTZ, NOT NULL).

    Both default to ``now()`` server-side on INSERT; ``updated_at`` additionally
    refreshes to ``now()`` on every UPDATE via ``onupdate``.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantMixin:
    """``organization_id`` tenant seam (UUID, NOT NULL, FK → organizations).

    FK resolved in P2-T3 once the organizations table exists.  Enables
    multi-tenant isolation to be switched on later without a schema change
    (NFR-S4).
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )


class AuthorshipMixin:
    """``created_by`` / ``updated_by`` provenance (UUID, nullable, FK → users).

    FKs resolved in P2-T3 once the users table exists.  Nullable because some
    rows are system-initiated (no actor).
    """

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class SoftDeleteMixin:
    """``deleted_at`` soft-delete marker (TIMESTAMPTZ, NULL = live row).

    Only the column (and an ``is_deleted`` convenience property) live here.
    Global soft-delete query filtering belongs to the data-access layer in a
    later task, not on the model.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    @property
    def is_deleted(self) -> bool:
        """True when this row has been soft-deleted."""
        return self.deleted_at is not None


class VersionMixin:
    """``version`` optimistic-lock counter (INTEGER, NOT NULL, default 1).

    Column and default ONLY. Optimistic-lock enforcement (mapper
    ``version_id_col`` / conflict detection) is intentionally deferred to P3-T4.
    """

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        default=1,
    )
