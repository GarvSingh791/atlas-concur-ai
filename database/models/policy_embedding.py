"""PolicyEmbedding domain model (RAG index).

Per DATABASE_SCHEMA.md §policy_embeddings.  Conventions applied:
  UUID PK, created_at ONLY (append-only; no updated_at).
  No tenant (scoped via policy_id), no authorship, no version.

embedding column: VECTOR(EMBEDDING_DIMENSION) — requires the pgvector extension
and the pgvector Python package.  EMBEDDING_DIMENSION (1536) is defined in
config/constants/embeddings.py and must match the provider model.
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, SmallInteger, Text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.constants.embeddings import EMBEDDING_DIMENSION
from database.models.base import Base
from database.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class PolicyEmbedding(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One vector chunk of a policy document for grounded Q&A with citations.

    CASCADE: embeddings are hard-deleted when the parent policy is deleted.
    created_at only — this is an append-only RAG index table.
    """

    __tablename__ = "policy_embeddings"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )

    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)

    # R4-T1: corpus-side contextual situating prefix; enters the EMBEDDING input +
    # chunk_tsv ONLY (chunk_text stays pristine; reranker + citations read chunk_text).
    # NULL on non-corpus / prefixes-off rows.
    context_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stable citation label back into the policy document.
    clause_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    # R0-T3 chunk metadata (nullable; legacy/stub rows leave these NULL).
    #   section            -- the within-document section / category label this unit
    #                         belongs to (e.g. "Section 5 — Air Travel", "Air Travel").
    #   parent_section_ref -- the parent-context group key used to assemble the
    #                         enclosing context handed to the generator: the SECTION
    #                         for policy clauses (siblings share it) and the unit's own
    #                         clause_ref for self-contained faq / howto / reference units
    #                         (parent = self).  Resolver groups by (policy_id, this).
    #   ordinal            -- the unit's position within its document, for stable
    #                         parent-context assembly order (robust to multi-prefix
    #                         sections like DATA-* + CONT-* in policy Section 20).
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_section_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordinal: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # Similarity search vector.  Dimension must match EMBEDDING_DIMENSION.
    embedding: Mapped[object] = mapped_column(
        Vector(EMBEDDING_DIMENSION),
        nullable=False,
    )

    # R1-T1 lexical-retrieval index: a Postgres full-text vector that powers the SPARSE
    # (BM25-flavored) leg of hybrid retrieval, complementing the dense `embedding` above.
    # Built (in the seeder, NOT GENERATED) from chunk_text + clause_ref + section + the
    # parent DOCUMENT TITLE (`policies.name` — a cross-table value a STORED GENERATED
    # column cannot reach).  Populated alongside each embedding by
    # database.seeds.chunk_tsv.populate_chunk_tsv; NULL on any non-corpus / pre-R1 row.
    chunk_tsv: Mapped[object | None] = mapped_column(TSVECTOR, nullable=True)

    __table_args__ = (
        Index("ix_policy_embeddings_policy_id", "policy_id"),
        Index(
            "ix_policy_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 200},
        ),
        # GIN index over the lexical vector for the sparse retrieval path (R1-T1).
        Index(
            "ix_policy_embeddings_chunk_tsv",
            "chunk_tsv",
            postgresql_using="gin",
        ),
    )

    policy: Mapped[object] = relationship("Policy", back_populates="embeddings")
