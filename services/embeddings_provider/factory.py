"""Factory: returns the configured EmbeddingsProvider based on Settings."""

from __future__ import annotations

import logging

from config.settings import get_settings
from services.embeddings_provider.interface import EmbeddingsProvider
from services.exceptions import ProviderError

logger = logging.getLogger(__name__)


def get_embeddings_provider() -> EmbeddingsProvider:
    """Return the EmbeddingsProvider configured via EMBEDDINGS_PROVIDER setting.

    Default: local (deterministic stub, no key required).
    """
    from services.embeddings_provider.local import LocalEmbeddingsProvider
    from services.embeddings_provider.real import RealEmbeddingsProvider

    settings = get_settings()
    mode = settings.EMBEDDINGS_PROVIDER

    if mode == "local":
        logger.debug("Embeddings provider: local (deterministic).")
        return LocalEmbeddingsProvider()
    if mode == "openai":
        logger.debug("Embeddings provider: openai (real).")
        return RealEmbeddingsProvider()
    if mode == "openrouter":
        from services.embeddings_provider.openrouter import OpenRouterEmbeddingsProvider

        logger.debug("Embeddings provider: openrouter (OpenAI models via OpenRouter).")
        return OpenRouterEmbeddingsProvider()
    raise ProviderError(f"Unknown EMBEDDINGS_PROVIDER: {mode!r}")
