"""P2-T7 Validate gate + Phase 2 DoD + Checkpoint: expense reports + entries CRUD.

Assertions (P2-T7 Validate gate):
  (a) POST /expense-reports with 2 entries → 201 draft, total_amount == sum(entries),
      exactly one 'expense_report.created' audit row; client-supplied total_amount ignored.
  (b) POST /expense-reports with accessible trip_id → ok; out-of-scope trip_id → 400.
  (c) GET /expense-reports scoped by role: employee→own, manager→own+reports, finance→org-wide.
  (d) Role without expense.read.own → 403 standard envelope.
  (e) Cross-org reports never returned; out-of-scope/nonexistent → 404.
  (f) GET /expense-reports/{id}/entries: in-scope → its entries; out-of-scope → 404.
  (g) PATCH /expenses/{id} with amount change → parent total_amount recomputed;
      exactly one 'expense.updated' audit row; non-draft parent → 409.
  (h) PATCH /expense-reports/{id} draft→submitted → 200, submitted_at set, version bumped,
      exactly one 'expense_report.status_changed' row with correct before/after.
  (i) PATCH illegal transitions (draft→approved, reimbursed→draft) → 409, no state change,
      zero new audit rows.
  (j) expenses has NO version/deleted_at; expense_reports has NO deleted_at.
  (k) Migration applies and downgrades cleanly.
  (l) Phase 2 DoD: login → RBAC-protected, scoped, audited CRUD across Users/Trips/Expenses.

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
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
from database.models.ai_decision_log import AiDecisionLog
from database.models.approval import Approval
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
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Scoped tables for create/drop (FK-safe order)
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
]

# ---------------------------------------------------------------------------
# Unique email / name constants (isolated from other test modules)
# ---------------------------------------------------------------------------

_ORG_A_NAME = "Expenses API Test Org A"
_ORG_B_NAME = "Expenses API Test Org B"
_FINANCE_A_EMAIL = "exp-finance-a@test.internal"
_MANAGER_A_EMAIL = "exp-manager-a@test.internal"
_EMPLOYEE_A_EMAIL = "exp-employee-a@test.internal"
_EMPLOYEE_B_EMAIL = "exp-employee-b@test.internal"
_FINANCE_B_EMAIL = "exp-finance-b@test.internal"
_TEST_PW = "test-password-expenses-api"

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
        pytest.skip("Postgres not reachable — skipping expenses API tests")

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
    """Seed RBAC + org A (4 users + pre-seeded reports/trips) + org B (1 user + 1 report)."""
    factory = get_sessionmaker()

    all_emails = [
        _FINANCE_A_EMAIL,
        _MANAGER_A_EMAIL,
        _EMPLOYEE_A_EMAIL,
        _EMPLOYEE_B_EMAIL,
        _FINANCE_B_EMAIL,
    ]

    # Prune stale data from a previous incomplete run (FK-safe order)
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            # Submitting now opens an approval (+ agent decision logs).
            session.query(Approval).filter(Approval.organization_id.in_(stale_org_ids)).delete()
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(Expense).filter(Expense.organization_id.in_(stale_org_ids)).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org_a = Organization(name=_ORG_A_NAME, base_currency="USD")
        org_b = Organization(name=_ORG_B_NAME, base_currency="USD")
        session.add_all([org_a, org_b])
        session.flush()

        hpw = hash_password(_TEST_PW)

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
            name="Employee B (report of manager_a)",
            email=_EMPLOYEE_B_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager_a.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee_b)
        session.flush()

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

        # Pre-seed a trip so trip_id scope tests can use it
        trip_employee_a = Trip(
            organization_id=org_a.id,
            traveler_id=employee_a.id,
            origin="NYC",
            primary_destination="LON",
            start_date="2026-07-01",
            end_date="2026-07-10",
            currency="USD",
            status=TripStatus.draft,
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        session.add(trip_employee_a)
        session.flush()

        # Cross-org trip (org B) — should be inaccessible to org A users
        trip_org_b = Trip(
            organization_id=org_b.id,
            traveler_id=finance_b.id,
            origin="BER",
            primary_destination="ROM",
            start_date="2026-10-01",
            end_date="2026-10-05",
            currency="EUR",
            status=TripStatus.draft,
            created_by=finance_b.id,
            updated_by=finance_b.id,
        )
        session.add(trip_org_b)
        session.flush()

        # Pre-seed reports for list/get/scoping tests
        report_employee_a = ExpenseReport(
            organization_id=org_a.id,
            owner_id=employee_a.id,
            title="Employee A Q3 Expenses",
            currency="USD",
            status=ExpenseReportStatus.draft,
            total_amount=0,
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        session.add(report_employee_a)
        session.flush()

        entry_a1 = Expense(
            organization_id=org_a.id,
            report_id=report_employee_a.id,
            category=ExpenseCategory.meals,
            amount="45.00",
            currency="USD",
            expense_date="2026-07-05",
            status=ExpenseStatus.draft,
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        entry_a2 = Expense(
            organization_id=org_a.id,
            report_id=report_employee_a.id,
            category=ExpenseCategory.ground_transport,
            amount="30.00",
            currency="USD",
            expense_date="2026-07-06",
            status=ExpenseStatus.draft,
            created_by=employee_a.id,
            updated_by=employee_a.id,
        )
        session.add_all([entry_a1, entry_a2])
        session.flush()
        report_employee_a.total_amount = "75.00"

        report_employee_b = ExpenseReport(
            organization_id=org_a.id,
            owner_id=employee_b.id,
            title="Employee B Q3 Expenses",
            currency="USD",
            status=ExpenseReportStatus.draft,
            total_amount=0,
            created_by=employee_b.id,
            updated_by=employee_b.id,
        )
        session.add(report_employee_b)

        report_manager_a = ExpenseReport(
            organization_id=org_a.id,
            owner_id=manager_a.id,
            title="Manager A Conference",
            currency="USD",
            status=ExpenseReportStatus.submitted,
            total_amount="200.00",
            submitted_at="2026-06-01T00:00:00+00:00",
            created_by=manager_a.id,
            updated_by=manager_a.id,
        )
        session.add(report_manager_a)

        # Cross-org report (org B) — should never appear in org A queries
        report_org_b = ExpenseReport(
            organization_id=org_b.id,
            owner_id=finance_b.id,
            title="Org B Report",
            currency="EUR",
            status=ExpenseReportStatus.draft,
            total_amount=0,
            created_by=finance_b.id,
            updated_by=finance_b.id,
        )
        session.add(report_org_b)

        session.commit()

    yield {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "finance_a_id": finance_a.id,
        "manager_a_id": manager_a.id,
        "employee_a_id": employee_a.id,
        "employee_b_id": employee_b.id,
        "finance_b_id": finance_b.id,
        "trip_employee_a_id": trip_employee_a.id,
        "trip_org_b_id": trip_org_b.id,
        "report_employee_a_id": report_employee_a.id,
        "report_employee_b_id": report_employee_b.id,
        "report_manager_a_id": report_manager_a.id,
        "report_org_b_id": report_org_b.id,
        "entry_a1_id": entry_a1.id,
        "entry_a2_id": entry_a2.id,
    }

    # Teardown — delete in FK-safe order
    with factory() as session:
        org_ids = [org_a.id, org_b.id]
        # Submitting now opens an approval (+ agent decision logs).
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# UAT2 (M12): an empty report is refused at submit (REPORT_EMPTY), so reports
# these tests submit carry one line — over the auto-approve ceiling, so the
# report stays "submitted" (escalated to a human) as these tests expect.
_SUBMITTABLE_ENTRY = {
    "category": "other",
    "amount": "750.00",
    "currency": "USD",
    "expense_date": "2026-06-10",
    "vendor": "Corner Deli",
}


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


def _audit_count(engine: Engine, entity_id: uuid.UUID, action: str) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE entity_id = :eid AND action = :action"),
            {"eid": str(entity_id), "action": action},
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# (a) POST /expense-reports — create with entries, derived total, ignored total
# ---------------------------------------------------------------------------


class TestCreateReport:
    def test_create_with_two_entries_derives_total(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee creates report with 2 entries → 201 draft; total_amount == sum of entries."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {
            "title": "Q4 Business Trip",
            "currency": "USD",
            "entries": [
                {
                    "category": "airfare",
                    "amount": "500.00",
                    "currency": "USD",
                    "expense_date": "2026-10-01",
                },
                {
                    "category": "lodging",
                    "amount": "300.00",
                    "currency": "USD",
                    "expense_date": "2026-10-02",
                },
            ],
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "draft"
        assert body["owner_id"] == str(seeded["employee_a_id"])
        assert body["organization_id"] == str(seeded["org_a_id"])
        assert body["version"] == 1
        # total_amount == sum of entries (not a client value)
        assert float(body["total_amount"]) == pytest.approx(800.00)
        report_id = uuid.UUID(body["id"])
        assert _audit_count(db_engine, report_id, "expense_report.created") == 1

    def test_client_supplied_total_amount_is_ignored(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """If client sends total_amount it should be ignored — derived from entries only."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        # Post with a single entry of 100 USD; total should be 100, not any client value
        payload = {
            "title": "Should derive total",
            "currency": "USD",
            "total_amount": "99999.00",  # client-supplied — must be ignored
            "entries": [
                {
                    "category": "meals",
                    "amount": "100.00",
                    "currency": "USD",
                    "expense_date": "2026-10-05",
                }
            ],
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 201, r.text
        body = r.json()
        # total is derived, not the client value
        assert float(body["total_amount"]) == pytest.approx(100.00)

    def test_create_without_entries(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Create report with no entries → total_amount == 0."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {"title": "Empty report", "currency": "USD"}
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 201, r.text
        assert float(r.json()["total_amount"]) == 0.0

    def test_finance_cannot_create(self, seeded: dict[str, Any], client: TestClient) -> None:
        """finance_admin lacks expense.create.own → 403."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        payload = {"title": "Finance report", "currency": "USD"}
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN


# ---------------------------------------------------------------------------
# (b) trip_id scope validation
# ---------------------------------------------------------------------------


class TestTripIdValidation:
    def test_accessible_trip_id_accepted(self, seeded: dict[str, Any], client: TestClient) -> None:
        """POST with an accessible trip_id → 201."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {
            "title": "Trip-linked report",
            "currency": "USD",
            "trip_id": str(seeded["trip_employee_a_id"]),
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 201, r.text
        assert r.json()["trip_id"] == str(seeded["trip_employee_a_id"])

    def test_out_of_scope_trip_id_rejected(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST with a cross-org trip_id → 400 (no existence leak)."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {
            "title": "Cross-org trip link",
            "currency": "USD",
            "trip_id": str(seeded["trip_org_b_id"]),
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST

    def test_nonexistent_trip_id_rejected(self, seeded: dict[str, Any], client: TestClient) -> None:
        """POST with a random UUID trip_id → 400 (no existence leak)."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {
            "title": "Ghost trip link",
            "currency": "USD",
            "trip_id": str(uuid.uuid4()),
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# (c/d/e) GET /expense-reports — scoping per role
# ---------------------------------------------------------------------------


class TestListReports:
    def test_employee_sees_only_own(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee only sees their own reports."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/expense-reports", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"items", "total", "page", "size", "pages"}
        for item in body["items"]:
            assert item["owner_id"] == str(seeded["employee_a_id"])

    def test_manager_sees_self_and_reports(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager sees own reports + direct reports', not unrelated employees."""
        headers = _bearer(seeded["manager_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/expense-reports", headers=headers)
        assert r.status_code == 200
        items = r.json()["items"]
        for item in items:
            assert item["organization_id"] == str(seeded["org_a_id"])
            oid = item["owner_id"]
            assert oid in (
                str(seeded["manager_a_id"]),
                str(seeded["employee_a_id"]),
                str(seeded["employee_b_id"]),
            ), f"Manager should not see owner {oid}"

    def test_finance_sees_org_wide(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance admin sees all org-A reports; no org-B reports."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/expense-reports", headers=headers)
        assert r.status_code == 200
        items = r.json()["items"]
        for item in items:
            assert item["organization_id"] == str(seeded["org_a_id"])
        org_b_report_id = str(seeded["report_org_b_id"])
        assert not any(item["id"] == org_b_report_id for item in items)

    def test_missing_read_permission_returns_403(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """finance_admin has expense.read.own; an unauthenticated call → 401."""
        r = client.get("/api/v1/expense-reports")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == ErrorCode.UNAUTHORIZED

    def test_paginated_response_shape(self, seeded: dict[str, Any], client: TestClient) -> None:
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/expense-reports?page=1&size=2", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "items" in body and "total" in body and "pages" in body
        assert body["page"] == 1


# ---------------------------------------------------------------------------
# (e) GET /expense-reports/{id} — scoping + 404
# ---------------------------------------------------------------------------


class TestGetReport:
    def test_in_scope_200(self, seeded: dict[str, Any], client: TestClient) -> None:
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_employee_a_id']}", headers=headers)
        assert r.status_code == 200
        assert r.json()["id"] == str(seeded["report_employee_a_id"])

    def test_out_of_scope_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee tries to fetch another employee's report → 404 (no existence leak)."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_employee_b_id']}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_nonexistent_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/expense-reports/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404

    def test_cross_org_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Org-A finance cannot see org-B report → 404."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/expense-reports/{seeded['report_org_b_id']}", headers=headers)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# (f) GET /expense-reports/{id}/entries
# ---------------------------------------------------------------------------


class TestGetReportEntries:
    def test_in_scope_returns_entries(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee fetches entries for own report → list with correct data."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(
            f"/api/v1/expense-reports/{seeded['report_employee_a_id']}/entries",
            headers=headers,
        )
        assert r.status_code == 200
        entries = r.json()
        assert isinstance(entries, list)
        assert len(entries) == 2
        for e in entries:
            assert e["report_id"] == str(seeded["report_employee_a_id"])

    def test_entries_carry_linked_receipt_id(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Each entry exposes its earliest linked receipt id (null when unlinked).

        Owner and the owner's direct manager both see it — the id is what lets a
        reviewer fetch the scoped, signed image link via GET /receipts/{id}.
        """
        factory = get_sessionmaker()
        with factory() as session:
            older = Receipt(
                organization_id=seeded["org_a_id"],
                uploaded_by=seeded["employee_a_id"],
                expense_id=seeded["entry_a1_id"],
                image_url="org/older",
                ocr_status=OcrStatus.pending,
                created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
            newer = Receipt(
                organization_id=seeded["org_a_id"],
                uploaded_by=seeded["employee_a_id"],
                expense_id=seeded["entry_a1_id"],
                image_url="org/newer",
                ocr_status=OcrStatus.pending,
                created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            )
            session.add_all([newer, older])
            session.commit()
            receipt_ids = [older.id, newer.id]
            older_id = older.id
        try:
            for user_key in ("employee_a_id", "manager_a_id"):
                headers = _bearer(seeded[user_key], seeded["org_a_id"])
                r = client.get(
                    f"/api/v1/expense-reports/{seeded['report_employee_a_id']}/entries",
                    headers=headers,
                )
                assert r.status_code == 200, r.text
                by_id = {e["id"]: e for e in r.json()}
                assert by_id[str(seeded["entry_a1_id"])]["receipt_id"] == str(older_id)
                assert by_id[str(seeded["entry_a2_id"])]["receipt_id"] is None
        finally:
            with factory() as session:
                session.query(Receipt).filter(Receipt.id.in_(receipt_ids)).delete()
                session.commit()

    def test_out_of_scope_parent_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee fetches entries for another employee's report → 404."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(
            f"/api/v1/expense-reports/{seeded['report_employee_b_id']}/entries",
            headers=headers,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# (g) PATCH /expenses/{id} — entry update + total recompute
# ---------------------------------------------------------------------------


class TestUpdateEntry:
    def test_amount_change_recomputes_total(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """PATCH entry amount → parent total_amount recomputed; audit row written."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])

        # Get current total
        r = client.get(f"/api/v1/expense-reports/{seeded['report_employee_a_id']}", headers=headers)
        original_total = float(r.json()["total_amount"])

        audit_before = _audit_count(db_engine, seeded["entry_a1_id"], "expense.updated")

        # Change entry_a1 amount from 45 to 60
        r2 = client.patch(
            f"/api/v1/expenses/{seeded['entry_a1_id']}",
            json={"amount": "60.00"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert float(r2.json()["amount"]) == pytest.approx(60.00)

        # Parent total should be recomputed (was 75 = 45+30; now 60+30=90)
        r3 = client.get(
            f"/api/v1/expense-reports/{seeded['report_employee_a_id']}", headers=headers
        )
        new_total = float(r3.json()["total_amount"])
        assert new_total == pytest.approx(original_total - 45.00 + 60.00)

        assert _audit_count(db_engine, seeded["entry_a1_id"], "expense.updated") == audit_before + 1

    def test_edit_entry_on_non_draft_report_409(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Edit entry on a submitted report → 409."""
        # report_manager_a is already submitted
        # First create an entry on it via direct DB insert (it's submitted so POST can't add to it)
        factory = get_sessionmaker()
        with factory() as session:
            entry = Expense(
                organization_id=seeded["org_a_id"],
                report_id=seeded["report_manager_a_id"],
                category=ExpenseCategory.meals,
                amount="25.00",
                currency="USD",
                expense_date="2026-06-02",
                status=ExpenseStatus.draft,
                created_by=seeded["manager_a_id"],
                updated_by=seeded["manager_a_id"],
            )
            session.add(entry)
            session.commit()
            submitted_entry_id = entry.id

        headers = _bearer(seeded["manager_a_id"], seeded["org_a_id"])
        r = client.patch(
            f"/api/v1/expenses/{submitted_entry_id}",
            json={"amount": "50.00"},
            headers=headers,
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_out_of_scope_entry_404(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Employee cannot edit another employee's entry → 404."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        # entry_a2 belongs to report_employee_a which belongs to employee_a → OK
        # But try a completely random uuid
        r = client.patch(
            f"/api/v1/expenses/{uuid.uuid4()}",
            json={"amount": "10.00"},
            headers=headers,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# (h) PATCH /expense-reports/{id} — legal transition
# ---------------------------------------------------------------------------


class TestLegalTransition:
    def test_draft_to_submitted(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee transitions draft → submitted → 200; submitted_at set; audit row written."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        # Create a fresh draft report
        r = client.post(
            "/api/v1/expense-reports",
            json={"title": "Submit test", "currency": "USD", "entries": [_SUBMITTABLE_ENTRY]},
            headers=headers,
        )
        assert r.status_code == 201
        report_id = r.json()["id"]
        old_version = r.json()["version"]

        audit_before = _audit_count(
            db_engine, uuid.UUID(report_id), "expense_report.status_changed"
        )

        r2 = client.patch(
            f"/api/v1/expense-reports/{report_id}",
            json={"status": "submitted"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "submitted"
        assert body["version"] == old_version + 1
        assert body["submitted_at"] is not None

        assert (
            _audit_count(db_engine, uuid.UUID(report_id), "expense_report.status_changed")
            == audit_before + 1
        )

    def test_audit_before_after_content(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Audit row for a transition has correct before/after status."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/expense-reports",
            json={
                "title": "Audit content test",
                "currency": "USD",
                "entries": [_SUBMITTABLE_ENTRY],
            },
            headers=headers,
        )
        assert r.status_code == 201
        report_id = r.json()["id"]

        r2 = client.patch(
            f"/api/v1/expense-reports/{report_id}",
            json={"status": "submitted"},
            headers=headers,
        )
        assert r2.status_code == 200

        from sqlalchemy import text as sa_text

        with db_engine.connect() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT before_state, after_state FROM audit_log "
                    "WHERE entity_id = :eid AND action = 'expense_report.status_changed'"
                ),
                {"eid": report_id},
            ).fetchone()

        assert row is not None
        assert row[0]["status"] == "draft"
        assert row[1]["status"] == "submitted"


# ---------------------------------------------------------------------------
# (i) Illegal transitions → 409, no state change, no audit
# ---------------------------------------------------------------------------


class TestIllegalTransitions:
    def _make_draft(self, client: TestClient, headers: dict) -> str:
        r = client.post(
            "/api/v1/expense-reports",
            json={"title": "Illegal transition test", "currency": "USD"},
            headers=headers,
        )
        assert r.status_code == 201
        return r.json()["id"]

    def test_draft_to_approved_409(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """draft → approved is illegal → 409, status unchanged, no audit row."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        report_id = self._make_draft(client, headers)

        audit_before = _audit_count(
            db_engine, uuid.UUID(report_id), "expense_report.status_changed"
        )

        r = client.patch(
            f"/api/v1/expense-reports/{report_id}",
            json={"status": "approved"},
            headers=headers,
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.CONFLICT

        # Status unchanged
        r2 = client.get(f"/api/v1/expense-reports/{report_id}", headers=headers)
        assert r2.json()["status"] == "draft"
        assert (
            _audit_count(db_engine, uuid.UUID(report_id), "expense_report.status_changed")
            == audit_before
        )

    def test_reimbursed_to_draft_409(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """reimbursed → draft is illegal → 409."""
        # Seed a reimbursed report directly
        factory = get_sessionmaker()
        with factory() as session:
            reimb_report = ExpenseReport(
                organization_id=seeded["org_a_id"],
                owner_id=seeded["employee_a_id"],
                title="Reimbursed report",
                currency="USD",
                status=ExpenseReportStatus.reimbursed,
                total_amount=0,
                created_by=seeded["employee_a_id"],
                updated_by=seeded["employee_a_id"],
            )
            session.add(reimb_report)
            session.commit()
            reimb_id = reimb_report.id

        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.patch(
            f"/api/v1/expense-reports/{reimb_id}",
            json={"status": "draft"},
            headers=headers,
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# (j) Structural checks
# ---------------------------------------------------------------------------


class TestStructural:
    def test_expenses_no_version_no_deleted_at(self, db_engine: Engine) -> None:
        """expenses must NOT have version or deleted_at columns."""
        cols = {col["name"] for col in sa_inspect(db_engine).get_columns("expenses")}
        assert "version" not in cols, "expenses must NOT have column 'version'"
        assert "deleted_at" not in cols, "expenses must NOT have column 'deleted_at'"

    def test_expense_reports_no_deleted_at(self, db_engine: Engine) -> None:
        """expense_reports must NOT have deleted_at."""
        cols = {col["name"] for col in sa_inspect(db_engine).get_columns("expense_reports")}
        assert "deleted_at" not in cols, "expense_reports must NOT have column 'deleted_at'"

    def test_expense_reports_has_version(self, db_engine: Engine) -> None:
        """expense_reports must have version column."""
        cols = {col["name"] for col in sa_inspect(db_engine).get_columns("expense_reports")}
        assert "version" in cols

    def test_amount_check_constraint_present(self, db_engine: Engine) -> None:
        """expenses must have amount >= 0 CHECK constraint."""
        constraints = sa_inspect(db_engine).get_check_constraints("expenses")
        names = [c["name"] for c in constraints]
        # The constraint name follows NAMING_CONVENTION: ck_expenses_<name>
        assert any(
            "amount_non_negative" in (n or "") for n in names
        ), f"Expected amount_non_negative CHECK constraint; found: {names}"
