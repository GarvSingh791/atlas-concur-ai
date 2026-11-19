"""Provider-backed, idempotent policy embedding indexer.

Implements a transactional replace strategy: delete existing rows for the given
policy, then insert a fresh set.  The caller is responsible for commit().

Allowed imports: database.models, services.embeddings_provider, config.constants,
sqlalchemy, stdlib.  Backend imports are prohibited at module level.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ai_agents.rag.chunker import chunk_policy_document
from config.constants.embeddings import EMBEDDING_DIMENSION
from database.models.policy import Policy
from database.models.policy_embedding import PolicyEmbedding
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.interface import EmbeddingsProvider

logger = logging.getLogger(__name__)


def reindex_policy(
    session: Session,
    *,
    policy_id: uuid.UUID,
    embedder: EmbeddingsProvider | None = None,
) -> int:
    """Re-index a single policy by replacing its embeddings rows.

    Parameters
    ----------
    session:
        Active SQLAlchemy session.  Caller is responsible for commit().
    policy_id:
        UUID of the Policy to index.
    embedder:
        Optional EmbeddingsProvider.  Defaults to get_embeddings_provider().

    Returns
    -------
    Number of chunks (rows) written.

    Raises
    ------
    ValueError
        If the policy is not found, or if any embedding vector has a dimension
        that does not match EMBEDDING_DIMENSION (nothing is written in that case).
    """
    policy = session.get(Policy, policy_id)
    if policy is None:
        raise ValueError(f"Policy {policy_id!s} not found")

    embedder = embedder or get_embeddings_provider()

    document_text: str = policy.document_text or ""
    chunks = chunk_policy_document(document_text)

    # Safety guard: this marker chunker only understands [LABEL]-marked documents
    # (the legacy single-policy stub).  The multi-document Helios corpus is chunked
    # + embedded STRUCTURALLY by the seed layer (database/seeds/corpus.chunking +
    # seed_all._embed_corpus) and carries NO [LABEL] markers, so this chunker yields
    # nothing for it.  Without this guard the transactional-replace DELETE below
    # would WIPE a corpus document's structured embeddings.  Skip when there is
    # nothing to index: write nothing, delete nothing, preserve existing rows.
    if not chunks:
        logger.info(
            "reindex_policy: policy_id=%s has no [LABEL]-marked chunks "
            "(structurally-chunked corpus document?) — skipping; embeddings preserved.",
            policy_id,
        )
        return 0

    chunk_texts = [c.chunk_text for c in chunks]

    vectors = embedder.embed_texts(chunk_texts)

    # --- Dimension guard (before any write) ---
    bad = [(i, len(v)) for i, v in enumerate(vectors) if len(v) != EMBEDDING_DIMENSION]
    if bad:
        details = ", ".join(f"chunk[{i}]={dim}" for i, dim in bad)
        raise ValueError(
            f"Embedding dimension mismatch for policy {policy_id!s}: "
            f"expected {EMBEDDING_DIMENSION}, got {details}"
        )

    # --- Transactional replace per policy ---
    session.execute(delete(PolicyEmbedding).where(PolicyEmbedding.policy_id == policy_id))

    new_rows = [
        PolicyEmbedding(
            policy_id=policy_id,
            clause_ref=chunk.clause_ref,
            chunk_text=chunk.chunk_text,
            embedding=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    session.add_all(new_rows)
    session.flush()

    row_count = len(new_rows)
    logger.info(
        "reindex_policy: policy_id=%s chunks=%d rows_written=%d",
        policy_id,
        row_count,
        row_count,
    )
    return row_count


def reindex_all_active(
    session: Session,
    *,
    embedder: EmbeddingsProvider | None = None,
) -> dict[uuid.UUID, int]:
    """Re-index all active policies.

    Parameters
    ----------
    session:
        Active SQLAlchemy session.  Caller is responsible for commit().
    embedder:
        Optional EmbeddingsProvider shared across all policies.

    Returns
    -------
    Mapping of policy_id → chunk count for each re-indexed policy.
    """
    active_policies = session.query(Policy).filter(Policy.is_active.is_(True)).all()

    results: dict[uuid.UUID, int] = {}
    for policy in active_policies:
        count = reindex_policy(session, policy_id=policy.id, embedder=embedder)
        results[policy.id] = count

    logger.info("reindex_all_active: indexed %d policies", len(results))
    return results
