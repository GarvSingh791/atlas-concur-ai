"""C7-14 regression: manager dashboard approval queue + Recent Activity scope.

Staging repro: ``GET /analytics/overview`` as a manager listed pending approval
e01cdf2a for another manager's report (GET /approvals/e01cdf2a → 404), counted
org-wide (3 vs 2), returned ``amount: null`` on every queue row, and built Recent
Activity from the last 10 ORG-WIDE audit events (while GET /audit is 403).

Live-DB (conftest db_session: outer transaction rolled back after each test).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User

pytestmark = pytest.mark.db


def _client(app: Any, user: User) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    c.headers.update(
        {"Authorization": f"Bearer {create_access_token(user.id, user.organization_id)}"}
    )
    return c


def _audit(
    session: Session, org: Organization, *, actor: User | None, entity_type: str, entity_id: Any
) -> AuditLog:
    row = AuditLog(
        organization_id=org.id,
        actor_id=actor.id if actor else None,
        action=f"{entity_type}.c7_14_probe",
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=None,
        after_state=None,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def topology(
    db_session: Session,
    rbac_catalog: None,
    org_a: Organization,
    users_a: dict[str, User],
) -> dict[str, Any]:
    """manager (team: employee) vs manager2 (team: carlos) in the same org."""
    manager = users_a["manager"]
    employee = users_a["employee"]
    manager2 = f.make_user(db_session, org=org_a, role=RoleKey.manager, name="Manager Two")
    carlos = f.make_user(
        db_session, org=org_a, role=RoleKey.employee, manager_id=manager2.id, name="Carlos"
    )

    mine = f.make_expense_report(
        db_session,
        owner=employee,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("1339.62"),
    )
    mine_trip = f.make_trip(db_session, traveler=employee, estimated_cost=Decimal("812.40"))
    theirs = f.make_expense_report(
        db_session,
        owner=carlos,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("999.00"),
    )
    a_mine = f.make_approval(db_session, submitter=employee, subject_id=mine.id)
    a_mine_trip = f.make_approval(
        db_session,
        submitter=employee,
        subject_type=ApprovalSubjectType.trip,
        subject_id=mine_trip.id,
    )
    a_theirs = f.make_approval(db_session, submitter=carlos, subject_id=theirs.id)
    # A resolved approval in-team must not show in the pending queue.
    f.make_approval(
        db_session,
        submitter=employee,
        subject_id=uuid.uuid4(),
        current_status=ApprovalStatus.approved,
    )

    # Audit events: in-team subject (acted by someone else), manager's own action,
    # out-of-team subject, out-of-team actor on org-level entity.
    ev_team_subject = _audit(
        db_session, org_a, actor=users_a["finance"], entity_type="expense_report", entity_id=mine.id
    )
    ev_team_approval_system = _audit(
        db_session, org_a, actor=None, entity_type="approval", entity_id=a_mine.id
    )
    ev_self = _audit(
        db_session, org_a, actor=manager, entity_type="approval", entity_id=a_mine_trip.id
    )
    ev_other_team = _audit(
        db_session, org_a, actor=carlos, entity_type="expense_report", entity_id=theirs.id
    )
    ev_other_approval = _audit(
        db_session, org_a, actor=None, entity_type="approval", entity_id=a_theirs.id
    )
    ev_org_level = _audit(
        db_session,
        org_a,
        actor=users_a["finance"],
        entity_type="policy_rule",
        entity_id=uuid.uuid4(),
    )
    return {
        "manager": manager,
        "manager2": manager2,
        "finance": users_a["finance"],
        "employee": employee,
        "a_mine": a_mine,
        "a_mine_trip": a_mine_trip,
        "a_theirs": a_theirs,
        "visible_events": {ev_team_subject.id, ev_team_approval_system.id, ev_self.id},
        "hidden_events": {ev_other_team.id, ev_other_approval.id, ev_org_level.id},
    }


def test_manager_queue_is_team_scoped_with_real_amounts(app: Any, topology: dict) -> None:
    client = _client(app, topology["manager"])
    body = client.get("/api/v1/analytics/overview").json()["manager"]

    ids = {row["id"] for row in body["approval_queue"]}
    assert ids == {str(topology["a_mine"].id), str(topology["a_mine_trip"].id)}
    assert str(topology["a_theirs"].id) not in ids
    assert body["approval_queue_total"] == 2

    by_id = {row["id"]: row for row in body["approval_queue"]}
    assert by_id[str(topology["a_mine"].id)]["amount"] == pytest.approx(1339.62)
    assert by_id[str(topology["a_mine"].id)]["currency"] == "USD"
    assert by_id[str(topology["a_mine_trip"].id)]["amount"] == pytest.approx(812.40)


def test_every_manager_queue_row_opens(app: Any, topology: dict) -> None:
    """Queue parity with GET /approvals: no row may 404 on open."""
    client = _client(app, topology["manager"])
    queue = client.get("/api/v1/analytics/overview").json()["manager"]["approval_queue"]
    assert queue
    for row in queue:
        assert client.get(f"/api/v1/approvals/{row['id']}").status_code == 200
    listed = {
        a["id"]
        for a in client.get("/api/v1/approvals", params={"current_status": "pending"}).json()[
            "items"
        ]
    }
    assert {row["id"] for row in queue} <= listed


def test_other_manager_sees_only_their_team(app: Any, topology: dict) -> None:
    client = _client(app, topology["manager2"])
    body = client.get("/api/v1/analytics/overview").json()["manager"]
    assert {row["id"] for row in body["approval_queue"]} == {str(topology["a_theirs"].id)}
    assert body["approval_queue_total"] == 1


def test_manager_recent_activity_is_team_scoped(app: Any, topology: dict) -> None:
    client = _client(app, topology["manager"])
    activity = client.get("/api/v1/analytics/overview").json()["manager"]["recent_activity"]
    ids = {uuid.UUID(a["id"]) for a in activity}
    assert topology["visible_events"] <= ids
    assert not (ids & topology["hidden_events"]), activity


def test_finance_activity_and_employee_unchanged(app: Any, topology: dict) -> None:
    fin = _client(app, topology["finance"]).get("/api/v1/analytics/overview").json()["finance"]
    fin_ids = {uuid.UUID(a["id"]) for a in fin["recent_activity"]}
    assert (topology["visible_events"] | topology["hidden_events"]) <= fin_ids

    emp = _client(app, topology["employee"]).get("/api/v1/analytics/overview").json()
    assert emp["role"] == "employee"
    assert emp["manager"] is None and emp["finance"] is None
