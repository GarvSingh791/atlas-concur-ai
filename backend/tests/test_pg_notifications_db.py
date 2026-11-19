"""Product gap 3 — derived notifications (GET /notifications + read state).

Scoping is the point: every test pairs the recipient with someone who must NOT
see the item (a second manager / employee of the same role, the report owner vs
their manager, another tenant).

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.app.modules.notifications import service as notif_service
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.notification_read import NotificationRead
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

API = "/api/v1"
NOW = datetime.now(timezone.utc)


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _feed(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/notifications", params=params)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["items"])


def _unread(client: TestClient) -> int:
    resp = client.get(f"{API}/notifications/unread-count")
    assert resp.status_code == 200, resp.text
    return int(resp.json()["unread"])


def _pending_report_approval(
    db: Session,
    owner: User,
    routed_to: User | None,
    *,
    title: str = "Q3 offsite",
    route: str = "escalate",
    updated_at: datetime | None = None,
) -> tuple[ExpenseReport, Approval]:
    report = f.make_expense_report(
        db, owner=owner, title=title, status=ExpenseReportStatus.submitted
    )
    summary: dict[str, Any] = {"route": route}
    if routed_to is not None:
        summary["routed_to_approver_id"] = str(routed_to.id)
    extra: dict[str, Any] = {"updated_at": updated_at} if updated_at else {}
    approval = f.make_approval(
        db,
        submitter=owner,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=ApprovalStatus.pending,
        risk_summary=summary,
        **extra,
    )
    return report, approval


def _decided(
    db: Session,
    owner: User,
    approver: User | None,
    decision: ApprovalDecision,
    *,
    title: str = "Client dinner",
    reason: str | None = None,
    at: datetime | None = None,
) -> tuple[ExpenseReport, Approval]:
    status = {
        ApprovalDecision.approve: (ExpenseReportStatus.approved, ApprovalStatus.approved),
        ApprovalDecision.auto_approve: (ExpenseReportStatus.approved, ApprovalStatus.auto_approved),
        ApprovalDecision.reject: (ExpenseReportStatus.rejected, ApprovalStatus.rejected),
        ApprovalDecision.request_changes: (
            ExpenseReportStatus.draft,
            ApprovalStatus.changes_requested,
        ),
    }[decision]
    extra: dict[str, Any] = {"created_at": at} if at else {}
    report = f.make_expense_report(db, owner=owner, title=title, status=status[0])
    approval = f.make_approval(
        db,
        submitter=owner,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=status[1],
    )
    f.make_approval_step(
        db,
        approval=approval,
        decision=decision,
        approver_id=approver.id if approver else None,
        reason=reason,
        decided_at=at or NOW,
        **extra,
    )
    return report, approval


@pytest.fixture()
def team(db_session: Session, org_a: Organization, users_a: dict[str, User]) -> dict[str, User]:
    """users_a + a second manager with their own report, and a second employee
    under the first manager (two users of each role)."""
    manager2 = f.make_user(db_session, org=org_a, role=RoleKey.manager, name="Manager Two")
    employee2 = f.make_user(
        db_session, org=org_a, role=RoleKey.employee, manager_id=users_a["manager"].id
    )
    employee3 = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=manager2.id)
    return {**users_a, "manager2": manager2, "employee2": employee2, "employee3": employee3}


# ---------------------------------------------------------------------------
# Approver items
# ---------------------------------------------------------------------------


def test_pending_approval_reaches_only_the_routed_manager(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _report, approval = _pending_report_approval(db_session, team["employee"], team["manager"])

    items = _feed(_client(app, team["manager"]))
    assert [i["kind"] for i in items] == ["approval_pending"]
    item = items[0]
    assert item["subject_type"] == "approval"
    assert item["subject_id"] == str(approval.id)
    assert item["link"] == f"/approvals?approval={approval.id}"
    assert "Q3 offsite" in item["body"]
    assert item["read"] is False

    # Same role, different team: nothing.
    assert _feed(_client(app, team["manager2"])) == []
    # The submitter is not told their own approval is "waiting on them".
    assert _feed(_client(app, team["employee"])) == []
    # A teammate of the submitter sees nothing either.
    assert _feed(_client(app, team["employee2"])) == []


def test_routed_to_someone_outside_scope_is_not_shown(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    """routed_to alone is not enough: the approval must be inside the caller's read
    scope (manager2 is not employee's manager)."""
    _pending_report_approval(db_session, team["employee"], team["manager2"])
    assert _feed(_client(app, team["manager2"])) == []


def test_finance_sees_escalations_but_not_own_or_auto(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _r, escalated = _pending_report_approval(db_session, team["employee"], team["manager"])
    _pending_report_approval(db_session, team["employee2"], team["manager"], route="auto_approve")
    # finance's own submission routed elsewhere: never shown to them
    _pending_report_approval(db_session, team["finance"], team["manager"], title="Mine")

    items = _feed(_client(app, team["finance"]))
    assert [(i["kind"], i["subject_id"]) for i in items] == [
        ("approval_escalated", str(escalated.id))
    ]


def test_finance_routed_item_is_pending_not_escalated(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _r, approval = _pending_report_approval(db_session, team["employee"], team["finance"])
    items = _feed(_client(app, team["finance"]))
    assert [(i["kind"], i["subject_id"]) for i in items] == [("approval_pending", str(approval.id))]


def test_old_escalations_fall_out_of_the_window(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _pending_report_approval(
        db_session, team["employee"], team["manager"], updated_at=NOW - timedelta(days=45)
    )
    assert _feed(_client(app, team["finance"])) == []


# ---------------------------------------------------------------------------
# Owner items
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "kind", "title"),
    [
        (ApprovalDecision.approve, "approved", "Expense report approved"),
        (ApprovalDecision.auto_approve, "approved", "Expense report auto-approved"),
        (ApprovalDecision.reject, "rejected", "Expense report rejected"),
        (ApprovalDecision.request_changes, "returned", "Expense report returned for changes"),
    ],
)
def test_owner_is_told_about_decisions_and_nobody_else_is(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    team: dict[str, User],
    decision: ApprovalDecision,
    kind: str,
    title: str,
) -> None:
    approver = None if decision == ApprovalDecision.auto_approve else team["manager"]
    report, _a = _decided(
        db_session, team["employee"], approver, decision, reason="Missing receipt"
    )
    items = _feed(_client(app, team["employee"]))
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == kind
    assert item["title"] == title
    assert item["subject_type"] == "expense_report"
    assert item["subject_id"] == str(report.id)
    assert item["link"] == f"/expenses?report={report.id}"
    if kind in ("rejected", "returned"):
        assert "Missing receipt" in item["body"]

    # The deciding manager and a same-role colleague of the owner see nothing.
    assert _feed(_client(app, team["manager"])) == []
    assert _feed(_client(app, team["employee2"])) == []


def test_trip_decision_links_to_the_trip(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    trip = f.make_trip(
        db_session,
        traveler=team["employee"],
        primary_destination="Lisbon",
        status=TripStatus.approved,
    )
    approval = f.make_approval(
        db_session,
        submitter=team["employee"],
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.approved,
    )
    f.make_approval_step(db_session, approval=approval, approver_id=team["manager"].id)
    items = _feed(_client(app, team["employee"]))
    assert [(i["kind"], i["subject_type"], i["link"]) for i in items] == [
        ("approved", "trip", f"/trips?trip={trip.id}")
    ]
    assert "Lisbon" in items[0]["body"]


def test_old_decisions_fall_out_of_the_window(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _decided(
        db_session,
        team["employee"],
        team["manager"],
        ApprovalDecision.approve,
        at=NOW - timedelta(days=31),
    )
    assert _feed(_client(app, team["employee"])) == []


def test_reimbursed_reports_and_completed_trips_surface_for_owner_only(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    report = f.make_expense_report(
        db_session, owner=team["employee"], title="Paid back", status=ExpenseReportStatus.reimbursed
    )
    trip = f.make_trip(db_session, traveler=team["employee"], status=TripStatus.completed)
    items = _feed(_client(app, team["employee"]))
    assert {(i["kind"], i["subject_id"]) for i in items} == {
        ("reimbursed", str(report.id)),
        ("completed", str(trip.id)),
    }
    assert _feed(_client(app, team["manager"])) == []
    assert _feed(_client(app, team["employee2"])) == []


def test_tenant_isolation(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    team: dict[str, User],
    users_b: dict[str, User],
) -> None:
    _decided(db_session, team["employee"], team["manager"], ApprovalDecision.approve)
    _pending_report_approval(db_session, team["employee"], team["manager"])
    # Org B users — including a forged routed id pointing at org B's finance —
    # see nothing from org A.
    _pending_report_approval(db_session, team["employee2"], users_b["finance"])
    assert _feed(_client(app, users_b["finance"])) == []
    assert _feed(_client(app, users_b["employee"])) == []


# ---------------------------------------------------------------------------
# Ordering, cap, pagination
# ---------------------------------------------------------------------------


def test_newest_first_and_paginated(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    for days in (5, 1, 3):
        _decided(
            db_session,
            team["employee"],
            team["manager"],
            ApprovalDecision.approve,
            title=f"Report {days}d",
            at=NOW - timedelta(days=days),
        )
    client = _client(app, team["employee"])
    bodies = [i["body"] for i in _feed(client)]
    assert bodies == ["Report 1d", "Report 3d", "Report 5d"]

    page2 = client.get(f"{API}/notifications", params={"page": 2, "size": 2}).json()
    assert page2["total"] == 3
    assert [i["body"] for i in page2["items"]] == ["Report 5d"]


def test_feed_is_capped(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    for n in range(notif_service.FEED_CAP + 5):
        _decided(
            db_session,
            team["employee"],
            team["manager"],
            ApprovalDecision.approve,
            title=f"R{n}",
            at=NOW - timedelta(minutes=n),
        )
    client = _client(app, team["employee"])
    body = client.get(f"{API}/notifications", params={"size": 100}).json()
    assert body["total"] == notif_service.FEED_CAP
    assert len(body["items"]) == notif_service.FEED_CAP
    assert body["items"][0]["body"] == "R0"
    assert _unread(client) == notif_service.FEED_CAP


# ---------------------------------------------------------------------------
# Read state
# ---------------------------------------------------------------------------


def test_mark_one_read_then_all(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _decided(db_session, team["employee"], team["manager"], ApprovalDecision.approve, title="A")
    _decided(db_session, team["employee"], team["manager"], ApprovalDecision.reject, title="B")
    client = _client(app, team["employee"])
    items = _feed(client)
    assert _unread(client) == 2

    first = items[0]["id"]
    resp = client.post(f"{API}/notifications/{first}/read")
    assert resp.status_code == 200 and resp.json() == {"marked": 1}
    assert client.post(f"{API}/notifications/{first}/read").status_code == 200  # idempotent
    assert _unread(client) == 1
    assert {i["id"]: i["read"] for i in _feed(client)}[first] is True

    resp = client.post(f"{API}/notifications/read-all")
    assert resp.status_code == 200 and resp.json() == {"marked": 1}
    assert _unread(client) == 0
    assert all(i["read"] for i in _feed(client))
    assert client.post(f"{API}/notifications/read-all").json() == {"marked": 0}


def test_cannot_mark_someone_elses_or_unknown_key(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _decided(db_session, team["employee"], team["manager"], ApprovalDecision.approve)
    owner_key = _feed(_client(app, team["employee"]))[0]["id"]
    other = _client(app, team["employee2"])
    for key in (owner_key, "decision:00000000-0000-0000-0000-000000000000", "x" * 150):
        resp = other.post(f"{API}/notifications/{key}/read")
        assert resp.status_code == 404, (key, resp.text)
    assert (
        db_session.scalar(
            sa.select(sa.func.count())
            .select_from(NotificationRead)
            .where(NotificationRead.user_id == team["employee2"].id)
        )
        == 0
    )
    # The owner's item is still unread for the owner.
    assert _unread(_client(app, team["employee"])) == 1


def test_read_state_is_per_user(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    """Finance and the routed manager see the same approval; one reading it does not
    mark it for the other."""
    _pending_report_approval(db_session, team["employee"], team["manager"])
    mgr = _client(app, team["manager"])
    fin = _client(app, team["finance"])
    assert _unread(mgr) == 1 and _unread(fin) == 1
    assert mgr.post(f"{API}/notifications/read-all").status_code == 200
    assert _unread(mgr) == 0
    assert _unread(fin) == 1


def test_resubmission_creates_a_fresh_pending_item(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    _r, approval = _pending_report_approval(db_session, team["employee"], team["manager"])
    mgr = _client(app, team["manager"])
    first_key = _feed(mgr)[0]["id"]
    mgr.post(f"{API}/notifications/read-all")
    assert _unread(mgr) == 0

    # Returned, then resubmitted: one human step in the history, pending again.
    f.make_approval_step(
        db_session,
        approval=approval,
        decision=ApprovalDecision.request_changes,
        approver_id=team["manager"].id,
        reason="Fix it",
    )
    items = _feed(mgr)
    assert len(items) == 1 and items[0]["id"] != first_key
    assert _unread(mgr) == 1


def test_read_all_prunes_only_stale_invisible_rows(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    emp = team["employee"]
    _decided(db_session, emp, team["manager"], ApprovalDecision.approve)
    db_session.add_all(
        [
            NotificationRead(
                user_id=emp.id, notification_key="decision:gone", read_at=NOW - timedelta(days=60)
            ),
            NotificationRead(user_id=emp.id, notification_key="decision:recent"),
        ]
    )
    db_session.flush()
    _client(app, emp).post(f"{API}/notifications/read-all")
    keys = set(
        db_session.scalars(
            sa.select(NotificationRead.notification_key).where(NotificationRead.user_id == emp.id)
        )
    )
    assert "decision:gone" not in keys
    assert "decision:recent" in keys
    assert len(keys) == 2  # recent + the visible item just marked


def test_unauthenticated_is_401(app: Any) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"{API}/notifications").status_code == 401
    assert client.get(f"{API}/notifications/unread-count").status_code == 401
    assert client.post(f"{API}/notifications/read-all").status_code == 401


def test_divergent_submitter_and_owner_shows_nothing(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    """Fail closed on a corrupt row: an approval submitted by `employee` on a report
    OWNED by `employee2` notifies neither of them."""
    report = f.make_expense_report(
        db_session, owner=team["employee2"], status=ExpenseReportStatus.approved
    )
    approval = f.make_approval(
        db_session,
        submitter=team["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=ApprovalStatus.approved,
    )
    f.make_approval_step(db_session, approval=approval, approver_id=team["manager"].id)
    assert _feed(_client(app, team["employee"])) == []
    assert _feed(_client(app, team["employee2"])) == []


def test_same_item_read_state_is_per_user(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User], org_a: Organization
) -> None:
    """Two finance admins see the SAME escalation (same id); one marking it read
    leaves it unread for the other."""
    finance2 = f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)
    _pending_report_approval(db_session, team["employee"], team["manager"])
    fin1 = _client(app, team["finance"])
    fin2 = _client(app, finance2)
    assert _feed(fin1)[0]["id"] == _feed(fin2)[0]["id"]
    fin1.post(f"{API}/notifications/read-all")
    assert _unread(fin1) == 0
    assert _unread(fin2) == 1
    assert _feed(fin2)[0]["read"] is False


def test_read_all_keeps_old_read_rows_of_visible_items(
    app: Any, rbac_catalog: None, db_session: Session, team: dict[str, User]
) -> None:
    """A pending approval can stay pending past the window; its old read row must
    survive pruning or it would come back as unread."""
    _pending_report_approval(db_session, team["employee"], team["manager"])
    mgr = _client(app, team["manager"])
    key = _feed(mgr)[0]["id"]
    db_session.add(
        NotificationRead(
            user_id=team["manager"].id, notification_key=key, read_at=NOW - timedelta(days=90)
        )
    )
    db_session.flush()
    mgr.post(f"{API}/notifications/read-all")
    assert _unread(mgr) == 0
    assert _feed(mgr)[0]["read"] is True
