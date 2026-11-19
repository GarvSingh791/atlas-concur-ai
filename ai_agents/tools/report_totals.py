"""One report-total rule for every writer (product gap 8, review fix).

A report's ``total_amount`` is in the REPORT's currency.  Entries in that
currency are summed exactly (plain SQL sum, byte-identical to the pre-FX total);
an entry in another supported currency is converted per entry with the static
reference table (``config.constants.fx_rates.convert`` — simulated reference
rates, ROUND_HALF_UP to the report currency's minor unit).  An entry in an
unsupported currency raises ``UnknownCurrency``: the total is never a silent 1:1
sum across currencies.  Each caller maps that to its own fail-closed outcome
(the API answers 422 ``CURRENCY_NOT_SUPPORTED``; the Receipt-Inbox release
changes nothing).

Used by the backend (``expense.fx.report_total``), the Receipt-Inbox release
(``receipt_pipeline.inbox``) and the expense agent's persistence, so the three
write paths can never disagree.  LAYERING LAW: never imports from backend/.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.constants.fx_rates import convert
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport

__all__ = ["compute_report_total"]


def _code(value: object) -> str:
    return str(value or "").strip().upper()


def compute_report_total(
    session: Session,
    report_id: uuid.UUID,
    *,
    raw_total: Decimal | None = None,
    exclude_expense_ids: Iterable[uuid.UUID] = (),
) -> Decimal:
    """The total of *report_id*'s entries in the report's own currency.

    ``raw_total`` — the caller's plain SQL sum of every entry, when it already
    has one (the backend); computed here otherwise.  ``exclude_expense_ids`` —
    entries to leave out (a line about to be released), only honoured when
    ``raw_total`` is not given.  Raises ``UnknownCurrency`` for an entry (or a
    report) in an unsupported currency.
    """
    excluded = list(exclude_expense_ids)
    if raw_total is None:
        raw_stmt = sa.select(sa.func.coalesce(sa.func.sum(Expense.amount), 0)).where(
            Expense.report_id == report_id
        )
        if excluded:
            raw_stmt = raw_stmt.where(Expense.id.notin_(excluded))
        raw_total = Decimal(str(session.scalar(raw_stmt) or 0))
    currency = session.scalar(
        sa.select(ExpenseReport.currency).where(ExpenseReport.id == report_id)
    )
    if currency is None:
        return raw_total
    target = _code(currency)
    foreign_stmt = sa.select(Expense.amount, Expense.currency).where(
        Expense.report_id == report_id,
        sa.func.upper(sa.func.trim(Expense.currency)) != target,
    )
    if excluded:
        foreign_stmt = foreign_stmt.where(Expense.id.notin_(excluded))
    foreign = session.execute(foreign_stmt).all()
    if not foreign:
        return raw_total
    total = raw_total
    for amount, code in foreign:
        value = Decimal(str(amount))
        total = total - value + convert(value, _code(code), target)
    return total
