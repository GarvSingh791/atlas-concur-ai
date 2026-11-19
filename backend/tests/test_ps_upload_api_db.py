"""Phase 11 (public-demo security): POST /receipts size, type and quota — DB.

Through the real route: an oversized, mistyped or corrupt upload is refused
before anything is stored, OCR'd or written; what IS stored is the sanitised
image under a server-generated key with the sniffed content type; the storage
quota counts the user's (and the organisation's) receipts and is freed by a
delete; every response carries ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.app.core.body_limit import MULTIPART_OVERHEAD_BYTES
from backend.app.core.security import create_access_token
from backend.app.modules.receipt.quota import receipt_usage
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.receipt_images import scenario_png
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.receipt import Receipt
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

API = "/api/v1"


class _Ocr(OcrProvider):
    """Reads nothing (no draft entry), and records what it was sent."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, image: bytes | str) -> OcrResult:
        self.calls += 1
        return OcrResult(fields={}, field_confidence={})


class _MemStorage(StorageProvider):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}

    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.objects[key] = data
        self.types[key] = content_type

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"https://example.invalid/{key}", expires_in=expires_in)


@pytest.fixture()
def providers(app: Any) -> Iterator[tuple[_MemStorage, _Ocr]]:
    mem, ocr = _MemStorage(), _Ocr()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: ocr
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    yield mem, ocr


def _client(app: Any, user: User) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    token = create_access_token(user.id, user.organization_id)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _jpeg_with_gps() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "PhoneMaker"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 44.0, 54.36)
    buf = io.BytesIO()
    Image.new("RGB", (48, 24), (120, 120, 120)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _a_real_pdf() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 200, 200)).save(buf, "PDF")
    return buf.getvalue()


def _post(client: TestClient, name: str, data: bytes, ctype: str) -> Any:
    return client.post(f"{API}/receipts", files={"file": (name, io.BytesIO(data), ctype)})


def _receipt_count(db: Session, user: User) -> int:
    return int(
        db.scalar(sa.select(sa.func.count(Receipt.id)).where(Receipt.uploaded_by == user.id)) or 0
    )


@pytest.fixture()
def employee(app: Any, rbac_catalog: None, users_a: dict[str, User]) -> User:
    return users_a["employee"]


# ---------------------------------------------------------------------------
# Refusals: nothing stored, nothing OCR'd, nothing written
# ---------------------------------------------------------------------------


def _assert_nothing_happened(db: Session, user: User, providers: tuple[_MemStorage, _Ocr]) -> None:
    mem, ocr = providers
    assert mem.objects == {}
    assert ocr.calls == 0
    assert _receipt_count(db, user) == 0


def test_oversize_by_content_length_is_413_and_nothing_is_stored(
    app: Any, db_session: Session, employee: User, providers: Any
) -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (get_settings().MAX_UPLOAD_BYTES + 1)
    r = _post(_client(app, employee), "big.png", big, "image/png")
    assert_error_envelope(r, 413)
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    _assert_nothing_happened(db_session, employee, providers)


def test_oversize_streamed_without_content_length_is_413(
    app: Any, db_session: Session, employee: User, providers: Any
) -> None:
    limit = get_settings().MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES

    def body() -> Iterator[bytes]:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
        yield b"Content-Type: image/png\r\n\r\n"
        sent = 0
        while sent <= limit:
            yield b"\x00" * 65_536
            sent += 65_536
        yield b"\r\n--b--\r\n"

    r = _client(app, employee).post(
        f"{API}/receipts",
        content=body(),
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert_error_envelope(r, 413)
    _assert_nothing_happened(db_session, employee, providers)


def test_file_just_over_the_upload_limit_is_413_even_within_multipart_allowance(
    app: Any, db_session: Session, employee: User, providers: Any
) -> None:
    """The middleware allows multipart overhead; the route re-checks the FILE exactly."""
    data = scenario_png("x") + b"\x00" * (
        get_settings().MAX_UPLOAD_BYTES + 1 - len(scenario_png("x"))
    )
    r = _post(_client(app, employee), "a.png", data, "image/png")
    assert_error_envelope(r, 413)
    _assert_nothing_happened(db_session, employee, providers)


@pytest.mark.parametrize(
    ("name", "data", "ctype", "status", "code"),
    [
        ("receipt.pdf", scenario_png("x"), "application/pdf", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ("scan.png", _a_real_pdf(), "image/png", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ("receipt.pdf", b"%PDF-1.7 broken", "application/pdf", 422, "INVALID_UPLOAD"),
        ("receipt.jpg", b"this is a text file", "image/jpeg", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ("receipt.png", b"<svg onload=alert(1)>", "image/png", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ("receipt.png", b"", "image/png", 422, "INVALID_UPLOAD"),
        ("receipt.png", scenario_png("truncated")[:30], "image/png", 422, "INVALID_UPLOAD"),
    ],
)
def test_bad_files_are_refused_before_storage(
    app: Any,
    db_session: Session,
    employee: User,
    providers: Any,
    name: str,
    data: bytes,
    ctype: str,
    status: int,
    code: str,
) -> None:
    r = _post(_client(app, employee), name, data, ctype)
    assert_error_envelope(r, status)
    assert r.json()["error"]["code"] == code
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    _assert_nothing_happened(db_session, employee, providers)


# Phase 12: PDF receipts are rasterised (honest PDFs are 201 now); see
# test_pdf_receipts_api_db.py.  A PDF whose name / type lies is still a 415
# and a damaged one a 422 (rows above).


# ---------------------------------------------------------------------------
# What is stored
# ---------------------------------------------------------------------------


def test_stored_object_is_sanitised_with_sniffed_type_and_server_key(
    app: Any, db_session: Session, employee: User, providers: Any
) -> None:
    mem, ocr = providers
    # A JPEG the client calls "application/octet-stream", named with a traversal path.
    r = _post(
        _client(app, employee),
        "../../../etc/passwd.jpg",
        _jpeg_with_gps(),
        "application/octet-stream",
    )
    assert r.status_code == 201, r.text
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert ocr.calls == 1

    (key,) = mem.objects
    org, name = key.split("/")
    assert org == str(employee.organization_id)
    assert name.endswith(".jpg") and uuid.UUID(name.removesuffix(".jpg"))
    assert "passwd" not in key and ".." not in key
    assert mem.types[key] == "image/jpeg"  # sniffed, not the claimed octet-stream

    stored = mem.objects[key]
    exif = Image.open(io.BytesIO(stored)).getexif()
    assert dict(exif) == {0x0112: 6}, "only the orientation may survive"
    assert not exif.get_ifd(0x8825), "GPS must be stripped before storage"

    receipt = db_session.get(Receipt, uuid.UUID(r.json()["id"]))
    assert receipt is not None and receipt.image_url == key
    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == receipt.id, AuditLog.action == "receipt.processed"
        )
    )
    assert audit is not None
    assert audit.after_state == {"stored_bytes": len(stored), "content_type": "image/jpeg"}


def test_get_receipt_carries_nosniff(
    app: Any, db_session: Session, employee: User, providers: Any
) -> None:
    client = _client(app, employee)
    r = _post(client, "a.png", scenario_png("x"), "image/png")
    assert r.status_code == 201, r.text
    g = client.get(f"{API}/receipts/{r.json()['id']}")
    assert g.status_code == 200
    assert g.headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


def test_user_file_quota_is_enforced_and_freed_by_delete(
    app: Any,
    db_session: Session,
    employee: User,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPLOAD_QUOTA_USER_FILES", 2)
    client = _client(app, employee)
    first = _post(client, "a.png", scenario_png("a"), "image/png")
    assert first.status_code == 201, first.text
    assert _post(client, "b.png", scenario_png("b"), "image/png").status_code == 201

    third = _post(client, "c.png", scenario_png("c"), "image/png")
    assert_error_envelope(third, 409)
    assert third.json()["error"]["code"] == "UPLOAD_QUOTA_EXCEEDED"
    mem, _ocr = providers
    assert len(mem.objects) == 2  # the refused file was never stored

    d = client.delete(f"{API}/receipts/{first.json()['id']}")
    assert d.status_code == 200, d.text
    again = _post(client, "c.png", scenario_png("c"), "image/png")
    assert again.status_code == 201, again.text


def test_user_byte_quota_counts_stored_bytes(
    app: Any,
    db_session: Session,
    employee: User,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one = len(scenario_png("a" * 40))
    monkeypatch.setattr(get_settings(), "UPLOAD_QUOTA_USER_BYTES", one * 2)
    client = _client(app, employee)
    for tag in ("a" * 40, "b" * 40):
        assert _post(client, "x.png", scenario_png(tag), "image/png").status_code == 201
    usage = receipt_usage(
        db_session, organization_id=employee.organization_id, uploaded_by=employee.id
    )
    assert usage.files == 2 and usage.bytes == one * 2

    r = _post(client, "x.png", scenario_png("c"), "image/png")
    assert_error_envelope(r, 409)
    assert r.json()["error"]["code"] == "UPLOAD_QUOTA_EXCEEDED"
    assert "storage limit" in r.json()["error"]["message"]


def test_org_quota_applies_across_users(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    rbac_catalog: None,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPLOAD_QUOTA_ORG_FILES", 1)
    first = _post(_client(app, users_a["employee"]), "a.png", scenario_png("a"), "image/png")
    assert first.status_code == 201, first.text
    second = _post(_client(app, users_a["manager"]), "b.png", scenario_png("b"), "image/png")
    assert_error_envelope(second, 409)
    assert "organisation" in second.json()["error"]["message"]


def test_quota_is_per_user_and_per_tenant(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    users_b: dict[str, User],
    rbac_catalog: None,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPLOAD_QUOTA_USER_FILES", 1)
    for user in (users_a["employee"], users_a["manager"], users_b["employee"]):
        r = _post(_client(app, user), "a.png", scenario_png("a"), "image/png")
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# The CI E2E receipt fixture still steers the local OCR stub after sanitising
# ---------------------------------------------------------------------------

_E2E_FIXTURE = (
    Path(__file__).resolve().parents[2] / "frontend" / "e2e" / "fixtures" / "receipt-meal-42_50.png"
)


def test_e2e_meal_fixture_extracts_42_50_with_the_local_ocr_stub(
    app: Any, db_session: Session, employee: User
) -> None:
    """employee-flow.spec.ts uploads this fixture with OCR_PROVIDER=local and
    expects "$42.50".  Its "meal" hint must survive the PNG sanitiser (which
    drops tEXt chunks), so it lives in the pixel data, in a stored deflate block.
    """
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: LocalOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    r = _post(
        _client(app, employee),
        "e2e-receipt.png",
        _E2E_FIXTURE.read_bytes(),
        "image/png",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ocr_status"] == "processed"
    assert body["extracted_fields"]["amount"] == 42.5
    assert body["extracted_fields"]["vendor"] == "The Capital Grille"


@pytest.mark.parametrize(
    ("setting", "value_of"),
    [
        ("UPLOAD_QUOTA_ORG_FILES", lambda one: 1),
        ("UPLOAD_QUOTA_ORG_BYTES", lambda one: one),
    ],
)
def test_org_quota_counts_only_its_own_tenant(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    users_b: dict[str, User],
    rbac_catalog: None,
    providers: Any,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value_of: Any,
) -> None:
    """Review fix: another tenant's receipts never count toward an org quota."""
    png = scenario_png("q" * 20)
    monkeypatch.setattr(get_settings(), setting, value_of(len(png)))
    # Org B fills its own quota exactly.
    assert _post(_client(app, users_b["employee"]), "b.png", png, "image/png").status_code == 201
    # Org A is still empty: its first upload fits, whatever org B holds.
    a1 = _post(_client(app, users_a["employee"]), "a.png", png, "image/png")
    assert a1.status_code == 201, a1.text
    # Now org A is exactly at its cap: its next upload (another user) is refused.
    a2 = _post(_client(app, users_a["manager"]), "a.png", png, "image/png")
    assert_error_envelope(a2, 409)
    assert a2.json()["error"]["code"] == "UPLOAD_QUOTA_EXCEEDED"
    assert "organisation" in a2.json()["error"]["message"]
    usage_a = receipt_usage(db_session, organization_id=users_a["employee"].organization_id)
    usage_b = receipt_usage(db_session, organization_id=users_b["employee"].organization_id)
    assert (usage_a.files, usage_a.bytes) == (1, len(png))
    assert (usage_b.files, usage_b.bytes) == (1, len(png))
