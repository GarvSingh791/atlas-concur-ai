"""Local deterministic embedder for P3-T5 seed only.

Uses a feature-hashing trick over numpy to produce a normalized EMBEDDING_DIMENSION-length
vector from any text string.  Properties:
  - Deterministic: same input always yields the same vector.
  - Normalized: unit L2 norm (matches cosine-space HNSW index on policy_embeddings).
  - Lexically similar: tokens shared between query and clause pull their hashes to the same
    dimensions, so ANN queries return semantically related clauses.
  - No API key, no network, no LLM provider.

FLAG (P3-T5 plan): semantic quality is intentionally lexical-only.  P4-T5 replaces this with
the real embeddings provider and re-indexes policy_embeddings.  This helper is NOT the
Phase-4 EmbeddingsProvider abstraction.

SHIM (P4-T1): The canonical implementation has been promoted to
services/embeddings_provider/local.py. This module re-exports embed() for
backward compatibility — seed_all.py and test_p3_seed.py continue to work unchanged.
"""

from services.embeddings_provider.local import embed  # noqa: F401

__all__ = ["embed"]
