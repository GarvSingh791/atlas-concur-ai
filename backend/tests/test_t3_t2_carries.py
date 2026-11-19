"""P6-T3.3 T2-carries verification suite.

Proves the T2 bug-fixes carry through:
  (a) validate_context_ref_id() now enforces record-level scoping (not just org-level)
      — confirmed via POST /conversations with context_ref_id.
  (b) PolicyRetrievalTool SQL is org-scoped — confirmed structurally (no pgvector needed).

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
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
from database.models.audit_log import AuditLog
from database.models.conversation import Conversation
from database.models.conversation_message import ConversationMessage
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
from database.models.trip import Trip, TripStatus
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Unique constants
# ---------------------------------------------------------------------------

_ORG_NAME = "T3-T2-Carries-Org1"
_ORG2_NAME = "T3-T2-Carries-Org2"
_EMP_A_EMAIL = "t3-carries-emp-a@test.internal"
_MGR_A_EMAIL = "t3-carries-mgr-a@test.internal"
_EMP_B_EMAIL = "t3-carries-emp-b@test.internal"
_MGR_B_EMAIL = "t3-carries-mgr-b@test.internal"
_USER_ORG2_EMAIL = "t3-carries-org2@test.internal"
_TEST_PW = "test-password-t3-carries"

# ---------------------------------------------------------------------------
# Scoped tables for create-if-missing
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
    cast(Table, Policy.__table__),
    cast(Table, Conversation.__table__),
    cast(Table, ConversationMessage.__table__),
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
        pytest.skip("Postgres not reachable — skipping T3 T2-carries tests")

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
    """Seed RBAC + Org1 (4 users + records) + Org2 (1 user + policy)."""
    factory = get_sessionmaker()

    all_emails = [
        _EMP_A_EMAIL,
        _MGR_A_EMAIL,
        _EMP_B_EMAIL,
        _MGR_B_EMAIL,
        _USER_ORG2_EMAIL,
    ]

    # Prune stale data from a previous incomplete run (FK-safe order).
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            session.query(Policy).filter(Policy.organization_id.in_(stale_org_ids)).delete()
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
        org1 = Organization(name=_ORG_NAME, base_currency="USD")
        org2 = Organization(name=_ORG2_NAME, base_currency="EUR")
        session.add_all([org1, org2])
        session.flush()

        hpw = hash_password(_TEST_PW)

        # -- Org1: managers (no manager_id) --
        mgr_a = User(
            organization_id=org1.id,
            name="T3-Carries Manager A",
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
            name="T3-Carries Manager B",
            email=_MGR_B_EMAIL,
            role=RoleKey.manager,
            department="Sales",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(mgr_b)
        session.flush()

        # -- Org1: employees --
        emp_a = User(
            organization_id=org1.id,
            name="T3-Carries Employee A",
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
            name="T3-Carries Employee B",
            email=_EMP_B_EMAIL,
            role=RoleKey.employee,
            department="Sales",
            manager_id=mgr_b.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(emp_b)
        session.flush()

        # -- Org2 user --
        user_org2 = User(
            organization_id=org2.id,
            name="T3-Carries Org2 User",
            email=_USER_ORG2_EMAIL,
            role=RoleKey.employee,
            department="General",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_org2)
        session.flush()

        # -- Trip (Org1, emp_a) --
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

        # -- ExpenseReport (Org1, emp_a) --
        report_a = ExpenseReport(
            organization_id=org1.id,
            owner_id=emp_a.id,
            title="T3 Carries Report A",
            status=ExpenseReportStatus.draft,
            currency="USD",
            total_amount=Decimal("0.00"),
            created_by=emp_a.id,
            updated_by=emp_a.id,
        )
        session.add(report_a)
        session.flush()

        # -- Policy Org1 --
        policy_org1 = Policy(
            organization_id=org1.id,
            name="T3 Carries Policy",
            version=1,
            effective_from=datetime.date(2026, 1, 1),
            is_active=True,
            created_by=emp_a.id,
            updated_by=emp_a.id,
        )
        session.add(policy_org1)
        session.flush()

        # -- Policy Org2 --
        policy_org2 = Policy(
            organization_id=org2.id,
            name="T3 Carries Policy",
            version=1,
            effective_from=datetime.date(2026, 1, 1),
            is_active=True,
            created_by=user_org2.id,
            updated_by=user_org2.id,
        )
        session.add(policy_org2)
        session.flush()

        session.commit()

    yield {
        "org1_id": org1.id,
        "org2_id": org2.id,
        "emp_a_id": emp_a.id,
        "emp_b_id": emp_b.id,
        "mgr_a_id": mgr_a.id,
        "mgr_b_id": mgr_b.id,
        "user_org2_id": user_org2.id,
        "trip_a_id": trip_a.id,
        "report_a_id": report_a.id,
        "policy_org1_id": policy_org1.id,
        "policy_org2_id": policy_org2.id,
    }

    # Teardown — delete in FK-safe order
    with factory() as session:
        org_ids = [org1.id, org2.id]
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _bearer(user_id: Any, org_id: Any) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# TestContextRefScoping
# ---------------------------------------------------------------------------


class TestContextRefScoping:
    """POST /conversations with context_ref_id — proves record-level scoping fix."""

    URL = "/api/v1/conversations"

    def test_context_ref_travel_own(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Employee can open a conversation scoped to their own trip."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "travel_planning",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["emp_a_id"], seeded["org1_id"]),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_context_ref_travel_cross_owner(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """Employee B cannot reference Employee A's trip (404 after fix)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "travel_planning",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["emp_b_id"], seeded["org1_id"]),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_context_ref_travel_manager_direct_report(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """Manager A can reference Employee A's trip (direct report in scope)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "travel_planning",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["mgr_a_id"], seeded["org1_id"]),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_context_ref_travel_manager_not_direct_report(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """Manager B cannot reference Employee A's trip (not B's direct report)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "travel_planning",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["mgr_b_id"], seeded["org1_id"]),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_context_ref_travel_cross_org(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Org2 user cannot reference Org1's trip (cross-org 404)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "travel_planning",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["user_org2_id"], seeded["org2_id"]),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_context_ref_expense_own(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Employee can open a conversation scoped to their own expense report."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "expense",
                "context_ref_id": str(seeded["report_a_id"]),
            },
            headers=_bearer(seeded["emp_a_id"], seeded["org1_id"]),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_context_ref_expense_cross_owner(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """Employee B cannot reference Employee A's expense report (404 after fix)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "expense",
                "context_ref_id": str(seeded["report_a_id"]),
            },
            headers=_bearer(seeded["emp_b_id"], seeded["org1_id"]),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_context_ref_policy_qa_with_ref_rejected(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """policy_qa context_type with context_ref_id → 422 (entity-scoping deferred)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "policy_qa",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["emp_a_id"], seeded["org1_id"]),
        )
        assert r.status_code == 422

    def test_context_ref_general_with_ref_rejected(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """general context_type with context_ref_id → 422 (ref not allowed for general)."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            self.URL,
            json={
                "context_type": "general",
                "context_ref_id": str(seeded["trip_a_id"]),
            },
            headers=_bearer(seeded["emp_a_id"], seeded["org1_id"]),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# TestPolicyQaScoping
# ---------------------------------------------------------------------------


class TestPolicyQaScoping:
    """Policy data is org-isolated — no pgvector required."""

    POLICIES_URL = "/api/v1/policies"

    def test_policy_catalog_org_isolation(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Each org sees only their own active policy via GET /policies."""
        client = TestClient(app, raise_server_exceptions=False)

        # Org1's token
        r1 = client.get(
            self.POLICIES_URL,
            headers=_bearer(seeded["emp_a_id"], seeded["org1_id"]),
        )
        assert r1.status_code == 200

        # Org2's token
        r2 = client.get(
            self.POLICIES_URL,
            headers=_bearer(seeded["user_org2_id"], seeded["org2_id"]),
        )
        assert r2.status_code == 200

        # PolicyResponse has an "id" field (see schemas.PolicyResponse)
        body1 = r1.json()
        body2 = r2.json()

        # Each org's response must contain their own policy id
        assert str(body1.get("id")) == str(seeded["policy_org1_id"])
        assert str(body2.get("id")) == str(seeded["policy_org2_id"])

        # Critical: each org must NOT see the other org's policy id
        assert str(seeded["policy_org1_id"]) not in str(body2)
        assert str(seeded["policy_org2_id"]) not in str(body1)

    def test_policy_rag_retrieval_sql_is_org_scoped(self, db_engine: Engine) -> None:
        """Structural assertion: PolicyRetrievalTool SQL contains org-scope filter.

        Proves the RAG retrieval cannot cross org boundaries even without running pgvector.
        """
        source_path = (
            Path(__file__).parent.parent.parent / "ai_agents" / "tools" / "policy_retrieval.py"
        )
        source = source_path.read_text()
        # The critical WHERE clause must be present
        assert "p.organization_id = :org" in source, (
            "PolicyRetrievalTool SQL must filter by p.organization_id = :org. "
            "This clause ensures cross-org RAG retrieval is impossible."
        )
        # And it must be bound via context.organization_id
        assert "context.organization_id" in source, (
            "PolicyRetrievalTool must bind :org from context.organization_id (user token), "
            "not from client input."
        )
