"""Product gap 8 (fxcard track): receipt previews in local docker.

In docker-compose the API reaches MinIO at ``http://minio:9000`` — a hostname the
browser cannot resolve — so a presigned link signed for it never loads.  With
``STORAGE_PUBLIC_ENDPOINT_URL`` set, ONLY presigning uses a client for that public
endpoint (SigV4 signs the host, so the link must be signed for the host the
browser opens); server-side put/get/delete stay on the internal endpoint.  Unset
(staging R2) -> exactly today's behaviour.

Hermetic: real boto3 signs locally (no network, no MinIO needed); the put test
stubs the client.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import config.settings as cs
from services.storage_provider.real import RealStorageProvider

pytest.importorskip("boto3")


@pytest.fixture()
def storage_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("STORAGE_PROVIDER", "minio")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("STORAGE_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "test-secret-key-123")
    monkeypatch.setenv("STORAGE_BUCKET", "atlas-receipts-dev")
    monkeypatch.setenv("STORAGE_FORCE_PATH_STYLE", "true")
    monkeypatch.delenv("STORAGE_PUBLIC_ENDPOINT_URL", raising=False)
    cs.get_settings.cache_clear()
    yield monkeypatch
    cs.get_settings.cache_clear()


def _presign(key: str = "receipts/org/abc.png") -> str:
    cs.get_settings.cache_clear()
    return RealStorageProvider().generate_presigned_url(key, expires_in=60).url


def test_unset_public_endpoint_signs_for_the_internal_endpoint(storage_env: Any) -> None:
    url = urlparse(_presign())
    assert url.scheme == "http"
    assert url.netloc == "minio:9000"
    assert url.path == "/atlas-receipts-dev/receipts/org/abc.png"
    assert parse_qs(url.query)["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_blank_public_endpoint_is_treated_as_unset(storage_env: pytest.MonkeyPatch) -> None:
    storage_env.setenv("STORAGE_PUBLIC_ENDPOINT_URL", "   ")
    assert urlparse(_presign()).netloc == "minio:9000"


def test_public_endpoint_signs_for_the_browser_host(storage_env: pytest.MonkeyPatch) -> None:
    storage_env.setenv("STORAGE_PUBLIC_ENDPOINT_URL", "http://localhost:9000")
    url = urlparse(_presign())
    assert url.netloc == "localhost:9000"
    assert url.path == "/atlas-receipts-dev/receipts/org/abc.png"
    query = parse_qs(url.query)
    # SigV4: the signature covers the host header only.
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Expires"] == ["60"]


def test_server_side_put_keeps_the_internal_endpoint(storage_env: pytest.MonkeyPatch) -> None:
    storage_env.setenv("STORAGE_PUBLIC_ENDPOINT_URL", "http://localhost:9000")
    cs.get_settings.cache_clear()
    seen: list[str | None] = []

    class _Client:
        def head_bucket(self, **_kw: Any) -> None:
            return None

        def put_object(self, **_kw: Any) -> None:
            return None

    provider = RealStorageProvider()

    def _fake_get_client(*, endpoint_url: str | None = None) -> Any:
        seen.append(endpoint_url)
        return _Client()

    storage_env.setattr(provider, "_get_client", _fake_get_client)
    provider.put_object("receipts/x.png", b"img", content_type="image/png")
    assert seen == [None]  # None -> STORAGE_ENDPOINT_URL (the internal host)


def test_get_client_override_reaches_boto3(storage_env: pytest.MonkeyPatch) -> None:
    client = RealStorageProvider()._get_client(endpoint_url="http://localhost:9000")
    assert client.meta.endpoint_url == "http://localhost:9000"
    assert RealStorageProvider()._get_client().meta.endpoint_url == "http://minio:9000"
