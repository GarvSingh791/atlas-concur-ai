"""Phase 11: presigned receipt links pin the served Content-Type + disposition.

An object stored by the phase-11 upload has the sniffed type as its key's
extension; its presigned GET link overrides the response headers so the
browser gets that type and an inline, server-named disposition — never a
client-declared type or file name.  Hermetic: boto3 signs locally.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest

import config.settings as cs
from services.storage_provider.real import RealStorageProvider

pytest.importorskip("boto3")


@pytest.fixture()
def storage_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("STORAGE_PROVIDER", "minio")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("STORAGE_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "test-secret-key-123")
    monkeypatch.setenv("STORAGE_BUCKET", "atlas-receipts-dev")
    monkeypatch.delenv("STORAGE_PUBLIC_ENDPOINT_URL", raising=False)
    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _query(key: str) -> dict[str, list[str]]:
    url = RealStorageProvider().generate_presigned_url(key, expires_in=60).url
    return parse_qs(urlparse(url).query)


@pytest.mark.usefixtures("storage_env")
@pytest.mark.parametrize(
    ("key", "ctype"),
    [
        ("org/0b1c.png", "image/png"),
        ("org/0b1c.jpg", "image/jpeg"),
        ("org/0b1c.webp", "image/webp"),
    ],
)
def test_presigned_link_pins_type_and_inline_disposition(key: str, ctype: str) -> None:
    q = _query(key)
    assert q["response-content-type"] == [ctype]
    ext = key.rsplit(".", 1)[1]
    assert q["response-content-disposition"] == [f'inline; filename="receipt.{ext}"']


@pytest.mark.usefixtures("storage_env")
def test_pdf_key_is_not_pinned_to_a_pdf_type() -> None:
    """No key is ever served as a PDF (phase 12 stores only the rendered PNG)."""
    q = _query("org/0b1c.pdf")
    assert "response-content-type" not in q


@pytest.mark.usefixtures("storage_env")
def test_png_rendered_from_a_pdf_is_served_as_png() -> None:
    """Phase 12: ``<uuid>-from-pdf.png`` is a PNG like any other upload."""
    q = _query("org/0b1c-from-pdf.png")
    assert q["response-content-type"] == ["image/png"]
    assert q["response-content-disposition"] == ['inline; filename="receipt.png"']


@pytest.mark.usefixtures("storage_env")
def test_legacy_key_without_extension_is_unchanged() -> None:
    q = _query("org/0b1c-legacy")
    assert "response-content-type" not in q
    assert "response-content-disposition" not in q
