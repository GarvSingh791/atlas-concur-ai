"""Add document_type discriminator to policies (R0-T2).

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-26

Additive only.  Adds a nullable ``document_type`` column (PG enum
``policy_document_type``) to ``policies`` so a single organization can hold the
multi-document Helios T&E corpus (policy / faq / howto / reference) and
retrieval can qualify citations by document type.

- The legacy single-policy stub row keeps ``document_type = NULL``.
- The ``policy_embeddings.embedding VECTOR(1536)`` column is untouched.
- Enum strategy mirrors migration 0004: ``create_type=False`` + explicit
  ``.create(checkfirst=True)`` on upgrade and ``.drop(checkfirst=True)`` on
  downgrade, so both directions are idempotent / re-runnable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

_policy_document_type_enum = postgresql.ENUM(
    "policy",
    "faq",
    "howto",
    "reference",
    name="policy_document_type",
    create_type=False,
)


def upgrade() -> None:
    # Create the PG enum type once (idempotent), then add the nullable column.
    _policy_document_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "policies",
        sa.Column("document_type", _policy_document_type_enum, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("policies", "document_type")
    # Guarded drop so the down-migration is re-runnable.
    _policy_document_type_enum.drop(op.get_bind(), checkfirst=True)
