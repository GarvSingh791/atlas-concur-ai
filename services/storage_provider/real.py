"""Real (S3-compatible / MinIO) storage adapter — lazily initializes on first call."""

from __future__ import annotations

import logging
from typing import Any

from config.settings import get_settings
from services.exceptions import ProviderConfigurationError, ProviderError, ProviderUnavailableError
from services.storage_provider.interface import PresignedUrl, StorageProvider

logger = logging.getLogger(__name__)


# Phase 11: objects stored by the receipt upload carry the SNIFFED type as the
# key's extension (``<org>/<uuid>.png``).  Presigned links pin the response
# Content-Type to it and serve the object inline under a server-chosen name, so
# the browser never trusts a client-declared type or file name.  Keys without a
# known extension (pre-phase-11 uploads, seeds) keep the stored object's type.
_SERVED_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def served_headers_for_key(key: str) -> dict[str, str]:
    """``ResponseContentType`` / ``ResponseContentDisposition`` for *key*, or {}."""
    name = key.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    content_type = _SERVED_TYPES.get(ext)
    if content_type is None:
        return {}
    return {
        "ResponseContentType": content_type,
        "ResponseContentDisposition": f'inline; filename="receipt.{ext}"',
    }


class RealStorageProvider(StorageProvider):
    """S3-compatible storage adapter (MinIO / AWS S3). Lazily creates the boto3 client."""

    def _get_client(self, *, endpoint_url: str | None = None) -> Any:
        """The boto3 S3 client; *endpoint_url* overrides STORAGE_ENDPOINT_URL.

        Only presigning passes an override (STORAGE_PUBLIC_ENDPOINT_URL): SigV4
        signs the Host header, so a link the BROWSER opens must be signed for the
        host the browser can reach.  Server-side put/get/delete keep the internal
        endpoint.
        """
        try:
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:
            raise ProviderUnavailableError("boto3 not installed; run: pip install boto3") from exc

        settings = get_settings()
        if not settings.STORAGE_SECRET_ACCESS_KEY:
            raise ProviderConfigurationError(
                "STORAGE_SECRET_ACCESS_KEY is required when STORAGE_PROVIDER=minio"
            )
        # Never log the secret key
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url or settings.STORAGE_ENDPOINT_URL,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
            aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
            region_name=settings.STORAGE_REGION,
            config=Config(
                s3={"addressing_style": "path"} if settings.STORAGE_FORCE_PATH_STYLE else {},
                # boto3>=1.36 sends CRC32 checksums by default, which some S3-compatible
                # stores (Cloudflare R2, older MinIO) reject. Only checksum when required.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                # s3v4 is required for presigned URLs against R2 — used by
                # GET /receipts/{id} for NFR-SEC4 short-lived receipt-image links.
                signature_version="s3v4",
            ),
        )

    def _bucket(self) -> str:
        return get_settings().STORAGE_BUCKET

    def _ensure_bucket(self, client: Any) -> None:
        """Idempotently ensure the target bucket exists (head -> create on 404).

        A deployed MinIO/S3 may not be pre-provisioned. Without this, the first
        upload fails with NoSuchBucket and surfaces as an opaque 500. Safe to call
        before every put; tolerant of a concurrent creator.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        bucket = self._bucket()
        try:
            client.head_bucket(Bucket=bucket)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            missing = status in (404, 301) or code in ("404", "NoSuchBucket", "NotFound")
            if not missing:
                raise ProviderError(f"Failed to access bucket {bucket!r}") from exc
        try:
            client.create_bucket(Bucket=bucket)
            logger.info("RealStorageProvider: created missing bucket %s", bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
            ):
                return
            raise ProviderError(f"Failed to create bucket {bucket!r}") from exc

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        client = self._get_client()
        try:
            self._ensure_bucket(client)
            client.put_object(Bucket=self._bucket(), Key=key, Body=data, ContentType=content_type)
        except ProviderError:
            raise
        except Exception as exc:  # translate boto3/botocore errors to a clean ProviderError
            raise ProviderError(f"Failed to put object: {key!r}") from exc
        logger.debug(
            "RealStorageProvider: put s3://%s/%s (%d bytes)", self._bucket(), key, len(data)
        )

    def get_object(self, key: str) -> bytes:
        try:
            client = self._get_client()
            response = client.get_object(Bucket=self._bucket(), Key=key)
            return response["Body"].read()
        except Exception as exc:
            # Translate S3 errors; never leak key details in message
            raise ProviderError(f"Failed to get object: {key!r}") from exc

    def delete_object(self, key: str) -> None:
        client = self._get_client()
        client.delete_object(Bucket=self._bucket(), Key=key)
        logger.debug("RealStorageProvider: deleted s3://%s/%s", self._bucket(), key)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        # Local docker (product gap 8): the API reaches MinIO at http://minio:9000,
        # a hostname the browser cannot resolve.  With STORAGE_PUBLIC_ENDPOINT_URL
        # set, the link is signed by a second client for that public endpoint.
        # Unset (staging R2) -> the internal endpoint, exactly as before.
        public = get_settings().STORAGE_PUBLIC_ENDPOINT_URL.strip()
        client = self._get_client(endpoint_url=public or None)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket(), "Key": key, **served_headers_for_key(key)},
            ExpiresIn=expires_in,
        )
        logger.debug("RealStorageProvider: presigned %s (expires_in=%ds)", key, expires_in)
        return PresignedUrl(url=url, expires_in=expires_in)
