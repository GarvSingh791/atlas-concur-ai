"""Policy domain model.

Per DATABASE_SCHEMA.md §policies.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id), authorship (created_by/updated_by).
  `version` here is a DOMAIN field (document version number, NOT the optimistic-lock
  VersionMixin — it has no server_default=1 and is not wired to mapper version_id_col).
  No soft-delete (hard-delete semantics; policy_rules and policy_embeddings cascade).

Unique constraint: (organization_id, name, version) — one published copy per scope.
"""

from __future__ import annotations

import enum

from sqlalchemy import BOOLEAN, Date, Index, Integer, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import (
    AuthorshipMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class PolicyDocumentType(str, enum.Enum):
    """Kind of corpus document a policies row holds (PG type policy_document_type).

    Added in R0-T2 so a single org can hold the multi-document T&E corpus
    (authoritative policy + FAQ + how-to guide + quick reference) and retrieval
    can qualify citations by document type.  Nullable on the table: pre-R0-T2
    rows (the legacy single-policy stub) carry NULL.
    """

    policy = "policy"
    faq = "faq"
    howto = "howto"
    reference = "reference"


class Policy(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TenantMixin,
    AuthorshipMixin,
    Base,
):
    """A versioned company policy document (source for rules and RAG Q&A)."""

    __tablename__ = "policies"

    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Domain version number (document versioning, not optimistic lock).
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    effective_from: Mapped[object] = mapped_column(Date, nullable=False)

    # Source text chunked for retrieval; NULL until document is uploaded.
    document_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Corpus document discriminator (R0-T2); NULL on the legacy single-policy stub.
    document_type: Mapped[PolicyDocumentType | None] = mapped_column(
        SAEnum(PolicyDocumentType, name="policy_document_type", create_type=True),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        server_default=text("true"),
        default=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_policies_organization_id_name_version",
        ),
        Index(
            "uq_policies_active_per_scope",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    rules: Mapped[list[object]] = relationship(
        "PolicyRule",
        back_populates="policy",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PolicyRule.created_at",
    )
    embeddings: Mapped[list[object]] = relationship(
        "PolicyEmbedding",
        back_populates="policy",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PolicyEmbedding.created_at",
    )
