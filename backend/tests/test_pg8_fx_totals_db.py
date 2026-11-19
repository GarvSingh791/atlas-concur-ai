"""Product gap 8 (fxcard track), review fixes: ONE total rule on every write path.

Pinned (DB-backed, rollback-isolated):
  * Receipt-Inbox release (smart matching frees an inbox line) recomputes the
    inbox total with the converting rule — a EUR line is never added 1:1 to a
    USD total; the released total equals the API's ``_compute_total``;
  * a release that would leave an uncomputable total (a legacy line in an
    unsupported currency) changes NOTHING and returns False (fail closed);
  * the expense agent's persisted report total converts foreign entries
    (mixed EUR + USD) and equals ``_compute_total``; an entry it skipped (bad
    date) is not counted;
  * the agent never writes an entry in an unsupported currency (advisory note);
  * ingest: a receipt in an unsupported currency (BRL) is never drafted — it is
    surfaced as unprocessed — and a receipt with no currency is in the org's
    base currency, not a hard-coded USD.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.expense_agent.assemble import assemble_report
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.ingest import ingest
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.agents.expense_agent.persistence import persist_expense_report
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.inbox import RECEIPT_INBOX_TITLE, release_inbox_draft
from backend.app.modules.expense.service import _compute_total
from database.models.expense import Expense, ExpenseMatchSource
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus, Receipt

pytestmark = [pytest.mark.db]

_INTENT = ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None)
_SNAPSHOT: dict[str, Any] = {
    "flags": [],
    "evaluated_entry_count": 0,
    "blocking_count": 0,
    "advisory_count": 0,
}


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id, granted_permission_keys=frozenset(), user_id=user.id
    )


def _inbox_line(session: Session, inbox: ExpenseReport, amount: str, currency: str) -> Expense:
    return f.make_expense(
        session,
        report=inbox,
        amount=amount,
        currency=currency,
        match_source=ExpenseMatchSource.receipt,
        vendor=f"Vendor {currency} {amount}",
    )


def _inbox(session: Session, owner: User) -> ExpenseReport:
    return f.make_expense_report(session, owner=owner, title=RECEIPT_INBOX_TITLE, currency="USD")


# ── Receipt-Inbox release ─────────────────────────────────────────────────────


def test_release_recomputes_the_inbox_total_with_conversion(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    inbox = _inbox(db_session, emp)
    usd = _inbox_line(db_session, inbox, "320.00", "USD")
    _inbox_line(db_session, inbox, "220.00", "EUR")
    _inbox_line(db_session, inbox, "10.00", "USD")
    inbox.total_amount = _compute_total(db_session, inbox.id)
    assert inbox.total_amount == Decimal("568.70")  # 320 + 238.70 + 10
    receipt = f.make_receipt(
        db_session, uploaded_by=emp, ocr_status=OcrStatus.processed, expense_id=usd.id
    )

    assert release_inbox_draft(db_session, context=_ctx(emp), receipt=receipt) is True
    db_session.flush()

    db_session.refresh(inbox)
    assert inbox.total_amount == Decimal("248.70")  # EUR 220.00 at 1.085 + USD 10.00
    assert inbox.total_amount == _compute_total(db_session, inbox.id)


def test_release_with_an_unsupported_legacy_line_changes_nothing(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    inbox = _inbox(db_session, emp)
    usd = _inbox_line(db_session, inbox, "30.00", "USD")
    _inbox_line(db_session, inbox, "120.00", "BRL")  # legacy row: refused on every write now
    inbox.total_amount = Decimal("150.00")
    receipt = f.make_receipt(
        db_session, uploaded_by=emp, ocr_status=OcrStatus.processed, expense_id=usd.id
    )
    db_session.flush()

    assert release_inbox_draft(db_session, context=_ctx(emp), receipt=receipt) is False
    db_session.flush()

    assert receipt.expense_id == usd.id
    assert db_session.get(Expense, usd.id) is not None
    db_session.refresh(inbox)
    assert inbox.total_amount == Decimal("150.00")


# ── Expense agent: assemble + persist ─────────────────────────────────────────


def _draft(amount: float, currency: str, vendor: str, day: str = "2026-06-10") -> DraftEntry:
    return DraftEntry(
        category="meals",
        amount=amount,
        currency=currency,
        expense_date=day,
        vendor=vendor,
        description=None,
        status="draft",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


def test_agent_built_mixed_currency_report_total_is_converted(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    entries = [
        _draft(50.0, "USD", "Cafe"),
        _draft(12.5, "USD", "Deli"),
        _draft(40.0, "EUR", "Bistro"),
    ]
    draft = assemble_report(entries, trip_id=None, today="2026-06-14")
    assert draft.currency == "USD" and draft.mixed_currency is True
    assert draft.total_amount == pytest.approx(105.90)  # 50 + 12.50 + 43.40 (EUR 40 at 1.085)

    report, _notes = persist_expense_report(
        intent=_INTENT,
        report=draft,
        flags_snapshot=_SNAPSHOT,
        reflection_outcome_value="passed",
        context=_ctx(emp),
        session=db_session,
    )

    assert report.total_amount == Decimal("105.90")
    assert report.total_amount == _compute_total(db_session, report.id)


def test_agent_persisted_total_counts_only_the_entries_it_wrote(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    draft = assemble_report(
        [_draft(20.0, "USD", "Good"), _draft(99.0, "USD", "Bad date", day="not-a-date")],
        trip_id=None,
        today="2026-06-14",
    )
    report, notes = persist_expense_report(
        intent=_INTENT,
        report=draft,
        flags_snapshot=_SNAPSHOT,
        reflection_outcome_value="passed",
        context=_ctx(emp),
        session=db_session,
    )
    assert any("invalid expense_date" in n for n in notes)
    assert report.total_amount == Decimal("20.00") == _compute_total(db_session, report.id)


def test_agent_never_writes_an_entry_in_an_unsupported_currency(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    draft = assemble_report(
        [_draft(30.0, "USD", "Deli"), _draft(120.0, "BRL", "Churrascaria")],
        trip_id=None,
        today="2026-06-14",
    )
    assert draft.currency == "USD"
    report, notes = persist_expense_report(
        intent=_INTENT,
        report=draft,
        flags_snapshot=_SNAPSHOT,
        reflection_outcome_value="passed",
        context=_ctx(emp),
        session=db_session,
    )
    currencies = db_session.scalars(
        sa.select(Expense.currency).where(Expense.report_id == report.id)
    ).all()
    assert list(currencies) == ["USD"]
    assert any("BRL" in n and "not supported" in n for n in notes), notes
    assert report.total_amount == Decimal("30.00") == _compute_total(db_session, report.id)


# ── Expense agent: ingest ─────────────────────────────────────────────────────


def _ocr_receipt(session: Session, user: User, fields: dict[str, Any]) -> Receipt:
    return f.make_receipt(
        session, uploaded_by=user, ocr_status=OcrStatus.processed, extracted_fields=fields
    )


def test_ingest_never_drafts_an_unsupported_currency_receipt(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    brl = _ocr_receipt(
        db_session,
        emp,
        {"amount": 120.0, "currency": "BRL", "date": "2026-06-11", "vendor": "Churrascaria"},
    )
    eur = _ocr_receipt(
        db_session,
        emp,
        {"amount": 60.0, "currency": "eur", "date": "2026-06-11", "vendor": "Cafe de Flore"},
    )
    data = ingest(_INTENT, context=_ctx(emp), session=db_session)
    assert str(brl.id) in data.unprocessed_receipts
    by_id = {r.receipt_id: r for r in data.receipts}
    assert str(brl.id) not in by_id
    assert by_id[str(eur.id)].currency == "EUR"


def test_ingest_defaults_a_missing_currency_to_the_org_base(
    db_session: Session, users_a: dict[str, User], org_a: Organization
) -> None:
    emp = users_a["employee"]
    org_a.base_currency = "EUR"
    db_session.flush()
    receipt = _ocr_receipt(
        db_session, emp, {"amount": 18.0, "date": "2026-06-11", "vendor": "Boulangerie"}
    )
    data = ingest(_INTENT, context=_ctx(emp), session=db_session)
    by_id = {r.receipt_id: r for r in data.receipts}
    assert by_id[str(receipt.id)].currency == "EUR"


def test_ingest_skips_a_card_swipe_in_an_unsupported_currency(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    ok = f.make_card_transaction(
        db_session, user=emp, merchant="Cafe", amount="20.00", txn_date=date(2026, 6, 11)
    )
    brl = f.make_card_transaction(
        db_session,
        user=emp,
        merchant="Churrascaria",
        amount="120.00",
        currency="BRL",
        txn_date=date(2026, 6, 11),
    )
    data = ingest(_INTENT, context=_ctx(emp), session=db_session)
    ids = {t.txn_id for t in data.card_txns}
    assert str(ok.id) in ids and str(brl.id) not in ids  # stays unmatched in the card feed
