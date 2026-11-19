"""Factory: returns the configured StorageProvider based on Settings."""

from __future__ import annotations

import logging

from config.settings import get_settings
from services.exceptions import ProviderError
from services.storage_provider.interface import StorageProvider

logger = logging.getLogger(__name__)


def get_storage_provider() -> StorageProvider:
    """Return the StorageProvider configured via STORAGE_PROVIDER setting.

    Default: local (filesystem-backed, no credentials required).
    """
    from services.storage_provider.local import LocalStorageProvider
    from services.storage_provider.real import RealStorageProvider

    settings = get_settings()
    mode = settings.STORAGE_PROVIDER

    if mode == "local":
        logger.debug("Storage provider: local (filesystem).")
        return LocalStorageProvider()
    if mode == "minio":
        logger.debug("Storage provider: minio (real S3-compatible).")
        return RealStorageProvider()
    raise ProviderError(f"Unknown STORAGE_PROVIDER: {mode!r}")
