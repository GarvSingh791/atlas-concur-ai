"""Tune the policy_embeddings HNSW index: ef_construction 64 -> 200.

Revision ID: 0012_hnsw_ef_construction_200
Revises: 0011
Create Date: 2026-07-02

AUTHORIZED INDEX-TUNING EXCEPTION to the no-migration-drift guard.

The 0005 indexing strategy built the ANN index
``ix_policy_embeddings_embedding_hnsw`` with pgvector's *default* build
parameters (m = 16, ef_construction = 64).  ``m = 16`` is the researched
optimum for 1536-d cosine-space embeddings and is LEFT UNCHANGED; only the
build-time ``ef_construction`` is raised from the pgvector default 64 into the
researched 128–200 quality band (top of band = 200).  A higher
``ef_construction`` produces a denser, higher-recall graph at index-BUILD time
only — query-time cost and the on-disk column are unaffected.

Invariants preserved (this migration is additive / reversible / no-downtime):
  * 1536-d embedding column (VECTOR(1536)) — untouched.
  * opclass ``vector_cosine_ops`` — untouched (still cosine space).
  * ``m = 16`` graph connectivity — untouched.
  * canonical index name ``ix_policy_embeddings_embedding_hnsw`` — preserved
    via a build-then-swap so the ORM model, 0005, and every downstream query
    plan keep referencing the SAME stable name.

Zero-downtime build-then-swap (CONCURRENTLY, so no table lock):
  1. CREATE the new graph under a temporary ``_v2`` name CONCURRENTLY.
  2. DROP the old default-param index CONCURRENTLY.
  3. RENAME ``_v2`` back to the canonical name (RENAME is a fast catalog-only
     metadata op — no rebuild, no CONCURRENTLY needed).
``downgrade()`` reverses this symmetrically, rebuilding the ef_construction=64
graph under a temporary ``_v0`` name and swapping it back into place.

CONCURRENTLY cannot run inside a transaction block, so upgrade/downgrade run
inside ``op.get_context().autocommit_block()``.

FORWARD-LOOKING: the current corpus is ~208 rows.  At that scale HNSW recall is
already effectively 1.0 and this change CANNOT move retrieval p50 today — it is
a correctness/quality provision for a larger future corpus, shipped now so the
index definition and the ORM model never drift apart.
"""

from __future__ import annotations

from alembic import op

# revision identifiers
revision: str = "0012_hnsw_ef_construction_200"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # CONCURRENTLY index DDL cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        # 1. Build the tuned (ef_construction=200) graph under a temp name.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_policy_embeddings_embedding_hnsw_v2 "
            "ON policy_embeddings USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 200)"
        )
        # 2. Drop the old default-param (ef_construction=64) graph.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_policy_embeddings_embedding_hnsw")
        # 3. Swap the temp name back to the canonical name (catalog-only).
        op.execute(
            "ALTER INDEX ix_policy_embeddings_embedding_hnsw_v2 "
            "RENAME TO ix_policy_embeddings_embedding_hnsw"
        )


def downgrade() -> None:
    # CONCURRENTLY index DDL cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        # 1. Rebuild the default-param (ef_construction=64) graph under a temp name.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_policy_embeddings_embedding_hnsw_v0 "
            "ON policy_embeddings USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
        # 2. Drop the tuned (ef_construction=200) graph.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_policy_embeddings_embedding_hnsw")
        # 3. Swap the temp name back to the canonical name (catalog-only).
        op.execute(
            "ALTER INDEX ix_policy_embeddings_embedding_hnsw_v0 "
            "RENAME TO ix_policy_embeddings_embedding_hnsw"
        )
