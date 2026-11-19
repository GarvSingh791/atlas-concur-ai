"""C7 group G1 — write scope, self-approval and approval-trigger state guards.

Regression tests for the staging acceptance defects:

  C7-01  manager / finance could PATCH (edit, submit, cancel) records they can
         only READ (a direct report's trip / expense entry / expense report).
  C7-02  the traveler could PATCH their own trip to approved / rejected.
  C7-03  a manager (or finance_admin) could open and decide an approval on their
         own submission; an escalation with no manager was routed to nobody.
  C7-13  POST /approvals accepted a DRAFT subject (and auto-approved it).

DB-backed (shared conftest: rolled-back session, real JWT role clients).  The
approval trigger runs the real deterministic ApprovalAgent with local providers.
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

from backend.app.core.security import create_access_token
from backend.tests import factories as f
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.trip import TripStatus
from database.models.trip_segment import SegmentType

pytestmark = pytest.mark.db

API = "/api/v1"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _audit_count(session: Session, entity_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.entity_id == entity_id)
        )
        or 0
    )


def _err(resp: Any, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


@pytest.fixture()
def emp_records(db_session: Session, users_a: dict[str, User]) -> dict[str, Any]:
    """Employee-owned draft trip, draft report and one entry (12.50)."""
    employee = users_a["employee"]
    trip = f.make_trip(db_session, traveler=employee, status=TripStatus.draft)
    report = f.make_expense_report(
        db_session, owner=employee, status=ExpenseReportStatus.draft, total_amount=Decimal("12.50")
    )
    entry = f.make_expense(db_session, report=report, amount="12.50")
    return {"trip": trip, "report": report, "entry": entry}


# ---------------------------------------------------------------------------
# C7-01 — readable is not writable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "finance"])
def test_non_owner_reader_cannot_cancel_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], emp_records, role
) -> None:
    # NB: finance_admin holds no trip/expense update permission, so over HTTP its
    # 403 comes from the permission gate; the manager's 403 comes from
    # require_owner (C7-01).  test_service_layer_* proves require_owner also
    # stops finance if the permission catalog ever grants it update rights.
    client = _client(app, users_a[role])
    trip = emp_records["trip"]
    assert client.get(f"{API}/trips/{trip.id}").status_code == 200  # READ unchanged
    before = _audit_count(db_session, trip.id)

    resp = client.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"})

    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft
    assert _audit_count(db_session, trip.id) == before


@pytest.mark.parametrize("role", ["manager", "finance"])
def test_non_owner_reader_cannot_edit_entry(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], emp_records, role
) -> None:
    client = _client(app, users_a[role])
    report, entry = emp_records["report"], emp_records["entry"]
    assert client.get(f"{API}/expense-reports/{report.id}/entries").status_code == 200

    resp = client.patch(f"{API}/expenses/{entry.id}", json={"amount": "999.00"})

    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(entry)
    db_session.refresh(report)
    assert entry.amount == Decimal("12.50")
    assert report.total_amount == Decimal("12.50")


@pytest.mark.parametrize("role", ["manager", "finance"])
def test_non_owner_reader_cannot_submit_report(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], emp_records, role
) -> None:
    client = _client(app, users_a[role])
    report = emp_records["report"]
    assert client.get(f"{API}/expense-reports/{report.id}").status_code == 200

    resp = client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"})
    _err(resp, 403, "FORBIDDEN")

    # Supplying the correct If-Match version does not turn a non-owner into an owner.
    resp2 = client.patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": str(report.version)},
    )
    _err(resp2, 403, "FORBIDDEN")

    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft
    assert report.submitted_at is None


def test_owner_mutations_still_succeed(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], emp_records
) -> None:
    client = _client(app, users_a["employee"])
    trip, report, entry = emp_records["trip"], emp_records["report"], emp_records["entry"]

    assert client.patch(f"{API}/expenses/{entry.id}", json={"amount": "20.00"}).status_code == 200
    r = client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"})
    assert r.status_code == 200, r.text
    # Submitting opens the approval in the same transaction (C7R); a $20
    # in-policy report is auto-approved by the Approval Agent.
    assert r.json()["status"] == "approved"
    assert r.json()["total_amount"] in ("20.00", "20.0", 20.0, "20")
    r = client.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"})
    assert r.status_code == 200, r.text


def test_manager_can_mutate_own_records(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    manager = users_a["manager"]
    trip = f.make_trip(db_session, traveler=manager, status=TripStatus.draft)
    report = f.make_expense_report(db_session, owner=manager, status=ExpenseReportStatus.draft)
    entry = f.make_expense(db_session, report=report, amount="5.00")
    client = _client(app, manager)
    assert client.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"}).status_code == 200
    assert client.patch(f"{API}/expenses/{entry.id}", json={"amount": "6.00"}).status_code == 200
    r = client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"})
    assert r.status_code == 200, r.text


def test_out_of_scope_writes_stay_404(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
    emp_records,
) -> None:
    """Callers who cannot READ the record get 404 (no existence leak), not 403."""
    other_manager = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    peer = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    trip, report, entry = emp_records["trip"], emp_records["report"], emp_records["entry"]
    # (finance_admin holds neither trip.update.own nor expense.update.own, so it is
    # stopped by the permission gate first — covered at service level below.)
    for user in (other_manager, peer, users_b["employee"]):
        client = _client(app, user)
        _err(client.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"}), 404, "NOT_FOUND")
        _err(client.patch(f"{API}/expenses/{entry.id}", json={"amount": "1.00"}), 404, "NOT_FOUND")
        _err(
            client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"}),
            404,
            "NOT_FOUND",
        )
    # Nonexistent ids are 404 too.
    client = _client(app, users_a["manager"])
    _err(
        client.patch(f"{API}/trips/{uuid.uuid4()}", json={"status": "cancelled"}), 404, "NOT_FOUND"
    )


def test_manager_cannot_attach_report_to_direct_reports_trip(
    app: Any, rbac_catalog: None, users_a: dict[str, User], emp_records
) -> None:
    client = _client(app, users_a["manager"])
    resp = client.post(
        f"{API}/expense-reports",
        json={"title": "x", "currency": "USD", "trip_id": str(emp_records["trip"].id)},
    )
    _err(resp, 400, "BAD_REQUEST")


def test_idempotency_key_of_another_user_does_not_replay_their_report(
    app: Any, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    key = f"c7-g1-{uuid.uuid4()}"
    emp = _client(app, users_a["employee"])
    r1 = emp.post(
        f"{API}/expense-reports",
        json={"title": "mine", "currency": "USD"},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    mgr = _client(app, users_a["manager"])
    r2 = mgr.post(
        f"{API}/expense-reports",
        json={"title": "other", "currency": "USD"},
        headers={"Idempotency-Key": key},
    )
    _err(r2, 409, "CONFLICT")
    # The owner's own replay still returns the original report.
    r3 = emp.post(
        f"{API}/expense-reports",
        json={"title": "mine", "currency": "USD"},
        headers={"Idempotency-Key": key},
    )
    assert r3.status_code == 201 and r3.json()["id"] == r1.json()["id"]


# ---------------------------------------------------------------------------
# C7-02 — the traveler cannot approve / reject their own trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["approved", "rejected"])
def test_traveler_cannot_decide_own_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], target: str
) -> None:
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=TripStatus.pending_approval)
    before = _audit_count(db_session, trip.id)
    resp = _client(app, users_a["employee"]).patch(
        f"{API}/trips/{trip.id}", json={"status": target}
    )
    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(trip)
    assert trip.status == TripStatus.pending_approval
    assert _audit_count(db_session, trip.id) == before


def test_illegal_trip_move_is_still_409(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=TripStatus.draft)
    resp = _client(app, users_a["employee"]).patch(
        f"{API}/trips/{trip.id}", json={"status": "approved"}
    )
    _err(resp, 409, "CONFLICT")


def test_manager_cannot_approve_direct_reports_trip_via_patch(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=TripStatus.pending_approval)
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/trips/{trip.id}", json={"status": "approved"}
    )
    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(trip)
    assert trip.status == TripStatus.pending_approval


# ---------------------------------------------------------------------------
# C7-03 — no self-approval (decide), for every role
# ---------------------------------------------------------------------------


def _own_approval(session: Session, user: User) -> Approval:
    report = f.make_expense_report(session, owner=user, status=ExpenseReportStatus.submitted)
    return f.make_approval(
        session,
        submitter=user,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=ApprovalStatus.pending,
    )


@pytest.mark.parametrize("role", ["manager", "finance"])
@pytest.mark.parametrize("decision", ["approve", "reject", "request_changes"])
def test_cannot_decide_own_approval(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    role: str,
    decision: str,
) -> None:
    approval = _own_approval(db_session, users_a[role])
    resp = _client(app, users_a[role]).patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": decision, "reason": "self"},
        headers={"If-Match": str(approval.version)},
    )
    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.steps == []


def test_cannot_reverse_own_auto_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    approval = _own_approval(db_session, users_a["manager"])
    approval.current_status = ApprovalStatus.auto_approved
    approval.is_auto_approved = True
    db_session.flush()
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    _err(resp, 403, "FORBIDDEN")


def test_self_managed_manager_cannot_decide_own(
    app: Any, rbac_catalog: None, db_session: Session, org_a: Organization
) -> None:
    """A manager whose manager_id points to themselves (data anomaly) is still blocked."""
    mgr = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    mgr.manager_id = mgr.id
    db_session.flush()
    approval = _own_approval(db_session, mgr)
    resp = _client(app, mgr).patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
    _err(resp, 403, "FORBIDDEN")


def test_subject_owner_check_even_if_submitter_diverges(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Belt-and-braces: the live subject owner is checked, not only submitter_id."""
    manager = users_a["manager"]
    report = f.make_expense_report(db_session, owner=manager, status=ExpenseReportStatus.submitted)
    approval = f.make_approval(
        db_session,
        submitter=users_a["employee"],  # divergent row
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
    )
    resp = _client(app, manager).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    _err(resp, 403, "FORBIDDEN")


def test_manager_decides_direct_reports_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    approval = _own_approval(db_session, users_a["employee"])
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_status"] == "approved"


def test_finance_decides_managers_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    approval = _own_approval(db_session, users_a["manager"])
    resp = _client(app, users_a["finance"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# C7-03 / C7-13 — trigger gate (real deterministic ApprovalAgent)
# ---------------------------------------------------------------------------


def _approvals_for(session: Session, subject_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Approval)
            .where(Approval.subject_id == subject_id)
        )
        or 0
    )


@pytest.mark.parametrize("role", ["manager", "finance"])
def test_cannot_trigger_approval_on_own_submission(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a, role
) -> None:
    report = f.make_expense_report(
        db_session, owner=users_a[role], status=ExpenseReportStatus.submitted
    )
    resp = _client(app, users_a[role]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(resp, 403, "FORBIDDEN")
    assert _approvals_for(db_session, report.id) == 0


def test_trigger_on_draft_report_is_400(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a
) -> None:
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.draft
    )
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")
    assert _approvals_for(db_session, report.id) == 0


@pytest.mark.parametrize("status", [TripStatus.draft, TripStatus.approved, TripStatus.cancelled])
def test_trigger_on_unsubmitted_trip_is_400(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a, status
) -> None:
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=status)
    resp = _client(app, users_a["finance"]).post(
        f"{API}/approvals", json={"subject_type": "trip", "subject_id": str(trip.id)}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")
    assert _approvals_for(db_session, trip.id) == 0


def test_trigger_outside_team_is_404(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, org_a, users_a
) -> None:
    other = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    report = f.make_expense_report(db_session, owner=other, status=ExpenseReportStatus.submitted)
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(resp, 404, "NOT_FOUND")


def test_trigger_on_submitted_report_succeeds_then_409(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a
) -> None:
    report = f.make_expense_report(
        db_session,
        owner=users_a["employee"],
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("900.00"),  # > 500 → escalates (pending)
    )
    client = _client(app, users_a["manager"])
    body = {"subject_type": "expense_report", "subject_id": str(report.id)}
    r1 = client.post(f"{API}/approvals", json=body)
    assert r1.status_code == 201, r1.text
    assert r1.json()["current_status"] == "pending"
    assert r1.json()["risk_summary"]["routed_to_approver_id"] == str(users_a["manager"].id)
    _err(client.post(f"{API}/approvals", json=body), 409, "CONFLICT")


def test_escalation_without_manager_routes_to_finance(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, org_a, users_a
) -> None:
    lone = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=None)
    report = f.make_expense_report(
        db_session, owner=lone, status=ExpenseReportStatus.submitted, total_amount=Decimal("720")
    )
    resp = _client(app, users_a["finance"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    assert resp.status_code == 201, resp.text
    summary = resp.json()["risk_summary"]
    assert resp.json()["current_status"] == "pending"
    assert summary["routed_to_approver_id"] == str(users_a["finance"].id)
    assert summary["routing_fallback"] == "finance_admin"


def test_self_managed_submitter_routes_to_finance(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, org_a, users_a
) -> None:
    mgr = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    mgr.manager_id = mgr.id
    db_session.flush()
    trip = f.make_trip(
        db_session,
        traveler=mgr,
        status=TripStatus.pending_approval,
        estimated_cost=Decimal("1500"),
    )
    resp = _client(app, users_a["finance"]).post(
        f"{API}/approvals", json={"subject_type": "trip", "subject_id": str(trip.id)}
    )
    assert resp.status_code == 201, resp.text
    routed = resp.json()["risk_summary"]["routed_to_approver_id"]
    assert routed == str(users_a["finance"].id)
    assert routed != str(mgr.id)


# ---------------------------------------------------------------------------
# Defence in depth — the service layer itself refuses non-owners (any role),
# independent of the HTTP permission catalog.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "finance"])
def test_service_layer_refuses_non_owner_writes(
    db_session: Session, users_a: dict[str, User], emp_records, role: str
) -> None:
    from backend.app.core.errors import ForbiddenError
    from backend.app.modules.expense import service as expense_service
    from backend.app.modules.expense.schemas import ExpenseUpdate
    from backend.app.modules.travel import service as travel_service

    user = users_a[role]
    with pytest.raises(ForbiddenError):
        travel_service.transition_trip(
            db_session, user, emp_records["trip"].id, TripStatus.cancelled
        )
    with pytest.raises(ForbiddenError):
        expense_service.transition_report(
            db_session, user, emp_records["report"].id, ExpenseReportStatus.submitted
        )
    with pytest.raises(ForbiddenError):
        expense_service.update_entry(
            db_session, user, emp_records["entry"].id, ExpenseUpdate(amount=Decimal("999.00"))
        )


def test_service_layer_finance_cannot_trigger_on_own_trip(
    db_session: Session, users_a: dict[str, User]
) -> None:
    from backend.app.core.errors import ForbiddenError
    from backend.app.modules.approval.service import validate_trigger_subject

    fin = users_a["finance"]
    trip = f.make_trip(db_session, traveler=fin, status=TripStatus.pending_approval)
    with pytest.raises(ForbiddenError):
        validate_trigger_subject(
            db_session, user=fin, subject_type=ApprovalSubjectType.trip, subject_id=trip.id
        )


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------


def _trip_approval(
    session: Session,
    users_a: dict[str, User],
    *,
    current_status: ApprovalStatus = ApprovalStatus.pending,
    trip_status: TripStatus = TripStatus.pending_approval,
) -> tuple[Any, Approval]:
    trip = f.make_trip(session, traveler=users_a["employee"], status=trip_status)
    approval = f.make_approval(
        session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=current_status,
        is_auto_approved=current_status == ApprovalStatus.auto_approved,
    )
    return trip, approval


def _trip_audits(session: Session, trip_id: uuid.UUID) -> list[AuditLog]:
    return list(
        session.scalars(
            sa.select(AuditLog).where(
                AuditLog.entity_id == trip_id, AuditLog.action == "trip.status_changed"
            )
        ).all()
    )


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("approve", TripStatus.approved), ("reject", TripStatus.rejected)],
)
def test_trip_decision_writes_trip_status(
    app: Any, rbac_catalog: None, db_session: Session, users_a, decision, expected
) -> None:
    """C7-02 follow-through: the approval workflow sets the trip outcome."""
    trip, approval = _trip_approval(db_session, users_a)
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": decision, "reason": "r"}
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(trip)
    assert trip.status == expected
    audits = _trip_audits(db_session, trip.id)
    assert len(audits) == 1
    assert audits[0].actor_id == users_a["manager"].id
    after = audits[0].after_state
    assert after is not None and after["status"] == expected.value


def test_workflow_approved_trip_can_then_be_completed_by_traveler(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    trip, approval = _trip_approval(db_session, users_a)
    # Product gap 8: a trip can be completed only once it has ended.
    trip.start_date = date.today() - timedelta(days=6)
    trip.end_date = date.today() - timedelta(days=3)
    db_session.flush()
    assert (
        _client(app, users_a["manager"])
        .patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
        .status_code
        == 200
    )
    resp = _client(app, users_a["employee"]).patch(
        f"{API}/trips/{trip.id}", json={"status": "completed"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"


def test_request_changes_returns_trip_to_draft(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """C7R: a returned trip goes back to draft (editable) instead of dead-ending."""
    trip, approval = _trip_approval(db_session, users_a)
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "request_changes", "reason": "r"}
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft
    audits = _trip_audits(db_session, trip.id)
    assert len(audits) == 1
    assert audits[0].after_state is not None and audits[0].after_state["status"] == "draft"


def test_returned_trip_resubmission_reopens_approval_for_a_new_decision(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """C7R: draft -> pending_approval reopens the SAME approval, routed to an
    independent approver with a fresh escalate-only assessment; the traveler
    still cannot decide it, the manager can, and the decision moves the trip."""
    trip, approval = _trip_approval(db_session, users_a)
    manager = _client(app, users_a["manager"])
    employee = _client(app, users_a["employee"])
    assert (
        manager.patch(
            f"{API}/approvals/{approval.id}", json={"decision": "request_changes", "reason": "r"}
        ).status_code
        == 200
    )
    # While returned, the approval cannot be decided again.
    assert manager.patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    ).status_code in (400, 409)

    resub = employee.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"})
    assert resub.status_code == 200, resub.text
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.is_auto_approved is False
    assert approval.risk_summary is not None
    assert approval.risk_summary.get("reassessed_on_resubmission") is True
    assert approval.risk_summary.get("route") == "escalate"
    routed = approval.risk_summary.get("routed_to_approver_id")
    assert routed is not None
    assert str(routed) != str(users_a["employee"].id)

    # Separation of duties still holds on the reopened approval.
    assert (
        employee.patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"}).status_code
        == 403
    )
    ok = manager.patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
    assert ok.status_code == 200, ok.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.approved


def test_returned_trip_tells_its_traveler_why(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """C7R review: a trip returned for changes is a draft again, but TripResponse
    carried no approval status / reason, so the traveler could not tell it was
    returned (it looked like any draft).  Old: no approval_status field."""
    trip, approval = _trip_approval(db_session, users_a)
    manager = _client(app, users_a["manager"])
    employee = _client(app, users_a["employee"])
    assert (
        manager.patch(
            f"{API}/approvals/{approval.id}",
            json={"decision": "request_changes", "reason": "Pick a cheaper hotel"},
        ).status_code
        == 200
    )
    detail = employee.get(f"{API}/trips/{trip.id}").json()
    assert detail["status"] == "draft"
    assert detail["approval_status"] == "changes_requested"
    assert detail["approval_reason"] == "Pick a cheaper hotel"
    listed = employee.get(f"{API}/trips").json()["items"]
    row = next(r for r in listed if r["id"] == str(trip.id))
    assert row["approval_status"] == "changes_requested"
    assert row["approval_reason"] == "Pick a cheaper hotel"

    resub = employee.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"})
    assert resub.status_code == 200, resub.text
    # Re-queued: pending again, and the stale reason is no longer shown.
    assert resub.json()["approval_status"] == "pending"
    assert resub.json()["approval_reason"] is None


def test_first_trip_submission_without_approval_is_unaffected(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=TripStatus.draft)
    resp = _client(app, users_a["employee"]).patch(
        f"{API}/trips/{trip.id}", json={"status": "pending_approval"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_approval"


def test_cannot_decide_after_trip_cancelled(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """C7-13 (decide side): trip cancelled after the trigger → decision refused."""
    trip, approval = _trip_approval(db_session, users_a)
    cancel = _client(app, users_a["employee"]).patch(
        f"{API}/trips/{trip.id}", json={"status": "cancelled"}
    )
    assert cancel.status_code == 200, cancel.text

    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")
    db_session.refresh(approval)
    db_session.refresh(trip)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.steps == []
    assert trip.status == TripStatus.cancelled


@pytest.mark.parametrize("report_status", [ExpenseReportStatus.draft, ExpenseReportStatus.approved])
def test_cannot_decide_report_not_in_submitted(
    app: Any, rbac_catalog: None, db_session: Session, users_a, report_status
) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"], status=report_status)
    approval = f.make_approval(
        db_session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
    )
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")


def test_trigger_auto_approved_trip_moves_to_approved_then_reversible(
    app: Any, rbac_catalog: None, local_ai: None, db_session: Session, users_a
) -> None:
    """An auto-approved trip leaves pending_approval; a human reversal still applies."""
    trip = f.make_trip(
        db_session,
        traveler=users_a["employee"],
        status=TripStatus.pending_approval,
        estimated_cost=Decimal("120"),
    )
    # Approval-time evaluation (C7-05) needs a priced segment; an unpriced one
    # fails closed to a human.  Rail carries no blocking rules.
    f.make_trip_segment(
        db_session,
        trip=trip,
        segment_type=SegmentType.rail,
        amount=Decimal("120"),
        currency="USD",
        in_policy=True,
    )
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "trip", "subject_id": str(trip.id)}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["current_status"] == "auto_approved", resp.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.approved
    auto_audit = _trip_audits(db_session, trip.id)
    assert len(auto_audit) == 1 and auto_audit[0].actor_id is None

    rev = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{resp.json()['id']}", json={"decision": "reject", "reason": "no"}
    )
    assert rev.status_code == 200, rev.text
    db_session.refresh(trip)
    assert trip.status == TripStatus.rejected


def test_cannot_reverse_auto_approval_after_trip_completed(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    trip, approval = _trip_approval(
        db_session,
        users_a,
        current_status=ApprovalStatus.auto_approved,
        trip_status=TripStatus.completed,
    )
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "reject", "reason": "late"}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")


def test_approved_state_only_decidable_for_auto_approved_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a
) -> None:
    """A PENDING approval whose trip is already approved is not decidable."""
    _trip, approval = _trip_approval(db_session, users_a, trip_status=TripStatus.approved)
    resp = _client(app, users_a["manager"]).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "reject", "reason": "x"}
    )
    _err(resp, 400, "INVALID_SUBJECT_STATE")


@pytest.mark.parametrize("subject", ["dangling", "other_owner"])
def test_submitter_alone_cannot_decide(
    app: Any, rbac_catalog: None, db_session: Session, users_a, subject
) -> None:
    """Isolates the submitter_id operand: the caller is the recorded submitter
    but NOT the subject owner (subject absent, or owned by the employee)."""
    manager = users_a["manager"]
    if subject == "dangling":
        subject_id = uuid.uuid4()
    else:
        subject_id = f.make_expense_report(
            db_session, owner=users_a["employee"], status=ExpenseReportStatus.submitted
        ).id
    approval = f.make_approval(
        db_session,
        submitter=manager,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=subject_id,
    )
    resp = _client(app, manager).patch(
        f"{API}/approvals/{approval.id}", json={"decision": "approve"}
    )
    _err(resp, 403, "FORBIDDEN")
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending


def test_router_needs_review_duplicate_backstop_is_409(
    app: Any,
    rbac_catalog: None,
    local_ai: None,
    db_session: Session,
    users_a,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent-trigger race: the pre-agent gate passed, but the row now exists.

    With validate_trigger_subject neutralised, the REAL agent hits its idempotency
    check, returns needs_review, and the router's step-4 backstop must 409 without
    persisting anything new.
    """
    from backend.app.modules.approval import service as approval_service

    monkeypatch.setattr(approval_service, "validate_trigger_subject", lambda *a, **k: None)
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.submitted
    )
    f.make_approval(
        db_session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
    )
    resp = _client(app, users_a["manager"]).post(
        f"{API}/approvals", json={"subject_type": "expense_report", "subject_id": str(report.id)}
    )
    _err(resp, 409, "CONFLICT")
    assert _approvals_for(db_session, report.id) == 1
