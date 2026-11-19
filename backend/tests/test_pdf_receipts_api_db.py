"""Phase 12 follow-up: PDF receipts through POST /receipts — DB.

End to end through the real route: a PDF upload creates ONE receipt whose
stored object is the PNG rendered from it (key ``<org>/<uuid>-from-pdf.png``);
no stored byte is PDF; OCR is sent the PNG; the response says "from PDF" with
the page counts and the sanitised file name (display only, never stored); the
audit row records the page counts but not the name; quotas count the PNG; a
password-protected or broken PDF is a 422 and nothing is stored, OCR'd or
written; a renderer crash is a 422, never a 500.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session

from backend.app.modules.receipt import pdf_raster as pr
from backend.app.modules.receipt import upload_guard as g
from backend.app.modules.receipt.quota import receipt_usage
from backend.tests import pdf_fixtures as f
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.test_ps_upload_api_db import (  # fixtures + helpers, no tests
    _client,
    _MemStorage,
    _Ocr,
    _post,
    _receipt_count,
    employee,
    providers,
)
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.receipt import Receipt
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrResult
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.factory import get_storage_provider

pytestmark = pytest.mark.db

API = "/api/v1"
PNG_SIG = b"\x89PNG\r\n\x1a\n"

__all__ = ["employee", "providers"]  # re-exported pytest fixtures


class _RecordingOcr(_Ocr):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[bytes] = []

    def extract(self, image: bytes | str) -> OcrResult:
        if isinstance(image, bytes):
            self.images.append(image)
        return super().extract(image)


@pytest.fixture()
def recording(app: Any) -> tuple[_MemStorage, _RecordingOcr]:
    mem, ocr = _MemStorage(), _RecordingOcr()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: ocr
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    return mem, ocr


def _assert_no_pdf_stored(mem: _MemStorage) -> None:
    for key, data in mem.objects.items():
        assert not key.lower().endswith(".pdf")
        assert data.startswith(PNG_SIG) or data[:3] == b"\xff\xd8\xff"
        assert b"%PDF" not in data, key


def _audit(db: Session, receipt_id: uuid.UUID) -> AuditLog:
    row = db.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == receipt_id, AuditLog.action == "receipt.processed"
        )
    )
    assert row is not None
    return row


def test_three_page_pdf_creates_one_receipt_backed_only_by_a_png(
    app: Any, db_session: Session, employee: User, recording: Any
) -> None:
    mem, ocr = recording
    client = _client(app, employee)
    r = _post(client, "Hotel folio (May).pdf", f.text_pdf(3), "application/pdf")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_format"] == "pdf"
    assert body["source_filename"] == "Hotel folio (May).pdf"
    assert (body["pdf_pages_total"], body["pdf_pages_used"]) == (3, 3)
    assert _receipt_count(db_session, employee) == 1

    (key,) = mem.objects
    org, name = key.split("/")
    assert org == str(employee.organization_id)
    assert name.endswith("-from-pdf.png")
    assert uuid.UUID(name.removesuffix("-from-pdf.png"))
    assert mem.types[key] == "image/png"
    _assert_no_pdf_stored(mem)
    stored = mem.objects[key]
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "PNG" and img.height > img.width  # three pages stacked

    # OCR saw exactly the stored PNG, never the PDF.
    assert ocr.images == [stored]

    receipt = db_session.get(Receipt, uuid.UUID(body["id"]))
    assert receipt is not None and receipt.image_url == key
    audit = _audit(db_session, receipt.id)
    assert audit.after_state == {
        "stored_bytes": len(stored),
        "content_type": "image/png",
        "source_format": "pdf",
        "pdf_pages_total": 3,
        "pdf_pages_used": 3,
    }
    assert "Hotel" not in str(audit.after_state)

    # Later reads still say "from PDF" (derived from the key); the transient
    # name and page counts are not stored.
    got = client.get(f"{API}/receipts/{body['id']}")
    assert got.status_code == 200
    assert got.json()["source_format"] == "pdf"
    assert got.json()["source_filename"] is None
    assert got.json()["pdf_pages_total"] is None


def test_long_pdf_uses_the_first_pages_and_says_so(
    app: Any, db_session: Session, employee: User, recording: Any
) -> None:
    r = _post(_client(app, employee), "statement.pdf", f.text_pdf(12), "application/pdf")
    assert r.status_code == 201, r.text
    assert (r.json()["pdf_pages_total"], r.json()["pdf_pages_used"]) == (12, 3)


def test_image_upload_is_unchanged(
    app: Any, db_session: Session, employee: User, recording: Any
) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (9, 9, 9)).save(buf, "PNG")
    r = _post(_client(app, employee), "a.png", buf.getvalue(), "image/png")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_format"] == "image"
    assert body["source_filename"] is None and body["pdf_pages_total"] is None
    mem, _ocr = recording
    (key,) = mem.objects
    assert "-from-pdf" not in key
    receipt = db_session.get(Receipt, uuid.UUID(body["id"]))
    assert receipt is not None
    assert set(_audit(db_session, receipt.id).after_state or {}) == {
        "stored_bytes",
        "content_type",
    }


@pytest.mark.parametrize(
    "make",
    [f.js_openaction_pdf, f.launch_pdf, f.embedded_file_pdf, f.xfa_pdf],
    ids=["javascript", "launch", "embedded-file", "xfa"],
)
def test_active_content_pdf_is_stored_as_inert_pixels(
    app: Any, db_session: Session, employee: User, recording: Any, make: Any
) -> None:
    r = _post(_client(app, employee), "evil.pdf", make(), "application/pdf")
    assert r.status_code == 201, r.text
    mem, _ocr = recording
    _assert_no_pdf_stored(mem)
    for marker in (f.MARK_JS, f.MARK_LAUNCH, f.MARK_EMBEDDED, f.MARK_XFA):
        assert all(marker not in data for data in mem.objects.values())


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (f.encrypted_pdf(), g.PDF_ENCRYPTED_MESSAGE),
        (f.text_pdf(1)[:200], g.PDF_UNREADABLE_MESSAGE),
        (f.text_pdf(mediabox=(0, 0, 0, 0)), g.PDF_NO_PAGES_MESSAGE),
    ],
    ids=["encrypted", "truncated", "no-pages"],
)
def test_unreadable_pdfs_are_422_and_nothing_happens(
    app: Any, db_session: Session, employee: User, recording: Any, data: bytes, message: str
) -> None:
    r = _post(_client(app, employee), "r.pdf", data, "application/pdf")
    assert_error_envelope(r, 422)
    assert r.json()["error"]["code"] == "INVALID_UPLOAD"
    assert r.json()["error"]["message"] == message
    mem, ocr = recording
    assert mem.objects == {} and ocr.calls == 0
    assert _receipt_count(db_session, employee) == 0


def test_huge_page_pdf_is_422(
    app: Any, db_session: Session, employee: User, recording: Any
) -> None:
    r = _post(_client(app, employee), "poster.pdf", f.text_pdf(mediabox=(0, 0, 9000, 9000)), "")
    assert_error_envelope(r, 422)
    assert "too large to be a receipt" in r.json()["error"]["message"]
    assert recording[0].objects == {}


def test_renderer_crash_is_422_never_500(
    app: Any,
    db_session: Session,
    employee: User,
    recording: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*_a: Any, **_k: Any) -> Any:
        raise pr.ChildFailedError("exit -11")

    monkeypatch.setattr(pr, "rasterize_pdf", crash)
    r = _post(_client(app, employee), "r.pdf", f.text_pdf(1), "application/pdf")
    assert_error_envelope(r, 422)
    assert r.json()["error"]["message"] == g.PDF_UNREADABLE_MESSAGE
    assert recording[0].objects == {}
    assert _receipt_count(db_session, employee) == 0


def test_busy_renderer_is_a_retryable_503_and_nothing_happens(
    app: Any,
    db_session: Session,
    employee: User,
    recording: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: a request that cannot get the render slot within the queue
    wait is refused at once (503 + Retry-After), not queued and not a 422."""
    monkeypatch.setattr(get_settings(), "UPLOAD_PDF_QUEUE_WAIT_SECONDS", 0.1)
    assert pr._RENDER_SLOTS.acquire(blocking=False)
    try:
        r = _post(_client(app, employee), "r.pdf", f.text_pdf(1), "application/pdf")
    finally:
        pr._RENDER_SLOTS.release()
    assert r.status_code == 503, r.text[:300]
    assert_error_envelope(r)
    assert r.json()["error"]["code"] == "PDF_RENDERER_BUSY"
    assert r.json()["error"]["message"] == g.PDF_BUSY_MESSAGE
    assert r.headers["Retry-After"] == "10"
    mem, ocr = recording
    assert mem.objects == {} and ocr.calls == 0
    assert _receipt_count(db_session, employee) == 0


@pytest.mark.parametrize(
    ("name", "ctype"),
    [("scan.png", "image/png"), ("r.pdf", "image/jpeg"), ("r.jpg", "application/pdf")],
)
def test_pdf_with_a_lying_name_or_type_is_415(
    app: Any, db_session: Session, employee: User, recording: Any, name: str, ctype: str
) -> None:
    r = _post(_client(app, employee), name, f.text_pdf(1), ctype)
    assert_error_envelope(r, 415)
    assert recording[0].objects == {}


def test_file_name_is_sanitised_and_never_reaches_the_key(
    app: Any, db_session: Session, employee: User, recording: Any
) -> None:
    name = "../../etc/réçu‮.pdf"
    r = _post(_client(app, employee), name, f.text_pdf(1), "application/pdf")
    assert r.status_code == 201, r.text
    shown = r.json()["source_filename"]
    assert shown == g.safe_filename(name)
    assert "/" not in shown and ".." not in shown and "‮" not in shown
    (key,) = recording[0].objects
    org, name_part = key.split("/")
    assert org == str(employee.organization_id)
    assert uuid.UUID(name_part.removesuffix("-from-pdf.png"))  # nothing of the client name


def test_pdf_counts_as_one_receipt_and_its_png_bytes_against_the_quota(
    app: Any,
    db_session: Session,
    employee: User,
    recording: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPLOAD_QUOTA_USER_FILES", 1)
    client = _client(app, employee)
    first = _post(client, "a.pdf", f.text_pdf(3), "application/pdf")
    assert first.status_code == 201, first.text
    mem, _ocr = recording
    (stored,) = mem.objects.values()
    usage = receipt_usage(
        db_session, organization_id=employee.organization_id, uploaded_by=employee.id
    )
    assert usage.files == 1 and usage.bytes == len(stored)

    second = _post(client, "b.pdf", f.text_pdf(1), "application/pdf")
    assert_error_envelope(second, 409)
    assert second.json()["error"]["code"] == "UPLOAD_QUOTA_EXCEEDED"
    assert len(mem.objects) == 1


def test_pdf_runs_the_normal_ocr_pipeline_with_the_local_provider(
    app: Any, db_session: Session, employee: User
) -> None:
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: LocalOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    r = _post(_client(app, employee), "receipt.pdf", f.pillow_pdf(1), "application/pdf")
    assert r.status_code == 201, r.text
    assert r.json()["ocr_status"] == "processed"
    _assert_no_pdf_stored(mem)
