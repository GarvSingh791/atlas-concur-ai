"""Live-DB tests for P3-T4 integrity rules.

Self-skips when Postgres is unreachable — hermetic CI stays green.

Coverage:
  A. Negative amount via API → 422 CHECK violation envelope
  B. Stale If-Match → 409 CONFLICT
  C. Current If-Match → 200, version increments by 1
  D. Same Idempotency-Key → same entity id
  E. Different Idempotency-Keys → different entity ids
  F. Direct DB: negative expense amount INSERT raises IntegrityError
  G. Direct DB: trip_segment end_datetime < start_datetime raises IntegrityError
  H. Direct DB: duplicate users(org, email) raises IntegrityError
  I. Direct DB: second active policy with same (org, name) raises IntegrityError
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.errors import ErrorCode
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from database.models import Base, Policy
from database.models.ai_decision_log import AiDecisionLog
from database.models.approval import Approval
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
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
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
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
    cast(Table, IdempotencyKey.__table__),
]

# Policy table may or may not be present (P3 phase); include it only if available
_POLICY_TABLE: Table = cast(Table, Policy.__table__)

# ---------------------------------------------------------------------------
# Unique email / org constants — isolated from other test modules
# ---------------------------------------------------------------------------

_ORG_A_NAME = "P3T4 Live Test Org A"
_EMPLOYEE_A_EMAIL = "p3t4-employee-a@test.internal"
_TEST_PW = "test-password-p3t4"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping P3-T4 live tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]

    # Include policies table if policy model is available and table not present
    policy_created = False
    if _POLICY_TABLE.name not in existing:
        try:
            Base.metadata.create_all(bind=engine, tables=[_POLICY_TABLE])
            policy_created = True
        except Exception:
            pass  # pgvector extension may be missing; skip policy table creation

    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)
        if policy_created:
            _POLICY_TABLE.drop(bind=engine, checkfirst=True)


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed org + employee user needed for API tests."""
    factory = get_sessionmaker()

    all_emails = [_EMPLOYEE_A_EMAIL]

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
            session.query(IdempotencyKey).filter(
                IdempotencyKey.organization_id.in_(stale_org_ids)
            ).delete()
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
        session.add(org_a)
        session.flush()

        hpw = hash_password(_TEST_PW)

        employee_a = User(
            organization_id=org_a.id,
            name="P3T4 Employee A",
            email=_EMPLOYEE_A_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee_a)
        session.flush()

        # Pre-seed a trip so TripSegment constraint tests have a parent
        trip = Trip(
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
        session.add(trip)
        session.flush()

        session.commit()

    yield {
        "org_a_id": org_a.id,
        "employee_a_id": employee_a.id,
        "trip_id": trip.id,
    }

    # Teardown in FK-safe order
    with factory() as session:
        org_ids = [org_a.id]
        # Prune policies if they exist
        try:
            session.query(Policy).filter(Policy.organization_id.in_(org_ids)).delete()
        except Exception:
            pass
        # Submitting now opens an approval (+ agent decision logs).
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(IdempotencyKey).filter(IdempotencyKey.organization_id.in_(org_ids)).delete()
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


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


def _assert_envelope(body: dict, expected_code: str) -> str:
    """Assert standard envelope exact shape; return correlation_id."""
    assert set(body.keys()) == {"error"}, f"Unexpected top-level keys: {set(body.keys())}"
    error = body["error"]
    assert set(error.keys()) == {
        "code",
        "message",
        "correlation_id",
    }, f"Unexpected error keys: {set(error.keys())}"
    assert error["code"] == expected_code
    assert isinstance(error["message"], str) and error["message"]
    cid = error["correlation_id"]
    assert isinstance(cid, str) and len(cid) > 0
    return cid


# ---------------------------------------------------------------------------
# A. Negative amount via API → 422 CHECK violation envelope
# ---------------------------------------------------------------------------


class TestNegativeAmountCheckViolation:
    def test_negative_amount_entry_returns_422(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST expense report with amount=-5.00 hits DB CHECK → 422 envelope."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        payload = {
            "title": "P3T4 check test",
            "currency": "USD",
            "entries": [
                {
                    "category": "meals",
                    "amount": "-5.00",
                    "currency": "USD",
                    "expense_date": "2026-10-01",
                }
            ],
        }
        r = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r.status_code == 422
        body = r.json()
        _assert_envelope(body, ErrorCode.VALIDATION)


# ---------------------------------------------------------------------------
# B & C. Optimistic lock via API (stale → 409; current → 200 + version bump)
# ---------------------------------------------------------------------------


# UAT2 (M12): an empty report is refused at submit (REPORT_EMPTY).  The line is
# over the auto-approve ceiling, so the report stays "submitted" (escalated).
_ONE_ENTRY = {
    "category": "other",
    "amount": "750.00",
    "currency": "USD",
    "expense_date": "2026-06-10",
    "vendor": "Corner Deli",
}


class TestOptimisticLock:
    def test_stale_if_match_returns_409(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """PATCH with stale If-Match → 409 CONFLICT."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        # Create a fresh draft report
        r = client.post(
            "/api/v1/expense-reports",
            json={"title": "lock test stale", "currency": "USD", "entries": [_ONE_ENTRY]},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        report_id = r.json()["id"]

        # PATCH with deliberately stale version
        patch_r = client.patch(
            f"/api/v1/expense-reports/{report_id}",
            json={"status": "submitted"},
            headers={**headers, "If-Match": "999"},
        )
        assert patch_r.status_code == 409
        _assert_envelope(patch_r.json(), ErrorCode.CONFLICT)

    def test_current_if_match_increments_version(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """PATCH with correct If-Match → 200, version increments by 1."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        # Create a fresh draft report
        r = client.post(
            "/api/v1/expense-reports",
            json={"title": "version test", "currency": "USD", "entries": [_ONE_ENTRY]},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        version = r.json()["version"]
        report_id = r.json()["id"]

        patch_r = client.patch(
            f"/api/v1/expense-reports/{report_id}",
            json={"status": "submitted"},
            headers={**headers, "If-Match": str(version)},
        )
        assert patch_r.status_code == 200, patch_r.text
        assert patch_r.json()["version"] == version + 1


# ---------------------------------------------------------------------------
# D & E. Idempotency via API
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_idempotency_key_returns_same_report(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST with same Idempotency-Key twice → same entity id, exactly one DB row."""
        idem_key = f"test-key-same-{uuid.uuid4().hex}"
        base_headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        headers = {**base_headers, "Idempotency-Key": idem_key}
        payload = {"title": "idem test", "currency": "USD", "entries": []}

        r1 = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        assert r1.status_code == 201, r1.text

        r2 = client.post("/api/v1/expense-reports", json=payload, headers=headers)
        # Replay may return 200 or 201 — the primary assertion is entity identity
        assert r2.status_code in (200, 201), r2.text
        assert r1.json()["id"] == r2.json()["id"]

        # Verify exactly one idempotency_keys row was inserted (not two)
        factory = get_sessionmaker()
        with factory() as session:
            count = session.scalar(
                sa.select(sa.func.count())
                .select_from(IdempotencyKey)
                .where(
                    IdempotencyKey.organization_id == seeded["org_a_id"],
                    IdempotencyKey.scope == "expense_report.create",
                    IdempotencyKey.idempotency_key == idem_key,
                )
            )
        assert count == 1, f"Expected exactly 1 idempotency row, found {count}"

    def test_different_idempotency_key_creates_new_report(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """POST with different Idempotency-Keys → different entity ids."""
        base_headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        key1 = f"unique-key-1-{uuid.uuid4().hex}"
        key2 = f"unique-key-2-{uuid.uuid4().hex}"
        headers1 = {**base_headers, "Idempotency-Key": key1}
        headers2 = {**base_headers, "Idempotency-Key": key2}
        payload = {"title": "two keys test", "currency": "USD", "entries": []}

        r1 = client.post("/api/v1/expense-reports", json=payload, headers=headers1)
        r2 = client.post("/api/v1/expense-reports", json=payload, headers=headers2)
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        assert r1.json()["id"] != r2.json()["id"]


# ---------------------------------------------------------------------------
# F–I. Direct DB constraint violation tests
# ---------------------------------------------------------------------------


class TestDirectDbConstraints:
    def test_negative_expense_amount_raises_integrity_error(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """Direct INSERT: Expense with amount < 0 must raise IntegrityError (CHECK constraint)."""
        factory = get_sessionmaker()

        # First create a parent report to satisfy the FK
        with factory() as session:
            report = ExpenseReport(
                organization_id=seeded["org_a_id"],
                owner_id=seeded["employee_a_id"],
                title="Constraint test report",
                currency="USD",
                status=ExpenseReportStatus.draft,
                total_amount=0,
                created_by=seeded["employee_a_id"],
                updated_by=seeded["employee_a_id"],
            )
            session.add(report)
            session.commit()
            report_id = report.id

        try:
            with pytest.raises(IntegrityError):
                with factory() as session:
                    bad_expense = Expense(
                        organization_id=seeded["org_a_id"],
                        report_id=report_id,
                        category=ExpenseCategory.meals,
                        amount=Decimal("-1.00"),
                        currency="USD",
                        expense_date=datetime.date(2026, 10, 1),
                        status=ExpenseStatus.draft,
                        created_by=seeded["employee_a_id"],
                        updated_by=seeded["employee_a_id"],
                    )
                    session.add(bad_expense)
                    session.flush()
        finally:
            # Cleanup parent report
            with factory() as session:
                session.query(ExpenseReport).filter_by(id=report_id).delete()
                session.commit()

    def test_trip_segment_end_before_start_raises_integrity_error(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """Direct INSERT: TripSegment with end_datetime < start_datetime raises IntegrityError."""
        factory = get_sessionmaker()
        start = datetime.datetime(2026, 10, 5, 10, 0, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.timezone.utc)  # before start

        with pytest.raises(IntegrityError):
            with factory() as session:
                bad_segment = TripSegment(
                    trip_id=seeded["trip_id"],
                    segment_type=SegmentType.flight,
                    start_datetime=start,
                    end_datetime=end,  # end < start → CHECK violation
                )
                session.add(bad_segment)
                session.flush()

    def test_duplicate_user_email_per_org_raises_integrity_error(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """Direct INSERT: second User with same (org_id, email) raises IntegrityError."""
        factory = get_sessionmaker()
        duplicate_email = f"dup-p3t4-{uuid.uuid4().hex[:8]}@test.internal"

        # Create first user
        with factory() as session:
            u1 = User(
                organization_id=seeded["org_a_id"],
                name="First User",
                email=duplicate_email,
                role=RoleKey.employee,
                status=UserStatus.active,
                password_hash="hash1",
            )
            session.add(u1)
            session.commit()
            u1_id = u1.id

        try:
            with pytest.raises(IntegrityError):
                with factory() as session:
                    u2 = User(
                        organization_id=seeded["org_a_id"],
                        name="Second User (duplicate email)",
                        email=duplicate_email,
                        role=RoleKey.employee,
                        status=UserStatus.active,
                        password_hash="hash2",
                    )
                    session.add(u2)
                    session.commit()
        finally:
            with factory() as session:
                session.query(User).filter_by(id=u1_id).delete()
                session.commit()

    def test_second_active_policy_same_name_raises_integrity_error(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """Direct INSERT: second active Policy with same (org, name) raises IntegrityError.

        The policies table has a partial unique index on (organization_id, name) WHERE is_active.
        Skips gracefully if the policies table is not present.
        """
        factory = get_sessionmaker()

        # Check policies table exists
        existing = set(sa_inspect(db_engine).get_table_names())
        if "policies" not in existing:
            pytest.skip("policies table not present — skipping policy constraint test")

        policy_name = f"P3T4 Unique Active Policy {uuid.uuid4().hex[:6]}"

        with factory() as session:
            p1 = Policy(
                organization_id=seeded["org_a_id"],
                name=policy_name,
                version=1,
                effective_from=datetime.date(2026, 1, 1),
                is_active=True,
                created_by=seeded["employee_a_id"],
                updated_by=seeded["employee_a_id"],
            )
            session.add(p1)
            session.commit()
            p1_id = p1.id

        try:
            with pytest.raises(IntegrityError):
                with factory() as session:
                    p2 = Policy(
                        organization_id=seeded["org_a_id"],
                        name=policy_name,
                        version=2,  # different version, same name, both active
                        effective_from=datetime.date(2026, 6, 1),
                        is_active=True,
                        created_by=seeded["employee_a_id"],
                        updated_by=seeded["employee_a_id"],
                    )
                    session.add(p2)
                    session.commit()
        finally:
            with factory() as session:
                session.query(Policy).filter_by(id=p1_id).delete()
                session.commit()
