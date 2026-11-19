"""Phase 10 known limit — re-filing is matched by LINEAGE, not by amount + date + currency.

Before: every line on a new report was compared with the lines of the owner's
withdrawn returned / rejected reports on amount (±max($1, 2%)), date (±3 days)
and currency only, so two different $24.50 lunches collided and the second one
went to a human with a ``refiled_returned_claim`` flag.

Now (migration 0023):

* moving a line out of a WITHDRAWN report records its origin on the line
  (``released_from_report_id`` / ``released_from_approval_id`` / ``released_at``)
  in the move's transaction, audited on the ``expense.moved`` row;
* the approval agent flags exactly those lines (strong flag, whatever the owner
  edited afterwards) and lines backed by a receipt the reviewer saw on the
  withdrawn report;
* a line RE-TYPED from scratch only matches when amount + date + currency AND the
  normalised vendor (else description) AND the category agree — flagged
  ``possible_refile`` (advisory: still escalated, never blocking);
* fail safe: a withdrawn report frozen before 0023 (legacy rows without vendor /
  category) keeps today's wide match; a line a REVIEWER SAW (listed on a return
  snapshot) that was later deleted — released or not — has its own content
  matched wide.  A line deleted before the report was ever submitted, or one
  added after the return, was never reviewed and widens nothing (integration
  fix for the blocking review finding).

Filing is never blocked and the fresh policy evaluation still runs.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.audit import record_audit
from backend.tests.test_pg_lifecycle_db import (
    API,
    _approval,
    _audits,
    _client,
    _decide,
    _flags,
    _move,
    _new_draft,
    _report_with,
    _returned,
    _submit,
)
from database.models.approval import ApprovalStatus
from database.models.approval_step import ApprovalStep
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.receipt import OcrStatus

pytestmark = pytest.mark.db

STRONG = {"refiled_returned_claim", "refiled_rejected_claim"}
POSSIBLE = "possible_refile"


def _types(flags: list[dict]) -> list[str]:
    return [str(fl.get("flag_type")) for fl in flags]


def _refile_flags(flags: list[dict]) -> list[dict]:
    return [fl for fl in flags if fl.get("flag_type") in STRONG | {POSSIBLE}]


def _withdrawn(
    db: Session, emp: Any, mgr: Any, owner: User, **kw: Any
) -> tuple[ExpenseReport, list[Expense]]:
    """A report the manager sent back that the owner then withdrew.

    Lines: 24.50 meals "Cafe Alpha" (2026-06-10) and 710.00 meals "Vendor 1".
    """
    report, entries = _returned(db, emp, mgr, owner, ["24.50", "710.00"], **kw)
    entries[0].vendor = "Cafe Alpha"
    db.flush()
    # Re-freeze nothing: the return snapshot was taken at the decision above.
    r = emp.post(f"{API}/expense-reports/{report.id}/withdraw")
    assert r.status_code == 200, r.text
    db.expire_all()
    return report, entries


def _draft_with_line(
    db: Session,
    owner: User,
    *,
    amount: str,
    vendor: str | None,
    day: date,
    category: ExpenseCategory = ExpenseCategory.meals,
    description: str | None = None,
) -> tuple[ExpenseReport, Expense]:
    report = f.make_expense_report(db, owner=owner, title="New claims")
    line = f.make_expense(
        db,
        report=report,
        amount=amount,
        vendor=vendor,
        description=description,
        category=category,
        expense_date=day,
    )
    report.total_amount = Decimal(amount)
    db.flush()
    return report, line


# ---------------------------------------------------------------------------
# Lineage is recorded by the move (same transaction, audited)
# ---------------------------------------------------------------------------


def test_moving_out_of_a_withdrawn_report_records_lineage_and_audits_it(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    withdrawn, (small, _big) = _withdrawn(db_session, emp, mgr, employee)
    approval_id = _approval(db_session, withdrawn.id).id

    target = _new_draft(emp)
    assert _move(emp, withdrawn.id, [small], target).status_code == 200
    db_session.expire_all()
    line = db_session.get(Expense, small.id)
    assert line is not None
    assert line.released_from_report_id == withdrawn.id
    assert line.released_from_approval_id == approval_id
    assert line.released_at is not None

    (moved,) = _audits(db_session, small.id, "expense.moved")
    lineage = (moved.after_state or {}).get("released_from")
    assert isinstance(lineage, dict), moved.after_state
    assert lineage == {
        "report_id": str(withdrawn.id),
        "approval_id": str(approval_id),
        "released_at": lineage["released_at"],
    }, moved.after_state

    # An ordinary move between two never-reviewed drafts records nothing.
    other = _new_draft(emp, "Other")
    plain_report, (plain,) = _report_with(db_session, employee, ["9.00"], title="Plain", day=1)
    assert _move(emp, plain_report.id, [plain], other).status_code == 200
    db_session.expire_all()
    untouched = db_session.get(Expense, plain.id)
    assert untouched is not None and untouched.released_from_report_id is None
    (plain_move,) = _audits(db_session, plain.id, "expense.moved")
    assert "released_from" not in (plain_move.after_state or {})


# ---------------------------------------------------------------------------
# Over-flagging fixed: unrelated lines no longer collide
# ---------------------------------------------------------------------------


def test_a_different_lunch_with_the_same_amount_and_date_is_not_a_refile(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    _withdrawn(db_session, emp, mgr, employee)

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Bistro Beta", day=date(2026, 6, 10)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert not _refile_flags(flags), flags
    assert body["status"] == "approved", flags


def test_same_vendor_but_another_category_is_not_a_refile(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    _withdrawn(db_session, emp, mgr, employee)

    fresh, _line = _draft_with_line(
        db_session,
        employee,
        amount="24.50",
        vendor="Cafe Alpha",
        day=date(2026, 6, 10),
        category=ExpenseCategory.ground_transport,
    )
    _submit(emp, fresh.id)
    assert not _refile_flags(_flags(db_session, fresh.id))


def test_a_retyped_copy_is_a_possible_refile_escalated_but_never_blocking(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    withdrawn, _ = _withdrawn(db_session, emp, mgr, employee, title="Team lunch")

    # Same claim typed again from scratch: vendor case / punctuation differ, a
    # one-day / small-amount nudge does not dodge the match.
    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.90", vendor="  CAFE alpha. ", day=date(2026, 6, 11)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert body["status"] == "submitted", flags  # filed, waiting for a human
    assert _approval(db_session, fresh.id).current_status == ApprovalStatus.pending
    found = _refile_flags(flags)
    assert _types(found) == [POSSIBLE], flags
    assert found[0]["severity"] == "advisory"
    assert found[0]["related_subject_id"] == str(withdrawn.id)
    assert "Team lunch" in found[0]["reason"]
    summary = _approval(db_session, fresh.id).risk_summary or {}
    assert summary.get("is_policy_compliant") is False, summary
    assert summary.get("route") == "escalate", summary


# ---------------------------------------------------------------------------
# Lineage: precise and edit-proof
# ---------------------------------------------------------------------------


def test_a_moved_line_edited_beyond_recognition_keeps_its_strong_flag(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    withdrawn, (small, _big) = _withdrawn(db_session, emp, mgr, employee, title="Q3 dinner")
    target = _new_draft(emp)
    assert _move(emp, withdrawn.id, [small], target).status_code == 200

    # Edit everything the old content match looked at — and try to clear the
    # lineage through the API (unknown fields are ignored, never applied).
    r = emp.patch(
        f"{API}/expenses/{small.id}",
        json={
            "amount": "61.00",
            "expense_date": "2026-02-01",
            "vendor": "Somewhere Else",
            "category": "other",
            "released_from_report_id": None,
            "released_at": None,
        },
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    line = db_session.get(Expense, small.id)
    assert line is not None and line.released_from_report_id == withdrawn.id

    body = _submit(emp, uuid.UUID(target))
    flags = _flags(db_session, uuid.UUID(target))
    assert body["status"] == "submitted", flags
    found = _refile_flags(flags)
    assert _types(found) == ["refiled_returned_claim"], flags
    assert found[0]["severity"] == "blocking"
    assert found[0]["related_subject_id"] == str(withdrawn.id)
    assert found[0]["match"] == "lineage"
    assert "Q3 dinner" in found[0]["reason"]


def test_only_the_moved_line_is_flagged_not_its_same_amount_neighbour(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    withdrawn, (small, _big) = _withdrawn(db_session, emp, mgr, employee)
    target = _new_draft(emp)
    assert _move(emp, withdrawn.id, [small], target).status_code == 200
    target_report = db_session.get(ExpenseReport, uuid.UUID(target))
    assert target_report is not None
    # Same amount and date as the released line, another vendor: not a refile.
    f.make_expense(
        db_session,
        report=target_report,
        amount="24.50",
        vendor="Different Diner",
        expense_date=date(2026, 6, 10),
    )
    target_report.total_amount = Decimal("49.00")
    db_session.flush()

    _submit(emp, target_report.id)
    found = _refile_flags(_flags(db_session, target_report.id))
    assert [fl["entry_ref"] for fl in found] == ["Cafe Alpha"], found
    assert found[0]["match"] == "lineage"


def test_a_line_released_from_a_rejected_report_carries_the_rejected_flag(
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
    assert emp.patch(f"{API}/expenses/{small.id}", json={"amount": "300.00"}).status_code == 200

    _submit(emp, uuid.UUID(target))
    found = _refile_flags(_flags(db_session, uuid.UUID(target)))
    assert _types(found) == ["refiled_rejected_claim"], found
    assert found[0]["match"] == "lineage"


# ---------------------------------------------------------------------------
# Evidence path: a receipt the reviewer saw
# ---------------------------------------------------------------------------


def test_a_line_backed_by_a_reviewed_receipt_is_flagged_strongly(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _report_with(db_session, employee, ["24.50", "710.00"])
    receipt = f.make_receipt(
        db_session, uploaded_by=employee, ocr_status=OcrStatus.processed, expense_id=small.id
    )
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200

    # A line typed from scratch (nothing in common) ends up backed by that
    # receipt (simulated directly: no lineage, no content match).
    fresh, line = _draft_with_line(
        db_session, employee, amount="88.00", vendor="Elsewhere", day=date(2026, 1, 5)
    )
    db_session.expire_all()
    stored = db_session.get(type(receipt), receipt.id)
    assert stored is not None
    stored.expense_id = line.id
    db_session.flush()

    _submit(emp, fresh.id)
    found = _refile_flags(_flags(db_session, fresh.id))
    assert _types(found) == ["refiled_returned_claim"], found
    assert found[0]["match"] == "receipt"
    assert found[0]["related_subject_id"] == str(report.id)


# ---------------------------------------------------------------------------
# Fail safe: delete + retype, legacy rows
# ---------------------------------------------------------------------------


def test_deleting_a_released_line_and_retyping_it_cannot_launder_the_return(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    withdrawn, (small, _big) = _withdrawn(db_session, emp, mgr, employee)
    target = _new_draft(emp)
    assert _move(emp, withdrawn.id, [small], target).status_code == 200
    r = emp.delete(f"{API}/expenses/{small.id}")
    assert r.status_code == 200, r.text

    # Retyped with a new vendor: the narrow match would miss it, but a released
    # line was deleted, so the withdrawn report keeps the wide (strong) match.
    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Renamed Cafe", day=date(2026, 6, 10)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert body["status"] == "submitted", flags
    found = _refile_flags(flags)
    assert _types(found) == ["refiled_returned_claim"], flags
    assert found[0]["match"] == "content"


def _legacy_withdraw(db: Session, report: ExpenseReport, owner: User) -> None:
    """Withdraw the way pre-0023 code did: the audit row holds amount / currency /
    date only, and the return snapshot holds no vendor / category either."""
    approval = _approval(db, report.id)
    for step in db.scalars(sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)):
        snap = step.subject_snapshot
        if isinstance(snap, dict):
            step.subject_snapshot = {
                **snap,
                "entries": [
                    {k: e.get(k) for k in ("amount", "currency", "expense_date")}
                    for e in snap.get("entries") or []
                ],
            }
    stored = db.get(ExpenseReport, report.id)
    assert stored is not None
    stored.withdrawn_at = sa.func.now()
    stored.withdrawn_by_id = owner.id
    lines = db.scalars(sa.select(Expense).where(Expense.report_id == report.id)).all()
    record_audit(
        db,
        organization_id=report.organization_id,
        action="expense_report.withdrawn",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=owner.id,
        before_state={"status": "draft"},
        after_state={
            "approval_id": str(approval.id),
            "approval_status": approval.current_status.value,
            "entries": [
                {
                    "entry_id": str(e.id),
                    "amount": str(e.amount),
                    "currency": e.currency,
                    "expense_date": str(e.expense_date),
                }
                for e in lines
            ],
        },
    )
    db.flush()


def test_a_legacy_withdrawal_keeps_todays_wide_match(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _returned(db_session, emp, mgr, employee, ["24.50", "710.00"])
    _legacy_withdraw(db_session, report, employee)

    # A pre-0023 move left no lineage: only the wide content match can catch it.
    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Anything", day=date(2026, 6, 11)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert body["status"] == "submitted", flags
    found = _refile_flags(flags)
    assert _types(found) == ["refiled_returned_claim"], flags
    assert found[0]["match"] == "content"


def test_deleting_a_line_from_the_returned_draft_before_withdrawing_cannot_hide_it(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Review fix: a line deleted from the RETURNED draft (before withdrawal) is
    in no withdrawal audit row and carries no lineage, so the report it was
    deleted from must keep the wide match — whatever the app / DB clocks say."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _returned(db_session, emp, mgr, employee, ["24.50", "710.00"])
    r = emp.delete(f"{API}/expenses/{small.id}")
    assert r.status_code == 200, r.text
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Totally Different", day=date(2026, 6, 10)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert body["status"] == "submitted", flags
    found = _refile_flags(flags)
    assert _types(found) == ["refiled_returned_claim"], flags
    assert found[0]["match"] == "content"
    assert found[0]["related_subject_id"] == str(report.id)


def test_a_second_release_keeps_the_first_lineage(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Review fix: a line released from rejected report A, filed on B, returned on
    B and released again keeps A as its origin (keep-first), so the flag stays
    ``refiled_rejected_claim`` naming A."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    rejected, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    _submit(emp, rejected.id)
    _decide(mgr, _approval(db_session, rejected.id), "reject")
    assert (
        emp.patch(f"{API}/expense-reports/{rejected.id}", json={"status": "draft"}).status_code
        == 200
    )
    assert emp.post(f"{API}/expense-reports/{rejected.id}/withdraw").status_code == 200

    b = uuid.UUID(_new_draft(emp, "Second try"))
    assert _move(emp, rejected.id, [small], b).status_code == 200
    _submit(emp, b)
    _decide(mgr, _approval(db_session, b), "request_changes")
    assert emp.post(f"{API}/expense-reports/{b}/withdraw").status_code == 200

    c = uuid.UUID(_new_draft(emp, "Third try"))
    assert _move(emp, b, [small], c).status_code == 200
    db_session.expire_all()
    line = db_session.get(Expense, small.id)
    assert line is not None and line.released_from_report_id == rejected.id

    moves = sorted(_audits(db_session, small.id, "expense.moved"), key=lambda a: a.created_at)
    assert len(moves) == 2, moves
    for moved in moves:
        lineage = (moved.after_state or {}).get("released_from") or {}
        assert lineage.get("report_id") == str(rejected.id), moved.after_state

    _submit(emp, c)
    found = _refile_flags(_flags(db_session, c))
    assert _types(found) == ["refiled_rejected_claim"], found
    assert found[0]["related_subject_id"] == str(rejected.id)
    assert found[0]["match"] == "lineage"


def _returned_then_rejected_release(
    app: Any, db: Session, users: dict[str, User]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Line released from RETURNED A, filed on B, B REJECTED, reopened, withdrawn,
    then moved to C and submitted.  Returns (A, B, C)."""
    employee, manager = users["employee"], users["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    a, (small, _big) = _report_with(db, employee, ["12.50", "710.00"], title="A")
    _submit(emp, a.id)
    _decide(mgr, _approval(db, a.id), "request_changes")
    assert emp.post(f"{API}/expense-reports/{a.id}/withdraw").status_code == 200

    b = uuid.UUID(_new_draft(emp, "B"))
    assert _move(emp, a.id, [small], b).status_code == 200
    _submit(emp, b)
    _decide(mgr, _approval(db, b), "reject")
    assert emp.patch(f"{API}/expense-reports/{b}", json={"status": "draft"}).status_code == 200
    assert emp.post(f"{API}/expense-reports/{b}/withdraw").status_code == 200

    c = uuid.UUID(_new_draft(emp, "C"))
    assert _move(emp, b, [small], c).status_code == 200
    db.expire_all()
    line = db.get(Expense, small.id)
    assert line is not None and line.released_from_report_id == a.id  # keep-first
    _submit(emp, c)
    return a.id, b, c


def test_a_later_rejection_wins_over_a_first_returned_origin(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Review fix: released from RETURNED A, then REJECTED on B, then moved to C.
    Lineage keeps A as the origin, but the reviewer rejected this line on B, so
    the flag is ``refiled_rejected_claim`` naming B (pre-phase-10 behaviour)."""
    _a, b, c = _returned_then_rejected_release(app, db_session, users_a)
    found = _refile_flags(_flags(db_session, c))
    assert _types(found) == ["refiled_rejected_claim"], found
    assert found[0]["related_subject_id"] == str(b)
    assert found[0]["match"] == "lineage"
    assert "'B'" in found[0]["reason"], found[0]["reason"]


def test_a_later_rejection_is_found_even_beyond_the_recent_withdrawn_window(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: the rejecting report is loaded because the line is listed on its
    withdrawal snapshot, not because it is among the most recently withdrawn."""
    from ai_agents.agents.approval_agent import evaluate

    monkeypatch.setattr(evaluate, "_WITHDRAWN_REF_LIMIT", 0)
    _a, b, c = _returned_then_rejected_release(app, db_session, users_a)
    found = _refile_flags(_flags(db_session, c))
    assert _types(found) == ["refiled_rejected_claim"], found
    assert found[0]["related_subject_id"] == str(b)


# ---------------------------------------------------------------------------
# Integration fix: a deletion only widens the match when a reviewer saw the line
# ---------------------------------------------------------------------------


def _typo_deleted_before_first_submit(
    db: Session, emp: Any, mgr: Any, owner: User
) -> ExpenseReport:
    """The review's reproduction: 24.50 'Cafe Alpha' + a 5.00 typo line; the typo
    is deleted from the DRAFT before the report is ever submitted; then submit,
    the manager requests changes, and the owner withdraws.  (A 710.00 line keeps
    the report above the auto-approval limit so a human reviews it.)"""
    report, (lunch, typo, _big) = _report_with(db, owner, ["24.50", "5.00", "710.00"])
    lunch.vendor = "Cafe Alpha"
    db.flush()
    r = emp.delete(f"{API}/expenses/{typo.id}")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert _submit(emp, report.id)["approval_status"] == "pending"
    _decide(mgr, _approval(db, report.id), "request_changes")
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db.expire_all()
    return report


def test_a_line_deleted_before_the_first_submission_never_widens_the_match(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Blocking review finding: deleting a typo from a draft BEFORE it was ever
    submitted made the later withdrawn report match wide, so an unrelated 24.50
    lunch from another vendor on the same date escalated as a re-filed claim."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    _typo_deleted_before_first_submit(db_session, emp, mgr, employee)

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Bistro Beta", day=date(2026, 6, 10)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert not _refile_flags(flags), flags
    assert body["status"] == "approved", flags

    # Nor does a 5.00 line on the typo's date: the typo was never reviewed.
    other, _line = _draft_with_line(
        db_session, employee, amount="5.00", vendor="Kiosk", day=date(2026, 6, 11)
    )
    _submit(emp, other.id)
    assert not _refile_flags(_flags(db_session, other.id))


def test_a_reviewed_line_deleted_after_the_return_and_retyped_identically_stays_strong(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """The legitimate case: the reviewer saw the line, the owner deleted it from
    the returned draft, withdrew, and typed the very same line again."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (lunch, _big) = _report_with(db_session, employee, ["24.50", "710.00"])
    lunch.vendor = "Cafe Alpha"
    db_session.flush()
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    assert emp.delete(f"{API}/expenses/{lunch.id}").status_code == 200
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Cafe Alpha", day=date(2026, 6, 10)
    )
    body = _submit(emp, fresh.id)
    flags = _flags(db_session, fresh.id)
    assert body["status"] == "submitted", flags
    found = _refile_flags(flags)
    assert _types(found) == ["refiled_returned_claim"], flags
    assert found[0]["match"] == "content"
    assert found[0]["related_subject_id"] == str(report.id)


def test_a_reviewed_deletion_widens_only_the_deleted_lines_content(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Deleting the reviewed 710.00 line after the return must not turn every
    unrelated line that resembles ANOTHER line of the report into a strong
    re-file: a different 24.50 lunch from another vendor is not flagged."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (lunch, big) = _report_with(db_session, employee, ["24.50", "710.00"])
    lunch.vendor = "Cafe Alpha"
    db_session.flush()
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    assert emp.delete(f"{API}/expenses/{big.id}").status_code == 200
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Bistro Beta", day=date(2026, 6, 10)
    )
    _submit(emp, fresh.id)
    assert not _refile_flags(_flags(db_session, fresh.id))

    # ...while a re-typed copy of the deleted reviewed line (new vendor) is strong.
    again, _copy = _draft_with_line(
        db_session, employee, amount="710.00", vendor="Somewhere Else", day=date(2026, 6, 11)
    )
    _submit(emp, again.id)
    found = _refile_flags(_flags(db_session, again.id))
    assert _types(found) == ["refiled_returned_claim"], found
    assert found[0]["match"] == "content"


def test_a_line_added_after_the_return_and_deleted_was_never_reviewed(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """A line the owner added to the returned draft and deleted again before
    withdrawing was never in front of a reviewer, so it does not widen the match."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (lunch, _big) = _returned(db_session, emp, mgr, employee, ["24.50", "710.00"])
    extra = f.make_expense(
        db_session, report=report, amount="24.50", vendor="Late Add", expense_date=date(2026, 6, 10)
    )
    db_session.flush()
    assert emp.delete(f"{API}/expenses/{extra.id}").status_code == 200
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()

    fresh, _line = _draft_with_line(
        db_session, employee, amount="24.50", vendor="Bistro Beta", day=date(2026, 6, 10)
    )
    _submit(emp, fresh.id)
    assert not _refile_flags(_flags(db_session, fresh.id))


def _strip_snapshot_entry_ids(db: Session, report: ExpenseReport) -> None:
    """Return snapshots frozen before the integration fix list no entry ids."""
    approval = _approval(db, report.id)
    for step in db.scalars(sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)):
        snap = step.subject_snapshot
        if isinstance(snap, dict):
            step.subject_snapshot = {
                **snap,
                "entries": [
                    {k: v for k, v in e.items() if k != "entry_id"}
                    for e in snap.get("entries") or []
                ],
            }
    db.flush()


def _set_deletion_time(db: Session, entry_id: Any, report: ExpenseReport, offset: str) -> None:
    """Separate production transactions: put the deletion's DB-clock time
    ``offset`` away from the report's first submission (the approval's creation)."""
    approval = _approval(db, report.id)
    db.execute(
        sa.text(
            "UPDATE audit_log SET created_at = CAST(:at AS timestamptz) + CAST(:off AS interval) "
            "WHERE entity_id = :e AND action = 'expense.deleted'"
        ),
        {"at": approval.created_at, "off": offset, "e": entry_id},
    )
    db.flush()


@pytest.mark.parametrize(("offset", "strong"), [("-1 minute", False), ("1 minute", True)])
def test_without_snapshot_entry_ids_only_deletions_after_the_first_submission_count(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    offset: str,
    strong: bool,
) -> None:
    """A snapshot that lists no entry ids cannot say what the reviewer saw: a
    deletion before the report first entered review never counts, one after it
    does (the database clocks of two separate transactions are compared)."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (lunch, big) = _report_with(db_session, employee, ["24.50", "710.00"])
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    assert emp.delete(f"{API}/expenses/{big.id}").status_code == 200
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()
    _strip_snapshot_entry_ids(db_session, report)
    _set_deletion_time(db_session, big.id, report, offset)

    fresh, _line = _draft_with_line(
        db_session, employee, amount="710.00", vendor="Somewhere Else", day=date(2026, 6, 11)
    )
    _submit(emp, fresh.id)
    found = _refile_flags(_flags(db_session, fresh.id))
    if strong:
        assert _types(found) == ["refiled_returned_claim"], found
    else:
        assert not found, found


def test_a_rejected_claim_retyped_with_another_vendor_is_still_flagged_strongly(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """Security review: the narrow vendor/category match applies to RETURNED history
    only. A claim a reviewer REJECTED keeps the amount/date/currency match, so
    retyping it under another vendor/description cannot slip past the narrow label."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _report_with(db_session, employee, ["24.50", "710.00"])
    small.vendor = "Cafe Alpha"
    db_session.flush()
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "reject")
    assert (
        emp.patch(f"{API}/expense-reports/{report.id}", json={"status": "draft"}).status_code == 200
    )
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    db_session.expire_all()

    fresh, _line = _draft_with_line(
        db_session,
        employee,
        amount="24.50",
        vendor="Bistro Beta",
        day=cast(date, small.expense_date),
    )
    _submit(emp, fresh.id)
    found = _refile_flags(_flags(db_session, fresh.id))
    assert _types(found) == ["refiled_rejected_claim"], found
