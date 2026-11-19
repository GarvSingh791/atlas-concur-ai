"""P5-T4 Validate gate: analytics overview endpoint.

Assertions:
  (a) GET /analytics/overview as employee → 200, role=="employee", employee section populated,
      manager and finance sections None.
  (b) GET /analytics/overview as manager → 200, role=="manager", manager section populated.
  (c) GET /analytics/overview as finance_admin → 200, role=="finance_admin", finance section
      populated.
  (d) GET /analytics/overview with no token → 401.
  (e) Employee KPI "Pending Approvals" reflects only their own records (not employee_b's).
  (f) Finance KPIs are non-empty and carry meaningful values (org-wide aggregation).
  (g) Cross-tenant isolation: org B finance user sees only org B data.
  (h) Static: analytics service.py contains no top-level LLM imports.

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from database.models import Base
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
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
from database.models.receipt import Receipt
from database.models.trip import Trip
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

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
    cast(Table, Trip.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ApprovalStep.__table__),
    cast(Table, Receipt.__table__),
]

# ---------------------------------------------------------------------------
# Unique email / name constants (isolated from other test modules)
# ---------------------------------------------------------------------------

_ORG_A_NAME = "Analytics API Test Org A"
_ORG_B_NAME = "Analytics API Test Org B"
_EMPLOYEE_A_EMAIL = "analytics-employee-a@test.internal"
_MANAGER_A_EMAIL = "analytics-manager-a@test.internal"
_FINANCE_A_EMAIL = "analytics-finance-a@test.internal"
_EMPLOYEE_B_EMAIL = "analytics-employee-b@test.internal"
_FINANCE_B_EMAIL = "analytics-finance-b@test.internal"
_TEST_PW = "test-password-analytics-api"

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
        pytest.skip("Postgres not reachable — skipping analytics API tests")

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
    """Seed RBAC + org A (4 users + expense reports + approvals) + org B (1 user + 1 report)."""
    factory = get_sessionmaker()

    all_emails = [
        _EMPLOYEE_A_EMAIL,
        _MANAGER_A_EMAIL,
        _FINANCE_A_EMAIL,
        _EMPLOYEE_B_EMAIL,
        _FINANCE_B_EMAIL,
    ]

    # Prune stale data from a previous incomplete run (FK-safe order).
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            # Delete in FK-safe order: receipts → expenses → approval_steps →
            # approvals → expense_reports → trips → audit_log → users → orgs
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(Expense).filter(Expense.organization_id.in_(stale_org_ids)).delete()
            session.query(ApprovalStep).filter(
                ApprovalStep.approval_id.in_(
                    session.query(Approval.id).filter(Approval.organization_id.in_(stale_org_ids))
                )
            ).delete(synchronize_session=False)
            session.query(Approval).filter(Approval.organization_id.in_(stale_org_ids)).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        # ---- Organisations ----
        org_a = Organization(name=_ORG_A_NAME, base_currency="USD")
        org_b = Organization(name=_ORG_B_NAME, base_currency="USD")
        session.add_all([org_a, org_b])
        session.flush()

        hpw = hash_password(_TEST_PW)

        # ---- Org A users ----
        manager_a = User(
            organization_id=org_a.id,
            name="Manager A",
            email=_MANAGER_A_EMAIL,
            role=RoleKey.manager,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(manager_a)
        session.flush()

        employee_a = User(
            organization_id=org_a.id,
            name="Employee A",
            email=_EMPLOYEE_A_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager_a.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee_a)
        session.flush()

        employee_b = User(
            organization_id=org_a.id,
            name="Employee B (direct report of manager_a)",
            email=_EMPLOYEE_B_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager_a.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee_b)
        session.flush()

        finance_a = User(
            organization_id=org_a.id,
            name="Finance Admin A",
            email=_FINANCE_A_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_a)
        session.flush()

        # ---- Org A: expense report for employee_a (submitted, pending approval) ----
        report_a = ExpenseReport(
            organization_id=org_a.id,
            owner_id=employee_a.id,
            title="Employee A Report",
            status=ExpenseReportStatus.submitted,
            total_amount=150.00,
            currency="USD",
            submitted_at=datetime.now(timezone.utc),
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        session.add(report_a)
        session.flush()

        # Add an expense entry to the report
        expense_a = Expense(
            organization_id=org_a.id,
            report_id=report_a.id,
            category=ExpenseCategory.meals,
            amount=150.00,
            currency="USD",
            expense_date=date(2026, 6, 1),
            status=ExpenseStatus.draft,
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        session.add(expense_a)
        session.flush()

        # Approval for employee_a's report: pending
        approval_a = Approval(
            organization_id=org_a.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_a.id,
            submitter_id=employee_a.id,
            current_status=ApprovalStatus.pending,
        )
        session.add(approval_a)
        session.flush()

        # ---- Org A: expense report for employee_b (submitted, auto_approved) ----
        report_b = ExpenseReport(
            organization_id=org_a.id,
            owner_id=employee_b.id,
            title="Employee B Report",
            status=ExpenseReportStatus.submitted,
            total_amount=200.00,
            currency="USD",
            submitted_at=datetime.now(timezone.utc),
            created_by=employee_b.id,
            updated_by=employee_b.id,
        )
        session.add(report_b)
        session.flush()

        expense_b = Expense(
            organization_id=org_a.id,
            report_id=report_b.id,
            category=ExpenseCategory.airfare,
            amount=200.00,
            currency="USD",
            expense_date=date(2026, 6, 2),
            status=ExpenseStatus.draft,
            created_by=employee_b.id,
            updated_by=employee_b.id,
        )
        session.add(expense_b)
        session.flush()

        approval_b = Approval(
            organization_id=org_a.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_b.id,
            submitter_id=employee_b.id,
            current_status=ApprovalStatus.auto_approved,
            is_auto_approved=True,
        )
        session.add(approval_b)
        session.flush()

        # ---- Org B user + report (cross-tenant isolation) ----
        finance_b = User(
            organization_id=org_b.id,
            name="Finance Admin B",
            email=_FINANCE_B_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_b)
        session.flush()

        report_org_b = ExpenseReport(
            organization_id=org_b.id,
            owner_id=finance_b.id,
            title="Org B Report",
            status=ExpenseReportStatus.approved,
            total_amount=999.00,
            currency="USD",
            submitted_at=datetime.now(timezone.utc),
            created_by=finance_b.id,
            updated_by=finance_b.id,
        )
        session.add(report_org_b)
        session.flush()

        session.commit()

    yield {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "employee_a_id": employee_a.id,
        "manager_a_id": manager_a.id,
        "finance_a_id": finance_a.id,
        "employee_b_id": employee_b.id,
        "finance_b_id": finance_b.id,
        "report_a_id": report_a.id,
        "report_b_id": report_b.id,
        "report_org_b_id": report_org_b.id,
    }

    # Teardown — delete in FK-safe order
    with factory() as session:
        org_ids = [org_a.id, org_b.id]
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete()
        session.query(ApprovalStep).filter(
            ApprovalStep.approval_id.in_(
                session.query(Approval.id).filter(Approval.organization_id.in_(org_ids))
            )
        ).delete(synchronize_session=False)
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Client fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: mint a bearer token
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# (a) Employee payload shape
# ---------------------------------------------------------------------------


class TestEmployeePayloadShape:
    def test_employee_payload_shape(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee GET /analytics/overview → 200, role==employee, employee section populated."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["role"] == "employee"
        assert body["employee"] is not None
        assert body["manager"] is None
        assert body["finance"] is None

        emp = body["employee"]
        assert isinstance(emp["kpis"], list)
        assert isinstance(emp["ai_recommendations"], list)
        assert len(emp["ai_recommendations"]) <= 3
        assert isinstance(emp["policy_warnings"], list)
        assert len(emp["policy_warnings"]) <= 3


# ---------------------------------------------------------------------------
# (b) Manager payload shape
# ---------------------------------------------------------------------------


class TestManagerPayloadShape:
    def test_manager_payload_shape(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager GET /analytics/overview → 200, role==manager, manager section populated."""
        headers = _bearer(seeded["manager_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["role"] == "manager"
        assert body["manager"] is not None
        assert body["employee"] is None
        assert body["finance"] is None

        mgr = body["manager"]
        assert isinstance(mgr["approval_queue"], list)
        assert isinstance(mgr["approval_queue_total"], int)


# ---------------------------------------------------------------------------
# (c) Finance payload shape
# ---------------------------------------------------------------------------


class TestFinancePayloadShape:
    def test_finance_payload_shape(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance GET /analytics/overview → 200, role==finance_admin, finance section populated."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["role"] == "finance_admin"
        assert body["finance"] is not None
        assert body["employee"] is None
        assert body["manager"] is None

        fin = body["finance"]
        assert isinstance(fin["kpis"], list)
        assert isinstance(fin["spend_over_time"], list)


# ---------------------------------------------------------------------------
# (d) Unauthenticated → 401
# ---------------------------------------------------------------------------


class TestUnauthenticated:
    def test_unauthenticated(self, client: TestClient) -> None:
        """GET /analytics/overview with no token → 401."""
        r = client.get("/api/v1/analytics/overview")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# (e) Record scoping: employee sees only own records
# ---------------------------------------------------------------------------


class TestRecordScopingEmployee:
    def test_record_scoping_employee_sees_only_own(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee A's Pending Approvals KPI reflects only their own pending approvals."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200, r.text
        emp = r.json()["employee"]

        kpis = emp["kpis"]
        assert len(kpis) > 0

        # Find the "Pending Approvals" KPI
        pending_kpi = next(
            (k for k in kpis if k["label"] == "Pending Approvals"),
            None,
        )
        assert pending_kpi is not None, f"Expected 'Pending Approvals' KPI in {kpis}"
        # employee_a has exactly 1 pending approval seeded; value must be >= 0
        assert pending_kpi["value"] >= 0
        # employee_b's auto_approved approval must NOT be counted for employee_a
        # employee_a has 1 pending, so value should be exactly 1
        assert pending_kpi["value"] == 1.0


# ---------------------------------------------------------------------------
# (f) Finance sees org-wide data: KPIs non-empty with values
# ---------------------------------------------------------------------------


class TestRecordScopingFinance:
    def test_record_scoping_finance_sees_all(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance KPIs are non-empty and carry a meaningful value."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200, r.text
        fin = r.json()["finance"]

        kpis = fin["kpis"]
        assert len(kpis) > 0

        # Every KPI must have a label and a numeric value — except an average with
        # nothing to average yet (product gap 1: the day-based cycle-time KPIs
        # are null, rendered "—", never a misleading 0).
        for kpi in kpis:
            assert "label" in kpi
            assert "value" in kpi
            if kpi["value"] is None:
                assert kpi["unit"] == "days", kpi
            else:
                assert isinstance(kpi["value"], int | float)
        labels = [k["label"] for k in kpis]
        assert "Avg Cycle Time" in labels and "Avg Time to Decision" in labels


# ---------------------------------------------------------------------------
# (g) Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    def test_cross_tenant_isolation(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance user from org B sees only org B's data, not org A's reports."""
        headers_b = _bearer(seeded["finance_b_id"], seeded["org_b_id"])
        r_b = client.get("/api/v1/analytics/overview", headers=headers_b)
        assert r_b.status_code == 200, r_b.text
        fin_b = r_b.json()["finance"]
        assert fin_b is not None

        # Also fetch org A's finance view for comparison
        headers_a = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r_a = client.get("/api/v1/analytics/overview", headers=headers_a)
        assert r_a.status_code == 200
        fin_a = r_a.json()["finance"]

        # Org B has 1 approved report (total_amount=999.00).
        # Org A has 0 approved/reimbursed reports (both are "submitted").
        # The Consolidated Spend KPI for org B must not equal org A's.
        # More specifically, they must not share the same spend_over_time list,
        # which is derived per-org.
        kpis_a = {k["label"]: k["value"] for k in fin_a["kpis"]}
        kpis_b = {k["label"]: k["value"] for k in fin_b["kpis"]}

        # Org A has no approved reports so consolidated spend = 0
        # Org B has one approved report (999.00) so consolidated spend > 0
        assert (
            "Consolidated Spend" in kpis_a
        ), "Expected 'Consolidated Spend' KPI in org A finance response"
        assert (
            "Consolidated Spend" in kpis_b
        ), "Expected 'Consolidated Spend' KPI in org B finance response"
        assert (
            kpis_a["Consolidated Spend"] != kpis_b["Consolidated Spend"]
        ), "Org A and Org B show the same consolidated spend — cross-tenant isolation broken"


# ---------------------------------------------------------------------------
# (h) Static: no LLM imports in analytics service
# ---------------------------------------------------------------------------


def test_no_llm_import_in_request_path() -> None:
    """Ensure analytics service.py does not import LLM libraries at module level."""
    import ast
    import pathlib

    src = pathlib.Path("backend/app/modules/analytics/service.py").read_text()
    tree = ast.parse(src)
    llm_modules = {"anthropic", "openai", "langchain"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert not any(
                    llm in (name or "") for llm in llm_modules
                ), f"LLM import found: {name}"
