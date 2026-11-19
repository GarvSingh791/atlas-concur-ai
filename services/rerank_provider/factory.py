"""Factory: returns the configured RerankProvider based on Settings.RERANK_PROVIDER (R2-T1)."""

from __future__ import annotations

import logging

from config.settings import get_settings
from services.exceptions import ProviderError
from services.rerank_provider.interface import RerankProvider

logger = logging.getLogger(__name__)


def get_rerank_provider() -> RerankProvider:
    """Return the RerankProvider configured via RERANK_PROVIDER.

    Default: local (deterministic lexical stand-in, no key, no network).  Each production
    adapter's deps are imported lazily INSIDE its branch, so the default / CI path never
    loads them.
    """
    from services.rerank_provider.local import LocalRerankProvider

    settings = get_settings()
    mode = settings.RERANK_PROVIDER

    if mode == "local":
        logger.debug("Rerank provider: local (deterministic lexical stand-in).")
        return LocalRerankProvider()
    if mode == "huggingface":
        from services.rerank_provider.huggingface import HFRerankProvider

        logger.debug("Rerank provider: huggingface (hosted bge-reranker-v2-m3 cross-encoder).")
        return HFRerankProvider()
    if mode == "cohere":
        from services.rerank_provider.cohere import CohereRerankProvider

        logger.debug("Rerank provider: cohere (hosted cross-encoder; swap-target skeleton).")
        return CohereRerankProvider()
    if mode == "novita":
        from services.rerank_provider.novita import NovitaRerankProvider

        logger.debug("Rerank provider: novita (hosted baai/bge-reranker-v2-m3 cross-encoder).")
        return NovitaRerankProvider()
    raise ProviderError(f"Unknown RERANK_PROVIDER: {mode!r}")
