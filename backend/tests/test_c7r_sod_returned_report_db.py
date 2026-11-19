"""C7R review — a returned report cannot be laundered into an auto-approval.

Since submit opens the approval and small, compliant reports auto-approve, an
owner whose report a reviewer RETURNED (request changes, or reject → reopen)
could escape the human by moving its entries into a fresh report and submitting
that one.  Segregation of duties now holds:

  * POST /expense-reports/{id}/entries/move → 409 ``REPORT_UNDER_REVIEW`` when
    the SOURCE report has human-review history (approval currently returned, or
    any past human reject / request_changes step — a reversed auto-approval
    included); moving entries INTO a returned report is allowed (it goes back to
    a human on resubmission — that is how "add the missing line" is met); moves
    between fresh drafts still work;
  * while the owner has an UNRESOLVED returned report, any other report they
    submit carries a blocking ``open_returned_report`` flag → escalated, never
    auto-approved (closes retype / edit-then-retype / vendor-swap re-filing);
    resubmitting the returned report clears it;
  * a FINALLY rejected subject is a closed outcome (no penalty on unrelated
    submissions); re-filing it (report: date ±1 day + amount ± max($1, 2%), any
    vendor; trip: same destination, overlapping dates) gets a blocking
    ``refiled_rejected_claim`` flag at APPROVAL time → escalated — the claimant's
    submit is never blocked by it;
  * a trip under human re-review cannot be cancelled (409 SUBJECT_UNDER_REVIEW).

Also (C7R review, MEDIUM): PATCH /receipts/{id} syncs the linked entry under the
report lock and follows an entry a concurrent move re-parented — never writing
to an entry that now sits on a submitted report.

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.app.modules.approval.lifecycle import subjects_with_human_review_history
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.receipt import OcrStatus, Receipt

pytestmark = pytest.mark.db

API = "/api/v1"
DET = "deterministic"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _report_with(
    db: Session, owner: User, amounts: list[str], *, title: str = "Trip"
) -> tuple[ExpenseReport, list[Expense]]:
    report = f.make_expense_report(db, owner=owner, title=title)
    report.currency = "USD"
    entries = [
        f.make_expense(
            db,
            report=report,
            amount=amount,
            vendor=f"Vendor {i}",
            expense_date=date(2026, 6, 10 + i),
        )
        for i, amount in enumerate(amounts)
    ]
    report.total_amount = sum((Decimal(a) for a in amounts), Decimal("0"))
    db.flush()
    return report, entries


def _approval(db: Session, report: ExpenseReport) -> Approval:
    approval = db.scalar(sa.select(Approval).where(Approval.subject_id == report.id))
    assert approval is not None
    return approval


def _submit(client: TestClient, report: ExpenseReport) -> dict:
    r = client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"})
    assert r.status_code == 200, r.text
    return r.json()


def _decide(client: TestClient, approval: Approval, decision: str) -> None:
    r = client.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": decision, "reason": "Attach the folio for the hotel."},
    )
    assert r.status_code == 200, r.text


def _new_draft(client: TestClient, title: str = "Fresh report") -> str:
    r = client.post(f"{API}/expense-reports", json={"title": title, "currency": "USD"})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _move(client: TestClient, source_id: Any, entries: list[Expense], target_id: Any) -> Any:
    return client.post(
        f"{API}/expense-reports/{source_id}/entries/move",
        json={"entry_ids": [str(e.id) for e in entries], "target_report_id": str(target_id)},
    )


def _assert_under_review(resp: Any) -> None:
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "REPORT_UNDER_REVIEW"
    assert "returned" in err["message"]


def _moved_audits(db: Session) -> int:
    return int(
        db.scalar(sa.select(sa.func.count(AuditLog.id)).where(AuditLog.action == "expense.moved"))
        or 0
    )


# ---------------------------------------------------------------------------
# (a) request changes → move out of the returned report → 409
# ---------------------------------------------------------------------------


def test_request_changes_then_move_into_fresh_report_is_refused(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    # $750 → escalated to the manager, who sends it back.
    report_a, (small, big) = _report_with(db_session, employee, ["40.00", "710.00"])
    assert _submit(emp, report_a)["approval_status"] == "pending"
    _decide(mgr, _approval(db_session, report_a), "request_changes")
    db_session.refresh(report_a)
    assert report_a.status == ExpenseReportStatus.draft

    # The escape: a fresh report C that would auto-approve a small entry.
    report_c = _new_draft(emp)
    _assert_under_review(_move(emp, report_a.id, [small], report_c))
    db_session.expire_all()
    assert db_session.get(Expense, small.id).report_id == report_a.id  # type: ignore[union-attr]
    assert _moved_audits(db_session) == 0

    # Moving entries INTO the returned report is allowed (reviewer: "add the
    # missing line") — the report still goes back to a human on resubmission.
    _, (stray,) = _report_with(db_session, employee, ["5.00"], title="Other draft")
    moved = _move(emp, stray.report_id, [stray], report_a.id)
    assert moved.status_code == 200, moved.text
    db_session.expire_all()
    assert db_session.get(Expense, stray.id).report_id == report_a.id  # type: ignore[union-attr]
    assert _moved_audits(db_session) == 1

    # The legitimate path still works: fix A and resubmit → the SAME approval
    # is re-queued for a human (never auto-approved).
    body = _submit(emp, report_a)
    assert body["status"] == "submitted"
    assert body["approval_status"] == "pending"


def test_reject_then_reopen_then_move_is_refused(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (entry, _other) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")
    reopen = emp.patch(f"{API}/expense-reports/{report_a.id}", json={"status": "draft"})
    assert reopen.status_code == 200, reopen.text

    _assert_under_review(_move(emp, report_a.id, [entry], _new_draft(emp)))
    db_session.refresh(entry)
    assert entry.report_id == report_a.id


def test_reversed_auto_approval_then_move_is_refused(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """A human reversing the agent's auto-approval returns the report too."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (entry,) = _report_with(db_session, employee, ["12.50"])
    assert _submit(emp, report_a)["status"] == "approved"  # auto-approved
    _decide(mgr, _approval(db_session, report_a), "request_changes")
    db_session.refresh(report_a)
    assert report_a.status == ExpenseReportStatus.draft

    _assert_under_review(_move(emp, report_a.id, [entry], _new_draft(emp)))


def test_past_human_return_counts_even_when_not_currently_returned(
    db_session: Session, users_a: dict[str, User]
) -> None:
    """History, not just the current status: a resubmitted (pending) approval
    with an earlier request_changes step still counts."""
    employee = users_a["employee"]
    returned_before, _ = _report_with(db_session, employee, ["10.00"], title="Was returned")
    auto_only, _ = _report_with(db_session, employee, ["10.00"], title="Auto only")
    fresh, _ = _report_with(db_session, employee, ["10.00"], title="Fresh")

    pending = f.make_approval(
        db_session,
        submitter=employee,
        subject_id=returned_before.id,
        current_status=ApprovalStatus.pending,
    )
    f.make_approval_step(
        db_session, approval=pending, decision=ApprovalDecision.request_changes, step_order=1
    )
    auto = f.make_approval(
        db_session,
        submitter=employee,
        subject_id=auto_only.id,
        current_status=ApprovalStatus.auto_approved,
        is_auto_approved=True,
    )
    f.make_approval_step(
        db_session, approval=auto, decision=ApprovalDecision.auto_approve, step_order=1
    )

    reviewed = subjects_with_human_review_history(
        db_session,
        organization_id=employee.organization_id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_ids=[returned_before.id, auto_only.id, fresh.id],
    )
    assert reviewed == {returned_before.id}
    # Tenant guard: another organization id sees nothing.
    assert (
        subjects_with_human_review_history(
            db_session,
            organization_id=uuid.uuid4(),
            subject_type=ApprovalSubjectType.expense_report,
            subject_ids=[returned_before.id],
        )
        == set()
    )


# ---------------------------------------------------------------------------
# (b) moves between fresh drafts still work
# ---------------------------------------------------------------------------


def test_move_between_two_fresh_drafts_still_works(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(app, employee)
    source, (entry, keep) = _report_with(db_session, employee, ["30.00", "20.00"])
    target = _new_draft(emp)

    r = _move(emp, source.id, [entry], target)
    assert r.status_code == 200, r.text
    assert r.json()["moved_entry_ids"] == [str(entry.id)]
    db_session.expire_all()
    assert str(db_session.get(Expense, entry.id).report_id) == target  # type: ignore[union-attr]
    assert db_session.get(Expense, keep.id).report_id == source.id  # type: ignore[union-attr]
    assert _moved_audits(db_session) == 1


# ---------------------------------------------------------------------------
# Retyping a rejected claim onto a fresh report never auto-approves
# ---------------------------------------------------------------------------


def test_refiled_rejected_claim_is_escalated_not_auto_approved(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")

    created = emp.post(
        f"{API}/expense-reports",
        json={
            "title": "Re-filed",
            "currency": "USD",
            "entries": [
                {
                    "category": small.category.value,
                    "amount": str(small.amount),
                    "currency": small.currency,
                    "expense_date": str(small.expense_date),
                    "vendor": small.vendor,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    report_c = db_session.get(ExpenseReport, uuid.UUID(created.json()["id"]))
    assert report_c is not None

    body = _submit(emp, report_c)
    assert body["status"] == "submitted"  # NOT auto-approved
    approval = _approval(db_session, report_c)
    assert approval.current_status == ApprovalStatus.pending
    flags = (approval.risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags


def _create_with_entry(client: TestClient, *, amount: str, vendor: str, day: date) -> str:
    created = client.post(
        f"{API}/expense-reports",
        json={
            "title": "Re-filed",
            "currency": "USD",
            "entries": [
                {
                    "category": ExpenseCategory.meals.value,
                    "amount": amount,
                    "currency": "USD",
                    "expense_date": str(day),
                    "vendor": vendor,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _assert_escalated_for_open_return(db: Session, client: TestClient, report_id: str) -> None:
    report = db.get(ExpenseReport, uuid.UUID(report_id))
    assert report is not None
    body = _submit(client, report)
    assert body["status"] == "submitted"  # NOT auto-approved
    approval = _approval(db, report)
    assert approval.current_status == ApprovalStatus.pending
    assert approval.is_auto_approved is False
    flags = (approval.risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "open_returned_report" for fl in flags), flags


def test_edit_then_retype_while_returned_report_is_open_never_auto_approves(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """request_changes → owner edits the returned entry's date → retypes the
    ORIGINAL $40 claim into a new report: content no longer matches anything,
    yet it must still reach a human."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "request_changes")
    original_day, original_vendor = small.expense_date, small.vendor or "Vendor 0"
    assert isinstance(original_day, date)
    r = emp.patch(f"{API}/expenses/{small.id}", json={"expense_date": "2026-01-01"})
    assert r.status_code == 200, r.text

    report_c = _create_with_entry(emp, amount="40.00", vendor=original_vendor, day=original_day)
    _assert_escalated_for_open_return(db_session, emp, report_c)


def test_vendor_swap_retype_after_reject_never_auto_approves(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")

    assert isinstance(small.expense_date, date)
    report_c_id = _create_with_entry(
        emp, amount="40.00", vendor="Totally Different Diner", day=small.expense_date
    )
    # A finally rejected report is not "open" — the approval-time re-file check
    # (vendor-agnostic, small tolerances) routes the copy to a human; nothing is
    # blocked at pre-submit.
    report_c = db_session.get(ExpenseReport, uuid.UUID(report_c_id))
    assert report_c is not None
    body = _submit(emp, report_c)
    assert body["status"] == "submitted"  # NOT auto-approved
    approval = _approval(db_session, report_c)
    assert approval.current_status == ApprovalStatus.pending
    flags = (approval.risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags
    assert not any(fl.get("flag_type") == "open_returned_report" for fl in flags), flags


def test_reject_then_reopen_then_retype_never_auto_approves(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Editing a rejected claim needs a reopen (→ draft): that is unresolved again."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")
    reopen = emp.patch(f"{API}/expense-reports/{report_a.id}", json={"status": "draft"})
    assert reopen.status_code == 200, reopen.text
    r = emp.patch(f"{API}/expenses/{small.id}", json={"amount": "12.49"})
    assert r.status_code == 200, r.text
    assert isinstance(small.expense_date, date)
    report_c = _create_with_entry(emp, amount="12.50", vendor="Cafe", day=small.expense_date)
    _assert_escalated_for_open_return(db_session, emp, report_c)


def test_finally_rejected_report_does_not_block_unrelated_auto_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """One old final rejection must not escalate every future report forever."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, _ = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")

    other, _ = _report_with(db_session, employee, ["12.00"], title="Coffee")
    assert _submit(emp, other)["status"] == "approved"


def test_return_edit_resubmit_then_retype_still_escalates(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Resubmitting is not resolving: until the reviewer RE-DECIDES the returned
    report, a retyped copy of its original items must reach a human."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "request_changes")
    original_day = small.expense_date
    assert isinstance(original_day, date)
    r = emp.patch(f"{API}/expenses/{small.id}", json={"expense_date": "2026-01-01"})
    assert r.status_code == 200, r.text
    assert _submit(emp, report_a)["approval_status"] == "pending"  # resubmitted

    report_c = _create_with_entry(emp, amount="40.00", vendor="Vendor 0", day=original_day)
    _assert_escalated_for_open_return(db_session, emp, report_c)


def test_returned_trip_blocks_auto_approval_of_a_refiled_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Trips get the same SoD rule: a manager returns a trip → an identical new
    trip from the traveler carries the open-return flag and is never auto-approved."""
    from database.models.trip import Trip

    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)

    def _trip(destination: str) -> Trip:
        return _auto_approvable_trip(db_session, employee, destination)

    # Control: on its own, such a trip auto-approves (so the assertion below is real).
    control = _trip("Austin")
    ok = emp.patch(f"{API}/trips/{control.id}", json={"status": "pending_approval"})
    assert ok.json()["status"] == "approved", ok.text

    first = _trip("Boston")
    body = emp.patch(f"{API}/trips/{first.id}", json={"status": "pending_approval"})
    assert body.status_code == 200, body.text
    _decide(mgr, _approval_for(db_session, first.id), "request_changes")
    db_session.expire_all()

    second = _trip("Boston")
    r = emp.patch(f"{API}/trips/{second.id}", json={"status": "pending_approval"})
    assert r.status_code == 200, r.text
    approval2 = _approval_for(db_session, second.id)
    assert approval2.current_status == ApprovalStatus.pending
    assert approval2.is_auto_approved is False
    flags = (approval2.risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "open_returned_report" for fl in flags), flags


def _auto_approvable_trip(db: Session, traveler: User, destination: str) -> Any:
    """A small, in-policy, future trip with a cost — the agent auto-approves it."""
    from datetime import timedelta

    from database.models.trip_segment import SegmentType

    start = date.today() + timedelta(days=14)
    trip = f.make_trip(
        db,
        traveler=traveler,
        primary_destination=destination,
        start_date=start,
        end_date=start + timedelta(days=2),
        estimated_cost=Decimal("298.00"),
    )
    f.make_trip_segment(
        db,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="Delta",
        amount=Decimal("298.00"),
        currency="USD",
        in_policy=True,
    )
    db.flush()
    return trip


def test_returned_trip_cannot_be_cancelled_to_escape_review(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    trip = _auto_approvable_trip(db_session, employee, "Denver")
    assert (
        emp.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"}).status_code == 200
    )
    _decide(mgr, _approval_for(db_session, trip.id), "request_changes")

    r = emp.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "SUBJECT_UNDER_REVIEW"

    # Resubmitted and awaiting re-decision: still not cancellable (no stranded approval).
    assert (
        emp.patch(f"{API}/trips/{trip.id}", json={"status": "pending_approval"}).status_code == 200
    )
    r2 = emp.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"})
    assert r2.status_code == 409, r2.text

    # Once the reviewer decides (approves), cancelling is an ordinary owner action again.
    _decide(mgr, _approval_for(db_session, trip.id), "approve")
    r3 = emp.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"})
    assert r3.status_code == 200, r3.text


def test_refiled_rejected_trip_is_escalated(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Trips cannot be reopened; re-filing a rejected one must reach a human."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    first = _auto_approvable_trip(db_session, employee, "Seattle")
    assert (
        emp.patch(f"{API}/trips/{first.id}", json={"status": "pending_approval"}).status_code == 200
    )
    _decide(mgr, _approval_for(db_session, first.id), "reject")

    again = _auto_approvable_trip(db_session, employee, "seattle")
    r = emp.patch(f"{API}/trips/{again.id}", json={"status": "pending_approval"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    flags = (_approval_for(db_session, again.id).risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags

    # An unrelated destination is not affected by the old rejection.
    other = _auto_approvable_trip(db_session, employee, "Miami")
    r2 = emp.patch(f"{API}/trips/{other.id}", json={"status": "pending_approval"})
    assert r2.json()["status"] == "approved", r2.text


def test_one_cent_edit_of_a_rejected_claim_is_still_escalated(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")
    assert isinstance(small.expense_date, date)
    report_c_id = _create_with_entry(emp, amount="39.99", vendor="Other", day=small.expense_date)
    report_c = db_session.get(ExpenseReport, uuid.UUID(report_c_id))
    assert report_c is not None
    assert _submit(emp, report_c)["status"] == "submitted"
    flags = (_approval(db_session, report_c).risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags


def test_genuine_expense_beside_a_rejected_line_is_never_blocked_at_submit(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """The re-file check only routes to a human — the claimant's submit is not blocked."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")
    assert isinstance(small.expense_date, date)
    report_c_id = _create_with_entry(
        emp, amount="40.00", vendor="Unrelated Taxi Co", day=small.expense_date
    )
    ev = emp.post(
        f"{API}/policy/evaluate",
        json={"subject_type": "expense_report", "subject_id": report_c_id},
    )
    assert ev.status_code == 200, ev.text
    blocking = [fl for fl in ev.json()["flags"] if fl.get("severity") == "blocking"]
    assert not blocking, ev.json()  # nothing for the claimant to "resolve" at submit
    report_c = db_session.get(ExpenseReport, uuid.UUID(report_c_id))
    assert report_c is not None
    assert _submit(emp, report_c)["status"] == "submitted"  # submitted; a human decides


@pytest.mark.parametrize("second_decision", ["reject", "approve"])
def test_edit_in_place_after_reject_cannot_launder_the_original_claim(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    second_decision: str,
) -> None:
    """reject → reopen → edit the rejected entry → resubmit → reviewer decides again →
    re-file the ORIGINAL claim on a new report: matched against the content frozen on
    the human return step (ApprovalStep.subject_snapshot) → escalated."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, (small, _big) = _report_with(db_session, employee, ["40.00", "710.00"])
    original_day = small.expense_date
    assert isinstance(original_day, date)
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "reject")
    step = db_session.scalar(
        sa.select(ApprovalStep).where(
            ApprovalStep.approval_id == _approval(db_session, report_a).id,
            ApprovalStep.decision == ApprovalDecision.reject,
        )
    )
    assert step is not None and step.subject_snapshot is not None
    # Phase 10: entries also freeze category / vendor / description (the re-file
    # check's narrow match); the claim figures are still frozen exactly.
    assert {"amount": "40.00", "currency": "USD", "expense_date": str(original_day)} in [
        {k: e[k] for k in ("amount", "currency", "expense_date")}
        for e in step.subject_snapshot["entries"]
    ]

    reopen = emp.patch(f"{API}/expense-reports/{report_a.id}", json={"status": "draft"})
    assert reopen.status_code == 200, reopen.text
    r = emp.patch(
        f"{API}/expenses/{small.id}", json={"expense_date": "2025-01-01", "amount": "1.00"}
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    report_a = db_session.get(ExpenseReport, report_a.id)  # type: ignore[assignment]
    assert _submit(emp, report_a)["approval_status"] == "pending"
    _decide(mgr, _approval(db_session, report_a), second_decision)

    report_c = _create_with_entry(emp, amount="40.00", vendor="Vendor 0", day=original_day)
    rc = db_session.get(ExpenseReport, uuid.UUID(report_c))
    assert rc is not None
    assert _submit(emp, rc)["status"] == "submitted"  # NOT auto-approved
    flags = (_approval(db_session, rc).risk_summary or {}).get("flags") or []
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags


def _approval_for(db: Session, subject_id: Any) -> Approval:
    approval = db.scalar(sa.select(Approval).where(Approval.subject_id == subject_id))
    assert approval is not None
    return approval


def test_reviewer_resolving_the_returned_report_clears_the_open_return_flag(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Resubmitted-but-undecided still counts; once the reviewer APPROVES the
    returned report, unrelated small reports auto-approve again (no permanent
    penalty)."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report_a, _ = _report_with(db_session, employee, ["40.00", "710.00"])
    _submit(emp, report_a)
    _decide(mgr, _approval(db_session, report_a), "request_changes")
    assert _submit(emp, report_a)["approval_status"] == "pending"  # back with a human

    pending_other, _ = _report_with(db_session, employee, ["11.00"], title="Snack")
    assert _submit(emp, pending_other)["status"] == "submitted"  # still unresolved → human

    _decide(mgr, _approval(db_session, report_a), "approve")  # reviewer resolves it

    other, _ = _report_with(db_session, employee, ["12.00"], title="Coffee")
    body = _submit(emp, other)
    assert body["status"] == "approved", body
    flags = (_approval(db_session, other).risk_summary or {}).get("flags") or []
    assert not any(fl.get("flag_type") == "open_returned_report" for fl in flags), flags


# ---------------------------------------------------------------------------
# PATCH /receipts/{id} — linked-entry sync under the report lock
# ---------------------------------------------------------------------------


def _receipted_entry(
    db: Session, owner: User, report: ExpenseReport, amount: str = "40.00"
) -> tuple[Expense, Any]:
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.meals,
        amount=amount,
        vendor="Corner Deli",
        expense_date=date(2026, 6, 10),
    )
    receipt = f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={
            "amount": float(amount),
            "vendor": "Corner Deli",
            "date": "2026-06-10",
            "currency": "USD",
        },
        field_provenance={"amount": DET, "vendor": DET, "date": DET, "currency": DET},
    )
    report.total_amount = Decimal(amount)
    db.flush()
    return entry, receipt


def _race_a_move(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
    entry: Expense,
    to_report: ExpenseReport,
) -> list[bool]:
    """After the sync's first unlocked report-id read, a concurrent move commits
    (entry → *to_report*) — written without syncing the ORM identity map."""
    # UAT2: the lock helpers live in the expense service (shared by the receipt
    # sync and the attach / delete paths); patch them there.
    from backend.app.modules.expense import service as expense_service

    real = expense_service.linked_entry_report_id
    fired: list[bool] = []

    def read_then_move(*args: Any, **kwargs: Any) -> Any:
        value = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            db.execute(
                sa.update(Expense)
                .where(Expense.id == entry.id)
                .values(report_id=to_report.id)
                .execution_options(synchronize_session=False)
            )
        return value

    monkeypatch.setattr(expense_service, "linked_entry_report_id", read_then_move)
    return fired


def test_receipt_sync_never_writes_an_entry_moved_onto_a_submitted_report(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = users_a["employee"]
    report_a = f.make_expense_report(db_session, owner=owner, title="A")
    report_b = f.make_expense_report(
        db_session, owner=owner, title="B", status=ExpenseReportStatus.submitted
    )
    entry, receipt = _receipted_entry(db_session, owner, report_a)
    report_b.total_amount = Decimal("0")
    db_session.flush()
    total_a, total_b = report_a.total_amount, report_b.total_amount
    fired = _race_a_move(monkeypatch, db_session, entry, report_b)

    resp = employee_client.patch(
        f"{API}/receipts/{receipt.id}", json={"field": "amount", "value": 99.0}
    )
    assert fired, "the interleaving hook did not run"
    # UAT2 hardening: the receipt now backs an entry on a SUBMITTED report, so it
    # is frozen evidence — the correction is refused (409 RECEIPT_LOCKED) and
    # neither the receipt nor the entry changes.
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "RECEIPT_LOCKED"
    db_session.expire_all()
    kept = db_session.get(Receipt, receipt.id)
    assert kept is not None and (kept.extracted_fields or {})["amount"] == 40.0

    db_session.expire_all()
    moved = db_session.get(Expense, entry.id)
    assert moved is not None
    assert moved.report_id == report_b.id
    assert moved.amount == Decimal("40.00")
    a = db_session.get(ExpenseReport, report_a.id)
    b = db_session.get(ExpenseReport, report_b.id)
    assert a is not None and b is not None
    assert a.total_amount == total_a
    assert b.total_amount == total_b
    assert (
        db_session.scalar(
            sa.select(AuditLog).where(
                AuditLog.entity_id == entry.id, AuditLog.action == "expense.updated"
            )
        )
        is None
    )


def test_receipt_sync_follows_an_entry_moved_to_another_draft(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = users_a["employee"]
    report_a = f.make_expense_report(db_session, owner=owner, title="A")
    report_b = f.make_expense_report(db_session, owner=owner, title="B")
    entry, receipt = _receipted_entry(db_session, owner, report_a)
    fired = _race_a_move(monkeypatch, db_session, entry, report_b)

    resp = employee_client.patch(
        f"{API}/receipts/{receipt.id}", json={"field": "amount", "value": 55.0}
    )
    assert fired
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked_expense_sync"] == "updated"

    db_session.expire_all()
    moved = db_session.get(Expense, entry.id)
    assert moved is not None and moved.report_id == report_b.id
    assert moved.amount == Decimal("55.00")
    a = db_session.get(ExpenseReport, report_a.id)
    b = db_session.get(ExpenseReport, report_b.id)
    assert a is not None and b is not None
    # The total follows the entry's CURRENT report (B); A is untouched by the sync.
    assert b.total_amount == Decimal("55.00")
    assert a.total_amount == Decimal("40.00")
