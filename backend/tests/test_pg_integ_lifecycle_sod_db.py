"""Product-gaps integration, security item (b) — a withdrawal must not launder a return.

Gap 4 lets an owner WITHDRAW a returned report and move its lines to a fresh
draft (the move skips ``_refuse_reviewed_reports`` for a withdrawn source).  The
security review found that a line released from a withdrawn-after-request-changes
report then auto-approved — the reviewer's return was laundered.  Integrated rule:

* such a line is still movable and fileable (never blocked) and the fresh policy
  evaluation still runs, but the approval agent ESCALATES it with a
  ``refiled_returned_claim`` flag naming the original return;
* unrelated lines on the new report are unaffected (they still auto-approve);
* a withdrawn report whose history holds a REJECT — including a legacy pre-0014
  reject that froze no snapshot, followed by a reopen, a request-changes and the
  withdrawal — is still treated as REJECTED (``refiled_rejected_claim``);
* trips follow the same rule (a withdrawn-after-request-changes trip re-filed).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.tests.test_pg_lifecycle_db import (
    API,
    _approval,
    _cheap_trip,
    _client,
    _decide,
    _flags,
    _move,
    _new_draft,
    _report_with,
    _returned,
    _returned_trip,
    _submit,
)
from database.models.approval import ApprovalStatus
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.expense_report import ExpenseReport
from database.models.identity import User

pytestmark = pytest.mark.db


def _types(flags: list[dict]) -> list[str]:
    return [str(fl.get("flag_type")) for fl in flags]


def test_line_from_withdrawn_returned_report_is_escalated_naming_the_return(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, (small, _big) = _returned(
        db_session, emp, mgr, employee, ["12.50", "710.00"], title="Client dinner Q3"
    )
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200

    # The released line moves to a fresh draft that also carries an unrelated line.
    target = _new_draft(emp, "Refiled")
    assert _move(emp, returned.id, [small], target).status_code == 200
    target_id = uuid.UUID(target)
    target_report = db_session.get(ExpenseReport, target_id)
    assert target_report is not None
    f.make_expense(
        db_session,
        report=target_report,
        amount="19.40",
        vendor="Unrelated Cab",
        expense_date=date(2026, 3, 2),
    )
    target_report.total_amount = Decimal(str(target_report.total_amount)) + Decimal("19.40")
    db_session.flush()

    # Filing is never blocked …
    body = _submit(emp, target_id)
    flags = _flags(db_session, target_id)
    # … but the submission never auto-approves: it waits for a human.
    assert body["status"] == "submitted", flags
    assert _approval(db_session, target_id).current_status == ApprovalStatus.pending

    refiled = [fl for fl in flags if fl.get("flag_type") == "refiled_returned_claim"]
    assert len(refiled) == 1, flags  # only the released line, not the unrelated one
    assert refiled[0]["severity"] == "blocking"
    assert refiled[0]["related_subject_id"] == str(returned.id)
    assert "Client dinner Q3" in refiled[0]["reason"]
    assert refiled[0]["entry_ref"] == small.vendor
    # Not mistaken for a rejected claim, and the closed return no longer counts
    # as an OPEN return either.
    assert "refiled_rejected_claim" not in _types(flags)
    assert "open_returned_report" not in _types(flags)
    # The fresh policy evaluation still ran on the new report.
    summary = _approval(db_session, target_id).risk_summary or {}
    assert summary.get("subject_in_policy") is not None, summary


def test_unrelated_lines_after_a_withdrawal_still_auto_approve(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, _ = _returned(db_session, emp, mgr, employee, ["12.50", "710.00"])
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200

    fresh, _ = _report_with(db_session, employee, ["13.75"], title="Fresh", day=24)
    assert _submit(emp, fresh.id)["status"] == "approved", _flags(db_session, fresh.id)


def test_legacy_reject_then_request_changes_then_withdrawal_stays_rejected(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Closes the lifecycle reviewer's gap: a pre-0014 REJECT (no snapshot), a
    reopen, a resubmission sent back with request-changes, then a withdrawal —
    the withdrawal row says ``changes_requested`` but the line was REJECTED."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "reject")
    approval_id = _approval(db_session, report.id).id
    # Legacy: the reject step froze nothing.
    for step in db_session.scalars(
        sa.select(ApprovalStep).where(
            ApprovalStep.approval_id == approval_id,
            ApprovalStep.decision == ApprovalDecision.reject,
        )
    ):
        step.subject_snapshot = None
    db_session.flush()

    reopen = emp.patch(f"{API}/expense-reports/{report.id}", json={"status": "draft"})
    assert reopen.status_code == 200, reopen.text
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    w = emp.post(f"{API}/expense-reports/{report.id}/withdraw")
    assert w.status_code == 200, w.text
    assert w.json()["approval_status"] == "changes_requested"

    target = _new_draft(emp)
    assert _move(emp, report.id, [small], target).status_code == 200
    body = _submit(emp, uuid.UUID(target))
    flags = _flags(db_session, uuid.UUID(target))
    assert body["status"] == "submitted", flags
    assert "refiled_rejected_claim" in _types(flags), flags
    assert "refiled_returned_claim" not in _types(flags), flags


def test_rejected_then_withdrawn_line_is_still_flagged_as_rejected(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "reject")
    assert (
        emp.patch(f"{API}/expense-reports/{report.id}", json={"status": "draft"}).status_code == 200
    )
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200

    target = _new_draft(emp)
    assert _move(emp, report.id, [small], target).status_code == 200
    body = _submit(emp, uuid.UUID(target))
    flags = _flags(db_session, uuid.UUID(target))
    assert body["status"] == "submitted", flags
    assert "refiled_rejected_claim" in _types(flags), flags
    assert "refiled_returned_claim" not in _types(flags), flags


def test_trip_refiled_after_withdrawing_a_returned_trip_is_escalated(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned = _returned_trip(db_session, employee, mgr)
    assert emp.post(f"{API}/trips/{returned.id}/withdraw").status_code == 200

    start = returned.start_date
    assert isinstance(start, date)
    again = _cheap_trip(db_session, employee, returned.primary_destination, start)
    r = emp.patch(f"{API}/trips/{again.id}", json={"status": "pending_approval"})
    assert r.status_code == 200, r.text
    flags = _flags(db_session, again.id)
    assert r.json()["status"] == "pending_approval", flags
    refiled = [fl for fl in flags if fl.get("flag_type") == "refiled_returned_claim"]
    assert refiled and refiled[0]["related_subject_id"] == str(returned.id), flags
    assert "open_returned_report" not in _types(flags), flags

    # A trip elsewhere still auto-approves.
    other = _cheap_trip(db_session, employee, "Austin", start + timedelta(days=60))
    r = emp.patch(f"{API}/trips/{other.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "approved", _flags(db_session, other.id)
