"""Local deterministic embedder — the canonical implementation.

Uses feature-hashing over numpy to produce a normalized EMBEDDING_DIMENSION-length
vector from any text string. Properties:
  - Deterministic: same input always yields the same vector.
  - Normalized: unit L2 norm (matches cosine-space HNSW index on policy_embeddings).
  - Idempotent: same input → same output.
  - No API key, no network.

database/seeds/_local_embedder.py re-exports embed() from here.
P4-T5 will replace this with the real embeddings provider and re-index policy_embeddings.
"""

from __future__ import annotations

import hashlib
import logging
import re

import numpy as np

from config.constants.embeddings import EMBEDDING_DIMENSION
from services.embeddings_provider.interface import EmbeddingsProvider

logger = logging.getLogger(__name__)

# Mixing seed, kept from the original feature-hash. It is folded into the
# blake2b key below so the bucket/sign distribution stays comparable to the
# previous implementation while becoming stable across processes.
_MIX_SEED: int = 0x9E3779B9
_MIX_KEY: bytes = _MIX_SEED.to_bytes(4, "big")


def _tokenize(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


def embed(text: str) -> list[float]:
    """Return a normalized 1536-dim feature-hash vector for *text*."""
    vec = np.zeros(EMBEDDING_DIMENSION, dtype=np.float64)
    tokens = _tokenize(text)

    for token in tokens:
        token_bytes = token.encode("utf-8")
        # Stable blake2b digests replace Python's salted hash(), which varied
        # across processes. The bucket digest is keyed with _MIX_SEED; the sign
        # digest uses an independent person= domain so the two are uncorrelated.
        bucket_digest = hashlib.blake2b(token_bytes, digest_size=8, key=_MIX_KEY).digest()
        bucket = int.from_bytes(bucket_digest, "big") % EMBEDDING_DIMENSION
        sign_digest = hashlib.blake2b(token_bytes, digest_size=8, person=b"emb-sign").digest()
        sign = 1 if (sign_digest[-1] & 1) == 0 else -1
        vec[bucket] += sign

    norm: float = float(np.linalg.norm(vec))
    if norm == 0.0:
        vec[0] = 1.0
        norm = 1.0
    return (vec / norm).tolist()


class LocalEmbeddingsProvider(EmbeddingsProvider):
    """Deterministic local embedder: no API key, no network."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [embed(t) for t in texts]
