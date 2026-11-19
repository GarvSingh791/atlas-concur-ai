"""FEATURE B end-to-end: receipt upload → auto-draft expense → policy verdict.

Self-skips when Postgres is unreachable (mark: db). Uses stub storage/OCR/LLM
providers so no MinIO/network is required. Seeds an active HOTEL-CAP policy rule
so the deterministic policy engine produces a real verdict.

Coverage:
  1. test_over_cap_upload_creates_draft_with_blocking_violation
       — POST /receipts (NYC hotel, $495/night) → 201; a draft Expense is created
         and linked (receipt.expense_id set); the parent report's pre_submit_flags
         carries a blocking policy_violation; the response policy_verdict says
         in_policy=False, requires_approver=manager.
  2. test_compliant_upload_is_in_policy
       — POST /receipts (NYC hotel, $210/night) → 201; draft created; verdict
         in_policy=True with zero blocking flags.
  3. test_policy_failure_still_returns_201_degrade
       — PolicyEvalTool.evaluate raises → upload STILL returns 201; no draft is
         created (expense_id stays null), policy_verdict is null.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.tests.receipt_images import png_name, scenario_of, scenario_upload
from database.models import Base
from database.models.ai_decision_log import AiDecisionLog
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseStatus
from database.models.expense_report import ExpenseReport
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import Receipt
from database.seeds.identity_seed import seed_rbac_catalog
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.local import LocalStorageProvider

pytestmark = [pytest.mark.db]

_ORG_NAME = "Receipts FeatB Test Org"
_EMPLOYEE_EMAIL = "receipts-featb-employee@test.internal"
_TEST_PW = "test-password-receipts-featb"

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, Policy.__table__),
    cast(Table, PolicyRule.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, AiDecisionLog.__table__),
]


# ---------------------------------------------------------------------------
# OCR stub — high-confidence lodging fields incl. per_night_rate EXTRA key
# ---------------------------------------------------------------------------


class _LodgingOcrProvider(OcrProvider):
    """Deterministic OCR returning over-cap or compliant NYC lodging by filename.

    All 6 routed keys carry high confidence so the deterministic path runs (no
    gap-fill, no LLM, no itinerary lookup). per_night_rate is an EXTRA key the
    pipeline now persists into extracted_fields.
    """

    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        compliant = "compliant" in key
        per_night = 210.0 if compliant else 495.0
        nights = 1 if compliant else 3
        return OcrResult(
            fields={
                "amount": per_night * nights,
                "currency": "USD",
                "date": "2026-06-10",
                "vendor": "Marriott New York",
                "location": "New York, NY",
                "expense_type": "hotel",
                "nights": nights,
                "per_night_rate": per_night,
            },
            field_confidence={
                "amount": 0.97,
                "currency": 0.99,
                "date": 0.96,
                "vendor": 0.94,
                "location": 0.93,
                "expense_type": 0.96,
            },
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping FEATURE B receipts API tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    factory = get_sessionmaker()

    # Prune stale rows from a previous incomplete run.
    with factory() as session:
        stale = session.query(User).filter(User.email == _EMPLOYEE_EMAIL).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(Expense).filter(Expense.organization_id.in_(stale_org_ids)).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            policy_ids = [
                p.id
                for p in session.query(Policy)
                .filter(Policy.organization_id.in_(stale_org_ids))
                .all()
            ]
            if policy_ids:
                session.query(PolicyRule).filter(PolicyRule.policy_id.in_(policy_ids)).delete()
            session.query(Policy).filter(Policy.organization_id.in_(stale_org_ids)).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org)
        session.flush()

        employee = User(
            organization_id=org.id,
            name="FeatB Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hash_password(_TEST_PW),
        )
        session.add(employee)
        session.flush()

        policy = Policy(
            organization_id=org.id,
            name="FeatB Travel Policy",
            version=1,
            effective_from=date(2026, 1, 1),
            is_active=True,
            created_by=employee.id,
            updated_by=employee.id,
        )
        session.add(policy)
        session.flush()

        rule = PolicyRule(
            policy_id=policy.id,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.hotel,
            source_clause_ref="HOTEL-CAP",
            status=PolicyRuleStatus.active,
            definition={
                "currency": "USD",
                "per": "night",
                "tiers": [
                    {
                        "tier": "tier1",
                        "cities": ["New York", "San Francisco", "London"],
                        "cap": 250,
                    },
                    {"tier": "domestic", "cap": 180},
                ],
                "exception_approver": "manager",
            },
            created_by=employee.id,
            updated_by=employee.id,
        )
        session.add(rule)
        session.commit()

        ids = {"org_id": org.id, "employee_id": employee.id}

    yield ids

    with factory() as session:
        org_ids = [ids["org_id"]]
        session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        policy_ids = [
            p.id for p in session.query(Policy).filter(Policy.organization_id.in_(org_ids)).all()
        ]
        if policy_ids:
            session.query(PolicyRule).filter(PolicyRule.policy_id.in_(policy_ids)).delete()
        session.query(Policy).filter(Policy.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from services.llm_provider.factory import get_llm_provider
    from services.ocr_provider.factory import get_ocr_provider
    from services.storage_provider.factory import get_storage_provider

    app.dependency_overrides[get_storage_provider] = lambda: LocalStorageProvider()
    app.dependency_overrides[get_ocr_provider] = lambda: _LodgingOcrProvider()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()

    yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.pop(get_storage_provider, None)
    app.dependency_overrides.pop(get_ocr_provider, None)
    app.dependency_overrides.pop(get_llm_provider, None)


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, org_id)}"}


def _file(name: str) -> dict:
    # The OCR stub branches on image CONTENT (it decodes the uploaded bytes), so the
    # discriminating marker ("compliant" / "overcap") must live in the body, not only
    # the filename — otherwise every upload hits the default over-cap branch.
    return {"file": scenario_upload(name, png_name(name))}


def _load_report_for_receipt(receipt_id: str, org_id: uuid.UUID) -> ExpenseReport | None:
    factory = get_sessionmaker()
    with factory() as session:
        receipt = session.get(Receipt, uuid.UUID(receipt_id))
        if receipt is None or receipt.expense_id is None:
            return None
        expense = session.get(Expense, receipt.expense_id)
        assert expense is not None
        return session.get(ExpenseReport, expense.report_id)


# ---------------------------------------------------------------------------
# 1. Over-cap upload → draft expense + blocking policy_violation
# ---------------------------------------------------------------------------


class TestOverCapUpload:
    def test_over_cap_upload_creates_draft_with_blocking_violation(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        r = client.post("/api/v1/receipts", files=_file("hotel-overcap.jpg"), headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()

        # A draft expense was created and linked.
        assert body["expense_id"] is not None, "receipt should link to an auto-drafted expense"
        # per_night_rate EXTRA key persisted additively into extracted_fields.
        assert body["extracted_fields"].get("per_night_rate") == 495.0

        # Response policy verdict summary — over cap.
        verdict = body["policy_verdict"]
        assert verdict is not None
        assert verdict["in_policy"] is False
        assert verdict["blocking_count"] >= 1
        assert verdict["requires_approver"] == "manager"
        assert "policy_violation" in verdict["flag_types"]

        # The parent report's pre_submit_flags carries the blocking violation.
        report = _load_report_for_receipt(body["id"], seeded["org_id"])
        assert report is not None
        assert report.title == "Receipt Inbox"
        snapshot = report.pre_submit_flags
        assert snapshot is not None
        assert snapshot["blocking_count"] >= 1
        assert any(
            f["flag_type"] == "policy_violation" and f["severity"] == "blocking"
            for f in snapshot["flags"]
        )


# ---------------------------------------------------------------------------
# 2. Compliant upload → in_policy, no blocking flags
# ---------------------------------------------------------------------------


class TestCompliantUpload:
    def test_compliant_upload_is_in_policy(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        r = client.post("/api/v1/receipts", files=_file("hotel-compliant.jpg"), headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()

        assert body["expense_id"] is not None
        verdict = body["policy_verdict"]
        assert verdict is not None
        assert verdict["in_policy"] is True
        assert verdict["blocking_count"] == 0

        # The Receipt Inbox report is shared across uploads in this module, so its
        # aggregate blocking_count can include a prior over-cap entry. Assert on THIS
        # compliant receipt's own expense instead: it must not be flagged.
        report = _load_report_for_receipt(body["id"], seeded["org_id"])
        assert report is not None
        factory = get_sessionmaker()
        with factory() as session:
            receipt = session.get(Receipt, uuid.UUID(body["id"]))
            assert receipt is not None and receipt.expense_id is not None
            expense = session.get(Expense, receipt.expense_id)
            assert expense is not None
            assert expense.status != ExpenseStatus.flagged


# ---------------------------------------------------------------------------
# 3. Policy-engine failure → still 201, degrade (no draft, null verdict)
# ---------------------------------------------------------------------------


class TestPolicyFailureDegrades:
    def test_policy_failure_still_returns_201_degrade(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        with patch(
            "backend.app.modules.receipt.router.PolicyEvalTool.evaluate",
            side_effect=RuntimeError("boom"),
        ):
            r = client.post("/api/v1/receipts", files=_file("hotel-overcap.jpg"), headers=headers)
        # Upload must STILL succeed (degrade, never block).
        assert r.status_code == 201, r.text
        body = r.json()
        # The auto-draft savepoint rolled back → no expense link, no verdict.
        assert body["expense_id"] is None
        assert body.get("policy_verdict") is None

        # The receipt itself persisted with its extracted fields.
        get = client.get(f"/api/v1/receipts/{body['id']}", headers=headers)
        assert get.status_code == 200, get.text
        assert get.json()["ocr_status"] == "processed"
