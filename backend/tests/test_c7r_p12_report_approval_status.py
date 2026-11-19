"""C7R / P12 — an approval decision on an EXPENSE REPORT moves the report.

Before the fix an approved report stayed ``submitted`` (only trips followed the
decision).  The approval workflow now drives the report through the expense
service's own workflow state machine (``apply_workflow_transition``):

  approve          submitted → approved
  reject           submitted → rejected
  request_changes  submitted → draft (returned to the employee, editable)
  auto-approve     submitted → approved (actor None — the agent decided)
  human reversal   approved (auto) → rejected / draft

Each move writes one ``expense_report.status_changed`` audit row and bumps the
report version.  A resubmission of a returned (or reopened rejected) report
re-queues the SAME approval as ``pending`` (history preserved, one approval per
subject), so the workflow never dead-ends.

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.app.core.errors import ConflictError
from backend.app.core.security import create_access_token
from backend.app.modules.approval import service as approval_service
from backend.app.modules.expense import service as expense_service
from backend.tests import factories as f
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User

pytestmark = pytest.mark.db

API = "/api/v1"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _err(resp: Any, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


def _report_audits(session: Session, report_id: uuid.UUID) -> list[AuditLog]:
    return list(
        session.scalars(
            sa.select(AuditLog)
            .where(
                AuditLog.entity_id == report_id,
                AuditLog.action == "expense_report.status_changed",
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
    )


def _approval_audits(session: Session, approval_id: uuid.UUID, action: str) -> list[AuditLog]:
    return list(
        session.scalars(
            sa.select(AuditLog).where(AuditLog.entity_id == approval_id, AuditLog.action == action)
        ).all()
    )


def _submitted_report(
    session: Session,
    users_a: dict[str, User],
    *,
    current_status: ApprovalStatus = ApprovalStatus.pending,
    report_status: ExpenseReportStatus = ExpenseReportStatus.submitted,
) -> tuple[ExpenseReport, Approval]:
    employee = users_a["employee"]
    report = f.make_expense_report(
        session, owner=employee, status=report_status, total_amount=Decimal("12.50")
    )
    f.make_expense(session, report=report, amount="12.50")
    approval = f.make_approval(
        session,
        submitter=employee,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=current_status,
        is_auto_approved=current_status == ApprovalStatus.auto_approved,
    )
    return report, approval


# ---------------------------------------------------------------------------
# Human decisions on a submitted report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("approve", ExpenseReportStatus.approved),
        ("reject", ExpenseReportStatus.rejected),
        ("request_changes", ExpenseReportStatus.draft),
    ],
)
def test_decision_moves_report_status_with_audit(
    app: Any, rbac_catalog: None, db_session: Session, users_a, decision, expected
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    version_before = report.version
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": decision, "reason": "because"},
        headers={"If-Match": str(approval.version)},
    )
    assert resp.status_code == 200, resp.text
    # The decision detail already carries the moved report.
    assert resp.json()["expense_report"]["report"]["status"] == expected.value

    db_session.refresh(report)
    assert report.status == expected
    assert report.version == version_before + 1
    assert report.updated_by == users_a["manager"].id

    audits = _report_audits(db_session, report.id)
    assert len(audits) == 1
    assert audits[0].actor_id == users_a["manager"].id
    assert audits[0].before_state is not None
    assert audits[0].before_state["status"] == "submitted"
    assert audits[0].after_state is not None
    assert audits[0].after_state["status"] == expected.value


def test_request_changes_returns_report_editable_and_clears_submitted_at(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    report.submitted_at = sa.func.now()
    db_session.flush()
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "attach the hotel folio"},
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft
    assert report.submitted_at is None

    # The employee can now edit the returned report's entries again.
    entry = report.entries[0]
    edit = _client(app, users_a["employee"]).patch(
        f"{API}/expenses/{entry.id}", json={"description": "fixed"}
    )
    assert edit.status_code == 200, edit.text


def test_resubmitting_returned_report_reopens_same_approval_then_approve(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """changes requested → employee resubmits → SAME approval pending → approve."""
    report, approval = _submitted_report(
        db_session, users_a, current_status=ApprovalStatus.auto_approved
    )
    # Auto-approve applied to the subject (as POST /approvals does).
    approval_service.apply_auto_approval_to_subject(db_session, approval)
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved

    manager = _client(app, users_a["manager"])
    rc = manager.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "missing receipt"},
    )
    assert rc.status_code == 200, rc.text
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft

    # A changes_requested approval cannot be decided while the report is out.
    _err(
        manager.patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"}),
        409,
        "CONFLICT",
    )

    resubmit = _client(app, users_a["employee"]).patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": str(report.version)},
    )
    assert resubmit.status_code == 200, resubmit.text

    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending
    # A reopened approval is a human-review item, not an auto-approved lane row.
    assert approval.is_auto_approved is False
    assert approval.risk_summary is not None and "resubmitted_at" in approval.risk_summary
    reopened = _approval_audits(db_session, approval.id, "approval.reopened")
    assert len(reopened) == 1
    assert reopened[0].actor_id == users_a["employee"].id
    assert reopened[0].before_state is not None
    assert reopened[0].before_state["current_status"] == "changes_requested"
    assert reopened[0].before_state["is_auto_approved"] is True
    # Exactly one approval per subject — the re-trigger path stays 409.
    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(Approval).where(Approval.subject_id == report.id)
    )
    assert count == 1

    ok = manager.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers={"If-Match": str(approval.version)},
    )
    assert ok.status_code == 200, ok.text
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved
    # Append-only history (the factory approval carries no auto_approve step):
    # the returned decision is kept, the fresh decision is appended after it.
    decisions = [s["decision"] for s in ok.json()["steps"]]
    assert decisions == [ApprovalDecision.request_changes.value, ApprovalDecision.approve.value]


def test_reopened_rejected_report_resubmission_requeues_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    manager = _client(app, users_a["manager"])
    employee = _client(app, users_a["employee"])
    assert (
        manager.patch(
            f"{API}/approvals/{approval.id}", json={"decision": "reject", "reason": "no"}
        ).status_code
        == 200
    )
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.rejected
    # rejected → draft is the owner's existing reopen edge; then resubmit.
    for target in ("draft", "submitted"):
        r = employee.patch(f"{API}/expense-reports/{report.id}", json={"status": target})
        assert r.status_code == 200, r.text
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending


def test_first_submission_opens_exactly_one_new_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """A first draft→submitted opens ONE new approval (nothing is "reopened").

    Previously this asserted that a first submission created no approval — that
    was the C7R defect (a submitted report never reached a reviewer).  The full
    submit → approval behaviour is covered in test_c7r_submit_opens_approval.py.
    """
    report = f.make_expense_report(db_session, owner=users_a["employee"])
    f.make_expense(db_session, report=report)  # UAT2 (M12): empty reports are refused
    r = _client(app, users_a["employee"]).patch(
        f"{API}/expense-reports/{report.id}", json={"status": "submitted"}
    )
    assert r.status_code == 200, r.text
    approvals = list(
        db_session.scalars(sa.select(Approval).where(Approval.subject_id == report.id)).all()
    )
    assert len(approvals) == 1
    assert _approval_audits(db_session, approvals[0].id, "approval.reopened") == []


# ---------------------------------------------------------------------------
# Auto-approval + reversal
# ---------------------------------------------------------------------------


def test_trigger_auto_approve_moves_report_to_approved(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /approvals → agent auto-approves → the report becomes approved."""
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.submitted
    )

    from backend.app.main_entry.app import app as real_app  # noqa: PLC0415
    from backend.app.modules.ai.runtime import get_orchestrator  # noqa: PLC0415

    class _AutoOrchestrator:
        def handle_task(self, task: Any, *, context: Any, session: Session) -> Any:
            session.add(
                Approval(
                    organization_id=report.organization_id,
                    subject_type=ApprovalSubjectType.expense_report,
                    subject_id=report.id,
                    submitter_id=report.owner_id,
                    current_status=ApprovalStatus.auto_approved,
                    is_auto_approved=True,
                )
            )
            session.flush()

            class _Result:
                status = "ok"

            class _Outcome:
                result = _Result()

            return _Outcome()

    assert real_app is app
    app.dependency_overrides[get_orchestrator] = lambda: _AutoOrchestrator()
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["current_status"] == "auto_approved"
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved
    audits = _report_audits(db_session, report.id)
    assert len(audits) == 1 and audits[0].actor_id is None

    # Re-trigger on the now-approved report keeps the 409 (approval exists).
    again = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(again, 409, "CONFLICT")


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("approve", ExpenseReportStatus.approved),
        ("reject", ExpenseReportStatus.rejected),
        ("request_changes", ExpenseReportStatus.draft),
    ],
)
def test_human_reversal_of_auto_approved_report(
    app: Any, rbac_catalog: None, db_session: Session, users_a, decision, expected
) -> None:
    report, approval = _submitted_report(
        db_session,
        users_a,
        current_status=ApprovalStatus.auto_approved,
        report_status=ExpenseReportStatus.approved,
    )
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": decision, "reason": "review"}
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(report)
    assert report.status == expected
    # Confirming the auto-approval is a no-op on the report (no audit row).
    assert len(_report_audits(db_session, report.id)) == (0 if decision == "approve" else 1)


def test_cannot_reverse_auto_approval_after_reimbursed(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(
        db_session,
        users_a,
        current_status=ApprovalStatus.auto_approved,
        report_status=ExpenseReportStatus.reimbursed,
    )
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "reject", "reason": "late"}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.reimbursed


def test_trigger_on_approved_report_without_approval_is_400(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a
) -> None:
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.approved
    )
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")


# ---------------------------------------------------------------------------
# Idempotency / concurrency
# ---------------------------------------------------------------------------


def test_second_decision_is_409_and_report_unchanged(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    manager = _client(app, users_a["manager"])
    assert (
        manager.patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"}).status_code
        == 200
    )
    again = manager.patch(
        f"{API}/approvals/{approval.id}", json={"decision": "reject", "reason": "x"}
    )
    _err(again, 409, "CONFLICT")
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved
    assert len(_report_audits(db_session, report.id)) == 1


def test_stale_if_match_leaves_report_submitted(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers={"If-Match": str(approval.version + 7)},
    )
    _err(resp, 409, "CONFLICT")
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.submitted
    assert _report_audits(db_session, report.id) == []


# ---------------------------------------------------------------------------
# Expense workflow state machine (service level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (ExpenseReportStatus.draft, ExpenseReportStatus.approved),
        (ExpenseReportStatus.rejected, ExpenseReportStatus.approved),
        (ExpenseReportStatus.reimbursed, ExpenseReportStatus.rejected),
        (ExpenseReportStatus.reimbursed, ExpenseReportStatus.draft),
    ],
)
def test_workflow_transition_refuses_illegal_edges_without_audit(
    db_session: Session, users_a, start, target
) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"], status=start)
    with pytest.raises(ConflictError):
        expense_service.apply_workflow_transition(
            db_session, report, target, actor_id=users_a["manager"].id
        )
    db_session.refresh(report)
    assert report.status == start
    assert _report_audits(db_session, report.id) == []


def test_owner_patch_still_cannot_use_workflow_only_edges(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """submitted → draft is a workflow edge (changes requested), not an owner recall."""
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.submitted
    )
    resp = _client(app, users_a["employee"]).patch(
        f"{API}/expense-reports/{report.id}", json={"status": "draft"}
    )
    _err(resp, 409, "CONFLICT")
    for target in ("approved", "rejected"):
        r = _client(app, users_a["employee"]).patch(
            f"{API}/expense-reports/{report.id}", json={"status": target}
        )
        _err(r, 403, "FORBIDDEN")


# ---------------------------------------------------------------------------
# Owner visibility: the employee (no approval.read) can see why it came back
# ---------------------------------------------------------------------------


def test_owner_sees_returned_status_and_reason_on_report(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report, approval = _submitted_report(db_session, users_a)
    employee = _client(app, users_a["employee"])

    pending = employee.get(f"{API}/expense-reports/{report.id}")
    assert pending.status_code == 200, pending.text
    assert pending.json()["approval_status"] == "pending"
    assert pending.json()["approval_reason"] is None

    rc = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "  attach the hotel folio "},
    )
    assert rc.status_code == 200, rc.text

    detail = employee.get(f"{API}/expense-reports/{report.id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "draft"
    assert body["approval_status"] == "changes_requested"
    assert body["approval_reason"] == "attach the hotel folio"

    listing = employee.get(f"{API}/expense-reports")
    assert listing.status_code == 200, listing.text
    row = next(i for i in listing.json()["items"] if i["id"] == str(report.id))
    assert row["approval_status"] == "changes_requested"


def test_report_without_approval_has_null_approval_fields(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"])
    body = _client(app, users_a["employee"]).get(f"{API}/expense-reports/{report.id}").json()
    assert body["approval_status"] is None
    assert body["approval_reason"] is None


# ---------------------------------------------------------------------------
# Review fixes: the reopened approval is RE-ASSESSED, never the stale snapshot
# ---------------------------------------------------------------------------

_STALE_IN_POLICY = {
    "route": "auto_approve",
    "subject_type": "expense_report",
    "subject_in_policy": True,
    "is_policy_compliant": True,
    "blocking_flag_count": 0,
    "requires_approver": False,
    "subject_total_usd": 12.5,
    "flags": [],
}


def _auto_approved_then_returned(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> tuple[ExpenseReport, Approval]:
    """An agent-auto-approved 12.50 report a manager has sent back (draft)."""
    report, approval = _submitted_report(
        db_session, users_a, current_status=ApprovalStatus.auto_approved
    )
    approval.risk_summary = dict(_STALE_IN_POLICY)
    db_session.flush()
    approval_service.apply_auto_approval_to_subject(db_session, approval)
    rc = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "missing receipt"},
    )
    assert rc.status_code == 200, rc.text
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft
    return report, approval


def _inflate_and_resubmit(
    app: Any, db_session: Session, users_a: dict[str, User], report: ExpenseReport
) -> None:
    employee = _client(app, users_a["employee"])
    entry_id = db_session.scalar(sa.select(Expense.id).where(Expense.report_id == report.id))
    edit = employee.patch(f"{API}/expenses/{entry_id}", json={"amount": "98765.00"})
    assert edit.status_code == 200, edit.text
    db_session.refresh(report)
    resubmit = employee.patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": str(report.version)},
    )
    assert resubmit.status_code == 200, resubmit.text


def test_resubmission_reassesses_risk_over_current_entries(
    app: Any, rbac_catalog: None, db_session: Session, org_a, users_a
) -> None:
    """Inflating a returned report must not inherit the old in-policy verdict."""
    policy = f.make_policy(db_session, org=org_a)
    f.make_policy_rule(
        db_session,
        policy=policy,
        definition={"currency": "USD", "tiers": [{"tier": "domestic", "cap": 500}]},
    )
    report, approval = _auto_approved_then_returned(app, db_session, users_a)

    _inflate_and_resubmit(app, db_session, users_a, report)

    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending
    summary = approval.risk_summary or {}
    # Fresh snapshot of the CURRENT contents, routed for a human decision.
    assert summary["route"] == "escalate"
    assert summary["reassessed_on_resubmission"] is True
    assert "resubmitted_at" in summary
    assert summary["subject_total_usd"] == pytest.approx(98765.0)
    assert summary["subject_in_policy"] is False
    assert summary["is_policy_compliant"] is False
    assert summary["blocking_flag_count"] >= 1
    assert summary["flags"], summary
    assert summary["routed_to_approver_id"] == str(users_a["manager"].id)

    # The approver's detail view shows the re-assessed verdict, not the stale one.
    detail = _client(app, users_a["manager"]).get(f"{API}/approvals/{approval.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["risk_summary"]["subject_total_usd"] == pytest.approx(98765.0)

    # The superseded snapshot is preserved in the audit trail.
    reopened = _approval_audits(db_session, approval.id, "approval.reopened")
    assert len(reopened) == 1
    assert reopened[0].before_state is not None
    assert reopened[0].before_state["risk_summary"] == _STALE_IN_POLICY
    assert reopened[0].after_state is not None
    assert reopened[0].after_state["risk_summary"]["subject_total_usd"] == pytest.approx(98765.0)


def test_reopened_auto_approval_is_routed_to_independent_approver(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """C7-03 holds on reopen: no manager → an active finance_admin, never nobody."""
    report, approval = _auto_approved_then_returned(app, db_session, users_a)
    users_a["employee"].manager_id = None
    db_session.flush()

    _inflate_and_resubmit(app, db_session, users_a, report)

    db_session.refresh(approval)
    summary = approval.risk_summary or {}
    assert approval.current_status == ApprovalStatus.pending
    assert summary["routed_to_approver_id"] == str(users_a["finance"].id)
    assert summary["routing_fallback"] == "finance_admin"


def test_reassessment_failure_drops_stale_verdict(
    app: Any, rbac_catalog: None, db_session: Session, users_a, monkeypatch
) -> None:
    """If the re-evaluation itself fails, no stale in-policy verdict survives."""
    from backend.app.modules.approval import lifecycle

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("policy engine unavailable")

    report, approval = _auto_approved_then_returned(app, db_session, users_a)
    monkeypatch.setattr(lifecycle, "assess_risk", _boom)

    _inflate_and_resubmit(app, db_session, users_a, report)

    db_session.refresh(approval)
    summary = approval.risk_summary or {}
    assert approval.current_status == ApprovalStatus.pending
    assert summary["route"] == "escalate"
    # Unknown (rendered "Not evaluated"), never the stale True nor a false "out of
    # policy" with no flags to explain it (C7R cross-review).
    assert summary["subject_in_policy"] is None
    assert summary["evaluation_failed"] is True
    assert summary["is_policy_compliant"] is None
    assert summary["subject_total_usd"] is None
    assert summary["policy_rationale"]
    # Still routed to a human approver (manager), never to nobody.
    assert summary["routed_to_approver_id"] == str(users_a["manager"].id)


def test_owner_reason_tracks_latest_returned_decision(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """Reason A → resubmit (no stale reason) → reason B shown, not A."""
    report, approval = _submitted_report(db_session, users_a)
    manager = _client(app, users_a["manager"])
    employee = _client(app, users_a["employee"])

    first = manager.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "reason A"},
    )
    assert first.status_code == 200, first.text
    assert employee.get(f"{API}/expense-reports/{report.id}").json()["approval_reason"] == (
        "reason A"
    )

    db_session.refresh(report)
    resubmit = employee.patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": str(report.version)},
    )
    assert resubmit.status_code == 200, resubmit.text
    body = employee.get(f"{API}/expense-reports/{report.id}").json()
    assert body["approval_status"] == "pending"
    assert body["approval_reason"] is None

    db_session.refresh(approval)
    second = manager.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "request_changes", "reason": "reason B"},
        headers={"If-Match": str(approval.version)},
    )
    assert second.status_code == 200, second.text
    body = employee.get(f"{API}/expense-reports/{report.id}").json()
    assert body["approval_status"] == "changes_requested"
    assert body["approval_reason"] == "reason B"
