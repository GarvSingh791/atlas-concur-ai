"""Product-gaps integration, item (d) — the bell surfaces the lifecycle events.

The shell track derived ``reimbursed`` / ``completed`` notifications from status
alone, before any endpoint could set those statuses.  Integrated, they are driven
end to end by the lifecycle track's real actions: finance reimburses a report
(owner notified, dated by ``reimbursed_at``, naming the reference), the traveler
completes an ended trip, and the elapsed-trip sweep completes another.  Nobody
but the owner sees them (not the finance admin who acted, not the manager).
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.tests.test_pg_lifecycle_db import (
    API,
    _approved_trip,
    _auto_approved_report,
    _client,
)
from database.models.identity import User
from database.seeds.complete_elapsed_trips import complete_elapsed

pytestmark = pytest.mark.db


def _feed(client: Any) -> list[dict[str, Any]]:
    resp = client.get(f"{API}/notifications")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["items"])


def _of(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [i for i in items if i["kind"] == kind]


def test_reimbursement_notifies_the_owner_with_its_reference(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, finance, manager = users_a["employee"], users_a["finance"], users_a["manager"]
    emp, fin = _client(app, employee), _client(app, finance)
    report = _auto_approved_report(db_session, emp, employee)
    assert _of(_feed(emp), "reimbursed") == []

    paid = fin.post(f"{API}/expense-reports/{report.id}/reimburse")
    assert paid.status_code == 200, paid.text
    reference = paid.json()["reimbursement_reference"]

    items = _of(_feed(emp), "reimbursed")
    assert len(items) == 1, items
    item = items[0]
    assert item["subject_id"] == str(report.id)
    assert item["link"] == f"/expenses?report={report.id}"
    assert reference in item["body"]
    assert item["read"] is False
    # The finance admin who acted and the manager are not notified.
    assert _of(_feed(fin), "reimbursed") == []
    assert _of(_feed(_client(app, manager)), "reimbursed") == []


def test_trip_completion_by_traveler_and_by_sweep_notifies_the_traveler(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp = _client(app, employee)
    by_hand = _approved_trip(db_session, employee, ends_in_days=-1)
    by_sweep = _approved_trip(db_session, employee, ends_in_days=-5)
    assert _of(_feed(emp), "completed") == []

    done = emp.post(f"{API}/trips/{by_hand.id}/complete")
    assert done.status_code == 200, done.text
    swept = complete_elapsed(lambda: contextlib.nullcontext(db_session))
    assert swept["failed"] == 0 and swept["completed"] >= 1
    db_session.expire_all()

    completed = {i["subject_id"] for i in _of(_feed(emp), "completed")}
    assert completed == {str(by_hand.id), str(by_sweep.id)}
    assert _of(_feed(_client(app, manager)), "completed") == []
