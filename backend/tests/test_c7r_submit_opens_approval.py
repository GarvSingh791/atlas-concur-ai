"""C7R — submitting an expense report / trip OPENS its approval (no POST needed).

Before the fix a submitted report (PATCH status=submitted) or trip (PATCH
status=pending_approval) created no approval: approvals were only opened by
POST /approvals, which no UI screen calls, so a real submission never reached a
reviewer.  The owner's submit now runs the SAME deterministic Approval Agent
path as the trigger, in the submit transaction, as a system principal:

  * in policy, ≤ $500 → approval auto_approved, subject moved to approved
    (actor None), agent audit + auto_approve step;
  * otherwise         → approval pending, routed to an independent approver
    (manager, else a finance_admin — never the submitter), subject unchanged;
  * returned subject resubmitted → the SAME approval is re-queued (no duplicate);
  * POST /approvals afterwards → 409 (idempotent, backward compatible);
  * agent failure → the submit still succeeds; the subject lands with a
    PENDING, "not evaluated" approval visible in the finance queue.

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus
from backend.app.core.security import create_access_token
from backend.app.modules.ai import runtime as ai_runtime
from backend.tests import factories as f
from database.models.ai_decision_log import AiAgent, AiDecisionLog
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import RoleKey, User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType

pytestmark = pytest.mark.db

API = "/api/v1"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _approvals(session: Session, subject_id: uuid.UUID) -> list[Approval]:
    return list(session.scalars(sa.select(Approval).where(Approval.subject_id == subject_id)).all())


def _audits(session: Session, entity_id: uuid.UUID, action: str) -> list[AuditLog]:
    return list(
        session.scalars(
            sa.select(AuditLog).where(AuditLog.entity_id == entity_id, AuditLog.action == action)
        ).all()
    )


def _draft_report(session: Session, owner: User, amount: str) -> ExpenseReport:
    report = f.make_expense_report(session, owner=owner, total_amount=Decimal(amount))
    f.make_expense(session, report=report, amount=amount, vendor="Cafe")
    return report


def _submit_report(app: Any, owner: User, report: ExpenseReport) -> Any:
    resp = _client(app, owner).patch(
        f"{API}/expense-reports/{report.id}", json={"status": "submitted"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _draft_trip(
    session: Session, traveler: User, *, estimated_cost: str | None, amount: str = "298.00"
) -> Trip:
    start = date.today() + timedelta(days=14)
    trip = f.make_trip(
        session,
        traveler=traveler,
        start_date=start,
        end_date=start + timedelta(days=2),
        estimated_cost=Decimal(estimated_cost) if estimated_cost is not None else None,
    )
    f.make_trip_segment(
        session,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="Delta",
        amount=Decimal(amount),
        currency="USD",
        in_policy=True,
    )
    return trip


def _submit_trip(app: Any, traveler: User, trip: Trip) -> Any:
    resp = _client(app, traveler).patch(
        f"{API}/trips/{trip.id}", json={"status": "pending_approval"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Expense reports
# ---------------------------------------------------------------------------


def test_report_submit_escalates_to_the_submitters_manager(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    report = _draft_report(db_session, employee, "750.00")  # > $500 → escalate

    body = _submit_report(app, employee, report)
    assert body["status"] == "submitted"
    assert body["approval_status"] == "pending"

    [approval] = _approvals(db_session, report.id)
    assert approval.subject_type == ApprovalSubjectType.expense_report
    assert approval.current_status == ApprovalStatus.pending
    assert approval.is_auto_approved is False
    assert approval.submitter_id == employee.id
    assert approval.risk_summary is not None
    assert approval.risk_summary["route"] == "escalate"
    assert approval.risk_summary["routed_to_approver_id"] == str(manager.id)
    assert approval.risk_summary["subject_total_usd"] == 750.0

    # The agent's own audit row (system actor) — written in the submit transaction.
    [escalated] = _audits(db_session, approval.id, "approval.escalated")
    assert escalated.actor_id is None
    # Agent decision log: a system principal, not the submitter.
    log = db_session.scalars(
        sa.select(AiDecisionLog).where(
            AiDecisionLog.organization_id == employee.organization_id,
            AiDecisionLog.agent == AiAgent.approval,
        )
    ).first()
    assert log is not None and log.user_id is None

    # It is in the manager's queue — the whole point of the fix.
    queue = _client(app, manager).get(f"{API}/approvals").json()["items"]
    assert str(approval.id) in {item["id"] for item in queue}


def test_report_submit_auto_approves_small_in_policy_report(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    report = _draft_report(db_session, employee, "12.50")

    body = _submit_report(app, employee, report)
    assert body["status"] == "approved"  # moved in the same transaction
    assert body["approval_status"] == "auto_approved"

    [approval] = _approvals(db_session, report.id)
    assert approval.current_status == ApprovalStatus.auto_approved
    assert approval.is_auto_approved is True
    steps = list(
        db_session.scalars(
            sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
        ).all()
    )
    assert [s.decision for s in steps] == [ApprovalDecision.auto_approve]
    assert len(_audits(db_session, approval.id, "approval.auto_approved")) == 1

    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved
    moves = _audits(db_session, report.id, "expense_report.status_changed")
    # owner's submit (actor = employee) + the agent's approve (actor None)
    assert {((m.after_state or {}).get("status"), m.actor_id) for m in moves} == {
        ("submitted", employee.id),
        ("approved", None),
    }


def test_manager_without_manager_is_routed_to_finance_never_to_self(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """C7-03 still holds on the submit path: never routed to the submitter."""
    manager, finance = users_a["manager"], users_a["finance"]
    assert manager.manager_id is None
    report = _draft_report(db_session, manager, "900.00")

    _submit_report(app, manager, report)

    [approval] = _approvals(db_session, report.id)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.risk_summary is not None
    assert approval.risk_summary["routed_to_approver_id"] == str(finance.id)
    assert approval.risk_summary["routing_fallback"] == "finance_admin"


def test_self_managed_submitter_is_routed_to_finance(
    app: Any, rbac_catalog: None, db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    finance = users_a["finance"]
    selfish = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    selfish.manager_id = selfish.id
    db_session.flush()
    report = _draft_report(db_session, selfish, "900.00")

    _submit_report(app, selfish, report)

    [approval] = _approvals(db_session, report.id)
    assert approval.risk_summary is not None
    routed = approval.risk_summary["routed_to_approver_id"]
    assert routed == str(finance.id)
    assert routed != str(selfish.id)


def test_trigger_after_submit_is_409_and_creates_no_duplicate(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    report = _draft_report(db_session, employee, "750.00")
    _submit_report(app, employee, report)

    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals",
        json={"subject_type": "expense_report", "subject_id": str(report.id)},
    )
    assert resp.status_code == 409, resp.text
    assert len(_approvals(db_session, report.id)) == 1


def test_returned_report_resubmission_requeues_the_same_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    report = _draft_report(db_session, employee, "750.00")
    _submit_report(app, employee, report)
    [approval] = _approvals(db_session, report.id)

    rc = _client(app, manager).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "attach the folio"},
    )
    assert rc.status_code == 200, rc.text

    body = _submit_report(app, employee, report)
    assert body["status"] == "submitted"
    [same] = _approvals(db_session, report.id)
    assert same.id == approval.id
    assert same.current_status == ApprovalStatus.pending
    assert len(_audits(db_session, approval.id, "approval.reopened")) == 1
    # The agent ran once (on the first submit), not again on the resubmission.
    assert len(_audits(db_session, approval.id, "approval.escalated")) == 1


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


def test_trip_submit_auto_approves_small_in_policy_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    trip = _draft_trip(db_session, employee, estimated_cost="298.00")

    body = _submit_trip(app, employee, trip)
    assert body["status"] == "approved"
    assert body["approval_status"] == "auto_approved"

    [approval] = _approvals(db_session, trip.id)
    assert approval.subject_type == ApprovalSubjectType.trip
    assert approval.current_status == ApprovalStatus.auto_approved
    assert approval.risk_summary is not None
    assert approval.risk_summary["subject_total_usd"] == 298.0
    db_session.refresh(trip)
    assert trip.status == TripStatus.approved
    moves = _audits(db_session, trip.id, "trip.status_changed")
    assert {((m.after_state or {}).get("status"), m.actor_id) for m in moves} == {
        ("pending_approval", employee.id),
        ("approved", None),
    }


def test_trip_submit_without_cost_escalates_to_manager(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Unknown cost is never auto-approved: pending, routed to the manager."""
    employee, manager = users_a["employee"], users_a["manager"]
    trip = _draft_trip(db_session, employee, estimated_cost=None)

    body = _submit_trip(app, employee, trip)
    assert body["status"] == "pending_approval"
    assert body["approval_status"] == "pending"

    [approval] = _approvals(db_session, trip.id)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.risk_summary is not None
    assert approval.risk_summary["routed_to_approver_id"] == str(manager.id)

    # The manager can now decide it through the normal flow.
    ok = _client(app, manager).patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
    assert ok.status_code == 200, ok.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.approved


def test_returned_trip_resubmission_requeues_the_same_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    trip = _draft_trip(db_session, employee, estimated_cost="1200.00", amount="1200.00")
    _submit_trip(app, employee, trip)
    [approval] = _approvals(db_session, trip.id)
    assert approval.current_status == ApprovalStatus.pending

    rc = _client(app, manager).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "cheaper fare"},
    )
    assert rc.status_code == 200, rc.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft

    body = _submit_trip(app, employee, trip)
    assert body["status"] == "pending_approval"
    [same] = _approvals(db_session, trip.id)
    assert same.id == approval.id
    assert same.current_status == ApprovalStatus.pending


# ---------------------------------------------------------------------------
# Fail-safe: the agent cannot run
# ---------------------------------------------------------------------------


class _BrokenOrchestrator:
    """Raises after a failing SQL statement — the savepoint must contain it."""

    def handle_task(self, task: Any, *, context: Any, session: Session) -> Any:
        session.execute(sa.text("SELECT * FROM no_such_table_c7r"))
        raise AssertionError("unreachable")


class _NeedsReviewOrchestrator:
    """Returns needs_review without persisting an approval."""

    def handle_task(self, task: Any, *, context: Any, session: Session) -> Any:
        class _Outcome:
            result = AgentResult(
                status=AgentResultStatus.needs_review, output={"denied": True, "reason": "x"}
            )

        return _Outcome()


@pytest.mark.parametrize("orchestrator", [_BrokenOrchestrator, _NeedsReviewOrchestrator])
def test_agent_failure_still_submits_into_a_pending_not_evaluated_approval(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: type,
) -> None:
    monkeypatch.setattr(ai_runtime, "get_orchestrator", lambda: orchestrator())
    employee, manager, finance = users_a["employee"], users_a["manager"], users_a["finance"]
    report = _draft_report(db_session, employee, "12.50")  # would auto-approve normally

    body = _submit_report(app, employee, report)
    # Never auto-approved without an evaluation; never stranded without an approval.
    assert body["status"] == "submitted"
    assert body["approval_status"] == "pending"

    [approval] = _approvals(db_session, report.id)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.is_auto_approved is False
    summary = approval.risk_summary or {}
    assert summary["evaluation_failed"] is True
    assert summary["is_policy_compliant"] is None
    assert summary["route"] == "escalate"
    assert summary["routed_to_approver_id"] == str(manager.id)
    [audit] = _audits(db_session, approval.id, "approval.escalated")
    assert audit.actor_id is None
    assert (audit.after_state or {}).get("evaluation_failed") is True

    # Visible in the org-wide finance queue.
    queue = _client(app, finance).get(f"{API}/approvals").json()["items"]
    assert str(approval.id) in {item["id"] for item in queue}


def test_agent_failure_on_trip_submit_is_fail_safe(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_runtime, "get_orchestrator", lambda: _BrokenOrchestrator())
    employee = users_a["employee"]
    trip = _draft_trip(db_session, employee, estimated_cost="298.00")

    body = _submit_trip(app, employee, trip)
    assert body["status"] == "pending_approval"
    [approval] = _approvals(db_session, trip.id)
    assert approval.current_status == ApprovalStatus.pending
    assert (approval.risk_summary or {})["evaluation_failed"] is True


# ---------------------------------------------------------------------------
# Seed consistency: subjects left 'submitted' under a decided approval
# ---------------------------------------------------------------------------


def test_seed_reconciles_submitted_subjects_with_decided_approvals(
    db_session: Session, org_a: Any, users_a: dict[str, User]
) -> None:
    """An older seed left "Q2 Summit Expenses" submitted under an approved approval."""
    from database.seeds.seed_all import _reconcile_decided_subjects  # noqa: PLC0415

    employee = users_a["employee"]

    def _report(status: ExpenseReportStatus, approval: ApprovalStatus) -> ExpenseReport:
        report = f.make_expense_report(db_session, owner=employee, status=status)
        f.make_approval(
            db_session,
            submitter=employee,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report.id,
            current_status=approval,
        )
        return report

    q2 = _report(ExpenseReportStatus.submitted, ApprovalStatus.approved)
    returned = _report(ExpenseReportStatus.submitted, ApprovalStatus.changes_requested)
    waiting = _report(ExpenseReportStatus.submitted, ApprovalStatus.pending)
    moved_on = _report(ExpenseReportStatus.reimbursed, ApprovalStatus.approved)
    trip = f.make_trip(db_session, traveler=employee, status=TripStatus.pending_approval)
    f.make_approval(
        db_session,
        submitter=employee,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.rejected,
    )
    q2_version = q2.version

    assert _reconcile_decided_subjects(db_session, org_a) == 3
    assert q2.status == ExpenseReportStatus.approved
    assert q2.version == q2_version + 1
    assert returned.status == ExpenseReportStatus.draft
    assert returned.submitted_at is None
    assert waiting.status == ExpenseReportStatus.submitted
    assert moved_on.status == ExpenseReportStatus.reimbursed
    assert trip.status == TripStatus.rejected
    # Idempotent.
    assert _reconcile_decided_subjects(db_session, org_a) == 0
