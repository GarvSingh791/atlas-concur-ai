"""C7-01 / C7-22 regression tests for the AI expense agent's persistence step.

C7-01: persist_expense_report must only store a trip_id the caller owns in
their own organization. A manager passing a direct report's trip_id, or anyone
passing another org's trip_id, gets a report with trip_id NULL plus an
advisory note.

C7-22: before re-pointing a receipt that is already linked elsewhere,
persistence releases its own Receipt-Inbox draft entry; any other link (e.g. a
submitted report's entry) is never touched.

DB-backed: self-skips without Postgres (conftest fixtures).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.expense_agent.assemble import assemble_report
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.agents.expense_agent.persistence import persist_expense_report
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline import RECEIPT_INBOX_TITLE
from database.models.expense import Expense, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.receipt import OcrStatus

_SNAPSHOT: dict[str, Any] = {
    "flags": [],
    "evaluated_entry_count": 1,
    "blocking_count": 0,
    "advisory_count": 0,
}


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id,
        granted_permission_keys=frozenset(),
        user_id=user.id,
        role=user.role.value,
    )


def _entry(*, primary_receipt_id: str | None = None) -> DraftEntry:
    return DraftEntry(
        category="meals",
        amount=42.0,
        currency="USD",
        expense_date="2026-06-10",
        vendor="Scope Cafe",
        description=None,
        status="matched",
        match_source="receipt",
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=primary_receipt_id,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def _persist(
    session: Session,
    user: User,
    *,
    trip_id: str | None = None,
    primary_receipt_id: str | None = None,
) -> tuple[ExpenseReport, list[str]]:
    report = assemble_report(
        [_entry(primary_receipt_id=primary_receipt_id)], trip_id=trip_id, today="2026-06-14"
    )
    return persist_expense_report(
        intent=ExpenseIntent(trip_id=trip_id, owner_id=None, conversation_id=None),
        report=report,
        flags_snapshot=dict(_SNAPSHOT),
        reflection_outcome_value="passed",
        context=_ctx(user),
        session=session,
    )


# ---------------------------------------------------------------------------
# C7-01 — trip_id ownership
# ---------------------------------------------------------------------------


def test_own_trip_id_is_kept(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    trip = f.make_trip(db_session, traveler=employee)
    report, notes = _persist(db_session, employee, trip_id=str(trip.id))
    assert report.trip_id == trip.id
    assert not any("trip_id" in n for n in notes)


def test_manager_cannot_link_direct_reports_trip(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    manager, employee = users_a["manager"], users_a["employee"]
    assert employee.manager_id == manager.id
    trip = f.make_trip(db_session, traveler=employee)
    report, notes = _persist(db_session, manager, trip_id=str(trip.id))
    assert report.trip_id is None
    assert report.owner_id == manager.id
    assert any(str(trip.id) in n and "stored as NULL" in n for n in notes)


def test_cannot_link_other_orgs_trip(
    db_session: Session,
    rbac_catalog: None,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    manager = users_a["manager"]
    foreign_trip = f.make_trip(db_session, traveler=users_b["employee"])
    report, notes = _persist(db_session, manager, trip_id=str(foreign_trip.id))
    assert report.trip_id is None
    assert any(str(foreign_trip.id) in n and "stored as NULL" in n for n in notes)


def test_nonexistent_trip_id_stored_null(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    import uuid

    missing = str(uuid.uuid4())
    report, notes = _persist(db_session, users_a["employee"], trip_id=missing)
    assert report.trip_id is None
    assert any(missing in n for n in notes)


# ---------------------------------------------------------------------------
# C7-22 — receipt re-link only releases the caller's own inbox draft
# ---------------------------------------------------------------------------


def test_receipt_on_submitted_report_is_not_relinked(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    submitted = f.make_expense_report(
        db_session, owner=employee, status=ExpenseReportStatus.submitted
    )
    old_entry = f.make_expense(
        db_session, report=submitted, amount="42.00", match_source=ExpenseMatchSource.receipt
    )
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        expense_id=old_entry.id,
    )
    report, notes = _persist(db_session, employee, primary_receipt_id=str(receipt.id))
    db_session.refresh(receipt)
    assert receipt.expense_id == old_entry.id
    assert db_session.get(Expense, old_entry.id) is not None
    assert any("already linked" in n for n in notes)


def test_receipt_on_own_inbox_draft_is_released_and_relinked(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    inbox = f.make_expense_report(db_session, owner=employee, title=RECEIPT_INBOX_TITLE)
    inbox_entry = f.make_expense(
        db_session, report=inbox, amount="42.00", match_source=ExpenseMatchSource.receipt
    )
    inbox_entry_id = inbox_entry.id
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        expense_id=inbox_entry_id,
    )
    report, notes = _persist(db_session, employee, primary_receipt_id=str(receipt.id))
    db_session.expire_all()
    assert db_session.get(Expense, inbox_entry_id) is None
    new_entry = db_session.query(Expense).filter(Expense.report_id == report.id).one()
    relinked = db_session.get(type(receipt), receipt.id)
    assert relinked is not None and relinked.expense_id == new_entry.id
    assert not any("already linked" in n for n in notes)
