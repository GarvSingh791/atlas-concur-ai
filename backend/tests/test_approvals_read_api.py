"""P5-T8 — Approval READ surfaces (GET list + GET detail) + concurrency.

Validate-gate coverage:
  G2  manager sees ONLY own + direct reports' approvals (server-scoped)
  G3  finance_admin sees the org-wide queue (superset)
  G5  decisions persist; version increments
  G7  stale If-Match version → 409 conflict (no silent overwrite)
  G8  auto-approved item carries its rationale (the auto_approve step reason)
  G10 subject polymorphism — both expense_report and trip render in detail
  RBAC employee (no approval.read) → 403

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.errors import ErrorCode
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from database.models import Base
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, Trip.__table__),
    cast(Table, TripSegment.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ApprovalStep.__table__),
]

_ORG_NAME = "Approvals Read Test Org"
_FINANCE_EMAIL = "appr-read-finance@test.internal"
_MANAGER_EMAIL = "appr-read-manager@test.internal"
_EMPLOYEE_EMAIL = "appr-read-employee@test.internal"
_TEST_PW = "test-password-approvals-read"


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping approvals read API tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


def _purge(session: Any, org_ids: list[uuid.UUID]) -> None:
    if not org_ids:
        return
    session.query(ApprovalStep).filter(
        ApprovalStep.approval_id.in_(
            session.query(Approval.id).filter(Approval.organization_id.in_(org_ids))
        )
    ).delete(synchronize_session=False)
    session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete(
        synchronize_session=False
    )
    session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete(
        synchronize_session=False
    )
    session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete(
        synchronize_session=False
    )
    session.query(TripSegment).filter(
        TripSegment.trip_id.in_(session.query(Trip.id).filter(Trip.organization_id.in_(org_ids)))
    ).delete(synchronize_session=False)
    session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete(synchronize_session=False)
    session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete(
        synchronize_session=False
    )
    session.query(User).filter(User.organization_id.in_(org_ids)).delete(synchronize_session=False)
    session.query(Organization).filter(Organization.id.in_(org_ids)).delete(
        synchronize_session=False
    )
    session.commit()


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Org + 3 users + approvals (expense_report, trip, auto-approved, out-of-team)."""
    factory = get_sessionmaker()
    utc = timezone.utc

    with factory() as session:
        stale = (
            session.query(User)
            .filter(User.email.in_([_FINANCE_EMAIL, _MANAGER_EMAIL, _EMPLOYEE_EMAIL]))
            .all()
        )
        _purge(session, list({u.organization_id for u in stale}))

    with factory() as session:
        seed_rbac_catalog(session)

        org = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org)
        session.flush()
        hpw = hash_password(_TEST_PW)

        manager = User(
            organization_id=org.id,
            name="Read Manager",
            email=_MANAGER_EMAIL,
            role=RoleKey.manager,
            department="Eng",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        finance = User(
            organization_id=org.id,
            name="Read Finance",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add_all([manager, finance])
        session.flush()

        employee = User(
            organization_id=org.id,
            name="Read Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Eng",
            manager_id=manager.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee)
        session.flush()

        # Real expense report (owner = employee) + entries
        report = ExpenseReport(
            organization_id=org.id,
            owner_id=employee.id,
            title="Read Report",
            status=ExpenseReportStatus.submitted,
            currency="USD",
            total_amount=Decimal("1234.50"),
            submitted_at=datetime(2026, 6, 12, tzinfo=utc),
            pre_submit_flags=None,
            created_by=employee.id,
            updated_by=employee.id,
        )
        session.add(report)
        session.flush()
        session.add_all(
            [
                Expense(
                    organization_id=org.id,
                    report_id=report.id,
                    category=ExpenseCategory.lodging,
                    amount=Decimal("867.00"),
                    currency="USD",
                    expense_date=date(2026, 6, 10),
                    description="Hotel",
                    status=ExpenseStatus.flagged,
                    created_by=employee.id,
                    updated_by=employee.id,
                ),
                Expense(
                    organization_id=org.id,
                    report_id=report.id,
                    category=ExpenseCategory.meals,
                    amount=Decimal("367.50"),
                    currency="USD",
                    expense_date=date(2026, 6, 11),
                    description="Meals",
                    status=ExpenseStatus.draft,
                    created_by=employee.id,
                    updated_by=employee.id,
                ),
            ]
        )

        # Real trip (traveler = employee) + segment
        trip = Trip(
            organization_id=org.id,
            traveler_id=employee.id,
            origin="LAX",
            primary_destination="ORD",
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 17),
            purpose="Client",
            estimated_cost=Decimal("1410.00"),
            budget_amount=Decimal("1800.00"),
            currency="USD",
            status=TripStatus.pending_approval,
            recommendation_context=None,
            created_by=employee.id,
            updated_by=employee.id,
        )
        session.add(trip)
        session.flush()
        session.add(
            TripSegment(
                trip_id=trip.id,
                segment_type=SegmentType.hotel,
                supplier="Hyatt",
                start_datetime=datetime(2026, 7, 15, 16, tzinfo=utc),
                end_datetime=datetime(2026, 7, 17, 11, tzinfo=utc),
                location="Chicago",
                amount=Decimal("890.00"),
                currency="USD",
                in_policy=False,
                policy_notes="Over cap",
                details={"nights": 2},
            )
        )

        # Pending expense-report approval (employee → visible to manager + finance), blocking flag
        appr_report = Approval(
            organization_id=org.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report.id,
            submitter_id=employee.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary={
                "route": "escalate",
                "subject_type": "expense_report",
                "subject_in_policy": False,
                "blocking_flag_count": 1,
                "requires_approver": False,
                "subject_total_usd": 1234.50,
                "routed_to_approver_id": str(manager.id),
                "flags": [
                    {
                        "entry_ref": "Hotel",
                        "flag_type": "policy_violation",
                        "rule_id": None,
                        "rule_type": "amount_cap",
                        "source_clause_ref": "HOTEL-CAP",
                        "reason": "Over cap.",
                        "requires_approver": None,
                        "observed": {"amount_usd": 867.0},
                        "severity": "blocking",
                    }
                ],
            },
        )
        # Pending trip approval (employee), no blocking flag → medium
        appr_trip = Approval(
            organization_id=org.id,
            subject_type=ApprovalSubjectType.trip,
            subject_id=trip.id,
            submitter_id=employee.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary={
                "route": "escalate",
                "subject_type": "trip",
                "subject_in_policy": False,
                "blocking_flag_count": 0,
                "requires_approver": False,
                "subject_total_usd": 1410.0,
                "routed_to_approver_id": str(manager.id),
                "flags": [
                    {
                        "segment_id": None,
                        "segment_type": "hotel",
                        "in_policy": False,
                        "policy_notes": "Over cap",
                    }
                ],
            },
        )
        # Auto-approved approval (employee) + system step carrying the rationale
        appr_auto = Approval(
            organization_id=org.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=employee.id,
            current_status=ApprovalStatus.auto_approved,
            is_auto_approved=True,
            risk_summary={
                "route": "auto_approve",
                "subject_type": "expense_report",
                "subject_in_policy": True,
                "blocking_flag_count": 0,
                "requires_approver": False,
                "subject_total_usd": 85.0,
                "flags": [],
            },
        )
        # Out-of-team approval (submitter = finance, NOT a manager report) → manager must NOT see
        appr_out = Approval(
            organization_id=org.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=finance.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary=None,
        )
        session.add_all([appr_report, appr_trip, appr_auto, appr_out])
        session.flush()
        session.add(
            ApprovalStep(
                approval_id=appr_auto.id,
                approver_id=None,
                step_order=1,
                decision=ApprovalDecision.auto_approve,
                reason=(
                    "Auto-approved: in policy, zero blocking flags,"
                    " total 85.00 USD ≤ 500.00 USD."
                ),
                decided_at=datetime(2026, 5, 20, tzinfo=utc),
            )
        )
        session.commit()

        ids = {
            "org_id": org.id,
            "manager_id": manager.id,
            "finance_id": finance.id,
            "employee_id": employee.id,
            "appr_report_id": appr_report.id,
            "appr_trip_id": appr_trip.id,
            "appr_auto_id": appr_auto.id,
            "appr_out_id": appr_out.id,
        }

    yield ids

    with factory() as session:
        _purge(session, [ids["org_id"]])


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, org_id)}"}


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /approvals — scoping (G2/G3) + RBAC
# ---------------------------------------------------------------------------


class TestListScoping:
    def test_manager_sees_only_team(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            "/api/v1/approvals", headers=_bearer(seeded["manager_id"], seeded["org_id"])
        )
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        ids = {i["id"] for i in items}
        # Sees the employee's approvals; NOT the finance-submitted (out-of-team) one.
        assert str(seeded["appr_report_id"]) in ids
        assert str(seeded["appr_out_id"]) not in ids
        for i in items:
            assert i["submitter_id"] in (str(seeded["manager_id"]), str(seeded["employee_id"]))

    def test_finance_sees_org_wide(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            "/api/v1/approvals", headers=_bearer(seeded["finance_id"], seeded["org_id"])
        )
        assert r.status_code == 200, r.text
        ids = {i["id"] for i in r.json()["items"]}
        # Org-wide superset includes the out-of-team approval the manager can't see.
        assert str(seeded["appr_out_id"]) in ids
        assert str(seeded["appr_report_id"]) in ids

    def test_employee_forbidden(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            "/api/v1/approvals", headers=_bearer(seeded["employee_id"], seeded["org_id"])
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_unauthorized(self, seeded: dict[str, Any]) -> None:
        assert _client().get("/api/v1/approvals").status_code == 401


# ---------------------------------------------------------------------------
# GET /approvals — derived risk_level + auto-approve rationale (G8) + filters
# ---------------------------------------------------------------------------


class TestListContent:
    def _items_by_id(self, seeded: dict[str, Any]) -> dict[str, dict[str, Any]]:
        r = _client().get(
            "/api/v1/approvals?size=100", headers=_bearer(seeded["finance_id"], seeded["org_id"])
        )
        assert r.status_code == 200, r.text
        return {i["id"]: i for i in r.json()["items"]}

    def test_blocking_flag_is_high_risk(self, seeded: dict[str, Any]) -> None:
        item = self._items_by_id(seeded)[str(seeded["appr_report_id"])]
        assert item["risk_level"] == "high"
        assert item["subject_type"] == "expense_report"
        assert item["submitter_name"] == "Read Employee"

    def test_auto_approved_is_low_with_rationale(self, seeded: dict[str, Any]) -> None:
        item = self._items_by_id(seeded)[str(seeded["appr_auto_id"])]
        assert item["risk_level"] == "low"
        assert item["is_auto_approved"] is True
        assert item["auto_approve_rationale"] is not None
        assert "Auto-approved" in item["auto_approve_rationale"]

    def test_trip_no_blocking_is_medium(self, seeded: dict[str, Any]) -> None:
        item = self._items_by_id(seeded)[str(seeded["appr_trip_id"])]
        assert item["risk_level"] == "medium"
        assert item["subject_type"] == "trip"

    def test_filter_by_status(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            "/api/v1/approvals?current_status=auto_approved",
            headers=_bearer(seeded["finance_id"], seeded["org_id"]),
        )
        assert r.status_code == 200, r.text
        statuses = {i["current_status"] for i in r.json()["items"]}
        assert statuses == {"auto_approved"}

    def test_invalid_filter_field_rejected(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            "/api/v1/approvals?bogus=1", headers=_bearer(seeded["finance_id"], seeded["org_id"])
        )
        assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# GET /approvals/{id} — detail + subject polymorphism (G10)
# ---------------------------------------------------------------------------


class TestDetail:
    def test_expense_report_context(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            f"/api/v1/approvals/{seeded['appr_report_id']}",
            headers=_bearer(seeded["manager_id"], seeded["org_id"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subject_type"] == "expense_report"
        assert body["risk_level"] == "high"
        assert body["risk_summary"]["flags"][0]["source_clause_ref"] == "HOTEL-CAP"
        assert body["expense_report"] is not None
        assert len(body["expense_report"]["entries"]) == 2
        assert body["trip"] is None
        assert body["steps"] == []
        assert body["version"] >= 1

    def test_trip_context(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            f"/api/v1/approvals/{seeded['appr_trip_id']}",
            headers=_bearer(seeded["manager_id"], seeded["org_id"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subject_type"] == "trip"
        assert body["trip"] is not None
        assert len(body["trip"]["segments"]) == 1
        assert body["expense_report"] is None

    def test_auto_approved_detail_has_step_rationale(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            f"/api/v1/approvals/{seeded['appr_auto_id']}",
            headers=_bearer(seeded["finance_id"], seeded["org_id"]),
        )
        assert r.status_code == 200, r.text
        steps = r.json()["steps"]
        assert len(steps) == 1
        assert steps[0]["decision"] == "auto_approve"
        assert steps[0]["approver_id"] is None
        assert "Auto-approved" in steps[0]["reason"]

    def test_manager_cannot_read_out_of_team(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            f"/api/v1/approvals/{seeded['appr_out_id']}",
            headers=_bearer(seeded["manager_id"], seeded["org_id"]),
        )
        assert r.status_code == 404, r.text

    def test_detail_not_found(self, seeded: dict[str, Any]) -> None:
        r = _client().get(
            f"/api/v1/approvals/{uuid.uuid4()}",
            headers=_bearer(seeded["finance_id"], seeded["org_id"]),
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# PATCH /approvals/{id} — optimistic concurrency (G7) + version increment (G5)
# ---------------------------------------------------------------------------


def _new_pending(org_id: uuid.UUID, submitter_id: uuid.UUID) -> uuid.UUID:
    factory = get_sessionmaker()
    with factory() as session:
        a = Approval(
            organization_id=org_id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=submitter_id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary=None,
        )
        session.add(a)
        session.commit()
        return a.id


class TestConcurrency:
    def test_stale_version_conflict(self, seeded: dict[str, Any]) -> None:
        approval_id = _new_pending(seeded["org_id"], seeded["employee_id"])
        r = _client().patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "approve"},
            headers={**_bearer(seeded["manager_id"], seeded["org_id"]), "If-Match": "999"},
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_success_increments_version(self, seeded: dict[str, Any]) -> None:
        approval_id = _new_pending(seeded["org_id"], seeded["employee_id"])
        headers = _bearer(seeded["manager_id"], seeded["org_id"])
        before = _client().get(f"/api/v1/approvals/{approval_id}", headers=headers).json()
        assert before["version"] == 1
        r = _client().patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "approve"},
            headers={**headers, "If-Match": "1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["current_status"] == "approved"
        assert r.json()["version"] == 2
