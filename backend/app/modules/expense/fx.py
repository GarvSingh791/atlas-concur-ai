"""Report totals across currencies (product gap 8).

A report's ``total_amount`` is in the REPORT's currency.  Entries in that
currency are summed exactly as before; an entry in another supported currency is
converted with the static reference table (``config.constants.fx_rates`` —
simulated reference rates, not live market data), per entry, ROUND_HALF_UP to
the report currency's minor unit.  An entry in an unsupported currency (refused at
every write path; only possible for legacy rows) makes the total
uncomputable: 422 ``CURRENCY_NOT_SUPPORTED`` instead of a silent 1:1 sum.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.tools.report_totals import compute_report_total
from backend.app.core.errors import UnprocessableEntityError
from config.constants.fx_rates import UnknownCurrency
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport

CURRENCY_NOT_SUPPORTED = "CURRENCY_NOT_SUPPORTED"


def _code(value: object) -> str:
    return str(value or "").strip().upper()


def report_total(session: Session, report_id: uuid.UUID, *, raw_total: Decimal) -> Decimal:
    """The report total in its own currency.

    ``raw_total`` is the plain SQL sum of every entry; it is returned
    unchanged when no entry is in another currency (byte-identical totals).
    The rule itself lives in ``ai_agents.tools.report_totals`` — shared with the
    Receipt-Inbox release and the expense agent's persistence, so every writer of
    ``total_amount`` computes it the same way; this wrapper maps an unsupported
    currency to 422 ``CURRENCY_NOT_SUPPORTED``.
    """
    try:
        return compute_report_total(session, report_id, raw_total=raw_total)
    except UnknownCurrency as exc:
        target = session.scalar(
            sa.select(ExpenseReport.currency).where(ExpenseReport.id == report_id)
        )
        raise UnprocessableEntityError(
            f"An entry is in an unsupported currency ({_code(exc.code)}); change it to a "
            f"supported currency so the report total can be computed in {_code(target)}.",
            code=CURRENCY_NOT_SUPPORTED,
        ) from None


def report_currency_map(
    session: Session, entries: Sequence[Expense], *, report: ExpenseReport | None = None
) -> dict[uuid.UUID, str]:
    """Report id -> currency for *entries* (one query; *report* is reused)."""
    out: dict[uuid.UUID, str] = {}
    if report is not None:
        out[report.id] = report.currency
    missing = {e.report_id for e in entries if e.report_id not in out}
    if missing:
        rows = session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.currency).where(ExpenseReport.id.in_(missing))
        ).all()
        out.update({rid: cur for rid, cur in rows})
    return out
