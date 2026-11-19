"""C7R-18 review round — concurrent "enter the total" PATCHes draft exactly ONE entry.

A receipt whose total was withheld has no draft entry; PATCH amount creates the
deferred draft. Two such PATCHes in flight at once (double-submit, client retry)
must not both draft: the second would orphan the first entry and double the
Receipt Inbox report total.

The race is forced deterministically with two real sessions on the test DB:
request B gets to a hook point, then request A is started on a worker thread and
given time to run to completion. Two hook points are pinned:
  * ``apply_user_correction`` — after B's pre-read (A commits before B's
    correction: needs a fresh read of the row);
  * ``_auto_draft_expense_from_receipt`` — after B decided to draft (both pass
    the "amount unknown, no entry yet" check: needs the row LOCK).
Without the fix A finishes (drafts + commits) in that window and B drafts
again; with the lock A blocks until B commits, then sees the amount +
expense_id B wrote and only syncs the existing entry.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.modules.receipt import router as receipt_router
from backend.app.modules.receipt.schemas import ReceiptCorrectionRequest
from database.models.expense import Expense
from database.models.identity import RoleKey, User
from database.models.receipt import OcrStatus, Receipt
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = [pytest.mark.db]

# How long B waits for A while holding (or, before the fix, not holding) the lock.
_A_HEAD_START_S = 3.0


@pytest.mark.parametrize("hook", ["apply_user_correction", "_auto_draft_expense_from_receipt"])
def test_concurrent_amount_patches_draft_a_single_entry(
    _schema: Any, monkeypatch: pytest.MonkeyPatch, hook: str
) -> None:
    if _schema is None:
        pytest.skip("Postgres not reachable")
    engine = _schema

    with Session(engine) as s0:
        seed_rbac_catalog(s0)
        org = f.make_org(s0, name=f"C7R18 Race Org {uuid.uuid4().hex[:8]}")
        emp = f.make_user(s0, org=org, role=RoleKey.employee)
        rec = f.make_receipt(
            s0,
            org=org,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": None,
                "currency": "USD",
                "date": "2026-06-11",
                "vendor": "Corner Deli",
                "location": "New York, NY",
                "expense_type": "meals",
                "review_flags": {"amount": "total_illegible"},
            },
            field_confidence={"amount": 0.0},
            field_provenance={"amount": "ai_inferred"},
        )
        s0.commit()
        rid, uid = rec.id, emp.id

    session_a, session_b = Session(engine), Session(engine)
    outcome: dict[str, Any] = {}
    original = getattr(receipt_router, hook)
    worker: list[threading.Thread] = []

    def run_a() -> None:
        try:
            out = receipt_router.correct_receipt(
                rid,
                ReceiptCorrectionRequest(field="amount", value=18.73),
                user=session_a.get(User, uid),  # type: ignore[arg-type]
                db=session_a,
            )
            outcome["a"] = out.linked_expense_sync
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            outcome["a_error"] = repr(exc)

    def wrapped(session: Session, **kw: Any) -> Any:
        if session is session_b and not worker:
            # B reached the hook point; let A race it.
            t = threading.Thread(target=run_a, daemon=True)
            worker.append(t)
            t.start()
            t.join(_A_HEAD_START_S)
        return original(session, **kw)

    monkeypatch.setattr(receipt_router, hook, wrapped)
    try:
        out_b = receipt_router.correct_receipt(
            rid,
            ReceiptCorrectionRequest(field="amount", value=18.73),
            user=session_b.get(User, uid),  # type: ignore[arg-type]
            db=session_b,
        )
        worker[0].join(30)
        assert not worker[0].is_alive(), "request A never finished"
    finally:
        session_a.close()
        session_b.close()

    assert "a_error" not in outcome, outcome.get("a_error")
    syncs = sorted([out_b.linked_expense_sync, outcome["a"]])
    # Exactly one request drafted; the other updated that same entry.
    assert syncs.count("drafted") == 1, syncs

    with Session(engine) as s:
        expenses = s.scalars(sa.select(Expense).where(Expense.created_by == uid)).all()
        assert len(expenses) == 1, f"{len(expenses)} draft entries for one receipt"
        receipt = s.get(Receipt, rid)
        assert receipt is not None
        assert receipt.expense_id == expenses[0].id


def test_deferred_draft_never_lands_on_an_inbox_submitted_mid_request(
    _schema: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C7R review: the deferred draft read the Receipt Inbox with a plain SELECT, so
    the owner could submit that inbox (and a manager auto-approve it) between this
    PATCH's read and its commit: the new entry then committed onto a submitted
    report the submit / approval evaluation never saw.

    Forced deterministically: request B (enter the total) pauses right after it
    picked the inbox; the owner's submit (A) runs on a worker thread meanwhile.
    Old behaviour: A commits in the window and returns total 10.00 while the
    report ends at 28.73 with an entry A never saw.  With the row lock A waits
    for B and submits the report WITH B's entry (A's response total == final).
    """
    if _schema is None:
        pytest.skip("Postgres not reachable")
    from decimal import Decimal

    from backend.app.modules.expense import service as expense_service
    from database.models.expense_report import ExpenseReport, ExpenseReportStatus

    engine = _schema
    with Session(engine) as s0:
        seed_rbac_catalog(s0)
        org = f.make_org(s0, name=f"C7R18 Inbox Lock Org {uuid.uuid4().hex[:8]}")
        emp = f.make_user(s0, org=org, role=RoleKey.employee)
        inbox = f.make_expense_report(
            s0, owner=emp, title=receipt_router._RECEIPT_INBOX_TITLE, total_amount=Decimal("10")
        )
        f.make_expense(s0, report=inbox, amount="10.00", vendor="Cafe")
        rec = f.make_receipt(
            s0,
            org=org,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": None,
                "currency": "USD",
                "date": "2026-06-11",
                "vendor": "Corner Deli",
                "location": "New York, NY",
                "expense_type": "meals",
                "review_flags": {"amount": "total_illegible"},
            },
            field_confidence={"amount": 0.0},
            field_provenance={"amount": "ai_inferred"},
        )
        s0.commit()
        rid, uid, inbox_id = rec.id, emp.id, inbox.id

    session_a, session_b = Session(engine), Session(engine)
    outcome: dict[str, Any] = {}
    original = receipt_router._find_or_create_receipt_inbox
    worker: list[threading.Thread] = []

    def run_submit() -> None:
        try:
            resp = expense_service.transition_report(
                session_a,
                session_a.get(User, uid),  # type: ignore[arg-type]
                inbox_id,
                ExpenseReportStatus.submitted,
            )
            session_a.commit()
            outcome["a_total"] = Decimal(str(resp.total_amount))
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            outcome["a_error"] = repr(exc)

    def wrapped(db: Session, user: User) -> Any:
        report = original(db, user)
        if db is session_b and not worker:
            t = threading.Thread(target=run_submit, daemon=True)
            worker.append(t)
            t.start()
            t.join(_A_HEAD_START_S)
        return report

    monkeypatch.setattr(receipt_router, "_find_or_create_receipt_inbox", wrapped)
    try:
        out_b = receipt_router.correct_receipt(
            rid,
            ReceiptCorrectionRequest(field="amount", value=18.73),
            user=session_b.get(User, uid),  # type: ignore[arg-type]
            db=session_b,
        )
        worker[0].join(30)
        assert not worker[0].is_alive(), "submit never finished"
    finally:
        session_a.close()
        session_b.close()

    assert "a_error" not in outcome, outcome.get("a_error")
    assert out_b.linked_expense_sync == "drafted"
    with Session(engine) as s:
        report = s.get(ExpenseReport, inbox_id)
        assert report is not None
        # Submitted, then auto-approved in the same transaction (in-policy, small).
        assert report.status == ExpenseReportStatus.approved
        # The submit saw every entry the submitted report holds.
        assert outcome["a_total"] == Decimal(str(report.total_amount)) == Decimal("28.73"), (
            outcome,
            report.total_amount,
        )


def test_entry_edit_never_commits_onto_a_report_submitted_mid_request(
    _schema: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C7R review (same race, PATCH /expenses/{id}): ``update_entry`` checked the
    parent's draft status on an unlocked read, so a submit committing between that
    check and the edit's commit left a submitted report holding an amount its
    submit never saw.  Old: A returns total 10.00, the report ends at 99.00.
    With the parent row locked, A waits and submits the edited report."""
    if _schema is None:
        pytest.skip("Postgres not reachable")
    from decimal import Decimal

    from backend.app.modules.expense import service as expense_service
    from backend.app.modules.expense.schemas import ExpenseUpdate
    from database.models.expense_report import ExpenseReport, ExpenseReportStatus

    engine = _schema
    with Session(engine) as s0:
        seed_rbac_catalog(s0)
        org = f.make_org(s0, name=f"C7R Entry Lock Org {uuid.uuid4().hex[:8]}")
        emp = f.make_user(s0, org=org, role=RoleKey.employee)
        report = f.make_expense_report(s0, owner=emp, title="Trip", total_amount=Decimal("10"))
        entry = f.make_expense(s0, report=report, amount="10.00", vendor="Cafe")
        s0.commit()
        uid, report_id, entry_id = emp.id, report.id, entry.id

    session_a, session_b = Session(engine), Session(engine)
    outcome: dict[str, Any] = {}
    original = expense_service._entry_snapshot
    worker: list[threading.Thread] = []

    def run_submit() -> None:
        try:
            resp = expense_service.transition_report(
                session_a,
                session_a.get(User, uid),  # type: ignore[arg-type]
                report_id,
                ExpenseReportStatus.submitted,
            )
            session_a.commit()
            outcome["a_total"] = Decimal(str(resp.total_amount))
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            outcome["a_error"] = repr(exc)

    def wrapped(e: Any) -> Any:
        if not worker:
            t = threading.Thread(target=run_submit, daemon=True)
            worker.append(t)
            t.start()
            t.join(_A_HEAD_START_S)
        return original(e)

    monkeypatch.setattr(expense_service, "_entry_snapshot", wrapped)
    try:
        expense_service.update_entry(
            session_b,
            session_b.get(User, uid),  # type: ignore[arg-type]
            entry_id,
            ExpenseUpdate(amount=Decimal("99.00")),
        )
        worker[0].join(30)
        assert not worker[0].is_alive(), "submit never finished"
    finally:
        session_a.close()
        session_b.close()

    assert "a_error" not in outcome, outcome.get("a_error")
    with Session(engine) as s:
        final = s.get(ExpenseReport, report_id)
        assert final is not None
        # Submitted, then auto-approved in the same transaction (in-policy, small).
        assert final.status == ExpenseReportStatus.approved
        assert outcome["a_total"] == Decimal(str(final.total_amount)) == Decimal("99.00"), (
            outcome,
            final.total_amount,
        )
