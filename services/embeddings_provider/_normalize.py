"""Shared L2 normalization for the real embeddings adapters (OpenAI + OpenRouter).

OpenAI returns unit-norm vectors at a model's NATIVE dimensionality, but a
Matryoshka-truncated request (``dimensions`` < native, e.g. text-embedding-3-large @1536)
is NOT renormalized server-side — so the real adapters renormalize here to honour the
EmbeddingsProvider contract (unit L2 norm, cosine-space HNSW).

A zero-norm vector has UNDEFINED cosine distance and is not HNSW-indexable, so we FAIL LOUD
(``ProviderError``) rather than insert an unretrievable, rank-perturbing row (the local
embedder guards the same degenerate case by pinning a unit basis vector).
"""

from __future__ import annotations

import math

from services.exceptions import ProviderError


def l2_normalize(vec: list[float]) -> list[float]:
    """Return *vec* scaled to unit L2 norm (idempotent for already-unit vectors)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise ProviderError("real embedder returned a zero-norm vector (undefined cosine distance)")
    return [x / norm for x in vec]
