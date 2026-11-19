"""Add R1-T1 lexical full-text index (chunk_tsv tsvector + GIN) to policy_embeddings.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-27

Additive only.  Adds one nullable ``tsvector`` column ``chunk_tsv`` to
``policy_embeddings`` plus a GIN index over it, enabling the sparse (lexical,
BM25-flavored) leg of hybrid retrieval (R1-T2) alongside the existing dense pgvector
path.  The column is populated by the seeder
(``database.seeds.chunk_tsv.populate_chunk_tsv``) from chunk_text + clause_ref +
section + the parent document title; it is intentionally NOT a GENERATED column
because the title lives on the parent ``policies`` row (cross-table).

The ``policy_embeddings.embedding VECTOR(1536)`` column and its HNSW index are
untouched.  ``downgrade()`` drops the GIN index then the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "policy_embeddings",
        sa.Column("chunk_tsv", postgresql.TSVECTOR(), nullable=True),
    )
    op.create_index(
        "ix_policy_embeddings_chunk_tsv",
        "policy_embeddings",
        ["chunk_tsv"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_policy_embeddings_chunk_tsv", table_name="policy_embeddings")
    op.drop_column("policy_embeddings", "chunk_tsv")
