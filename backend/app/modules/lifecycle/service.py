"""Report & trip lifecycle actions (product gaps 1, 4 and 8).

* ``reimburse_report``  — finance marks an APPROVED expense report reimbursed
  (simulated payment: a server-generated reference, no money moves).
* ``withdraw_report`` / ``withdraw_trip`` — the owner closes a RETURNED subject
  they no longer want to fix, so it stops blocking their future auto-approvals
  (approval agent ``_open_returned_subject_flags``) without deleting its approval
  history.
* ``complete_trip`` — the traveler (or finance) marks an approved trip that has
  ended as completed.

Security invariants (same as the expense / travel services):
  1. Every load goes through ``scope_query`` (tenant first, then role): unreadable
     or nonexistent → 404, never an existence leak.
  2. Writes are owner-only (403) except the two finance actions, which are
     permission-gated by the router and additionally refuse the caller's own
     report (segregation of duties: no self-reimbursement).
  3. Every subject row is locked ``FOR UPDATE`` and changed by a version CAS
     (stale If-Match → 409); illegal states → 409 with no mutation and no audit.
  4. Every state change writes exactly one audit row in the same transaction.
  5. Money is Decimal end to end.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.concurrency import guarded_version_update
from backend.app.core.errors import ConflictError, ForbiddenError, NotFoundError
from backend.app.core.rbac import require_owner, scope_query
from backend.app.modules.expense import service as expense_service
from backend.app.modules.expense.fx import report_total
from backend.app.modules.expense.schemas import ExpenseReportResponse
from backend.app.modules.lifecycle.schemas import ReimbursementQueue, ReimbursementQueueItem
from backend.app.modules.travel import service as travel_service
from backend.app.modules.travel.schemas import TripResponse
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import RoleKey, User
from database.models.trip import Trip, TripStatus

# Error codes (stable, machine-readable; the message is user-facing).
ALREADY_REIMBURSED = "ALREADY_REIMBURSED"
REPORT_NOT_APPROVED = "REPORT_NOT_APPROVED"
APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
REIMBURSEMENT_TOTAL_MISMATCH = "REIMBURSEMENT_TOTAL_MISMATCH"
REPORT_NOT_RETURNED = "REPORT_NOT_RETURNED"
TRIP_NOT_RETURNED = "TRIP_NOT_RETURNED"
TRIP_WITHDRAWN = "TRIP_WITHDRAWN"
TRIP_NOT_APPROVED = "TRIP_NOT_APPROVED"

# An approval is a decided APPROVAL only in these states (human or agent).
_APPROVED_DECISIONS = frozenset({ApprovalStatus.approved, ApprovalStatus.auto_approved})
# A subject is "returned" to its owner while its approval is in these states.
_RETURNED_APPROVAL = frozenset({ApprovalStatus.changes_requested, ApprovalStatus.rejected})

REIMBURSE_REFERENCE_PREFIX = "REIMB"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reimbursement_reference(report_id: uuid.UUID, at: datetime) -> str:
    """``REIMB-<yyyymmdd>-<12 hex of the report id>`` — unique per report (0016 index)."""
    return f"{REIMBURSE_REFERENCE_PREFIX}-{at:%Y%m%d}-{report_id.hex[:12].upper()}"


def _subject_approval(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> Approval | None:
    return session.scalars(
        sa.select(Approval).where(
            Approval.organization_id == organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    ).first()


def _lock_report(session: Session, current_user: User, report_id: uuid.UUID) -> ExpenseReport:
    stmt = scope_query(
        sa.select(ExpenseReport).where(ExpenseReport.id == report_id),
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    report = session.scalars(
        stmt.with_for_update(of=ExpenseReport).execution_options(populate_existing=True)
    ).first()
    if report is None:
        raise NotFoundError("Expense report not found.")
    return report


def _lock_trip(session: Session, current_user: User, trip_id: uuid.UUID) -> Trip:
    stmt = scope_query(
        sa.select(Trip).where(Trip.id == trip_id),
        user=current_user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    trip = session.scalars(
        stmt.with_for_update(of=Trip).execution_options(populate_existing=True)
    ).first()
    if trip is None:
        raise NotFoundError("Trip not found.")
    return trip


def _check_version(entity: Any, expected_version: int | None, noun: str) -> None:
    if expected_version is not None and expected_version != entity.version:
        raise ConflictError(f"{noun} was modified by another request.")


def _report_state(report: ExpenseReport) -> dict[str, Any]:
    return {
        "status": report.status.value,
        "version": report.version,
        "owner_id": str(report.owner_id),
        "title": report.title,
        "total_amount": str(report.total_amount),
        "currency": report.currency,
        "submitted_at": str(report.submitted_at) if report.submitted_at else None,
        "reimbursed_at": str(report.reimbursed_at) if report.reimbursed_at else None,
        "reimbursement_reference": report.reimbursement_reference,
        "reimbursement_amount": (
            str(report.reimbursement_amount) if report.reimbursement_amount is not None else None
        ),
        "withdrawn_at": str(report.withdrawn_at) if report.withdrawn_at else None,
    }


def _trip_state(trip: Trip) -> dict[str, Any]:
    return {
        "status": trip.status.value,
        "version": trip.version,
        "traveler_id": str(trip.traveler_id),
        "primary_destination": trip.primary_destination,
        "start_date": str(trip.start_date),
        "end_date": str(trip.end_date),
        "withdrawn_at": str(trip.withdrawn_at) if trip.withdrawn_at else None,
    }


# ---------------------------------------------------------------------------
# Gap 1 — simulated reimbursement (finance)
# ---------------------------------------------------------------------------


def reimburse_report(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    *,
    note: str | None,
    expected_version: int | None = None,
) -> ExpenseReportResponse:
    """Mark an APPROVED report reimbursed (simulated payment); audit; commit.

    404 unreadable; 403 the caller owns the report (no self-reimbursement, even
    for finance); 409 ``ALREADY_REIMBURSED`` (idempotent-safe: a second call
    changes nothing and writes no audit row), ``REPORT_NOT_APPROVED`` (any other
    status), ``APPROVAL_NOT_APPROVED`` (no decided-approved approval — missing,
    pending, returned or rejected), ``REIMBURSEMENT_TOTAL_MISMATCH`` (the stored
    total no longer equals its lines, or there is nothing to pay — fail closed),
    stale If-Match.  The report row lock serialises against a concurrent approval
    reversal (decide_approval locks the same row; a reimbursed report is no
    longer decidable).
    """
    report = _lock_report(session, current_user, report_id)
    if report.owner_id == current_user.id:
        raise ForbiddenError("You cannot reimburse your own expense report.")
    if report.status == ExpenseReportStatus.reimbursed:
        raise ConflictError(
            "This expense report has already been reimbursed.", code=ALREADY_REIMBURSED
        )
    if report.status != ExpenseReportStatus.approved:
        raise ConflictError(
            f"Only an approved expense report can be reimbursed (this one is "
            f"'{report.status.value}').",
            code=REPORT_NOT_APPROVED,
        )
    approval = _subject_approval(
        session,
        organization_id=report.organization_id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
    )
    if approval is None or approval.current_status not in _APPROVED_DECISIONS:
        raise ConflictError(
            "This report has no completed approval, so it can't be reimbursed.",
            code=APPROVAL_NOT_APPROVED,
        )
    _check_version(report, expected_version, "Expense report")

    raw_lines_total = Decimal(
        str(
            session.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(Expense.amount), 0)).where(
                    Expense.report_id == report.id,
                    Expense.organization_id == report.organization_id,
                )
            )
        )
    )
    # Product-gaps integration: the total is in the REPORT's currency (gap 8) —
    # recompute it with the same converting rule every writer of total_amount
    # uses, so a report with a foreign-currency line can be reimbursed and a
    # drifted total still fails closed.  Byte-identical for single-currency
    # reports; an unsupported currency is a 422 (never a guessed amount).
    lines_total = report_total(session, report.id, raw_total=raw_lines_total)
    amount = Decimal(str(report.total_amount))
    if amount <= 0 or lines_total != amount:
        raise ConflictError(
            "This report's total does not match its lines, so it can't be reimbursed. "
            "Ask the report owner or an administrator to check it.",
            code=REIMBURSEMENT_TOTAL_MISMATCH,
        )

    before = _report_state(report)
    at = _now()
    guarded_version_update(
        session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=report.version,
        values={
            "status": ExpenseReportStatus.reimbursed,
            "reimbursed_at": at,
            "reimbursed_by_id": current_user.id,
            "reimbursement_reference": reimbursement_reference(report.id, at),
            "reimbursement_amount": amount,
            "updated_by": current_user.id,
        },
    )
    session.refresh(report)
    record_audit(
        session,
        organization_id=report.organization_id,
        action="expense_report.reimbursed",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state=before,
        after_state={**_report_state(report), "simulated": True, "note": note},
    )
    session.commit()
    return expense_service.get_report(session, current_user, report.id)


def reimbursement_queue(session: Session, current_user: User) -> ReimbursementQueue:
    """APPROVED reports in the caller's read scope (finance: org-wide), oldest first."""
    base = scope_query(
        sa.select(ExpenseReport).where(ExpenseReport.status == ExpenseReportStatus.approved),
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    total = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0
    reports = session.scalars(
        base.order_by(ExpenseReport.submitted_at.asc().nulls_last(), ExpenseReport.id).limit(100)
    ).all()
    names: dict[uuid.UUID, str] = {}
    approvals: dict[uuid.UUID, ApprovalStatus] = {}
    if reports:
        for user_id, name in session.execute(
            sa.select(User.id, User.name).where(
                User.organization_id == current_user.organization_id,
                User.id.in_({r.owner_id for r in reports}),
            )
        ).all():
            names[user_id] = name
        for subject_id, status in session.execute(
            sa.select(Approval.subject_id, Approval.current_status).where(
                Approval.organization_id == current_user.organization_id,
                Approval.subject_type == ApprovalSubjectType.expense_report,
                Approval.subject_id.in_([r.id for r in reports]),
            )
        ).all():
            approvals[subject_id] = status
    items = [
        ReimbursementQueueItem(
            id=r.id,
            title=r.title,
            owner_id=r.owner_id,
            owner_name=names.get(r.owner_id),
            total_amount=Decimal(str(r.total_amount)),
            currency=r.currency,
            submitted_at=r.submitted_at,  # type: ignore[arg-type]
            approval_status=approvals[r.id].value if r.id in approvals else None,
            version=r.version,
            can_reimburse=(
                r.owner_id != current_user.id
                and r.id in approvals
                and approvals[r.id] in _APPROVED_DECISIONS
            ),
        )
        for r in reports
    ]
    return ReimbursementQueue(items=items, total=int(total))


# ---------------------------------------------------------------------------
# Gap 4 — owner withdraws a RETURNED report / trip
# ---------------------------------------------------------------------------


def withdraw_report(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    *,
    reason: str | None,
    expected_version: int | None = None,
) -> ExpenseReportResponse:
    """Close a RETURNED report its owner no longer wants to fix; audit; commit.

    Returned = a draft whose approval a human sent back (changes_requested, or
    rejected and reopened).  404 unreadable, 403 not the owner, 409
    ``REPORT_WITHDRAWN`` (already withdrawn), ``REPORT_NOT_RETURNED`` (never
    submitted — delete it instead —, under review, approved, reimbursed or
    finally rejected), stale If-Match.  The approval and its history stay;
    status stays ``draft`` and ``withdrawn_at`` closes the report: every write
    path then refuses it, the approval agent stops counting it as an open
    return, and its lines may be moved to the owner's other drafts.  The audit
    row freezes the lines and the approval status at withdrawal (the agent's
    re-file check reads it for a withdrawn REJECTED report).
    """
    report = _lock_report(session, current_user, report_id)
    require_owner(report.owner_id, user=current_user)
    expense_service.refuse_withdrawn(report)
    approval = _subject_approval(
        session,
        organization_id=report.organization_id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
    )
    if (
        report.status != ExpenseReportStatus.draft
        or approval is None
        or approval.current_status not in _RETURNED_APPROVAL
    ):
        raise ConflictError(
            "Only a report a reviewer returned to you can be withdrawn. A draft that was "
            "never submitted can be deleted instead.",
            code=REPORT_NOT_RETURNED,
        )
    _check_version(report, expected_version, "Expense report")

    entries = session.execute(
        sa.select(
            Expense.id,
            Expense.amount,
            Expense.currency,
            Expense.expense_date,
            Expense.category,
            Expense.vendor,
            Expense.description,
        )
        .where(Expense.report_id == report.id, Expense.organization_id == report.organization_id)
        .order_by(Expense.created_at, Expense.id)
    ).all()
    before = _report_state(report)
    guarded_version_update(
        session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=report.version,
        values={
            "withdrawn_at": _now(),
            "withdrawn_by_id": current_user.id,
            "withdrawal_reason": reason,
            "updated_by": current_user.id,
        },
    )
    session.refresh(report)
    record_audit(
        session,
        organization_id=report.organization_id,
        action="expense_report.withdrawn",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state=before,
        after_state={
            **_report_state(report),
            "approval_id": str(approval.id),
            "approval_status": approval.current_status.value,
            "reason": reason,
            "entries": [
                {
                    "entry_id": str(eid),
                    "amount": str(amount),
                    "currency": currency,
                    "expense_date": day.isoformat() if day is not None else None,
                    # Phase 10: the re-file check's narrow match for re-typed lines.
                    "category": category.value,
                    "vendor": vendor,
                    "description": description,
                }
                for eid, amount, currency, day, category, vendor, description in entries
                if amount is not None
            ],
        },
    )
    session.commit()
    return expense_service.get_report(session, current_user, report.id)


def withdraw_trip(
    session: Session,
    current_user: User,
    trip_id: uuid.UUID,
    *,
    reason: str | None,
) -> TripResponse:
    """Close a RETURNED trip (draft, approval changes_requested) → cancelled; audit; commit.

    The ordinary cancel (PATCH status=cancelled) refuses a returned trip
    (SUBJECT_UNDER_REVIEW) because it would silently strand the return; this is
    the explicit, audited owner action instead.  404 unreadable, 403 not the
    traveler, 409 ``TRIP_WITHDRAWN`` / ``TRIP_NOT_RETURNED``.  The approval and
    its history stay; ``withdrawn_at`` marks the cancellation as a withdrawal.
    """
    trip = _lock_trip(session, current_user, trip_id)
    require_owner(trip.traveler_id, user=current_user)
    if trip.withdrawn_at is not None:
        raise ConflictError("This trip has already been withdrawn.", code=TRIP_WITHDRAWN)
    approval = _subject_approval(
        session,
        organization_id=trip.organization_id,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
    )
    if (
        trip.status != TripStatus.draft
        or approval is None
        or approval.current_status not in _RETURNED_APPROVAL
    ):
        raise ConflictError(
            "Only a trip a reviewer returned to you can be withdrawn.", code=TRIP_NOT_RETURNED
        )
    before = _trip_state(trip)
    guarded_version_update(
        session,
        Trip,
        entity_id=trip.id,
        expected_version=trip.version,
        values={
            "status": TripStatus.cancelled,
            "withdrawn_at": _now(),
            "withdrawn_by_id": current_user.id,
            "withdrawal_reason": reason,
            "updated_by": current_user.id,
        },
    )
    session.refresh(trip)
    record_audit(
        session,
        organization_id=trip.organization_id,
        action="trip.withdrawn",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=current_user.id,
        before_state=before,
        after_state={
            **_trip_state(trip),
            "approval_id": str(approval.id),
            "approval_status": approval.current_status.value,
            "reason": reason,
        },
    )
    session.commit()
    return travel_service.get_trip(session, current_user, trip.id)


# ---------------------------------------------------------------------------
# Gap 8 — trip completion
# ---------------------------------------------------------------------------


def complete_trip(session: Session, current_user: User, trip_id: uuid.UUID) -> TripResponse:
    """Mark an APPROVED trip that has ended as completed; audit; commit.

    The traveler, or a finance admin (org-wide read scope), may do it; anyone
    else who can read the trip (a manager) → 403.  409 ``TRIP_NOT_APPROVED``
    (completed is terminal: a second call is refused, no audit) or
    ``TRIP_NOT_ENDED`` (end date after today, UTC).
    """
    trip = _lock_trip(session, current_user, trip_id)
    if trip.traveler_id != current_user.id and current_user.role != RoleKey.finance_admin:
        raise ForbiddenError("Only the traveler or finance can mark this trip completed.")
    if trip.status != TripStatus.approved:
        raise ConflictError(
            f"Only an approved trip can be marked completed (this one is "
            f"'{trip.status.value}').",
            code=TRIP_NOT_APPROVED,
        )
    travel_service.refuse_unfinished_trip(trip)
    before = _trip_state(trip)
    guarded_version_update(
        session,
        Trip,
        entity_id=trip.id,
        expected_version=trip.version,
        values={"status": TripStatus.completed, "updated_by": current_user.id},
    )
    session.refresh(trip)
    record_audit(
        session,
        organization_id=trip.organization_id,
        action="trip.completed",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=current_user.id,
        before_state=before,
        after_state={**_trip_state(trip), "trigger": "manual"},
    )
    session.commit()
    return travel_service.get_trip(session, current_user, trip.id)
