"""#3b / C4 — GET /api/v1/audit row-level behaviour against real Postgres.

The hermetic suite (test_audit_api.py) proves the org predicate and filter
columns reach the SQL; this module proves the RESULT ROWS are correct:

  - cross-org rows are excluded (both ``total`` and the returned ids)
  - ``?action=`` returns only matching rows (row-level filter correctness)
  - ``?entity_type=`` + ``?actor_id=`` narrow correctly; actor_name resolves
  - ``?entity_id=`` of ANOTHER org's row returns ``total == 0`` (no leak)
  - default order is most-recent first

Rows are seeded through ``record_audit`` — the single audit write path — inside
the function-scoped rollback session, so nothing persists between tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.app.core.audit import record_audit
from backend.tests.api.endpoints import API
from database.models.audit_log import AuditLog
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

_URL = f"{API}/audit"


def _seed(
    session: Session,
    org: Organization,
    *,
    action: str,
    entity_type: str = "trip",
    actor: User | None = None,
) -> AuditLog:
    return record_audit(
        session,
        organization_id=org.id,
        action=action,
        entity_type=entity_type,
        entity_id=uuid.uuid4(),
        actor_id=actor.id if actor is not None else None,
        after_state={"status": "draft"},
    )


def _all_items(client: TestClient, **params: str) -> dict:
    r = client.get(_URL, params={"size": 100, **params})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def test_audit_cross_org_rows_excluded(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
    finance_client: TestClient,
) -> None:
    a_rows = [
        _seed(db_session, org_a, action="trip.created", actor=users_a["employee"]),
        _seed(db_session, org_a, action="trip.submitted", actor=users_a["employee"]),
        _seed(db_session, org_a, action="approval.decided", entity_type="approval"),
    ]
    b_rows = [
        _seed(db_session, org_b, action="trip.created", actor=users_b["employee"]),
        _seed(db_session, org_b, action="trip.created", actor=users_b["employee"]),
    ]
    a_ids = {str(r.id) for r in a_rows}
    b_ids = {str(r.id) for r in b_rows}

    body = _all_items(finance_client)
    returned = {item["id"] for item in body["items"]}

    assert body["total"] == len(a_rows)
    assert returned == a_ids
    assert returned.isdisjoint(b_ids)

    # Filtering on a value shared by both orgs still only yields org A rows.
    created = _all_items(finance_client, action="trip.created")
    assert created["total"] == 1
    assert {i["id"] for i in created["items"]} == {str(a_rows[0].id)}


def test_audit_entity_id_of_other_org_returns_empty(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
    finance_client: TestClient,
) -> None:
    _seed(db_session, org_a, action="trip.created", actor=users_a["employee"])
    foreign = _seed(db_session, org_b, action="trip.created", actor=users_b["employee"])

    body = _all_items(finance_client, entity_id=str(foreign.entity_id))
    assert body["total"] == 0
    assert body["items"] == []

    # Same for actor_id of a foreign user — the filter never widens the org scope.
    by_actor = _all_items(finance_client, actor_id=str(users_b["employee"].id))
    assert by_actor["total"] == 0


def test_audit_action_filter_row_level(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    finance_client: TestClient,
) -> None:
    for _ in range(3):
        _seed(db_session, org_a, action="expense_report.submitted", entity_type="expense_report")
    _seed(db_session, org_a, action="trip.created", actor=users_a["employee"])
    _seed(db_session, org_a, action="approval.decided", entity_type="approval")

    body = _all_items(finance_client, action="expense_report.submitted")
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert {i["action"] for i in body["items"]} == {"expense_report.submitted"}

    none = _all_items(finance_client, action="does.not.exist")
    assert none["total"] == 0 and none["items"] == []


def test_audit_entity_type_and_actor_filters_and_actor_name(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    finance_client: TestClient,
) -> None:
    employee = users_a["employee"]
    mine = _seed(db_session, org_a, action="trip.created", actor=employee)
    _seed(db_session, org_a, action="trip.created", actor=users_a["manager"])
    system = _seed(db_session, org_a, action="approval.decided", entity_type="approval")

    by_actor = _all_items(finance_client, actor_id=str(employee.id))
    assert by_actor["total"] == 1
    item = by_actor["items"][0]
    assert item["id"] == str(mine.id)
    assert item["actor_name"] == employee.name

    by_type = _all_items(finance_client, entity_type="approval")
    assert by_type["total"] == 1
    sys_item = by_type["items"][0]
    assert sys_item["id"] == str(system.id)
    assert sys_item["actor_id"] is None and sys_item["actor_name"] is None


def test_audit_default_order_most_recent_first(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    finance_client: TestClient,
) -> None:
    # created_at defaults to now(), which is constant inside one transaction —
    # stamp distinct times so the ordering is observable.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for i in (2, 0, 3, 1):
        row = _seed(db_session, org_a, action=f"trip.step{i}", actor=users_a["employee"])
        row.created_at = base + timedelta(hours=i)

    db_session.flush()

    desc = [i["action"] for i in _all_items(finance_client)["items"]]
    assert desc == ["trip.step3", "trip.step2", "trip.step1", "trip.step0"]
    asc = [i["action"] for i in _all_items(finance_client, sort="created_at:asc")["items"]]
    assert asc == ["trip.step0", "trip.step1", "trip.step2", "trip.step3"]
