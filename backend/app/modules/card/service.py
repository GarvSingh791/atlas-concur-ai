"""Card feed service (product gap 8) — the caller's OWN card transactions.

Card transactions are system-captured (the simulated corporate-card feed) and
consumed by smart matching.  This read lists them for their cardholder only:
``owner_scope_query`` (tenant + ``user_id == caller``) for every role — a
manager sees their own swipes, never their reports' (the feed is a personal
inbox like the Receipts page, not a team view).  Read-only: no audit row.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.errors import BadRequestError
from backend.app.core.money import amount_conversion
from backend.app.core.pagination import Page, PaginationParams
from backend.app.core.rbac import owner_scope_query
from backend.app.modules.card.schemas import CardTransactionItem, CardTransactionStatus
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization, User
from database.models.receipt import Receipt


def _is_matched() -> Any:
    return sa.or_(CardTransaction.matched.is_(True), CardTransaction.expense_id.is_not(None))


def list_card_transactions(
    session: Session,
    user: User,
    pagination: PaginationParams,
    *,
    status: CardTransactionStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Page[CardTransactionItem]:
    """The caller's card transactions, newest first, filtered and paginated."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise BadRequestError("date_from must be on or before date_to.")

    stmt = owner_scope_query(
        sa.select(CardTransaction),
        user=user,
        ownership_column=CardTransaction.user_id,
        org_column=CardTransaction.organization_id,
    )
    if status == "matched":
        stmt = stmt.where(_is_matched())
    elif status == "unmatched":
        stmt = stmt.where(sa.not_(_is_matched()))
    if date_from is not None:
        stmt = stmt.where(CardTransaction.txn_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(CardTransaction.txn_date <= date_to)

    total = int(session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        session.scalars(
            stmt.order_by(
                CardTransaction.txn_date.desc(),
                CardTransaction.created_at.desc(),
                CardTransaction.id.desc(),
            )
            .offset(pagination.offset)
            .limit(pagination.limit)
        ).all()
    )
    links = _own_links(session, user, [t.expense_id for t in rows if t.expense_id is not None])
    base = session.scalar(
        sa.select(Organization.base_currency).where(Organization.id == user.organization_id)
    )
    items = [_item(t, links.get(t.expense_id) if t.expense_id else None, base) for t in rows]
    return Page.create(items=items, total=total, page=pagination.page, size=pagination.size)


def _own_links(
    session: Session, user: User, expense_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID | None]]:
    """Expense id -> (report id, first receipt id) for entries on the CALLER's reports.

    "First receipt" is a fixed rule: the earliest upload (``created_at``), ties
    (receipts written in one transaction share it) broken by the lowest id.
    """
    if not expense_ids:
        return {}
    own = session.execute(
        sa.select(Expense.id, Expense.report_id)
        .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
        .where(
            Expense.id.in_(expense_ids),
            Expense.organization_id == user.organization_id,
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id == user.id,
        )
    ).all()
    reports = {eid: rid for eid, rid in own}
    receipts: dict[uuid.UUID, uuid.UUID] = {}
    if reports:
        for receipt_id, expense_id in session.execute(
            sa.select(Receipt.id, Receipt.expense_id)
            .where(
                Receipt.expense_id.in_(list(reports)),
                Receipt.organization_id == user.organization_id,
            )
            .order_by(Receipt.created_at, Receipt.id)
        ).all():
            if expense_id is not None:
                receipts.setdefault(expense_id, receipt_id)
    return {eid: (rid, receipts.get(eid)) for eid, rid in reports.items()}


def _item(
    txn: CardTransaction,
    link: tuple[uuid.UUID, uuid.UUID | None] | None,
    base: str | None,
) -> CardTransactionItem:
    matched = bool(txn.matched) or txn.expense_id is not None
    return CardTransactionItem(
        id=txn.id,
        merchant=txn.merchant,
        txn_date=txn.txn_date,  # type: ignore[arg-type]
        amount=txn.amount,  # type: ignore[arg-type]
        currency=txn.currency,
        last_four=txn.last_four,
        status="matched" if matched else "unmatched",
        expense_id=txn.expense_id if link is not None else None,
        report_id=link[0] if link is not None else None,
        receipt_id=link[1] if link is not None else None,
        amount_conversion=amount_conversion(txn.amount, txn.currency, base),
    )
