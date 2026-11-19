"""Add R0-T3 chunk metadata to policy_embeddings (section, parent_section_ref, ordinal).

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-26

Additive only.  Adds three nullable columns to ``policy_embeddings`` so each
chunk of the multi-document Helios corpus (R0-T3) carries:
  - ``section``            — the within-document section / category label.
  - ``parent_section_ref`` — the parent-context group key (the SECTION for policy
                             clauses; the unit's own ref for self-contained faq /
                             howto / reference units) used to assemble the enclosing
                             context handed to the generator (parent-document
                             retrieval).
  - ``ordinal``            — the unit's position within its document, for stable
                             parent-context assembly order.

The legacy single-policy stub rows leave all three NULL.  The
``policy_embeddings.embedding VECTOR(1536)`` column and every index are untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("policy_embeddings", sa.Column("section", sa.Text(), nullable=True))
    op.add_column(
        "policy_embeddings",
        sa.Column("parent_section_ref", sa.Text(), nullable=True),
    )
    op.add_column("policy_embeddings", sa.Column("ordinal", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("policy_embeddings", "ordinal")
    op.drop_column("policy_embeddings", "parent_section_ref")
    op.drop_column("policy_embeddings", "section")
