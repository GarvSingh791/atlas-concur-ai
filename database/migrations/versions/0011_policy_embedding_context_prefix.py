"""Add R4-T1 contextual situating prefix (context_prefix) to policy_embeddings.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-29

Additive only.  Adds one nullable ``Text`` column ``context_prefix`` to
``policy_embeddings`` carrying the corpus-side contextual situating prefix.  The
prefix enters the EMBEDDING input + the ``chunk_tsv`` lexical vector ONLY; the
``chunk_text`` column stays pristine (reranker input + citation snippets read it),
preserving tau*-invariance.  The column is populated by the seeder; NULL on any
non-corpus / prefixes-off row.

The ``policy_embeddings.embedding VECTOR(1536)`` column, its HNSW index, and the
``chunk_tsv`` GIN index are untouched.  ``downgrade()`` drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "policy_embeddings",
        sa.Column("context_prefix", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("policy_embeddings", "context_prefix")
