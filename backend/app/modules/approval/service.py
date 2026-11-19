"""Approval module service — data-access helpers + decision logic."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.receipt_pipeline.retention import SNAPSHOT_RECEIPTS_KEY, report_receipt_ids
from backend.app.core.audit import record_audit
from backend.app.core.concurrency import guarded_version_update
from backend.app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.core.rbac import scope_query
from backend.app.core.validation import validate_required_reason
from backend.app.modules.approval.filters import (
    APPROVAL_EXTENDED_FILTER_FIELDS,
    apply_approval_extended_filters,
    exception_types_of,
    split_filters,
)
from backend.app.modules.approval.grade_routing import (
    approval_scope,
    ensure_approver_grade,
    grade_requirement,
    required_grade_of,
)
from backend.app.modules.approval.lifecycle import (
    ensure_independent_approver as ensure_independent_approver,  # re-export (router)
)
from backend.app.modules.approval.risk import derive_risk_level
from backend.app.modules.approval.schemas import (
    ApprovalDetailResponse,
    ApprovalListItem,
    ApprovalStepResponse,
    ExpenseReportContext,
    TripContext,
)
from backend.app.modules.expense.schemas import ExpenseReportResponse
from backend.app.modules.expense.service import (
    apply_workflow_transition,
    entries_with_receipt_ids,
)
from backend.app.modules.travel.schemas import SegmentResponse, TripResponse
from backend.app.modules.travel.service import (
    apply_workflow_status as apply_trip_workflow_status,
)
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment

# Machine code for "the subject exists but is not in a state an approval can be
# opened on" (C7-13).  Rendered through the standard envelope via BadRequestError.
INVALID_SUBJECT_STATE = "INVALID_SUBJECT_STATE"

# The ONE subject state on which an approval may be opened (C7-13): the owner has
# submitted it.  A draft (or an already-decided/cancelled subject) is refused.
_TRIGGERABLE_STATUS: dict[ApprovalSubjectType, str] = {
    ApprovalSubjectType.expense_report: ExpenseReportStatus.submitted.value,
    ApprovalSubjectType.trip: TripStatus.pending_approval.value,
}


def _load_subject_scoped(
    session: Session,
    *,
    user: User,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> ExpenseReport | Trip | None:
    """Load an approval subject through the caller's READ scope (or None)."""
    if subject_type == ApprovalSubjectType.expense_report:
        report_stmt = scope_query(
            sa.select(ExpenseReport).where(ExpenseReport.id == subject_id),
            user=user,
            ownership_column=ExpenseReport.owner_id,
            org_column=ExpenseReport.organization_id,
            session=session,
        )
        return session.scalars(report_stmt).first()
    if subject_type == ApprovalSubjectType.trip:
        trip_stmt = scope_query(
            sa.select(Trip).where(Trip.id == subject_id),
            user=user,
            ownership_column=Trip.traveler_id,
            org_column=Trip.organization_id,
            session=session,
        )
        return session.scalars(trip_stmt).first()
    return None


def _subject_owner_id(subject: ExpenseReport | Trip) -> uuid.UUID:
    return subject.owner_id if isinstance(subject, ExpenseReport) else subject.traveler_id


def validate_trigger_subject(
    session: Session,
    *,
    user: User,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> None:
    """Gate POST /approvals BEFORE the agent runs (C7-03, C7-13).

    Order (each check only runs when the previous passed):
      1. Subject not readable by the caller (missing / other tenant / outside a
         manager's team)            → 404 (no existence leak).
      2. Caller owns the subject     → 403 (no self-approval, any role).
      3. An approval already exists  → 409 (unchanged re-trigger semantics).
      4. Subject not submitted (report != submitted, trip != pending_approval)
                                     → 400 INVALID_SUBJECT_STATE.
    """
    subject = _load_subject_scoped(
        session, user=user, subject_type=subject_type, subject_id=subject_id
    )
    if subject is None:
        raise NotFoundError("Approval subject not found.")

    if _subject_owner_id(subject) == user.id:
        raise ForbiddenError("You cannot open an approval on your own submission.")

    existing = session.scalars(
        sa.select(Approval.id).where(
            Approval.organization_id == user.organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    ).first()
    if existing is not None:
        raise ConflictError("An approval already exists for this subject.")

    required = _TRIGGERABLE_STATUS[subject_type]
    if subject.status.value != required:
        raise BadRequestError(
            f"An approval can only be opened on a {subject_type.value} in "
            f"'{required}' status (current status: '{subject.status.value}').",
            code=INVALID_SUBJECT_STATE,
        )


def get_approval_for_user(
    session: Session,
    *,
    approval_id: uuid.UUID,
    user: User,
) -> Approval:
    """Load an approval scoped to the user's org and manager scope."""
    stmt = sa.select(Approval).where(Approval.id == approval_id)
    # Follow-up (grades): a manager also reaches an approval routed to them by grade.
    stmt = approval_scope(stmt, user=user, session=session)
    approval = session.scalars(stmt).first()
    if approval is None:
        raise NotFoundError("Approval not found.")
    return approval


# ---------------------------------------------------------------------------
# Read surface (P5-T8) — list query
# ---------------------------------------------------------------------------

# Allow-listed filter / sort fields (deny-by-default via parse_filters/parse_sort).
APPROVAL_FILTER_FIELDS: set[str] = {
    "current_status",
    "subject_type",
    "is_auto_approved",
    *APPROVAL_EXTENDED_FILTER_FIELDS,  # Product gap 6 (US-F2 AC1) — approval/filters.py
}
APPROVAL_SORT_FIELDS: set[str] = {"created_at", "updated_at", "current_status"}


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise BadRequestError(f"Invalid boolean value '{value}' (expected true/false).")


# QA round: a returned subject its owner WITHDREW keeps its approval at
# ``changes_requested`` (the approval and its history stay), but it is closed — the
# queue, its status filter and the CSV show it as "withdrawn", not as awaiting fixes.
WITHDRAWN_DISPLAY_STATUS = "withdrawn"


def _subject_withdrawn() -> sa.ColumnElement[bool]:
    """The approval's subject (report or trip, same tenant) has been withdrawn."""
    report_w = sa.exists().where(
        ExpenseReport.id == Approval.subject_id,
        ExpenseReport.organization_id == Approval.organization_id,
        ExpenseReport.withdrawn_at.isnot(None),
    )
    trip_w = sa.exists().where(
        Trip.id == Approval.subject_id,
        Trip.organization_id == Approval.organization_id,
        Trip.withdrawn_at.isnot(None),
    )
    return sa.or_(
        sa.and_(Approval.subject_type == ApprovalSubjectType.expense_report, report_w),
        sa.and_(Approval.subject_type == ApprovalSubjectType.trip, trip_w),
    )


def _withdrawn_return() -> sa.ColumnElement[bool]:
    return sa.and_(
        Approval.current_status == ApprovalStatus.changes_requested, _subject_withdrawn()
    )


def _status_predicate(value: str) -> sa.ColumnElement[bool]:
    if value == WITHDRAWN_DISPLAY_STATUS:
        return _withdrawn_return()
    try:
        status = ApprovalStatus(value)
    except ValueError as exc:
        valid = ", ".join([*(s.value for s in ApprovalStatus), WITHDRAWN_DISPLAY_STATUS])
        raise BadRequestError(f"Invalid current_status '{value}'. Valid: {valid}.") from exc
    if status == ApprovalStatus.changes_requested:
        return sa.and_(Approval.current_status == status, sa.not_(_subject_withdrawn()))
    return Approval.current_status == status


def _apply_filters(stmt: sa.Select, filters: dict[str, str]) -> sa.Select:
    for field, value in filters.items():
        if field == "current_status":
            stmt = stmt.where(_status_predicate(value))
        elif field == "subject_type":
            try:
                stmt = stmt.where(Approval.subject_type == ApprovalSubjectType(value))
            except ValueError as exc:
                valid = ", ".join(s.value for s in ApprovalSubjectType)
                raise BadRequestError(f"Invalid subject_type '{value}'. Valid: {valid}.") from exc
        elif field == "is_auto_approved":
            stmt = stmt.where(Approval.is_auto_approved == _parse_bool(value))
    return stmt


def _apply_sort(stmt: sa.Select, sort: list[tuple[str, str]]) -> sa.Select:
    for field, direction in sort:
        col = getattr(Approval, field)
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    return stmt


def _apply_default_order(stmt: sa.Select) -> sa.Select:
    """Prioritized queue order: pending → changes_requested → resolved; within
    each, higher blocking-flag count first, then oldest first (most urgent up top).
    """
    status_priority = sa.case(
        (Approval.current_status == ApprovalStatus.pending, 0),
        (_withdrawn_return(), 2),  # closed by its owner: with the resolved rows
        (Approval.current_status == ApprovalStatus.changes_requested, 1),
        else_=2,
    )
    blocking = sa.func.coalesce(
        sa.cast(Approval.risk_summary["blocking_flag_count"].astext, sa.Integer), 0
    )
    return stmt.order_by(status_priority.asc(), blocking.desc(), Approval.created_at.asc())


def _as_decimal(value: object) -> Decimal | None:
    """Coerce a (loosely-typed Mapped[object]) money column to Decimal | None."""
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _subject_summary_fields(
    approval: Approval,
    report_map: dict[uuid.UUID, ExpenseReport],
    trip_map: dict[uuid.UUID, Trip],
) -> tuple[str | None, Decimal | None, str | None]:
    """Return (title, amount, currency) for an approval's polymorphic subject."""
    if approval.subject_type == ApprovalSubjectType.expense_report:
        report = report_map.get(approval.subject_id)
        if report is not None:
            return report.title, _as_decimal(report.total_amount), report.currency
    elif approval.subject_type == ApprovalSubjectType.trip:
        trip = trip_map.get(approval.subject_id)
        if trip is not None:
            title = f"{trip.origin} → {trip.primary_destination}"
            return title, _as_decimal(trip.estimated_cost), trip.currency
    return None, None, None


def _to_list_items(session: Session, approvals: list[Approval]) -> list[ApprovalListItem]:
    """Resolve submitter names + subject summaries (batched) into list rows."""
    if not approvals:
        return []

    submitter_ids = {a.submitter_id for a in approvals}
    submitters = list(session.scalars(sa.select(User).where(User.id.in_(submitter_ids))))
    name_map: dict[uuid.UUID, str] = {u.id: u.name for u in submitters}
    department_map: dict[uuid.UUID, str | None] = {u.id: u.department for u in submitters}

    report_ids = {
        a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.expense_report
    }
    trip_ids = {a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.trip}

    report_map: dict[uuid.UUID, ExpenseReport] = {}
    if report_ids:
        report_map = {
            r.id: r
            for r in session.scalars(
                sa.select(ExpenseReport).where(ExpenseReport.id.in_(report_ids))
            )
        }

    trip_map: dict[uuid.UUID, Trip] = {}
    if trip_ids:
        trip_map = {t.id: t for t in session.scalars(sa.select(Trip).where(Trip.id.in_(trip_ids)))}

    # Auto-approve rationale (the auto_approve step reason) for auto-approved rows,
    # so the collapsed auto-approved lane shows it inline (US-M2).
    auto_ids = [a.id for a in approvals if a.is_auto_approved]
    rationale_map: dict[uuid.UUID, str] = {}
    if auto_ids:
        auto_steps = session.scalars(
            sa.select(ApprovalStep).where(
                ApprovalStep.approval_id.in_(auto_ids),
                ApprovalStep.decision == ApprovalDecision.auto_approve,
            )
        )
        rationale_map = {s.approval_id: s.reason for s in auto_steps if s.reason is not None}

    items: list[ApprovalListItem] = []
    for a in approvals:
        title, amount, currency = _subject_summary_fields(a, report_map, trip_map)
        subject = (
            report_map.get(a.subject_id)
            if a.subject_type == ApprovalSubjectType.expense_report
            else trip_map.get(a.subject_id)
        )
        withdrawn = (
            a.current_status == ApprovalStatus.changes_requested
            and subject is not None
            and subject.withdrawn_at is not None
        )
        items.append(
            ApprovalListItem(
                id=a.id,
                organization_id=a.organization_id,
                subject_type=a.subject_type.value,
                subject_id=a.subject_id,
                submitter_id=a.submitter_id,
                submitter_name=name_map.get(a.submitter_id),
                subject_title=title,
                subject_amount=amount,
                currency=currency,
                current_status=a.current_status.value,
                subject_withdrawn=withdrawn,
                display_status=WITHDRAWN_DISPLAY_STATUS if withdrawn else a.current_status.value,
                is_auto_approved=a.is_auto_approved,
                risk_level=derive_risk_level(
                    risk_summary=a.risk_summary, is_auto_approved=a.is_auto_approved
                ),
                auto_approve_rationale=rationale_map.get(a.id),
                created_at=a.created_at,
                version=a.version,
                submitter_department=department_map.get(a.submitter_id),
                required_approver_grade=required_grade_of(a),
                exception_types=exception_types_of(
                    a.risk_summary,
                    report_map[a.subject_id].pre_submit_flags
                    if a.subject_type == ApprovalSubjectType.expense_report
                    and a.subject_id in report_map
                    else None,
                ),
            )
        )
    return items


def scoped_filtered_approvals(
    session: Session, current_user: User, raw_filters: dict[str, str]
) -> sa.Select:
    """The role-scoped approvals SELECT with every allow-listed filter applied.

    Shared by the list and the finance CSV export (Product gap 8) so both apply
    exactly the same scope + filters.  Filters only AND onto scope_query.
    """
    filters = parse_filters(raw_filters, APPROVAL_FILTER_FIELDS)
    core, extended = split_filters(filters, APPROVAL_EXTENDED_FILTER_FIELDS)

    base = approval_scope(sa.select(Approval), user=current_user, session=session)
    base = _apply_filters(base, core)
    return apply_approval_extended_filters(
        base, extended, organization_id=current_user.organization_id
    )


def list_approvals(
    session: Session,
    current_user: User,
    pagination: PaginationParams,
    raw_filters: dict[str, str],
    raw_sort: str | None,
) -> Page[ApprovalListItem]:
    """Return a paginated, role-scoped list of approvals visible to *current_user*.

    Scope (scope_query): manager → own + direct reports; finance_admin → org-wide.
    Default order is prioritized (pending first, highest blocking-flag first, oldest first).
    """
    sort = parse_sort(raw_sort, APPROVAL_SORT_FIELDS)
    base = scoped_filtered_approvals(session, current_user, raw_filters)

    total: int = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0

    base = _apply_sort(base, sort) if sort else _apply_default_order(base)
    base = base.offset(pagination.offset).limit(pagination.limit)
    approvals = list(session.scalars(base).all())

    return Page.create(
        items=_to_list_items(session, approvals),
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


# ---------------------------------------------------------------------------
# Read surface (P5-T8) — detail query
# ---------------------------------------------------------------------------


def _load_subject_context(
    session: Session, approval: Approval
) -> tuple[ExpenseReportContext | None, TripContext | None]:
    """Load the approval's polymorphic subject + children, tenant-guarded.

    The subject_id has no DB FK (app-layer polymorphism), so the load is filtered
    by organization_id to stay tenant-safe even on corrupt references.
    """
    if approval.subject_type == ApprovalSubjectType.expense_report:
        report = session.scalars(
            sa.select(ExpenseReport).where(
                ExpenseReport.id == approval.subject_id,
                ExpenseReport.organization_id == approval.organization_id,
            )
        ).first()
        if report is None:
            return None, None
        entries = session.scalars(
            sa.select(Expense).where(Expense.report_id == report.id).order_by(Expense.created_at)
        ).all()
        return (
            ExpenseReportContext(
                report=ExpenseReportResponse.model_validate(report),
                entries=entries_with_receipt_ids(session, entries),
            ),
            None,
        )

    if approval.subject_type == ApprovalSubjectType.trip:
        trip = session.scalars(
            sa.select(Trip).where(
                Trip.id == approval.subject_id,
                Trip.organization_id == approval.organization_id,
            )
        ).first()
        if trip is None:
            return None, None
        segments = session.scalars(
            sa.select(TripSegment)
            .where(TripSegment.trip_id == trip.id)
            .order_by(TripSegment.start_datetime)
        ).all()
        return (
            None,
            TripContext(
                trip=TripResponse.model_validate(trip),
                segments=[SegmentResponse.model_validate(s) for s in segments],
            ),
        )

    return None, None


def get_approval_detail(
    session: Session, current_user: User, approval_id: uuid.UUID
) -> ApprovalDetailResponse:
    """Return the full, role-scoped approval detail, or 404.

    Out-of-scope and nonexistent ids both return 404 (no existence leak) via
    get_approval_for_user. Includes risk_summary, decision history (steps), and
    the subject context (expense_report + entries OR trip + segments).
    """
    approval = get_approval_for_user(session, approval_id=approval_id, user=current_user)

    submitter_name = session.scalar(sa.select(User.name).where(User.id == approval.submitter_id))

    steps = list(
        session.scalars(
            sa.select(ApprovalStep)
            .where(ApprovalStep.approval_id == approval.id)
            .order_by(ApprovalStep.step_order)
        )
    )
    approver_ids = {s.approver_id for s in steps if s.approver_id is not None}
    approver_names: dict[uuid.UUID, str] = {
        u.id: u.name for u in session.scalars(sa.select(User).where(User.id.in_(approver_ids)))
    }
    step_responses = [
        ApprovalStepResponse(
            id=s.id,
            approver_id=s.approver_id,
            approver_name=approver_names.get(s.approver_id) if s.approver_id else None,
            step_order=s.step_order,
            decision=s.decision.value,
            reason=s.reason,
            decided_at=s.decided_at,
            created_at=s.created_at,
        )
        for s in steps
    ]

    expense_report_ctx, trip_ctx = _load_subject_context(session, approval)
    subject_ctx = (
        expense_report_ctx.report if expense_report_ctx else trip_ctx.trip if trip_ctx else None
    )
    withdrawn = (
        approval.current_status == ApprovalStatus.changes_requested
        and subject_ctx is not None
        and subject_ctx.withdrawn_at is not None
    )

    return ApprovalDetailResponse(
        id=approval.id,
        organization_id=approval.organization_id,
        subject_type=approval.subject_type.value,
        subject_id=approval.subject_id,
        submitter_id=approval.submitter_id,
        submitter_name=submitter_name,
        current_status=approval.current_status.value,
        subject_withdrawn=withdrawn,
        display_status=WITHDRAWN_DISPLAY_STATUS if withdrawn else approval.current_status.value,
        is_auto_approved=approval.is_auto_approved,
        risk_summary=approval.risk_summary,
        risk_level=derive_risk_level(
            risk_summary=approval.risk_summary, is_auto_approved=approval.is_auto_approved
        ),
        version=approval.version,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
        steps=step_responses,
        expense_report=expense_report_ctx,
        trip=trip_ctx,
        grade_requirement=grade_requirement(session, approval, current_user),
    )


# ---------------------------------------------------------------------------
# Decision (P4-T13 + P5-T8) — human decide with concurrency + reverse
# ---------------------------------------------------------------------------

_DECISION_MAP: dict[str, tuple[ApprovalStatus, ApprovalDecision]] = {
    "approve": (ApprovalStatus.approved, ApprovalDecision.approve),
    "reject": (ApprovalStatus.rejected, ApprovalDecision.reject),
    "request_changes": (ApprovalStatus.changes_requested, ApprovalDecision.request_changes),
}

# Statuses on which a human decision is allowed: pending (first decision) OR
# auto_approved (US-M2 AC2 reverse-within-policy — a human overrides the agent).
# Already-resolved approvals (approved/rejected/changes_requested) → 409.
_DECIDABLE_STATUSES = frozenset({ApprovalStatus.pending, ApprovalStatus.auto_approved})


def _subject_owner_matches(session: Session, approval: Approval, user_id: uuid.UUID) -> bool:
    """True iff the approval's subject (tenant-guarded) is owned by *user_id*."""
    if approval.subject_type == ApprovalSubjectType.expense_report:
        owner = session.scalar(
            sa.select(ExpenseReport.owner_id).where(
                ExpenseReport.id == approval.subject_id,
                ExpenseReport.organization_id == approval.organization_id,
            )
        )
    elif approval.subject_type == ApprovalSubjectType.trip:
        owner = session.scalar(
            sa.select(Trip.traveler_id).where(
                Trip.id == approval.subject_id,
                Trip.organization_id == approval.organization_id,
            )
        )
    else:
        return False
    return owner is not None and owner == user_id


# Trip status the approval decision writes back (C7-02 review: approve/reject
# are workflow outcomes, so the workflow — not the traveler — must set them).
# request_changes returns the trip to its traveler as a draft (see below).
_TRIP_STATUS_ON_DECISION: dict[str, TripStatus] = {
    "approve": TripStatus.approved,
    "reject": TripStatus.rejected,
    # Returned to the traveler (C7R): the trip goes back to draft so it can be
    # edited; resubmitting it (draft -> pending_approval) reopens this approval
    # with a fresh risk assessment (approval.lifecycle.reopen_on_resubmission).
    "request_changes": TripStatus.draft,
}

# Expense-report status the approval decision writes back (C7R / P12), applied
# through the expense service's workflow state machine.  request_changes returns
# the report to its owner as an editable draft; resubmitting it re-queues this
# same approval (approval.lifecycle.reopen_on_resubmission).
_REPORT_STATUS_ON_DECISION: dict[str, ExpenseReportStatus] = {
    "approve": ExpenseReportStatus.approved,
    "reject": ExpenseReportStatus.rejected,
    "request_changes": ExpenseReportStatus.draft,
}

# The subject status an AUTO-approval moves it to — and so the one extra status
# on which a human may still reverse that auto-approval.
_AUTO_APPROVED_STATUS: dict[ApprovalSubjectType, str] = {
    ApprovalSubjectType.expense_report: ExpenseReportStatus.approved.value,
    ApprovalSubjectType.trip: TripStatus.approved.value,
}


def _load_subject_for_update(session: Session, approval: Approval) -> ExpenseReport | Trip | None:
    """Tenant-guarded, row-locked load of an approval's subject (None when absent)."""
    if approval.subject_type == ApprovalSubjectType.expense_report:
        return session.scalars(
            sa.select(ExpenseReport)
            .where(
                ExpenseReport.id == approval.subject_id,
                ExpenseReport.organization_id == approval.organization_id,
            )
            .with_for_update()
        ).first()
    if approval.subject_type == ApprovalSubjectType.trip:
        return session.scalars(
            sa.select(Trip)
            .where(Trip.id == approval.subject_id, Trip.organization_id == approval.organization_id)
            .with_for_update()
        ).first()
    return None


def _assert_subject_decidable(approval: Approval, subject: ExpenseReport | Trip | None) -> None:
    """Refuse a decision once the subject has left its submitted state (C7-13).

    The subject must still be awaiting the decision: report ``submitted`` / trip
    ``pending_approval``.  A subject that was AUTO-approved (and so moved to
    ``approved``) may still be reversed while it is ``approved``.  A cancelled,
    completed, reimbursed or otherwise moved-on subject → 400
    INVALID_SUBJECT_STATE.  A dangling approval (subject row absent) has nothing
    to act on and is left to the other guards.
    """
    if subject is None:
        return
    allowed = {_TRIGGERABLE_STATUS[approval.subject_type]}
    if approval.current_status == ApprovalStatus.auto_approved:
        allowed.add(_AUTO_APPROVED_STATUS[approval.subject_type])
    if subject.status.value not in allowed:
        raise BadRequestError(
            f"This {approval.subject_type.value} is no longer awaiting a decision "
            f"(current status: '{subject.status.value}').",
            code=INVALID_SUBJECT_STATE,
        )


def _apply_decision_to_subject(
    session: Session,
    subject: ExpenseReport | Trip | None,
    decision: str,
    *,
    actor_id: uuid.UUID | None,
) -> None:
    """Write the decision outcome onto the subject (trip C7-02, report C7R / P12)."""
    if isinstance(subject, Trip):
        apply_trip_workflow_status(
            session, subject, _TRIP_STATUS_ON_DECISION[decision], actor_id=actor_id
        )
    elif isinstance(subject, ExpenseReport):
        apply_workflow_transition(
            session, subject, _REPORT_STATUS_ON_DECISION[decision], actor_id=actor_id
        )


def apply_auto_approval_to_subject(session: Session, approval: Approval) -> None:
    """After POST /approvals: an AUTO-approved subject moves to ``approved``.

    Trip pending_approval → approved; expense report submitted → approved
    (C7R / P12).  Flush-only (the router owns the commit).  Actor is None — the
    agent decided.  A subject no longer in its submitted state is left alone.
    """
    if approval.current_status != ApprovalStatus.auto_approved:
        return
    subject = _load_subject_for_update(session, approval)
    if subject is None or subject.status.value != _TRIGGERABLE_STATUS[approval.subject_type]:
        return
    _apply_decision_to_subject(session, subject, "approve", actor_id=None)


def _subject_snapshot(
    session: Session, approval: Approval, subject: object
) -> dict[str, object] | None:
    """Freeze the returned subject's claim content on the human return step (C7R SoD).

    Report: each entry's id / amount / currency / date / category / vendor /
    description, plus the receipt ids backing it.
    Trip: destination + dates.  The
    Approval Agent's re-file check matches later submissions against this, so editing
    the returned subject in place cannot launder its original content.
    """
    if approval.subject_type == ApprovalSubjectType.expense_report:
        rows = session.execute(
            sa.select(
                Expense.amount,
                Expense.currency,
                Expense.expense_date,
                Expense.category,
                Expense.vendor,
                Expense.description,
                Expense.id,
            ).where(
                Expense.report_id == approval.subject_id,
                Expense.organization_id == approval.organization_id,
            )
        ).all()
        return {
            "entries": [
                {
                    # Known-limits integration: which lines the reviewer saw (a
                    # deleted line only widens the re-file match if it is here).
                    "entry_id": str(entry_id),
                    "amount": str(amount),
                    "currency": currency,
                    "expense_date": day.isoformat() if day is not None else None,
                    # Phase 10: what the re-file check's narrow match compares.
                    "category": category.value,
                    "vendor": vendor,
                    "description": description,
                }
                for amount, currency, day, category, vendor, description, entry_id in rows
                if amount is not None
            ],
            # UAT2: the receipts the reviewer saw — a receipt attached after the
            # return is not yet evidence (retention.unreviewed_receipt_ids).
            SNAPSHOT_RECEIPTS_KEY: [
                str(rid)
                for rid in report_receipt_ids(
                    session,
                    organization_id=approval.organization_id,
                    report_id=approval.subject_id,
                )
            ],
        }
    if isinstance(subject, Trip):
        return {
            "destination": subject.primary_destination,
            "start_date": subject.start_date.isoformat() if subject.start_date else None,  # type: ignore[attr-defined]
            "end_date": subject.end_date.isoformat() if subject.end_date else None,  # type: ignore[attr-defined]
        }
    return None


def decide_approval(
    session: Session,
    current_user: User,
    approval_id: uuid.UUID,
    *,
    decision: str,
    reason: str | None,
    expected_version: int | None = None,
) -> ApprovalDetailResponse:
    """Record a human decision on an approval; commit + audit; return fresh detail.

    Scope-checked load (404 out-of-scope). Deciding on one's own submission → 403
    for every role (C7-03). Decidable on pending OR auto_approved
    (reverse-within-policy). reason required for reject/request_changes. Optimistic
    concurrency via If-Match version → 409 ConflictError on a stale version. The
    agent NEVER auto-rejects — reject is always a recorded human step.
    """
    approval = get_approval_for_user(session, approval_id=approval_id, user=current_user)

    # Segregation of duties (C7-03): nobody — finance_admin included — decides an
    # approval on their own submission.  Checked against the recorded submitter
    # AND the live subject owner (belt-and-braces against a divergent row).
    if approval.submitter_id == current_user.id or _subject_owner_matches(
        session, approval, current_user.id
    ):
        raise ForbiddenError("You cannot decide an approval on your own submission.")

    # Follow-up (grades): a flag naming an approver grade (SVP / VP) can only be
    # decided by a user whose grade of record meets it (403 INSUFFICIENT_APPROVER_GRADE).
    ensure_approver_grade(approval, current_user)

    if approval.current_status not in _DECIDABLE_STATUSES:
        raise ConflictError(
            f"Approval in status '{approval.current_status.value}' cannot be decided."
        )

    if decision not in _DECISION_MAP:
        raise BadRequestError("Invalid decision.")

    # C7-13 (decide side): the subject must still be awaiting this decision — e.g.
    # a trip the traveler cancelled after the trigger cannot be "approved".  The
    # row lock serialises against a concurrent traveler PATCH.
    subject = _load_subject_for_update(session, approval)
    _assert_subject_decidable(approval, subject)

    if decision in ("reject", "request_changes"):
        validate_required_reason(decision, reason)

    new_status, approval_decision = _DECISION_MAP[decision]
    before_status = approval.current_status.value

    # Append the decision step (append-only history; reverse → step_order 2+).
    step = ApprovalStep(
        approval_id=approval.id,
        approver_id=current_user.id,
        step_order=len(approval.steps) + 1,
        decision=approval_decision,
        reason=reason.strip() if reason else reason,
        decided_at=datetime.now(timezone.utc),
        subject_snapshot=(
            _subject_snapshot(session, approval, subject)
            if approval_decision in (ApprovalDecision.reject, ApprovalDecision.request_changes)
            else None
        ),
    )
    session.add(step)
    session.flush()

    # Optimistic-lock the status transition (CAS on version). Stale → 409.
    lock_version = expected_version if expected_version is not None else approval.version
    guarded_version_update(
        session,
        Approval,
        entity_id=approval.id,
        expected_version=lock_version,
        values={"current_status": new_status},
    )
    session.refresh(approval)

    # C7-02 / C7R-P12 follow-through: the workflow (not the owner) sets the
    # subject outcome — trip status, or the expense report through the expense
    # service's workflow state machine.  Same transaction as the decision.
    _apply_decision_to_subject(session, subject, decision, actor_id=current_user.id)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="approval.decided",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=current_user.id,
        before_state={"current_status": before_status},
        after_state={"current_status": new_status.value},
    )

    session.commit()
    return get_approval_detail(session, current_user, approval_id)
