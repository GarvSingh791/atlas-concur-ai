"""Policy knowledge-base indexer — RAG (retrieval-augmented generation) sub-package.

Provides deterministic chunking of policy documents, an idempotent indexer that
writes per-chunk embeddings to the database, and a grounded retrieval flow that
applies a similarity threshold and returns citations or a "policy is silent" result.
All module-level imports are backend-free (services, database, config, stdlib only).

Public exports
--------------
PolicyChunk           -- frozen dataclass: (clause_ref, chunk_text)
chunk_policy_document -- parse a policy document_text into ordered PolicyChunk list
reindex_policy        -- (re-)index a single policy by UUID, return chunk count
reindex_all_active    -- (re-)index all active policies, return {policy_id: count} dict
GroundedRetriever     -- grounded RAG flow: threshold + citation assembly
GroundedRetrieval     -- result type: status + clauses + citations + confidence
GroundedClause        -- one threshold-passing chunk with clause_ref + similarity
GroundingStatus       -- "grounded" | "silent"
"""

from __future__ import annotations

from ai_agents.rag.chunker import PolicyChunk, chunk_policy_document
from ai_agents.rag.indexer import reindex_all_active, reindex_policy
from ai_agents.rag.retrieval import (
    GroundedClause,
    GroundedRetrieval,
    GroundedRetriever,
    GroundingStatus,
)

__all__ = [
    "PolicyChunk",
    "chunk_policy_document",
    "reindex_policy",
    "reindex_all_active",
    "GroundedRetriever",
    "GroundedRetrieval",
    "GroundedClause",
    "GroundingStatus",
]
