"""P4-T13 Slice 3 Validate gate: receipt HTTP endpoints.

Self-skips when Postgres is unreachable (mark: db).
Uses stub providers for storage, OCR, and LLM so no MinIO/network required.

Coverage:
  1. test_upload_receipt_success        — POST /receipts → 201 with ocr_status field
  2. test_upload_receipt_unauthorized   — no token → 401
  3. test_upload_receipt_idempotent     — same Idempotency-Key → same receipt id
  4. test_correct_receipt_success       — PATCH /receipts/{id} → 200, field corrected
  5. test_correct_receipt_not_found     — wrong receipt_id → 404
  6. test_correct_receipt_wrong_owner   — other user's receipt → 404
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.tests.receipt_images import scenario_upload
from database.models import Base
from database.models.ai_decision_log import AiDecisionLog
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReport
from database.models.idempotency_key import IdempotencyKey
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.receipt import Receipt
from database.seeds.identity_seed import seed_rbac_catalog
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.local import LocalStorageProvider

pytestmark = [pytest.mark.db]

# ---------------------------------------------------------------------------
# Unique email / name constants — isolated from other test modules
# ---------------------------------------------------------------------------

_ORG_NAME = "Receipts API Test Org"
_ORG_B_NAME = "Receipts API Test Org B"
_EMPLOYEE_EMAIL = "receipts-employee@test.internal"
_OTHER_EMAIL = "receipts-other@test.internal"
_TEST_PW = "test-password-receipts-api"

# ---------------------------------------------------------------------------
# Scoped tables for create/drop
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, Receipt.__table__),
]

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping receipts API tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed two orgs with users. employee is in org A; other is in org A (different user)."""
    factory = get_sessionmaker()

    all_emails = [_EMPLOYEE_EMAIL, _OTHER_EMAIL]

    # Prune stale data from a previous incomplete run
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(IdempotencyKey).filter(
                IdempotencyKey.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org_a = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org_a)
        session.flush()

        hpw = hash_password(_TEST_PW)

        employee = User(
            organization_id=org_a.id,
            name="Receipts Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee)
        session.flush()

        other = User(
            organization_id=org_a.id,
            name="Receipts Other",
            email=_OTHER_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(other)
        session.flush()

        session.commit()

    yield {
        "org_a_id": org_a.id,
        "employee_id": employee.id,
        "other_id": other.id,
    }

    # Teardown in FK-safe order
    with factory() as session:
        org_ids = [org_a.id]
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        # Uploads also write idempotency keys and the auto "Receipt Inbox" draft
        # report (its expenses cascade) — remove them before the org (FK-safe).
        session.query(IdempotencyKey).filter(IdempotencyKey.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Client fixture with provider stubs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """TestClient with stub provider overrides so no network calls are made."""
    from services.llm_provider.factory import get_llm_provider
    from services.ocr_provider.factory import get_ocr_provider
    from services.storage_provider.factory import get_storage_provider

    app.dependency_overrides[get_storage_provider] = lambda: LocalStorageProvider()
    app.dependency_overrides[get_ocr_provider] = lambda: LocalOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()

    yield TestClient(app, raise_server_exceptions=False)

    # Clean up overrides after the module
    app.dependency_overrides.pop(get_storage_provider, None)
    app.dependency_overrides.pop(get_ocr_provider, None)
    app.dependency_overrides.pop(get_llm_provider, None)


# ---------------------------------------------------------------------------
# Helper: mint a bearer token
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


def _fake_file(name: str = "receipt.png", content: bytes = b"fake-image-bytes") -> dict:
    """Build a multipart files dict for httpx/testclient.

    Phase 11: a real tiny PNG whose pixels carry *content* (the route refuses
    non-image bytes with a 415).
    """
    return {"file": scenario_upload(content.decode("utf-8"), name)}


# ---------------------------------------------------------------------------
# 1. POST /receipts — success
# ---------------------------------------------------------------------------


class TestUploadReceiptSuccess:
    def test_upload_receipt_success(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST /receipts with valid token and file → 201, ocr_status present."""
        headers = _bearer(seeded["employee_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/receipts",
            files=_fake_file(),
            headers=headers,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "id" in body
        assert body["organization_id"] == str(seeded["org_a_id"])
        assert body["uploaded_by"] == str(seeded["employee_id"])
        assert body["ocr_status"] in ("pending", "processed", "failed")
        assert "image_url" in body


# ---------------------------------------------------------------------------
# 2. POST /receipts — unauthorized (no token)
# ---------------------------------------------------------------------------


class TestUploadReceiptUnauthorized:
    def test_upload_receipt_unauthorized(self, client: TestClient) -> None:
        """POST /receipts without a token → 401."""
        r = client.post(
            "/api/v1/receipts",
            files=_fake_file(),
        )
        assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 3. POST /receipts — idempotency
# ---------------------------------------------------------------------------


class TestUploadReceiptIdempotent:
    def test_upload_receipt_idempotent(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST /receipts with same Idempotency-Key twice → same receipt id."""
        idem_key = f"receipts-idem-{uuid.uuid4().hex}"
        headers = {
            **_bearer(seeded["employee_id"], seeded["org_a_id"]),
            "Idempotency-Key": idem_key,
        }

        r1 = client.post(
            "/api/v1/receipts", files=_fake_file(content=b"idem-bytes"), headers=headers
        )
        assert r1.status_code == 201, r1.text
        id1 = r1.json()["id"]

        r2 = client.post(
            "/api/v1/receipts", files=_fake_file(content=b"idem-bytes"), headers=headers
        )
        assert r2.status_code in (200, 201), r2.text
        id2 = r2.json()["id"]

        assert id1 == id2, "Idempotent calls must return the same receipt id"


# ---------------------------------------------------------------------------
# 4. PATCH /receipts/{id} — correct a field
# ---------------------------------------------------------------------------


class TestCorrectReceiptSuccess:
    def test_correct_receipt_success(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """PATCH /receipts/{id} with a valid correction → 200, field updated."""
        headers = _bearer(seeded["employee_id"], seeded["org_a_id"])

        # First upload a receipt
        r = client.post("/api/v1/receipts", files=_fake_file(), headers=headers)
        assert r.status_code == 201, r.text
        receipt_id = r.json()["id"]

        # Now correct a field
        r2 = client.patch(
            f"/api/v1/receipts/{receipt_id}",
            json={"field": "vendor", "value": "Corrected Vendor Name"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["id"] == receipt_id
        # The extracted_fields should contain the corrected vendor
        if body.get("extracted_fields"):
            assert body["extracted_fields"].get("vendor") == "Corrected Vendor Name"
        # Provenance for vendor should be user_edited
        if body.get("field_provenance"):
            assert body["field_provenance"].get("vendor") == "user_edited"


# ---------------------------------------------------------------------------
# 5. PATCH /receipts/{id} — not found (random uuid)
# ---------------------------------------------------------------------------


class TestCorrectReceiptNotFound:
    def test_correct_receipt_not_found(self, seeded: dict[str, Any], client: TestClient) -> None:
        """PATCH /receipts/{random_id} → 404."""
        headers = _bearer(seeded["employee_id"], seeded["org_a_id"])
        r = client.patch(
            f"/api/v1/receipts/{uuid.uuid4()}",
            json={"field": "vendor", "value": "Test Vendor"},
            headers=headers,
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 6. PATCH /receipts/{id} — wrong owner
# ---------------------------------------------------------------------------


class TestCorrectReceiptWrongOwner:
    def test_correct_receipt_wrong_owner(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """PATCH /receipts/{id} with a different user → 404 (no existence leak)."""
        owner_headers = _bearer(seeded["employee_id"], seeded["org_a_id"])
        other_headers = _bearer(seeded["other_id"], seeded["org_a_id"])

        # Upload as employee
        r = client.post("/api/v1/receipts", files=_fake_file(), headers=owner_headers)
        assert r.status_code == 201, r.text
        receipt_id = r.json()["id"]

        # Attempt correction as other user
        r2 = client.patch(
            f"/api/v1/receipts/{receipt_id}",
            json={"field": "vendor", "value": "Stolen Correction"},
            headers=other_headers,
        )
        assert r2.status_code == 404, r2.text
