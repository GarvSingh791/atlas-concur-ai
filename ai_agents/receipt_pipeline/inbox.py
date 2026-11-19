"""Receipt Inbox helpers — the auto-drafted inbox entry a receipt upload creates.

A receipt upload (FEATURE B) auto-drafts one Expense into the uploader's draft
"Receipt Inbox" report and links ``receipt.expense_id`` to it. That link must
not hide the receipt from smart-match (C7-22): when assembly matches the
receipt to a card transaction, the card entry takes the receipt and the inbox
draft entry is released so the same charge cannot be claimed twice.

Safety invariants (every helper enforces all of them):
  * same organization AND the receipt's uploader owns the inbox report;
  * the inbox report is still ``draft`` (a submitted/approved report is NEVER
    touched) and has never been sent for approval (UAT2 — a returned inbox
    holds reviewed evidence);
  * the entry is a receipt-sourced (``match_source=receipt``) entry.

LAYERING LAW: never imports from backend/.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.report_totals import compute_report_total
from config.constants.fx_rates import UnknownCurrency
from database.models.approval import Approval, ApprovalSubjectType
from database.models.expense import Expense, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.receipt import Receipt

logger = logging.getLogger(__name__)

# Title of the auto-created draft report that collects receipt-upload expenses.
RECEIPT_INBOX_TITLE = "Receipt Inbox"


def _inbox_entry_stmt(
    *, organization_id: uuid.UUID, user_id: uuid.UUID, expense_ids: Iterable[uuid.UUID]
) -> sa.Select:
    # UAT2: an inbox that has EVER been sent for approval (returned for changes,
    # reopened after a reject) holds reviewed evidence — its entries are never
    # released (deleted) here.  The same rule as the inbox finder, which only
    # reuses never-reviewed inboxes, and as the owner's DELETE of an entry.
    reviewed = sa.select(Approval.id).where(
        Approval.organization_id == organization_id,
        Approval.subject_type == ApprovalSubjectType.expense_report,
        Approval.subject_id == ExpenseReport.id,
    )
    return (
        sa.select(Expense)
        .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
        .where(
            Expense.id.in_(list(expense_ids)),
            Expense.organization_id == organization_id,
            Expense.match_source == ExpenseMatchSource.receipt,
            ExpenseReport.organization_id == organization_id,
            ExpenseReport.owner_id == user_id,
            ExpenseReport.title == RECEIPT_INBOX_TITLE,
            ExpenseReport.status == ExpenseReportStatus.draft,
            ~reviewed.exists(),
        )
    )


def editable_inbox_expense_ids(
    session: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    expense_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """Return the subset of *expense_ids* that are releasable inbox draft entries."""
    ids = [eid for eid in expense_ids if eid is not None]
    if not ids:
        return set()
    stmt = _inbox_entry_stmt(organization_id=organization_id, user_id=user_id, expense_ids=ids)
    return {e.id for e in session.scalars(stmt).all()}


def _prune_inbox_flags(report: ExpenseReport, removed: Expense, remaining_vendors: set) -> None:
    """Drop the removed entry's pre-submit flags and recompute the counts."""
    snapshot = report.pre_submit_flags
    if not isinstance(snapshot, dict):
        return
    refs = {str(removed.id)}
    # The auto-draft keys flags by vendor when one is present; only drop a
    # vendor-keyed flag when no remaining inbox entry shares that vendor.
    if removed.vendor and removed.vendor not in remaining_vendors:
        refs.add(removed.vendor)
    flags = [
        f
        for f in (snapshot.get("flags") or [])
        if not (
            isinstance(f, dict)
            and (f.get("entry_ref") in refs or f.get("entry_id") == str(removed.id))
        )
    ]
    report.pre_submit_flags = {
        **snapshot,
        "flags": flags,
        "evaluated_entry_count": max(0, int(snapshot.get("evaluated_entry_count", 0) or 0) - 1),
        "blocking_count": sum(
            1 for f in flags if isinstance(f, dict) and f.get("severity") == "blocking"
        ),
        "advisory_count": sum(
            1 for f in flags if isinstance(f, dict) and f.get("severity") == "advisory"
        ),
    }


def release_inbox_draft(
    session: Session,
    *,
    context: AgentContext,
    receipt: Receipt,
    refresh_flags: bool = True,
    released_reports: set[uuid.UUID] | None = None,
) -> bool:
    """Remove the receipt's auto-drafted Receipt-Inbox entry so it can be re-linked.

    Call this BEFORE re-pointing ``receipt.expense_id`` at a smart-matched card
    entry. Returns True when the receipt is now free to link: either it had no
    link, or its link was an editable inbox draft entry that has been deleted
    (report total recomputed, the inbox's pre-submit check re-run). Returns
    False — and changes NOTHING — when the receipt is linked to anything else (a
    submitted report, another user's report, a non-inbox entry) or the inbox
    total could not be computed afterwards (product gap 8); the caller must
    then leave the existing link alone. Flush-only; the caller commits.

    UAT2 defect D: pruning the released line's flags is not enough — a twin left
    in the inbox kept a ``possible_duplicate`` naming the released line — so the
    inbox's check is re-run (``refresh_persisted_flags``; on an evaluation
    failure the pruned snapshot stays).  A caller releasing several lines in one
    go passes ``refresh_flags=False`` plus ``released_reports`` (filled with the
    inbox ids) and refreshes each inbox ONCE when it is done.
    """
    if receipt.organization_id != context.organization_id or receipt.uploaded_by != (
        context.user_id
    ):
        return False
    if receipt.expense_id is None:
        return True
    stmt = _inbox_entry_stmt(
        organization_id=context.organization_id,
        user_id=context.user_id,
        expense_ids=[receipt.expense_id],
    )
    entry = session.scalars(stmt).first()
    if entry is None:
        return False

    report = session.get(ExpenseReport, entry.report_id)
    if report is None:  # pragma: no cover - guaranteed by the join above
        return False

    # Product gap 8: the inbox total after the release, with the SAME converting
    # rule as every other total writer (a EUR line is never added 1:1 to a USD
    # total).  Computed BEFORE any change: a line the rule cannot convert (a
    # legacy unsupported currency) means the release changes NOTHING.
    try:
        new_total = compute_report_total(session, report.id, exclude_expense_ids=[entry.id])
    except UnknownCurrency:
        logger.warning(
            "receipt inbox: not releasing entry %s — report %s holds an entry in an "
            "unsupported currency",
            entry.id,
            report.id,
        )
        return False

    receipt.expense_id = None
    session.flush()
    session.delete(entry)
    session.flush()

    report.total_amount = new_total
    remaining_vendors = set(
        session.scalars(
            sa.select(Expense.vendor).where(
                Expense.report_id == report.id, Expense.vendor.is_not(None)
            )
        ).all()
    )
    _prune_inbox_flags(report, entry, remaining_vendors)
    report.updated_by = context.user_id
    session.flush()
    if released_reports is not None:
        released_reports.add(report.id)
    if refresh_flags:
        refresh_inbox_flags(session, context=context, report=report)
    logger.info(
        "receipt inbox: released draft entry %s (receipt %s) from report %s",
        entry.id,
        receipt.id,
        report.id,
    )
    return True


def refresh_inbox_flags(session: Session, *, context: AgentContext, report: ExpenseReport) -> None:
    """Re-run *report*'s pre-submit check after lines left it (UAT2 defect D; flush-only).

    The persisted-flags refresh lives with the expense agent, whose persistence
    module imports this one — hence the deferred import.
    """
    from ai_agents.agents.expense_agent.persistence import refresh_persisted_flags

    refresh_persisted_flags(report, context=context, session=session)
