"""StorageProvider interface and data types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PresignedUrl:
    """A scoped, time-bounded presigned URL (NFR-SEC4).

    url: the presigned URL string (not public; expires after expires_in seconds)
    expires_in: TTL in seconds
    """

    url: str
    expires_in: int


class StorageProvider(ABC):
    """S3-compatible object storage interface.

    All presigned URLs are short-lived and scoped (NFR-SEC4).
    The database stores object keys (references), never the binary data or public URLs.
    """

    @abstractmethod
    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Upload data to the store under the given key."""

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Download and return the object bytes for the given key."""

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Delete the object at the given key."""

    @abstractmethod
    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        """Generate a scoped, time-bounded presigned URL for GET access.

        expires_in: URL TTL in seconds (default matches STORAGE_PRESIGNED_URL_EXPIRY_SECONDS).
        The URL must expire and must not be a permanent public URL (NFR-SEC4).
        """
