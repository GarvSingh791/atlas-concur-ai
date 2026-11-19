"""C2 — receipt image serving via short-lived signed links (NFR-SEC4).

``GET /receipts/{id}`` carries ``image_access_url`` + ``image_access_expires_at``
minted ONLY after RBAC (``expense.read.own``) + record scoping on
``Receipt.uploaded_by``:

  * owner, owner's direct manager, finance_admin -> 200 with a signed link;
  * any other same-org user, or a cross-tenant user -> 404 (no existence leak)
    and the storage provider is never asked to sign;
  * the link TTL is hard-capped at 300 s regardless of settings;
  * ``file://`` (LocalStorageProvider) links are suppressed -> null;
  * a ProviderError degrades to a null link (still 200);
  * the URL is never logged nor written to the audit trail;
  * POST / PATCH never carry a link.

Also covers the ``receipt_id`` reference on expense entries (owner entries view,
PATCH /expenses/{id}, and the approval detail) that lets a manager/finance
reviewer reach the image.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.app.modules.receipt import router as receipt_router
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from backend.tests.receipt_images import scenario_upload
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from services.exceptions import ProviderError
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl
from services.storage_provider.local import LocalStorageProvider

pytestmark = pytest.mark.db

_SENTINEL = "SENTINEL-SIGNATURE-c2"
_KEY = "org/abc"


class _FakeStorage(LocalStorageProvider):
    """Local storage for put/get, but presigns to a recognisable https URL.

    Records every ``(key, expires_in)`` presign call so tests can prove the
    provider is never consulted for an out-of-scope caller.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.raise_error = False
        # An exception a real provider might let escape unwrapped (e.g. botocore).
        self.raise_unwrapped: Exception | None = None
        self.scheme = "https"

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        self.calls.append((key, expires_in))
        if self.raise_unwrapped is not None:
            raise self.raise_unwrapped
        if self.raise_error:
            raise ProviderError(f"cannot presign {key!r}")
        return PresignedUrl(
            url=f"{self.scheme}://signed.test/{key}?X-Amz-Signature={_SENTINEL}",
            expires_in=expires_in,
        )


@pytest.fixture()
def fake_storage(app: Any) -> Iterator[_FakeStorage]:
    fake = _FakeStorage()
    app.dependency_overrides[get_storage_provider] = lambda: fake
    yield fake
    # app fixture clears dependency_overrides in its own teardown.


def _bearer_client(application: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(application, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _url(receipt_id: uuid.UUID) -> str:
    return f"{API}/receipts/{receipt_id}"


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (Pydantic emits a trailing ``Z``; py3.10-safe)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_signed(body: dict[str, Any], before: datetime) -> None:
    url = body["image_access_url"]
    assert isinstance(url, str) and url.startswith("https://signed.test/")
    expires_at = _parse_ts(body["image_access_expires_at"])
    assert before < expires_at <= datetime.now(timezone.utc) + timedelta(seconds=300)
    expires_in = body["image_access_expires_in"]
    assert isinstance(expires_in, int) and 1 <= expires_in <= 300


# ---------------------------------------------------------------------------
# Scope: who gets a link
# ---------------------------------------------------------------------------


def test_owner_gets_signed_link_within_ttl(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)
    before = datetime.now(timezone.utc)

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The raw object key is still exposed (backward compatible); the link is separate.
    assert body["image_url"] == _KEY
    _assert_signed(body, before)
    assert fake_storage.calls == [(_KEY, 300)]


def test_owners_direct_manager_gets_link(
    db_session: Session,
    users_a: dict[str, User],
    manager_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)
    before = datetime.now(timezone.utc)

    resp = manager_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    _assert_signed(resp.json(), before)


def test_finance_admin_gets_link(
    db_session: Session,
    users_a: dict[str, User],
    finance_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)
    before = datetime.now(timezone.utc)

    resp = finance_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    _assert_signed(resp.json(), before)


def test_other_manager_is_404_and_never_signs(
    app: Any,
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    rbac_catalog: None,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)
    other_manager = f.make_user(db_session, org=org_a, role=RoleKey.manager)

    resp = _bearer_client(app, other_manager).get(_url(receipt.id))

    assert_error_envelope(resp, 404)
    assert fake_storage.calls == []


def test_same_org_stranger_is_404_and_never_signs(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    receipt = f.make_receipt(db_session, uploaded_by=stranger, image_url=_KEY)

    resp = employee_client.get(_url(receipt.id))

    assert_error_envelope(resp, 404)
    assert fake_storage.calls == []


def test_cross_tenant_is_404_and_never_signs(
    db_session: Session,
    users_a: dict[str, User],
    employee_client_b: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    resp = employee_client_b.get(_url(receipt.id))

    assert_error_envelope(resp, 404)
    assert fake_storage.calls == []


def test_nonexistent_receipt_is_404(
    employee_client: TestClient, fake_storage: _FakeStorage
) -> None:
    assert_error_envelope(employee_client.get(_url(uuid.uuid4())), 404)
    assert fake_storage.calls == []


# ---------------------------------------------------------------------------
# Link hygiene: TTL cap, degrade paths, no leaks
# ---------------------------------------------------------------------------


def test_ttl_is_hard_capped_at_300_seconds(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        receipt_router,
        "get_settings",
        lambda: SimpleNamespace(STORAGE_PRESIGNED_URL_EXPIRY_SECONDS=3600),
    )
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)
    before = datetime.now(timezone.utc)

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert fake_storage.calls == [(_KEY, 300)]
    _assert_signed(resp.json(), before)


def test_shorter_configured_ttl_is_honoured(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        receipt_router,
        "get_settings",
        lambda: SimpleNamespace(STORAGE_PRESIGNED_URL_EXPIRY_SECONDS=60),
    )
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert fake_storage.calls == [(_KEY, 60)]
    expires_at = _parse_ts(resp.json()["image_access_expires_at"])
    assert expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60)
    assert resp.json()["image_access_expires_in"] == 60


def test_empty_key_yields_null_link_without_signing(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url="")

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_access_url"] is None
    assert resp.json()["image_access_expires_at"] is None
    assert fake_storage.calls == []


def test_provider_error_degrades_to_null_link(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    fake_storage.raise_error = True
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_access_url"] is None
    assert resp.json()["image_access_expires_at"] is None
    assert resp.json()["image_access_expires_in"] is None


def test_unwrapped_provider_exception_degrades_to_null_link(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK error a real provider does not wrap must not 500 the read."""
    # Alembic's fileConfig (run by the schema fixture) disables pre-existing
    # loggers; re-enable the router's so caplog really sees its records.
    monkeypatch.setattr(receipt_router.logger, "disabled", False)
    fake_storage.raise_unwrapped = ValueError(f"bad endpoint https://signed.test/{_SENTINEL}")
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    with caplog.at_level(logging.DEBUG):
        resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_access_url"] is None
    assert resp.json()["image_access_expires_at"] is None
    assert resp.json()["image_access_expires_in"] is None
    # Only the exception type is logged — never its message (key / endpoint).
    assert "ValueError" in caplog.text
    assert _SENTINEL not in caplog.text


def test_non_browser_scheme_is_suppressed(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    fake_storage.scheme = "file"
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    resp = employee_client.get(_url(receipt.id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_access_url"] is None
    assert resp.json()["image_access_expires_at"] is None


def test_real_local_provider_file_url_is_suppressed(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
) -> None:
    """The real LocalStorageProvider signs a file:// URL — never exposed."""
    local = LocalStorageProvider()
    key = f"c2-test/{uuid.uuid4()}"
    local.put_object(key, b"\x89PNG fake", content_type="image/png")
    app.dependency_overrides[get_storage_provider] = lambda: local
    try:
        receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=key)
        resp = employee_client.get(_url(receipt.id))
    finally:
        local.delete_object(key)

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_access_url"] is None
    assert "file://" not in resp.text


def test_signed_url_is_never_logged_or_audited(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    employee_client: TestClient,
    manager_client: TestClient,
    fake_storage: _FakeStorage,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alembic's fileConfig disables pre-existing loggers; re-enable the router's
    # so the "never logged" assertion is not vacuous.
    monkeypatch.setattr(receipt_router.logger, "disabled", False)
    receipt = f.make_receipt(db_session, uploaded_by=users_a["employee"], image_url=_KEY)

    with caplog.at_level(logging.DEBUG):
        assert employee_client.get(_url(receipt.id)).status_code == 200
        assert manager_client.get(_url(receipt.id)).status_code == 200
        # Degrade path logs a warning — still no URL / signature in it.
        fake_storage.raise_error = True
        assert employee_client.get(_url(receipt.id)).status_code == 200

    assert "image link unavailable" in caplog.text  # the logger really is captured
    assert _SENTINEL not in caplog.text
    assert "signed.test" not in caplog.text

    audit_rows = db_session.scalars(
        sa.select(AuditLog).where(AuditLog.organization_id == org_a.id)
    ).all()
    for row in audit_rows:
        assert _SENTINEL not in str(row.before_state)
        assert _SENTINEL not in str(row.after_state)


def test_post_and_patch_never_carry_a_link(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    local_providers: None,
) -> None:
    fake = _FakeStorage()
    app.dependency_overrides[get_storage_provider] = lambda: fake

    created = employee_client.post(
        f"{API}/receipts",
        files={"file": scenario_upload("fake", "r.png")},
    )
    assert created.status_code == 201, created.text
    assert created.json()["image_access_url"] is None
    assert created.json()["image_access_expires_at"] is None

    patched = employee_client.patch(
        _url(uuid.UUID(created.json()["id"])), json={"field": "vendor", "value": "V"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["image_access_url"] is None
    assert patched.json()["image_access_expires_at"] is None
    assert fake.calls == []


# ---------------------------------------------------------------------------
# receipt_id on expense entries (the reviewer's path to the image)
# ---------------------------------------------------------------------------


def _report_with_receipts(
    db_session: Session, owner: User, *, status: ExpenseReportStatus
) -> tuple[Any, Any, Any, Any]:
    report = f.make_expense_report(db_session, owner=owner, status=status)
    with_receipt = f.make_expense(db_session, report=report)
    without_receipt = f.make_expense(db_session, report=report)
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    first = f.make_receipt(db_session, uploaded_by=owner, expense_id=with_receipt.id, created_at=t0)
    # A later receipt on the same entry — the earliest one is exposed.
    f.make_receipt(
        db_session,
        uploaded_by=owner,
        expense_id=with_receipt.id,
        created_at=t0 + timedelta(hours=1),
    )
    return report, with_receipt, without_receipt, first


def test_report_entries_carry_earliest_receipt_id(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    manager_client: TestClient,
) -> None:
    report, with_receipt, without_receipt, first = _report_with_receipts(
        db_session, users_a["employee"], status=ExpenseReportStatus.draft
    )

    for client in (employee_client, manager_client):
        resp = client.get(f"{API}/expense-reports/{report.id}/entries")
        assert resp.status_code == 200, resp.text
        by_id = {e["id"]: e for e in resp.json()}
        assert by_id[str(with_receipt.id)]["receipt_id"] == str(first.id)
        assert by_id[str(without_receipt.id)]["receipt_id"] is None


def test_patch_entry_response_carries_receipt_id(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
) -> None:
    _report, with_receipt, _without, first = _report_with_receipts(
        db_session, users_a["employee"], status=ExpenseReportStatus.draft
    )

    resp = employee_client.patch(f"{API}/expenses/{with_receipt.id}", json={"vendor": "Cafe"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["receipt_id"] == str(first.id)


def test_approval_detail_entries_carry_receipt_id(
    db_session: Session,
    users_a: dict[str, User],
    manager_client: TestClient,
    fake_storage: _FakeStorage,
) -> None:
    owner = users_a["employee"]
    report, with_receipt, without_receipt, first = _report_with_receipts(
        db_session, owner, status=ExpenseReportStatus.submitted
    )
    approval = f.make_approval(db_session, submitter=owner, subject_id=report.id)

    resp = manager_client.get(f"{API}/approvals/{approval.id}")

    assert resp.status_code == 200, resp.text
    entries = {e["id"]: e for e in resp.json()["expense_report"]["entries"]}
    assert entries[str(with_receipt.id)]["receipt_id"] == str(first.id)
    assert entries[str(without_receipt.id)]["receipt_id"] is None

    # The reviewer can then follow the id to a scoped, signed image link.
    image = manager_client.get(_url(first.id))
    assert image.status_code == 200, image.text
    assert image.json()["image_access_url"].startswith("https://signed.test/")
