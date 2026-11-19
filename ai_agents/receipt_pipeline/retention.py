"""Receipt evidence retention (UAT2) — which receipts must never be deleted or reused.

A receipt that is, or was, evidence on an expense report with ANY approval history
(the report was submitted at least once, so an approver or the approval agent may
have relied on it) is kept for audit:

  * it is never hard-deleted (row or stored image);
  * once detached from its entry (the entry was deleted from a returned report), it
    is never linked to another entry.

There is no column for this (no migration): the marker is an append-only
``audit_log`` row ``receipt.retained_for_audit`` written when such a receipt is
detached, read back through the (entity_type, entity_id) audit index.  A receipt
still LINKED is judged by its report's approval history directly.

UAT2 hardening — relied-on evidence is FROZEN: a receipt kept for audit, or one
backing an entry whose report is not a draft or has any approval history (a
reviewer — or the approval agent — has seen it), can no longer be corrected
(``correction_lock_reason``; PATCH /receipts → 409 RECEIPT_LOCKED).  Corrections
on receipts of never-submitted drafts stay allowed.

A receipt attached to a RETURNED report (a draft again, with approval history)
that no return snapshot lists (``ApprovalStep.subject_snapshot["receipt_ids"]``,
frozen when the reviewer returned it) has not been seen by anyone yet: it stays
correctable and deletable until the report is resubmitted
(``unreviewed_receipt_ids``) — otherwise one wrong attachment while fixing a
returned report would be a dead end.  A LEGACY return step (frozen before
receipt ids were, e.g. the seeded returned report) falls back to time: a receipt
whose latest own (non-shared) ``expense.receipt_attached`` row on the report is
newer than the return is unreviewed; anything else stays reviewed.

LAYERING LAW: never imports from backend/.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.receipt_pipeline.evidence import backing_expense_ids
from database.models.approval import Approval, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.receipt import Receipt

RECEIPT_RETAINED_ACTION = "receipt.retained_for_audit"
RECEIPT_RETAINED_MESSAGE = "This receipt was part of a submitted report and is kept for audit."
RECEIPT_LOCKED = "RECEIPT_LOCKED"
RECEIPT_LOCKED_MESSAGE = (
    "This receipt is part of a submitted report and can't be changed — attach a new "
    "receipt instead."
)


def report_has_approval(
    session: Session, *, organization_id: uuid.UUID, report_id: uuid.UUID
) -> bool:
    """True when *report_id* has ever been sent for approval (any approvals row)."""
    return (
        session.scalar(
            sa.select(Approval.id)
            .where(
                Approval.organization_id == organization_id,
                Approval.subject_type == ApprovalSubjectType.expense_report,
                Approval.subject_id == report_id,
            )
            .limit(1)
        )
        is not None
    )


def retained_receipt_ids(
    session: Session, *, organization_id: uuid.UUID, receipt_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of *receipt_ids* carrying the ``receipt.retained_for_audit`` marker."""
    ids = [rid for rid in receipt_ids if rid is not None]
    if not ids:
        return set()
    return set(
        session.scalars(
            sa.select(AuditLog.entity_id).where(
                AuditLog.organization_id == organization_id,
                AuditLog.entity_type == "receipt",
                AuditLog.entity_id.in_(ids),
                AuditLog.action == RECEIPT_RETAINED_ACTION,
            )
        ).all()
    )


def receipt_retained_for_audit(
    session: Session, *, organization_id: uuid.UUID, receipt_id: uuid.UUID
) -> bool:
    """True when *receipt_id* was detached from reviewed evidence and must be kept."""
    return bool(
        retained_receipt_ids(session, organization_id=organization_id, receipt_ids=[receipt_id])
    )


SNAPSHOT_RECEIPTS_KEY = "receipt_ids"
ATTACH_RECEIPT_ID_KEY = "receipt_id"
RECEIPT_ATTACHED_ACTION = "expense.receipt_attached"
# after_state key of an attach that SHARED a receipt already backing another line of
# the same report (itemized siblings): never proof the receipt arrived from outside.
ATTACH_SHARED_KEY = "shared_with_expense_id"


def report_receipt_ids(
    session: Session, *, organization_id: uuid.UUID, report_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every receipt backing an entry of *report_id* (direct link or shared sibling)."""
    entry_ids = set(
        session.scalars(
            sa.select(Expense.id).where(
                Expense.report_id == report_id, Expense.organization_id == organization_id
            )
        ).all()
    )
    if not entry_ids:
        return []
    rows = session.scalars(
        sa.select(Receipt).where(
            Receipt.organization_id == organization_id,
            sa.or_(
                Receipt.expense_id.in_(list(entry_ids)),
                Receipt.grounding_context["shared_expense_ids"].is_not(None),
            ),
        )
    ).all()
    return sorted((r.id for r in rows if entry_ids.intersection(backing_expense_ids(r))), key=str)


def unreviewed_receipt_ids(
    session: Session,
    *,
    organization_id: uuid.UUID,
    report_id: uuid.UUID,
    receipt_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """The subset of *receipt_ids* that no reviewer has seen on *report_id*.

    Every human return / rejection step freezes the receipts backing the report
    (``subject_snapshot["receipt_ids"]``).  A receipt is UNREVIEWED when the latest
    such step recorded receipts and no such step lists it — i.e. it was attached
    after the report came back.

    LEGACY latest step (no ``receipt_ids``: pre-0014 NULL snapshot, or one frozen
    before receipt ids were): fall back to time — a receipt no step lists whose
    LATEST own attach row on this report (``expense.receipt_attached``, after_state
    report_id / receipt_id, not a share onto a sibling line) is newer than that
    step's ``decided_at`` is unreviewed.  This cannot unlock a receipt that was on
    the report at the return: it could only get a newer own-attach row after being
    unlinked from the report, and unlinking a reviewed receipt from a report with
    approval history keeps it for audit (never relinkable); sharing it onto another
    line writes a share row, which never counts.  Fail closed: no return step, a
    legacy step without a decision time, or no newer own-attach row -> reviewed.
    Only meaningful while the report is a draft: once resubmitted it is evidence again.
    """
    ids = [rid for rid in receipt_ids if rid is not None]
    if not ids:
        return set()
    steps = session.execute(
        sa.select(ApprovalStep.subject_snapshot, ApprovalStep.decided_at)
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .where(
            Approval.organization_id == organization_id,
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id == report_id,
            ApprovalStep.decision.in_([ApprovalDecision.reject, ApprovalDecision.request_changes]),
        )
        .order_by(ApprovalStep.step_order.desc())
    ).all()
    if not steps:
        return set()
    seen: set[str] = set()
    for snap, _decided_at in steps:
        if isinstance(snap, dict):
            seen.update(str(x) for x in snap.get(SNAPSHOT_RECEIPTS_KEY) or [])
    unlisted = {rid for rid in ids if str(rid) not in seen}
    latest, decided_at = steps[0]
    if isinstance(latest, dict) and SNAPSHOT_RECEIPTS_KEY in latest:
        return unlisted
    if decided_at is None or not unlisted:
        return set()
    return _attached_after(
        session,
        organization_id=organization_id,
        report_id=report_id,
        receipt_ids=unlisted,
        after=decided_at,
    )


def _attached_after(
    session: Session,
    *,
    organization_id: uuid.UUID,
    report_id: uuid.UUID,
    receipt_ids: set[uuid.UUID],
    after: datetime,
) -> set[uuid.UUID]:
    """The subset of *receipt_ids* whose latest own attach row on *report_id* is after *after*."""
    receipt_key = AuditLog.after_state[ATTACH_RECEIPT_ID_KEY].astext
    rows = session.execute(
        sa.select(receipt_key, sa.func.max(AuditLog.created_at))
        .where(
            AuditLog.organization_id == organization_id,
            AuditLog.entity_type == "expense",
            AuditLog.action == RECEIPT_ATTACHED_ACTION,
            AuditLog.after_state["report_id"].astext == str(report_id),
            receipt_key.in_([str(rid) for rid in receipt_ids]),
            sa.not_(AuditLog.after_state.has_key(ATTACH_SHARED_KEY)),
        )
        .group_by(receipt_key)
    ).all()
    newer = {str(rid) for rid, last in rows if last is not None and last > after}
    return {rid for rid in receipt_ids if str(rid) in newer}


def correction_locked_receipt_ids(
    session: Session, *, organization_id: uuid.UUID, receipts: Iterable[Receipt]
) -> set[uuid.UUID]:
    """The subset of *receipts* whose extracted fields may no longer be corrected.

    Locked = kept for audit, or backing (``expense_id`` or a shared sibling link)
    an entry whose report is not a draft, or has approval history (submitted at
    least once: a returned report's receipts were seen by a reviewer) — except a
    receipt no reviewer has seen (``unreviewed_receipt_ids``).
    """
    rows = [r for r in receipts if r is not None]
    if not rows:
        return set()
    locked = retained_receipt_ids(
        session, organization_id=organization_id, receipt_ids=[r.id for r in rows]
    )
    backing = {r.id: backing_expense_ids(r) for r in rows if r.id not in locked}
    entry_ids = {eid for ids in backing.values() for eid in ids}
    if not entry_ids:
        return locked
    reported = session.execute(
        sa.select(Expense.id, ExpenseReport.id, ExpenseReport.status, ExpenseReport.withdrawn_at)
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            Expense.id.in_(list(entry_ids)),
            Expense.organization_id == organization_id,
            ExpenseReport.organization_id == organization_id,
        )
    ).all()
    # A WITHDRAWN report (product gap 4: draft + withdrawn_at) is closed — locked
    # like any non-draft report.
    report_of = {
        eid: (rid, status if withdrawn_at is None else None)
        for eid, rid, status, withdrawn_at in reported
    }
    report_ids = {rid for rid, _status in report_of.values()}
    with_approval = (
        set(
            session.scalars(
                sa.select(Approval.subject_id).where(
                    Approval.organization_id == organization_id,
                    Approval.subject_type == ApprovalSubjectType.expense_report,
                    Approval.subject_id.in_(list(report_ids)),
                )
            ).all()
        )
        if report_ids
        else set()
    )
    for receipt_id, ids in backing.items():
        for eid in ids:
            found = report_of.get(eid)
            if found is None:
                continue
            rid, status = found
            if status != ExpenseReportStatus.draft:
                locked.add(receipt_id)
                break
            if rid in with_approval and receipt_id not in unreviewed_receipt_ids(
                session, organization_id=organization_id, report_id=rid, receipt_ids=[receipt_id]
            ):
                locked.add(receipt_id)
                break
    return locked


def correction_lock_reason(session: Session, *, receipt: Receipt) -> str | None:
    """Why *receipt* can't be corrected (None = it can) — see the module docstring."""
    locked = correction_locked_receipt_ids(
        session, organization_id=receipt.organization_id, receipts=[receipt]
    )
    return RECEIPT_LOCKED_MESSAGE if receipt.id in locked else None
