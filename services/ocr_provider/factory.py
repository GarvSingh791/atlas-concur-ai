"""Factory: returns the configured OcrProvider based on Settings."""

from __future__ import annotations

import logging

from config.settings import get_settings
from services.exceptions import ProviderError
from services.ocr_provider.interface import OcrProvider

logger = logging.getLogger(__name__)


def get_ocr_provider() -> OcrProvider:
    """Return the OcrProvider configured via OCR_PROVIDER setting.

    Default: local (deterministic canned extraction, no key required);
    openrouter selects real vision OCR.  Settings validation rejects any other
    value (including the removed ``anthropic`` stub) before this runs.
    """
    from services.ocr_provider.local import LocalOcrProvider
    from services.ocr_provider.openrouter import OpenRouterVisionOcrProvider

    settings = get_settings()
    mode = settings.OCR_PROVIDER

    if mode == "local":
        logger.debug("OCR provider: local (deterministic canned).")
        return LocalOcrProvider()
    if mode == "openrouter":
        logger.debug("OCR provider: openrouter (Gemini vision).")
        return OpenRouterVisionOcrProvider()
    raise ProviderError(f"Unknown OCR_PROVIDER: {mode!r}")
