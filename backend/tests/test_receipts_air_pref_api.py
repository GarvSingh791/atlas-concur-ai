"""Receipt upload of an AIRFARE receipt → AIR-PREF preferred-carrier verdict.

Regression for the auto-draft path never populating EvalItem.supplier, which made
AIR-PREF fail as BLOCKING for every uploaded airfare receipt (even United).

Self-skips when Postgres is unreachable (mark: db). Stub storage/OCR/LLM providers;
seeds an active AIR-PREF rule mirroring the seed definition.

Coverage:
  1. United Airlines receipt → no blocking AIR-PREF flag, expense not flagged.
  2. Spirit Airlines receipt → blocking AIR-PREF policy_violation, manager approver,
     expense flagged.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any, cast

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

_ORG_NAME = "Receipts AirPref Test Org"
_EMPLOYEE_EMAIL = "receipts-airpref-employee@test.internal"
_TEST_PW = "test-password-receipts-airpref"

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
# OCR stub — high-confidence airfare fields; vendor chosen by uploaded content
# ---------------------------------------------------------------------------


class _AirfareOcrProvider(OcrProvider):
    """Deterministic OCR: "spirit" in the uploaded bytes → Spirit Airlines, else United."""

    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        vendor = "Spirit Airlines" if "spirit" in key else "United Airlines"
        return OcrResult(
            fields={
                "amount": 420.0,
                "currency": "USD",
                "date": "2026-06-10",
                "vendor": vendor,
                "location": "Chicago, IL",
                "expense_type": "airfare",
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
        pytest.skip("Postgres not reachable — skipping AIR-PREF receipts API tests")

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
            name="AirPref Employee",
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
            name="AirPref Travel Policy",
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
            rule_type=PolicyRuleType.preferred_supplier,
            applies_to=PolicyAppliesTo.flight,
            source_clause_ref="AIR-PREF",
            status=PolicyRuleStatus.active,
            definition={
                "preferred_suppliers": ["United Airlines", "Delta Air Lines", "Lufthansa"],
                "within_pct": 10,
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
    app.dependency_overrides[get_ocr_provider] = lambda: _AirfareOcrProvider()
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
# Helpers
# ---------------------------------------------------------------------------


def _expense_and_report(receipt_id: str) -> tuple[Expense, ExpenseReport]:
    factory = get_sessionmaker()
    with factory() as session:
        receipt = session.get(Receipt, uuid.UUID(receipt_id))
        assert receipt is not None and receipt.expense_id is not None
        expense = session.get(Expense, receipt.expense_id)
        assert expense is not None
        report = session.get(ExpenseReport, expense.report_id)
        assert report is not None
        session.expunge_all()
        return expense, report


def _air_pref_flags(report: ExpenseReport, entry_ref: str) -> list[dict[str, Any]]:
    snapshot = report.pre_submit_flags or {}
    return [
        f
        for f in snapshot.get("flags", [])
        if f.get("source_clause_ref") == "AIR-PREF" and f.get("entry_ref") == entry_ref
    ]


# ---------------------------------------------------------------------------
# 1. Preferred carrier (United) → no blocking AIR-PREF flag
# ---------------------------------------------------------------------------


class TestPreferredCarrierUpload:
    def test_united_receipt_is_in_policy(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        r = client.post("/api/v1/receipts", files=_file("flight-united.jpg"), headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["extracted_fields"].get("vendor") == "United Airlines"
        assert body["expense_id"] is not None

        verdict = body["policy_verdict"]
        assert verdict is not None
        assert verdict["in_policy"] is True
        assert verdict["blocking_count"] == 0
        assert "policy_violation" not in verdict["flag_types"]

        expense, report = _expense_and_report(body["id"])
        assert expense.status != ExpenseStatus.flagged
        assert not [
            f for f in _air_pref_flags(report, "United Airlines") if f["severity"] == "blocking"
        ]


# ---------------------------------------------------------------------------
# 2. Non-preferred carrier (Spirit) → blocking AIR-PREF violation
# ---------------------------------------------------------------------------


class TestNonPreferredCarrierUpload:
    def test_spirit_receipt_blocks_on_air_pref(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        r = client.post("/api/v1/receipts", files=_file("flight-spirit.jpg"), headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["extracted_fields"].get("vendor") == "Spirit Airlines"
        assert body["expense_id"] is not None

        verdict = body["policy_verdict"]
        assert verdict is not None
        assert verdict["in_policy"] is False
        assert verdict["blocking_count"] >= 1
        assert verdict["requires_approver"] == "manager"
        assert "policy_violation" in verdict["flag_types"]

        expense, report = _expense_and_report(body["id"])
        assert expense.status == ExpenseStatus.flagged
        blocking = [
            f for f in _air_pref_flags(report, "Spirit Airlines") if f["severity"] == "blocking"
        ]
        assert len(blocking) == 1
        assert blocking[0]["flag_type"] == "policy_violation"
        assert blocking[0]["requires_approver"] == "manager"
