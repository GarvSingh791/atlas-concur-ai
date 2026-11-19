"""Local filesystem-backed storage adapter (no network, no credentials needed)."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from config.settings import get_settings
from services.exceptions import ProviderError
from services.storage_provider.interface import PresignedUrl, StorageProvider

logger = logging.getLogger(__name__)


class LocalStorageProvider(StorageProvider):
    """Filesystem-backed storage for dev/CI.

    No credentials required. Stores objects as files under base_dir/<key>.
    Presigned URLs are scoped file:// references with an ?expires= timestamp.
    """

    def _base_dir(self) -> Path:
        settings = get_settings()
        raw = settings.STORAGE_LOCAL_PATH
        base = Path(raw) if raw else Path(tempfile.gettempdir()) / "atlas-storage"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _key_path(self, key: str) -> Path:
        base = self._base_dir().resolve()
        p = (base / key).resolve()
        # Path.relative_to() is the correct traversal guard — str.startswith() is
        # exploitable when base=/tmp/atlas-storage and key resolves to /tmp/atlas-storage-evil/...
        try:
            p.relative_to(base)
        except ValueError as exc:
            raise ProviderError(f"Key {key!r} would escape the storage base directory.") from exc
        return p

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        path = self._key_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.debug("LocalStorageProvider: put %s (%d bytes)", key, len(data))

    def get_object(self, key: str) -> bytes:
        path = self._key_path(key)
        if not path.exists():
            raise ProviderError(f"Object not found: {key!r}")
        return path.read_bytes()

    def delete_object(self, key: str) -> None:
        path = self._key_path(key)
        if path.exists():
            path.unlink()
            logger.debug("LocalStorageProvider: deleted %s", key)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        path = self._key_path(key)
        if not path.exists():
            raise ProviderError(f"Object not found, cannot presign: {key!r}")
        expiry_ts = int(time.time()) + expires_in
        url = f"file://{path}?expires={expiry_ts}"
        logger.debug("LocalStorageProvider: presigned %s (expires_in=%ds)", key, expires_in)
        return PresignedUrl(url=url, expires_in=expires_in)
