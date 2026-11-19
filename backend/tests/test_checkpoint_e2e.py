"""P4-T13 Slice 5 — End-to-end checkpoint test (G17).

Scripted flow: plan → select → capture → assemble → approve
with a deterministic stub orchestrator per AI step.

The test exercises the full HTTP layer using hermetic stubs for all
provider dependencies (storage, OCR, LLM, orchestrator).  No real LLM
key or external service is required.

pytestmark: db — self-skips when Postgres is unreachable.
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
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.app.modules.ai.runtime import get_orchestrator
from backend.tests.receipt_images import scenario_upload
from database.models import Base
from database.models.ai_decision_log import AiDecisionLog
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
from database.models.conversation import Conversation
from database.models.conversation_message import ConversationMessage
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
from database.models.receipt import Receipt
from database.seeds.identity_seed import seed_rbac_catalog
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.local import LocalOcrProvider
from services.storage_provider.local import LocalStorageProvider

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Unique constants — isolated from all other test modules
# ---------------------------------------------------------------------------

_ORG_NAME = "Checkpoint E2E Test Org"
_FINANCE_EMAIL = "e2e-finance-admin@test.internal"
_EMPLOYEE_EMAIL = "e2e-employee@test.internal"
_TEST_PW = "test-password-e2e-checkpoint"

# ---------------------------------------------------------------------------
# Tables scoped for create/drop — FK-safe (children first in teardown)
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, AiDecisionLog.__table__),
    cast(Table, Conversation.__table__),
    cast(Table, ConversationMessage.__table__),
    cast(Table, IdempotencyKey.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, ApprovalStep.__table__),
    cast(Table, Approval.__table__),
    cast(Table, ExpenseReport.__table__),
]

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
        pytest.skip("Postgres not reachable — skipping checkpoint e2e tests")

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
    """Seed org + finance_admin + employee + one ExpenseReport for approval."""
    factory = get_sessionmaker()

    all_emails = [_FINANCE_EMAIL, _EMPLOYEE_EMAIL]

    # Prune stale rows from a previous incomplete run (FK-safe order)
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            # Approval steps → approvals → expense_reports → receipts
            # → conversation messages → conversations → audit/decision logs
            # → idempotency keys → users → organizations
            session.query(ApprovalStep).filter(
                ApprovalStep.approval_id.in_(
                    session.query(Approval.id).filter(Approval.organization_id.in_(stale_org_ids))
                )
            ).delete(synchronize_session=False)
            session.query(Approval).filter(Approval.organization_id.in_(stale_org_ids)).delete()
            session.query(ConversationMessage).filter(
                ConversationMessage.conversation_id.in_(
                    session.query(Conversation.id).filter(
                        Conversation.organization_id.in_(stale_org_ids)
                    )
                )
            ).delete(synchronize_session=False)
            session.query(Conversation).filter(
                Conversation.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(IdempotencyKey).filter(
                IdempotencyKey.organization_id.in_(stale_org_ids)
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

        # finance_admin: has APPROVAL_DECIDE_REPORTS + POLICY_READ + POLICY_MANAGE
        finance_admin = User(
            organization_id=org.id,
            name="Checkpoint Finance Admin",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_admin)
        session.flush()

        # employee: has expense/trip permissions; subject of the expense report
        employee = User(
            organization_id=org.id,
            name="Checkpoint Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=finance_admin.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee)
        session.flush()

        # An expense report owned by the employee — used as approval subject
        expense_report = ExpenseReport(
            organization_id=org.id,
            owner_id=employee.id,
            title="NYC Trip Expenses — Checkpoint E2E",
            status=ExpenseReportStatus.submitted,
            currency="USD",
            total_amount=0,
            created_by=employee.id,
            updated_by=employee.id,
        )
        session.add(expense_report)
        session.flush()

        session.commit()

    yield {
        "org_id": org.id,
        "finance_admin_id": finance_admin.id,
        "employee_id": employee.id,
        "expense_report_id": expense_report.id,
    }

    # Teardown (FK-safe order — children before parents)
    with factory() as session:
        org_ids = [org.id]
        session.query(ApprovalStep).filter(
            ApprovalStep.approval_id.in_(
                session.query(Approval.id).filter(Approval.organization_id.in_(org_ids))
            )
        ).delete(synchronize_session=False)
        session.query(Approval).filter(Approval.organization_id.in_(org_ids)).delete()
        session.query(ConversationMessage).filter(
            ConversationMessage.conversation_id.in_(
                session.query(Conversation.id).filter(Conversation.organization_id.in_(org_ids))
            )
        ).delete(synchronize_session=False)
        session.query(Conversation).filter(Conversation.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        session.query(IdempotencyKey).filter(IdempotencyKey.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Stub orchestrators
# ---------------------------------------------------------------------------


def _make_conversation_stub() -> Any:
    """Deterministic orchestrator for conversation (plan/select/assemble) steps."""
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )
    from database.models.ai_decision_log import AiAgent, AiRoute

    mock_orch = MagicMock()

    def _handle_task(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
        decision = RoutingDecision(
            target_agent=AiAgent.travel,
            route=AiRoute.deterministic,
            task_type="travel_planning",
            confidence=0.99,
            rationale="e2e stub",
        )
        result = AgentResult(
            status=AgentResultStatus.ok,
            output={"answer": "E2E stub response."},
            citations=(),
        )
        return OrchestratorOutcome(decision=decision, result=result, log_id=None)

    mock_orch.handle_task = _handle_task
    return mock_orch


def _make_approval_stub(
    org_id: uuid.UUID,
    submitter_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> Any:
    """Deterministic orchestrator for the approval trigger step.

    Creates an Approval row (flush-only) so the endpoint can commit and return it.
    """
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )
    from database.models.ai_decision_log import AiAgent, AiRoute

    mock_orch = MagicMock()

    def _handle_task(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
        approval = Approval(
            organization_id=org_id,
            subject_type=subject_type,
            subject_id=subject_id,
            submitter_id=submitter_id,
            current_status=ApprovalStatus.pending,
            is_auto_approved=False,
            risk_summary=None,
        )
        session.add(approval)
        session.flush()

        decision = RoutingDecision(
            target_agent=AiAgent.approval,
            route=AiRoute.deterministic,
            task_type="approval_decision",
            confidence=1.0,
            rationale="e2e stub",
        )
        result = AgentResult(
            status=AgentResultStatus.ok,
            output={
                "approval_id": str(approval.id),
                "current_status": ApprovalStatus.pending.value,
                "auto_approved": False,
            },
            citations=(),
        )
        return OrchestratorOutcome(decision=decision, result=result, log_id=None)

    mock_orch.handle_task = _handle_task
    return mock_orch


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Checkpoint E2E test class
# ---------------------------------------------------------------------------


class TestCheckpointE2E:
    """G17 checkpoint: plan → select → capture → assemble → approve."""

    def test_full_checkpoint_flow(self, db_engine: Engine, seeded: dict[str, Any]) -> None:
        """Single flow exercises all five AI pipeline steps end-to-end.

        Step 1  Plan     — POST /conversations (context_type=travel_planning) → 201
        Step 2  Select   — POST /conversations/{id}/messages → SSE (event: done)
        Step 3  Capture  — POST /receipts (multipart) → 201
        Step 4  Assemble — POST /conversations (context_type=expense) → 201
                         — POST /conversations/{id}/messages → SSE (event: done)
        Step 5  Approve  — POST /approvals → 201 with current_status
        """
        org_id = seeded["org_id"]
        finance_admin_id = seeded["finance_admin_id"]
        employee_id = seeded["employee_id"]
        expense_report_id = seeded["expense_report_id"]

        fa_headers = _bearer(finance_admin_id, org_id)
        emp_headers = _bearer(employee_id, org_id)

        # ------------------------------------------------------------------
        # Step 1: Plan — create a travel_planning conversation
        # ------------------------------------------------------------------
        app.dependency_overrides[get_orchestrator] = _make_conversation_stub
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r1 = client.post(
                    "/api/v1/conversations",
                    json={"context_type": "travel_planning"},
                    headers=fa_headers,
                )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        assert r1.status_code == 201, f"Step 1 Plan failed: {r1.text}"
        plan_conv_id = r1.json()["id"]
        assert plan_conv_id, "Expected a conversation id from Step 1"

        # ------------------------------------------------------------------
        # Step 2: Select — send a travel intent message → SSE stream
        # ------------------------------------------------------------------
        app.dependency_overrides[get_orchestrator] = _make_conversation_stub
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r2 = client.post(
                    f"/api/v1/conversations/{plan_conv_id}/messages",
                    json={"content": "I need to fly to NYC next week."},
                    headers=fa_headers,
                )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        assert r2.status_code == 200, f"Step 2 Select failed: {r2.text}"
        assert "event: done" in r2.text, f"Step 2: SSE body missing 'event: done':\n{r2.text}"

        # ------------------------------------------------------------------
        # Step 3: Capture — upload a receipt image
        # ------------------------------------------------------------------
        from services.llm_provider.factory import get_llm_provider
        from services.ocr_provider.factory import get_ocr_provider
        from services.storage_provider.factory import get_storage_provider

        app.dependency_overrides[get_storage_provider] = lambda: LocalStorageProvider()
        app.dependency_overrides[get_ocr_provider] = lambda: LocalOcrProvider()
        app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r3 = client.post(
                    "/api/v1/receipts",
                    files={"file": scenario_upload("fake-image-data", "test_receipt.png")},
                    headers=emp_headers,
                )
        finally:
            app.dependency_overrides.pop(get_storage_provider, None)
            app.dependency_overrides.pop(get_ocr_provider, None)
            app.dependency_overrides.pop(get_llm_provider, None)

        assert r3.status_code == 201, f"Step 3 Capture failed: {r3.text}"
        receipt_body = r3.json()
        assert "id" in receipt_body, "Step 3: response missing 'id'"
        assert receipt_body["ocr_status"] in (
            "pending",
            "processed",
            "failed",
        ), f"Step 3: unexpected ocr_status: {receipt_body['ocr_status']}"

        # ------------------------------------------------------------------
        # Step 4a: Assemble — create an expense conversation
        # ------------------------------------------------------------------
        app.dependency_overrides[get_orchestrator] = _make_conversation_stub
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r4a = client.post(
                    "/api/v1/conversations",
                    json={"context_type": "expense"},
                    headers=emp_headers,
                )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        assert r4a.status_code == 201, f"Step 4a Assemble (create conv) failed: {r4a.text}"
        expense_conv_id = r4a.json()["id"]

        # ------------------------------------------------------------------
        # Step 4b: Assemble — send assemble message → SSE stream
        # ------------------------------------------------------------------
        app.dependency_overrides[get_orchestrator] = _make_conversation_stub
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r4b = client.post(
                    f"/api/v1/conversations/{expense_conv_id}/messages",
                    json={"content": "Assemble my expense report for the NYC trip."},
                    headers=emp_headers,
                )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        assert r4b.status_code == 200, f"Step 4b Assemble (send msg) failed: {r4b.text}"
        assert "event: done" in r4b.text, f"Step 4b: SSE body missing 'event: done':\n{r4b.text}"

        # ------------------------------------------------------------------
        # Step 5: Approve — trigger approval for the expense report
        # ------------------------------------------------------------------
        approval_stub = _make_approval_stub(
            org_id=org_id,
            submitter_id=finance_admin_id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=expense_report_id,
        )
        app.dependency_overrides[get_orchestrator] = lambda: approval_stub
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r5 = client.post(
                    "/api/v1/approvals",
                    json={
                        "subject_type": "expense_report",
                        "subject_id": str(expense_report_id),
                    },
                    headers=fa_headers,
                )
        finally:
            app.dependency_overrides.pop(get_orchestrator, None)

        assert r5.status_code == 201, f"Step 5 Approve failed: {r5.text}"
        approval_body = r5.json()
        assert approval_body["current_status"] in (
            "auto_approved",
            "pending",
        ), f"Step 5: unexpected current_status: {approval_body['current_status']}"
        assert approval_body["organization_id"] == str(org_id)
        assert approval_body["subject_id"] == str(expense_report_id)
