"""Product-gaps integration — the manager Team view does not count withdrawn reports.

A withdrawn report (lifecycle gap 4) keeps ``status = draft`` with
``withdrawn_at`` set; it is closed and hidden from the default report lists.  The
team track's per-member ``reports_by_status`` counted it as an open draft.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import User

pytestmark = pytest.mark.db

URL = "/api/v1/team/members"


def test_withdrawn_report_is_not_counted_as_an_open_draft(
    manager_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    alice = users_a["employee"]
    f.make_expense_report(db_session, owner=alice, status=ExpenseReportStatus.draft)
    withdrawn = f.make_expense_report(db_session, owner=alice, status=ExpenseReportStatus.draft)
    withdrawn.withdrawn_at = datetime.now(timezone.utc)
    withdrawn.withdrawn_by_id = alice.id
    db_session.flush()

    resp = manager_client.get(URL)
    assert resp.status_code == 200, resp.text
    member = next(m for m in resp.json()["items"] if m["id"] == str(alice.id))
    assert member["reports_by_status"]["draft"] == 1
