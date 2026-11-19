"""Real (OpenAI) embeddings adapter — lazily initializes on first call."""

from __future__ import annotations

import logging
from typing import Any

from config.constants.embeddings import EMBEDDING_DIMENSION
from config.settings import get_settings
from services.embeddings_provider._normalize import l2_normalize
from services.embeddings_provider.interface import EmbeddingsProvider
from services.exceptions import ProviderConfigurationError, ProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)


class RealEmbeddingsProvider(EmbeddingsProvider):
    """OpenAI embeddings adapter. Lazily creates the client on first call."""

    def _get_client(self) -> Any:
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:
            raise ProviderUnavailableError(
                "openai SDK not installed; run: pip install openai"
            ) from exc

        settings = get_settings()
        if not settings.EMBEDDINGS_API_KEY:
            raise ProviderConfigurationError(
                "EMBEDDINGS_API_KEY is required when EMBEDDINGS_PROVIDER=openai"
            )
        return openai.OpenAI(api_key=settings.EMBEDDINGS_API_KEY)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        # Fence 1 (1536-d only): the requested dimensionality MUST match the pgvector schema
        # dim — a real dimension change is a separate breaking migration (out of scope).  Fail
        # loud rather than silently insert wrong-width vectors.  Checked FIRST (a config error
        # needs no client/SDK), so the guard fires regardless of key/SDK availability.
        if settings.EMBEDDINGS_DIMENSIONS != EMBEDDING_DIMENSION:
            raise ProviderConfigurationError(
                f"EMBEDDINGS_DIMENSIONS={settings.EMBEDDINGS_DIMENSIONS} must equal the schema "
                f"dimension {EMBEDDING_DIMENSION} (1536-d only — Fence 1)."
            )
        client = self._get_client()
        # `dimensions=` lets text-embedding-3-large be Matryoshka-truncated to 1536 (config-only
        # upgrade); for text-embedding-3-small @1536 it is the native width (no-op truncation).
        response = client.embeddings.create(
            model=settings.EMBEDDINGS_MODEL,
            input=texts,
            dimensions=settings.EMBEDDINGS_DIMENSIONS,
        )
        vectors = [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
        out: list[list[float]] = []
        for vec in vectors:
            if len(vec) != EMBEDDING_DIMENSION:
                raise ProviderError(
                    f"Embeddings dimension mismatch: expected {EMBEDDING_DIMENSION}, got {len(vec)}"
                )
            # Renormalize after a (possible) Matryoshka truncation so cosine distance is valid.
            out.append(l2_normalize(list(vec)))
        return out
