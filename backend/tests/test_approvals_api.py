"""P4-T13 Slice 4 — Approval HTTP endpoints validate gate.

Assertions:
  1. POST /approvals (finance_admin) → 201 with current_status in (auto_approved, pending)
  2. POST /approvals without token → 401
  3. POST /approvals as employee (no APPROVAL_DECIDE_REPORTS) → 403
  4. POST /approvals duplicate (same subject) → 409
  5. PATCH /approvals/{id} decision=approve → 200 status=approved + audit row written
  6. PATCH /approvals/{id} decision=reject, no reason → 422
  7. PATCH /approvals/{random-uuid} → 404
  8. PATCH /approvals/{id} on auto_approved approval → 409

Live-DB — self-skips when Postgres is unreachable.
The orchestrator dependency is overridden with a stub that creates an Approval
row in the session so the test does not require an LLM key or a real subject.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.errors import ErrorCode
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.app.modules.ai.runtime import get_orchestrator
from database.models import Base
from database.models.ai_decision_log import AiDecisionLog
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
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
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Scoped tables (FK-safe order — children before parents for teardown)
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, AiDecisionLog.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ApprovalStep.__table__),
    cast(Table, Trip.__table__),
    cast(Table, ExpenseReport.__table__),
]

# ---------------------------------------------------------------------------
# Unique name/email constants — isolated from other test modules
# ---------------------------------------------------------------------------

_ORG_NAME = "Approvals API Test Org"
_FINANCE_EMAIL = "appr-finance@test.internal"
_MANAGER_EMAIL = "appr-manager@test.internal"
_EMPLOYEE_EMAIL = "appr-employee@test.internal"
_TEST_PW = "test-password-approvals-api"

# ---------------------------------------------------------------------------
# DB engine fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping approvals API tests")

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
    """Seed RBAC + org with finance_admin, manager, and employee users."""
    factory = get_sessionmaker()

    all_emails = [_FINANCE_EMAIL, _MANAGER_EMAIL, _EMPLOYEE_EMAIL]

    # Prune stale data from a previous incomplete run (FK-safe order)
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
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
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org)
        session.flush()

        hpw = hash_password(_TEST_PW)

        finance_admin = User(
            organization_id=org.id,
            name="Finance Admin (Approvals Test)",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_admin)
        session.flush()

        manager = User(
            organization_id=org.id,
            name="Manager (Approvals Test)",
            email=_MANAGER_EMAIL,
            role=RoleKey.manager,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(manager)
        session.flush()

        employee = User(
            organization_id=org.id,
            name="Employee (Approvals Test)",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee)
        session.flush()

        session.commit()

    yield {
        "org_id": org.id,
        "finance_admin_id": finance_admin.id,
        "manager_id": manager.id,
        "employee_id": employee.id,
    }

    # Teardown in FK-safe order
    with factory() as session:
        org_ids = [org.id]
        session.query(ApprovalStep).filter(
            ApprovalStep.approval_id.in_(
                session.query(Approval.id).filter(Approval.organization_id.in_(org_ids))
            )
        ).delete(synchronize_session=False)
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Stub orchestrator — creates an Approval in the session deterministically
# ---------------------------------------------------------------------------


def _make_stub_orchestrator(
    org_id: uuid.UUID,
    submitter_id: uuid.UUID,
    *,
    status: ApprovalStatus = ApprovalStatus.pending,
) -> Any:
    """Return a factory function for the stub orchestrator dependency.

    The stub writes an Approval row (flush-only) so the endpoint can commit and
    query it.  Returns AgentResultStatus.ok in all non-error scenarios so the
    router proceeds normally.
    """
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )
    from database.models.ai_decision_log import AiAgent, AiRoute

    def _stub_factory() -> Any:
        mock_orch = MagicMock()

        def _handle_task(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
            # Parse the subject info from the task payload
            subject_type_raw = task.payload.get("subject_type", "expense_report")
            subject_id_raw = task.payload.get("subject_id", str(uuid.uuid4()))

            subject_type = ApprovalSubjectType(subject_type_raw)
            subject_id = uuid.UUID(str(subject_id_raw))

            # Write Approval row (flush-only — endpoint commits)
            approval = Approval(
                organization_id=org_id,
                subject_type=subject_type,
                subject_id=subject_id,
                submitter_id=submitter_id,
                current_status=status,
                is_auto_approved=(status == ApprovalStatus.auto_approved),
                risk_summary=None,
            )
            session.add(approval)
            session.flush()

            decision = RoutingDecision(
                target_agent=AiAgent.approval,
                route=AiRoute.deterministic,
                task_type="approval_decision",
                confidence=1.0,
                rationale="stub",
            )
            result = AgentResult(
                status=AgentResultStatus.ok,
                output={
                    "approval_id": str(approval.id),
                    "current_status": status.value,
                    "auto_approved": status == ApprovalStatus.auto_approved,
                },
                citations=(),
            )
            return OrchestratorOutcome(decision=decision, result=result, log_id=None)

        mock_orch.handle_task = _handle_task
        return mock_orch

    return _stub_factory


def _make_duplicate_stub_orchestrator() -> Any:
    """Stub that returns needs_review (simulates IdempotencyError from agent)."""
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )
    from database.models.ai_decision_log import AiAgent, AiRoute

    def _stub_factory() -> Any:
        mock_orch = MagicMock()

        def _handle_task(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
            decision = RoutingDecision(
                target_agent=AiAgent.approval,
                route=AiRoute.deterministic,
                task_type="approval_decision",
                confidence=1.0,
                rationale="stub-duplicate",
            )
            result = AgentResult(
                status=AgentResultStatus.needs_review,
                output={"denied": True, "reason": "Approval already exists (idempotency)"},
                citations=(),
            )
            return OrchestratorOutcome(decision=decision, result=result, log_id=None)

        mock_orch.handle_task = _handle_task
        return mock_orch

    return _stub_factory


def _make_subject(
    subject_type: str,
    owner_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    submitted: bool = True,
) -> uuid.UUID:
    """Insert a real approval subject owned by *owner_id* (C7-13: POST /approvals
    now validates the subject exists, is in scope, is not the caller's own and is
    in its submitted state)."""
    from datetime import date
    from decimal import Decimal

    factory = get_sessionmaker()
    with factory() as session:
        obj: Trip | ExpenseReport
        if subject_type == "trip":
            obj = Trip(
                organization_id=org_id,
                traveler_id=owner_id,
                origin="SFO",
                primary_destination="JFK",
                start_date=date(2027, 1, 10),
                end_date=date(2027, 1, 12),
                currency="USD",
                status=TripStatus.pending_approval if submitted else TripStatus.draft,
                created_by=owner_id,
                updated_by=owner_id,
            )
        else:
            obj = ExpenseReport(
                organization_id=org_id,
                owner_id=owner_id,
                title="Approvals API subject",
                currency="USD",
                status=ExpenseReportStatus.submitted if submitted else ExpenseReportStatus.draft,
                total_amount=Decimal("10.00"),
                created_by=owner_id,
                updated_by=owner_id,
            )
        session.add(obj)
        session.commit()
        return obj.id


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. POST /approvals success
# ---------------------------------------------------------------------------


class TestTriggerApproval:
    def test_trigger_approval_success(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """finance_admin POST /approvals → 201 with a valid current_status."""
        subject_id = _make_subject("expense_report", seeded["employee_id"], seeded["org_id"])
        stub_factory = _make_stub_orchestrator(
            seeded["org_id"], seeded["employee_id"], status=ApprovalStatus.pending
        )
        app.dependency_overrides[get_orchestrator] = stub_factory
        try:
            client = TestClient(app, raise_server_exceptions=False)
            headers = _bearer(seeded["finance_admin_id"], seeded["org_id"])
            r = client.post(
                "/api/v1/approvals",
                json={"subject_type": "expense_report", "subject_id": str(subject_id)},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["current_status"] in ("auto_approved", "pending")
            assert body["organization_id"] == str(seeded["org_id"])
            assert body["subject_id"] == str(subject_id)
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

    def test_trigger_approval_auto_approved(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """finance_admin POST /approvals → 201 auto_approved path."""
        subject_id = _make_subject("trip", seeded["employee_id"], seeded["org_id"])
        stub_factory = _make_stub_orchestrator(
            seeded["org_id"], seeded["employee_id"], status=ApprovalStatus.auto_approved
        )
        app.dependency_overrides[get_orchestrator] = stub_factory
        try:
            client = TestClient(app, raise_server_exceptions=False)
            headers = _bearer(seeded["finance_admin_id"], seeded["org_id"])
            r = client.post(
                "/api/v1/approvals",
                json={"subject_type": "trip", "subject_id": str(subject_id)},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["current_status"] == "auto_approved"
            assert body["is_auto_approved"] is True
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

    def test_trigger_approval_unauthorized(self, seeded: dict[str, Any]) -> None:
        """No token → 401."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            "/api/v1/approvals",
            json={"subject_type": "expense_report", "subject_id": str(uuid.uuid4())},
        )
        assert r.status_code == 401

    def test_trigger_approval_forbidden(self, seeded: dict[str, Any]) -> None:
        """employee (no APPROVAL_DECIDE_REPORTS) → 403."""
        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["employee_id"], seeded["org_id"])
        r = client.post(
            "/api/v1/approvals",
            json={"subject_type": "expense_report", "subject_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_trigger_approval_duplicate(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """POST with same subject twice → second call returns 409.

        First call uses a normal stub that creates the Approval row.
        Second call uses a needs_review stub (simulates IdempotencyError) and
        the router queries the existing row → raises ConflictError.
        """
        org_id = seeded["org_id"]
        submitter_id = seeded["employee_id"]
        subject_id = _make_subject("expense_report", submitter_id, org_id)

        # First call: creates the Approval row
        stub_factory = _make_stub_orchestrator(org_id, submitter_id, status=ApprovalStatus.pending)
        app.dependency_overrides[get_orchestrator] = stub_factory
        try:
            client = TestClient(app, raise_server_exceptions=False)
            headers = _bearer(seeded["finance_admin_id"], org_id)
            r1 = client.post(
                "/api/v1/approvals",
                json={"subject_type": "expense_report", "subject_id": str(subject_id)},
                headers=headers,
            )
            assert r1.status_code == 201, r1.text
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        # Second call: agent returns needs_review (idempotency); Approval already exists → 409
        dup_stub_factory = _make_duplicate_stub_orchestrator()
        app.dependency_overrides[get_orchestrator] = dup_stub_factory
        try:
            client2 = TestClient(app, raise_server_exceptions=False)
            r2 = client2.post(
                "/api/v1/approvals",
                json={"subject_type": "expense_report", "subject_id": str(subject_id)},
                headers=headers,
            )
            assert r2.status_code == 409, r2.text
            assert r2.json()["error"]["code"] == ErrorCode.CONFLICT
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)


# ---------------------------------------------------------------------------
# 5. PATCH /approvals/{id} — decide
# ---------------------------------------------------------------------------


def _create_pending_approval(
    db_engine: Engine,
    org_id: uuid.UUID,
    submitter_id: uuid.UUID,
) -> uuid.UUID:
    """Directly insert a pending Approval into the DB, bypassing the agent."""
    factory = get_sessionmaker()
    with factory() as session:
        approval = Approval(
            organization_id=org_id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=submitter_id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary=None,
        )
        session.add(approval)
        session.commit()
        return approval.id


def _create_auto_approved_approval(
    db_engine: Engine,
    org_id: uuid.UUID,
    submitter_id: uuid.UUID,
) -> uuid.UUID:
    """Directly insert an auto_approved Approval into the DB."""
    factory = get_sessionmaker()
    with factory() as session:
        approval = Approval(
            organization_id=org_id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=uuid.uuid4(),
            submitter_id=submitter_id,
            current_status=ApprovalStatus.auto_approved,
            is_auto_approved=True,
            risk_summary=None,
        )
        session.add(approval)
        session.commit()
        return approval.id


class TestDecideApproval:
    def test_decide_approval_approve(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """PATCH /approvals/{id} decision=approve → 200 status=approved + audit row."""
        org_id = seeded["org_id"]
        # The employee is the submitter; finance_admin (org-wide scope) decides.
        # C7-03: a caller can never decide an approval on their own submission.
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "approve"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["current_status"] == "approved"
        assert body["id"] == str(approval_id)

        # Verify audit row was written
        from sqlalchemy import text as sa_text

        with db_engine.connect() as conn:
            row = conn.execute(
                sa_text(
                    "SELECT before_state, after_state FROM audit_log "
                    "WHERE entity_id = :eid AND action = 'approval.decided'"
                ),
                {"eid": str(approval_id)},
            ).fetchone()
        assert row is not None
        assert row[0]["current_status"] == "pending"
        assert row[1]["current_status"] == "approved"

    def test_decide_approval_reject(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """PATCH /approvals/{id} decision=reject with reason → 200 status=rejected."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "reject", "reason": "Out of policy."},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["current_status"] == "rejected"

    def test_decide_approval_request_changes(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """PATCH /approvals/{id} decision=request_changes with reason → 200."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "request_changes", "reason": "Need receipts."},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["current_status"] == "changes_requested"

    def test_decide_approval_reject_requires_reason(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """PATCH decision=reject without reason → 422."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "reject"},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == ErrorCode.VALIDATION

    def test_decide_approval_request_changes_requires_reason(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """PATCH decision=request_changes without reason → 422."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "request_changes"},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == ErrorCode.VALIDATION

    def test_decide_approval_invalid_decision(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """PATCH with invalid decision string → 400."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "banana"},
            headers=headers,
        )
        assert r.status_code == 400, r.text

    def test_decide_approval_not_found(self, seeded: dict[str, Any]) -> None:
        """PATCH /approvals/{random-uuid} → 404."""
        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], seeded["org_id"])
        r = client.patch(
            f"/api/v1/approvals/{uuid.uuid4()}",
            json={"decision": "approve"},
            headers=headers,
        )
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_decide_reverse_on_auto_approved(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """PATCH on an auto_approved approval → 200 (US-M2 AC2 reverse-within-policy).

        P5-T8 changed the rule: auto_approved is now decidable (a human override),
        recorded as a new step. Resolved states (approved/rejected) remain 409.
        """
        org_id = seeded["org_id"]
        approval_id = _create_auto_approved_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "reject", "reason": "Reversed on review — out of policy."},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["current_status"] == "rejected"

    def test_decide_on_resolved_approval_conflict(
        self, db_engine: Engine, seeded: dict[str, Any]
    ) -> None:
        """Deciding an already-resolved (approved) approval → 409."""
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["employee_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["finance_admin_id"], org_id)
        r1 = client.patch(
            f"/api/v1/approvals/{approval_id}", json={"decision": "approve"}, headers=headers
        )
        assert r1.status_code == 200, r1.text
        r2 = client.patch(
            f"/api/v1/approvals/{approval_id}", json={"decision": "approve"}, headers=headers
        )
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["code"] == ErrorCode.CONFLICT

    def test_decide_approval_unauthorized(self, seeded: dict[str, Any]) -> None:
        """PATCH without token → 401."""
        client = TestClient(app, raise_server_exceptions=False)
        r = client.patch(
            f"/api/v1/approvals/{uuid.uuid4()}",
            json={"decision": "approve"},
        )
        assert r.status_code == 401

    def test_decide_approval_forbidden(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """PATCH as employee (no APPROVAL_DECIDE_REPORTS) → 403.

        The approval is submitted by someone OTHER than the caller (and its subject
        is not the caller's), so only the permission gate can produce the 403 — the
        C7-03 self-approval guard cannot mask a permission-catalog regression.
        """
        org_id = seeded["org_id"]
        approval_id = _create_pending_approval(db_engine, org_id, seeded["finance_admin_id"])

        client = TestClient(app, raise_server_exceptions=False)
        headers = _bearer(seeded["employee_id"], org_id)
        r = client.patch(
            f"/api/v1/approvals/{approval_id}",
            json={"decision": "approve"},
            headers=headers,
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN
