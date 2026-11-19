"""Factory: returns the configured LlmProvider based on Settings.LLM_PROVIDER."""

from __future__ import annotations

import logging

from config.settings import get_settings
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmProvider

logger = logging.getLogger(__name__)


def get_llm_provider() -> LlmProvider:
    """Return the LlmProvider configured via LLM_PROVIDER setting.

    Default: local (deterministic stub, no key required).
    """
    from services.llm_provider.local import LocalLlmProvider
    from services.llm_provider.real import RealLlmProvider

    settings = get_settings()
    mode = settings.LLM_PROVIDER

    if mode == "local":
        logger.debug("LLM provider: local (deterministic stub).")
        return LocalLlmProvider()
    if mode == "anthropic":
        logger.debug("LLM provider: anthropic (real).")
        return RealLlmProvider()
    if mode == "openrouter":
        from services.llm_provider.openrouter import OpenRouterLlmProvider

        logger.debug("LLM provider: openrouter (Gemini via OpenRouter).")
        return OpenRouterLlmProvider()
    raise ProviderError(f"Unknown LLM_PROVIDER: {mode!r}")
