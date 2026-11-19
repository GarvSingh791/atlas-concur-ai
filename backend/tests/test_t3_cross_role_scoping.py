"""P6-T3 Adversarial cross-role access test suite.

Proves every row of the P6-T3 Scoping Matrix — trips, expense reports, receipts,
approvals, analytics, users, policies, conversations — across the role graph:

  Org1: employee_a (→mgr_a), employee_b (→mgr_b), manager_a, manager_b, finance_f
  Org2: user_org2

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
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
from database.models.audit_log import AuditLog
from database.models.conversation import Conversation
from database.models.conversation_message import ConversationMessage
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
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
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
    cast(Table, TripSegment.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, Approval.__table__),
    cast(Table, Policy.__table__),
    cast(Table, PolicyRule.__table__),
]

# ---------------------------------------------------------------------------
# Unique email / name constants (isolated from other test modules)
# ---------------------------------------------------------------------------

_ORG1_NAME = "T3-Scoping-Org1"
_ORG2_NAME = "T3-Scoping-Org2"
_EMP_A_EMAIL = "t3-scoping-emp-a@test.internal"
_EMP_B_EMAIL = "t3-scoping-emp-b@test.internal"
_MGR_A_EMAIL = "t3-scoping-mgr-a@test.internal"
_MGR_B_EMAIL = "t3-scoping-mgr-b@test.internal"
_FINANCE_EMAIL = "t3-scoping-finance@test.internal"
_ORG2_USER_EMAIL = "t3-scoping-org2-user@test.internal"
_TEST_PW = "test-password-t3-scoping"

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
        pytest.skip("Postgres not reachable — skipping T3 cross-role scoping tests")

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
    """Seed RBAC + Org1 (5 users + records) + Org2 (1 user + trip)."""
    factory = get_sessionmaker()

    all_emails = [
        _EMP_A_EMAIL,
        _EMP_B_EMAIL,
        _MGR_A_EMAIL,
        _MGR_B_EMAIL,
        _FINANCE_EMAIL,
        _ORG2_USER_EMAIL,
    ]

    # Prune stale data from a previous incomplete run (FK-safe order).
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(Approval).filter(Approval.organization_id.in_(stale_org_ids)).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            # Expense entries cascade with reports; delete reports to cascade
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            # PolicyRules cascade with Policies; delete policies to cascade
            session.query(Policy).filter(Policy.organization_id.in_(stale_org_ids)).delete()
            # Delete conversations and messages scoped to stale org_ids
            stale_conv_ids_q = session.query(Conversation.id).filter(
                Conversation.organization_id.in_(stale_org_ids)
            )
            session.query(ConversationMessage).filter(
                ConversationMessage.conversation_id.in_(stale_conv_ids_q)
            ).delete(synchronize_session=False)
            session.query(Conversation).filter(
                Conversation.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        # -- Organizations --
        org1 = Organization(name=_ORG1_NAME, base_currency="USD")
        org2 = Organization(name=_ORG2_NAME, base_currency="EUR")
        session.add_all([org1, org2])
        session.flush()

        hpw = hash_password(_TEST_PW)

        # -- Org1 users --
        mgr_a = User(
            organization_id=org1.id,
            name="T3 Manager A",
            email=_MGR_A_EMAIL,
            role=RoleKey.manager,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(mgr_a)
        session.flush()

        mgr_b = User(
            organization_id=org1.id,
            name="T3 Manager B",
            email=_MGR_B_EMAIL,
            role=RoleKey.manager,
            department="Sales",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(mgr_b)
        session.flush()

        emp_a = User(
            organization_id=org1.id,
            name="T3 Employee A",
            email=_EMP_A_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=mgr_a.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(emp_a)
        session.flush()

        emp_b = User(
            organization_id=org1.id,
            name="T3 Employee B",
            email=_EMP_B_EMAIL,
            role=RoleKey.employee,
            department="Sales",
            manager_id=mgr_b.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(emp_b)
        session.flush()

        finance_f = User(
            organization_id=org1.id,
            name="T3 Finance F",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_f)
        session.flush()

        # -- Org2 user --
        user_org2 = User(
            organization_id=org2.id,
            name="T3 Org2 User",
            email=_ORG2_USER_EMAIL,
            role=RoleKey.employee,
            department="General",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_org2)
        session.flush()

        # -- Trips --
        trip_a = Trip(
            organization_id=org1.id,
            traveler_id=emp_a.id,
            origin="SFO",
            primary_destination="NYC",
            start_date=datetime.date(2026, 7, 1),
            end_date=datetime.date(2026, 7, 5),
            currency="USD",
            status=TripStatus.draft,
            created_by=emp_a.id,
            updated_by=emp_a.id,
        )
        session.add(trip_a)
        session.flush()

        seg_a = TripSegment(
            trip_id=trip_a.id,
            segment_type=SegmentType.flight,
            supplier="United",
            currency="USD",
        )
        session.add(seg_a)

        trip_b = Trip(
            organization_id=org1.id,
            traveler_id=emp_b.id,
            origin="LAX",
            primary_destination="CHI",
            start_date=datetime.date(2026, 7, 10),
            end_date=datetime.date(2026, 7, 12),
            currency="USD",
            status=TripStatus.draft,
            created_by=emp_b.id,
            updated_by=emp_b.id,
        )
        session.add(trip_b)

        trip_org2 = Trip(
            organization_id=org2.id,
            traveler_id=user_org2.id,
            origin="BER",
            primary_destination="PAR",
            start_date=datetime.date(2026, 7, 20),
            end_date=datetime.date(2026, 7, 22),
            currency="EUR",
            status=TripStatus.draft,
            created_by=user_org2.id,
            updated_by=user_org2.id,
        )
        session.add(trip_org2)
        session.flush()

        # -- Expense Reports + Entries --
        report_a = ExpenseReport(
            organization_id=org1.id,
            owner_id=emp_a.id,
            # submitted: approvals a1/a2 are opened on it, and a decision is only
            # accepted while the subject is awaiting one (C7-13 decide-side guard).
            status=ExpenseReportStatus.submitted,
            title="T3 Report A",
            currency="USD",
            total_amount=Decimal("100.00"),
            created_by=emp_a.id,
            updated_by=emp_a.id,
        )
        session.add(report_a)
        session.flush()

        expense_a = Expense(
            organization_id=org1.id,
            report_id=report_a.id,
            category=ExpenseCategory.meals,
            amount=Decimal("100.00"),
            currency="USD",
            expense_date=datetime.date(2026, 7, 1),
            status=ExpenseStatus.draft,
            created_by=emp_a.id,
            updated_by=emp_a.id,
        )
        session.add(expense_a)

        report_b = ExpenseReport(
            organization_id=org1.id,
            owner_id=emp_b.id,
            status=ExpenseReportStatus.draft,
            title="T3 Report B",
            currency="USD",
            total_amount=Decimal("50.00"),
            created_by=emp_b.id,
            updated_by=emp_b.id,
        )
        session.add(report_b)
        session.flush()

        expense_b = Expense(
            organization_id=org1.id,
            report_id=report_b.id,
            category=ExpenseCategory.meals,
            amount=Decimal("50.00"),
            currency="USD",
            expense_date=datetime.date(2026, 7, 10),
            status=ExpenseStatus.draft,
            created_by=emp_b.id,
            updated_by=emp_b.id,
        )
        session.add(expense_b)
        session.flush()

        # -- Receipt --
        receipt_a = Receipt(
            organization_id=org1.id,
            uploaded_by=emp_a.id,
            image_url="s3://test-bucket/receipt-a.jpg",
            ocr_status=OcrStatus.pending,
        )
        session.add(receipt_a)
        session.flush()

        # -- Approvals --
        # approval_a1: for GET read tests + M_B's 404 test — must stay pending
        approval_a1 = Approval(
            organization_id=org1.id,
            submitter_id=emp_a.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_a.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
        )
        session.add(approval_a1)

        # approval_a2: consumed by M_A's successful PATCH test
        approval_a2 = Approval(
            organization_id=org1.id,
            submitter_id=emp_a.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_a.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
        )
        session.add(approval_a2)

        # approval_b1: consumed by Finance's PATCH test + Finance's list test
        approval_b1 = Approval(
            organization_id=org1.id,
            submitter_id=emp_b.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_b.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
        )
        session.add(approval_b1)

        # approval_b2: for M_A's 404 test — must stay pending
        approval_b2 = Approval(
            organization_id=org1.id,
            submitter_id=emp_b.id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report_b.id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
        )
        session.add(approval_b2)
        session.flush()

        # -- Policy + PolicyRule --
        policy_org1 = Policy(
            organization_id=org1.id,
            name="T3 Test Policy",
            version=1,
            effective_from=datetime.date(2026, 1, 1),
            is_active=True,
            created_by=finance_f.id,
            updated_by=finance_f.id,
        )
        session.add(policy_org1)
        session.flush()

        policy_rule_org1 = PolicyRule(
            policy_id=policy_org1.id,
            rule_type=PolicyRuleType.amount_cap,
            applies_to=PolicyAppliesTo.meals,
            definition={"cap": 50, "currency": "USD"},
            status=PolicyRuleStatus.active,
            created_by=finance_f.id,
            updated_by=finance_f.id,
        )
        session.add(policy_rule_org1)
        session.flush()

        session.commit()

    yield {
        "org1_id": org1.id,
        "org2_id": org2.id,
        "emp_a_id": emp_a.id,
        "emp_b_id": emp_b.id,
        "mgr_a_id": mgr_a.id,
        "mgr_b_id": mgr_b.id,
        "finance_id": finance_f.id,
        "user_org2_id": user_org2.id,
        "trip_a_id": trip_a.id,
        "trip_b_id": trip_b.id,
        "trip_org2_id": trip_org2.id,
        "report_a_id": report_a.id,
        "report_b_id": report_b.id,
        "expense_a_id": expense_a.id,
        "expense_b_id": expense_b.id,
        "receipt_a_id": receipt_a.id,
        "approval_a1_id": approval_a1.id,
        "approval_a2_id": approval_a2.id,
        "approval_b1_id": approval_b1.id,
        "approval_b2_id": approval_b2.id,
        "policy_org1_id": policy_org1.id,
        "policy_rule_org1_id": policy_rule_org1.id,
    }

    # Teardown — delete in FK-safe order
    with factory() as session:
        org_ids = [org1.id, org2.id]
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(Policy).filter(Policy.organization_id.in_(org_ids)).delete()
        conv_ids_q = session.query(Conversation.id).filter(
            Conversation.organization_id.in_(org_ids)
        )
        session.query(ConversationMessage).filter(
            ConversationMessage.conversation_id.in_(conv_ids_q)
        ).delete(synchronize_session=False)
        session.query(Conversation).filter(Conversation.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


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
# TestTripScoping
# ---------------------------------------------------------------------------


class TestTripScoping:
    def test_employee_own_trip_list(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A sees only own trips."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/trips", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["trip_a_id"]) in ids
        assert str(seeded["trip_b_id"]) not in ids

    def test_employee_cross_owner_trip_404(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee A cannot see Employee B's trip — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/trips/{seeded['trip_b_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_employee_cross_org_trip_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A cannot see Org2 trip (cross-tenant) — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/trips/{seeded['trip_org2_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_manager_a_sees_own_and_reports_trips(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager A sees own trips + Employee A's trips, but NOT Employee B's."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/trips", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["trip_a_id"]) in ids  # A is M_A's direct report
        assert str(seeded["trip_b_id"]) not in ids  # B is NOT M_A's direct report

    def test_manager_b_cannot_see_trip_a(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Manager B cannot see Employee A's trip (different team) — 404."""
        headers = _bearer(seeded["mgr_b_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/trips/{seeded['trip_a_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_finance_sees_all_org_trips(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance sees all trips in Org1, not Org2."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get("/api/v1/trips", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["trip_a_id"]) in ids
        assert str(seeded["trip_b_id"]) in ids
        assert str(seeded["trip_org2_id"]) not in ids  # different org

    def test_segments_cross_owner_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A cannot see segments of Employee B's trip — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/trips/{seeded['trip_b_id']}/segments", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_patch_trip_finance_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance cannot PATCH a trip (lacks trip.update.own permission) — 403."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/trips/{seeded['trip_a_id']}",
            json={"status": "pending_approval"},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_patch_trip_cross_owner_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A cannot PATCH Employee B's trip — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/trips/{seeded['trip_b_id']}",
            json={"status": "pending_approval"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# TestExpenseScoping
# ---------------------------------------------------------------------------


class TestExpenseScoping:
    def test_employee_cross_owner_report_404(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee A cannot GET Employee B's expense report — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_b_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_employee_cross_owner_entries_404(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee A cannot GET entries of Employee B's expense report — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_b_id']}/entries", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_manager_b_cannot_see_report_a(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager B cannot GET Employee A's report (different team) — 404."""
        headers = _bearer(seeded["mgr_b_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_a_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_finance_can_see_all_reports(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance can see all reports in Org1."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get("/api/v1/expense-reports", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["report_a_id"]) in ids
        assert str(seeded["report_b_id"]) in ids

    def test_patch_report_finance_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance cannot PATCH an expense report (lacks expense.update.own) — 403."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/expense-reports/{seeded['report_a_id']}",
            json={"status": "submitted"},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_patch_report_cross_owner_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A cannot PATCH Employee B's report — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/expense-reports/{seeded['report_b_id']}",
            json={"status": "submitted"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_patch_expense_cross_owner_404(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee A cannot PATCH an expense entry in Employee B's report — 404."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/expenses/{seeded['expense_b_id']}",
            json={"description": "Hacked"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_patch_expense_finance_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance cannot PATCH an expense entry (lacks expense.update.own) — 403."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/expenses/{seeded['expense_a_id']}",
            json={"description": "Hacked"},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN


# ---------------------------------------------------------------------------
# TestReceiptScoping
# ---------------------------------------------------------------------------


class TestReceiptScoping:
    def test_patch_receipt_cross_owner_404(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee B cannot PATCH Employee A's receipt — 404 (uploader-scoped)."""
        headers = _bearer(seeded["emp_b_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/receipts/{seeded['receipt_a_id']}",
            json={"field": "vendor", "value": "Hacked"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_patch_receipt_manager_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Manager A cannot PATCH Employee A's receipt (receipts are uploader-only) — 404."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/receipts/{seeded['receipt_a_id']}",
            json={"field": "vendor", "value": "Hacked"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_patch_receipt_finance_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance cannot PATCH a receipt (lacks expense.update.own) — 403."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/receipts/{seeded['receipt_a_id']}",
            json={"field": "vendor", "value": "Hacked"},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN


# ---------------------------------------------------------------------------
# TestApprovalScoping
# Note: "cannot PATCH" tests (→ 404) run BEFORE "can PATCH" (→ 200) so that
# approval_a1 and approval_b2 remain in pending state for GET read assertions.
# ---------------------------------------------------------------------------


class TestApprovalScoping:
    def test_employee_approvals_list_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee has no approval.read permission → 403."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/approvals", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_employee_approval_detail_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee cannot GET approval detail (no approval.read perm) → 403."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/approvals/{seeded['approval_a1_id']}", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_manager_b_cannot_see_approval_a(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager B cannot see Employee A's approval (different team) — 404."""
        headers = _bearer(seeded["mgr_b_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/approvals/{seeded['approval_a1_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_manager_a_can_see_approval_a(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Manager A can see Employee A's approval (A is M_A's direct report) — 200."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/approvals/{seeded['approval_a1_id']}", headers=headers)
        assert r.status_code == 200

    def test_finance_can_see_all_approvals(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance can see all approvals in Org1 — both A and B."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get("/api/v1/approvals", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["approval_a1_id"]) in ids
        assert str(seeded["approval_b1_id"]) in ids

    def test_manager_b_cannot_patch_approval_a(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager B cannot PATCH Employee A's approval (different team) — 404."""
        headers = _bearer(seeded["mgr_b_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/approvals/{seeded['approval_a1_id']}",
            json={"decision": "approve", "reason": "ok"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_manager_a_cannot_patch_approval_b(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager A cannot PATCH Employee B's approval (B is not M_A's report) — 404."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/approvals/{seeded['approval_b2_id']}",
            json={"decision": "approve", "reason": "ok"},
            headers=headers,
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_manager_a_can_patch_approval_a(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager A CAN PATCH Employee A's approval (A is M_A's direct report) — 200."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/approvals/{seeded['approval_a2_id']}",
            json={"decision": "approve", "reason": "approved by manager a"},
            headers=headers,
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# TestAnalyticsScoping
# ---------------------------------------------------------------------------


class TestAnalyticsScoping:
    def test_employee_gets_own_analytics(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee gets 200 — own overview structure."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200

    def test_manager_gets_team_analytics(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Manager A gets 200 — team overview."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200

    def test_analytics_manager_approval_queue_flag(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """FLAG: Manager B's approval_queue may surface Employee A's data (org-wide).

        Per plan: test and flag, do NOT auto-fix. Uses pytest.xfail if the org-wide
        leak is confirmed (not a test failure — a documented finding for Garv).
        """
        headers = _bearer(seeded["mgr_b_id"], seeded["org1_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200
        body = r.json()
        queue = body.get("approval_queue", [])
        queue_ids = [item.get("id") for item in queue]
        if str(seeded["approval_a1_id"]) in queue_ids:
            pytest.xfail(
                "ANALYTICS_FLAG: Manager B sees Employee A's approval in org-wide queue "
                "(analytics/service.py _build_manager_overview: 'managers approve org-wide'). "
                "PRD says team-only analytics. Flagged for Garv's decision — not auto-fixed in T3."
            )

    def test_finance_gets_org_analytics(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance gets 200 — org-wide analytics."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get("/api/v1/analytics/overview", headers=headers)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# TestUsersScoping
# ---------------------------------------------------------------------------


class TestUsersScoping:
    def test_employee_users_list_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee lacks users.read → 403."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_manager_users_list_403(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Manager lacks users.read → 403."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_finance_can_list_users(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance (users.read) can see all Org1 users."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert str(seeded["emp_a_id"]) in ids
        assert str(seeded["emp_b_id"]) in ids

    def test_finance_cross_org_user_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance cannot see Org2 user (cross-tenant) — 404."""
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/users/{seeded['user_org2_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# TestPolicyScoping
# ---------------------------------------------------------------------------


class TestPolicyScoping:
    def test_any_role_can_read_policy(self, seeded: dict[str, Any], client: TestClient) -> None:
        """All roles with policy.read can GET /policies (org-scoped, 200)."""
        for role_key in ["emp_a_id", "mgr_a_id", "finance_id"]:
            headers = _bearer(seeded[role_key], seeded["org1_id"])
            r = client.get("/api/v1/policies", headers=headers)
            assert r.status_code == 200, f"Role {role_key} should be able to read policy"

    def test_policy_authoring_requires_finance_employee_403(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee cannot PATCH policy rules (lacks policy.manage) → 403."""
        headers = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/policy-rules/{seeded['policy_rule_org1_id']}",
            json={"is_active": False},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_policy_authoring_requires_finance_manager_403(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager cannot PATCH policy rules (lacks policy.manage) → 403."""
        headers = _bearer(seeded["mgr_a_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/policy-rules/{seeded['policy_rule_org1_id']}",
            json={"is_active": False},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_finance_can_author_policy(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Finance can PATCH policy rules (has policy.manage) — 200.

        The P5-T9 contract is a lifecycle transition (``status``: active/archived);
        the old ``is_active`` flag no longer exists and is rejected with 422.
        """
        headers = _bearer(seeded["finance_id"], seeded["org1_id"])
        r = client.patch(
            f"/api/v1/policy-rules/{seeded['policy_rule_org1_id']}",
            json={"status": "archived"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "archived"


# ---------------------------------------------------------------------------
# TestConversationsOwnership
# ---------------------------------------------------------------------------


class TestConversationsOwnership:
    def test_conversation_cross_owner_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee A cannot access Employee B's conversation — 404."""
        # Create conv as employee_b
        headers_b = _bearer(seeded["emp_b_id"], seeded["org1_id"])
        r = client.post(
            "/api/v1/conversations", json={"context_type": "general"}, headers=headers_b
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]

        # Employee A tries to access it
        headers_a = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.get(f"/api/v1/conversations/{conv_id}", headers=headers_a)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_conversation_cross_org_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Org2 user cannot access Org1 conversation — 404."""
        headers_a = _bearer(seeded["emp_a_id"], seeded["org1_id"])
        r = client.post(
            "/api/v1/conversations", json={"context_type": "general"}, headers=headers_a
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]

        headers_org2 = _bearer(seeded["user_org2_id"], seeded["org2_id"])
        r = client.get(f"/api/v1/conversations/{conv_id}", headers=headers_org2)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND
