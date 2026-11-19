"""Expense service — business logic for expense_reports + expenses.

Security invariants:
  1. Every query is scope_query'd before execution — tenant isolation first, then role.
  2. Entries are only reachable through a scope-resolved parent report (no direct queries).
  3. Illegal state transitions are rejected before any mutation; no audit row is written.
  4. All audit writes go through record_audit() in the same transaction as the mutation.
  5. total_amount is DERIVED from entries and recomputed atomically — never accepted from clients.
  6. Entry edits are only allowed when the parent report is in draft status.
  7. Mutations are OWNER-ONLY for every role (C7-01): read scope (scope_query)
     locates the record (404 when unreadable); require_owner then rejects a
     reader who is not the owner (manager, finance_admin) with 403.
  8. Decision outcomes (approved / rejected / returned-to-draft) are applied
     ONLY by the approval workflow through apply_workflow_transition (C7R/P12),
     validated against WORKFLOW_TRANSITIONS — never through the owner's PATCH.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.presubmit import (
    ESCALATING_FLAG_TYPES,
    POSSIBLE_DUPLICATE,
    PreSubmitFlag,
    build_flags_snapshot,
    draft_entries_for_expenses,
    receipt_total_covers,
    run_presubmit,
)
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.evidence import (
    backing_expense_ids,
    machine_reading,
    machine_total,
    set_shared_expense_ids,
    shared_expense_ids,
    shared_with_any,
)
from ai_agents.receipt_pipeline.inbox import (
    RECEIPT_INBOX_TITLE,
    editable_inbox_expense_ids,
    release_inbox_draft,
)
from ai_agents.receipt_pipeline.retention import (
    ATTACH_RECEIPT_ID_KEY,
    ATTACH_SHARED_KEY,
    RECEIPT_ATTACHED_ACTION,
    RECEIPT_RETAINED_ACTION,
    RECEIPT_RETAINED_MESSAGE,
    receipt_retained_for_audit,
    report_has_approval,
    retained_receipt_ids,
    unreviewed_receipt_ids,
)
from backend.app.core.audit import record_audit
from backend.app.core.concurrency import guarded_version_update
from backend.app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from backend.app.core.idempotency import idempotency_lookup, idempotency_record
from backend.app.core.money import amount_conversion
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.core.rbac import (
    narrow_to_caller,
    owner_scope_query,
    require_owner,
    scope_query,
)
from backend.app.modules.approval.filters import (
    REPORT_EXTENDED_FILTER_FIELDS,
    apply_report_extended_filters,
    split_filters,
)
from backend.app.modules.approval.lifecycle import (
    HUMAN_RETURN_DECISIONS,
    open_approval_on_submission,
    owner_approval_info,
    subjects_with_human_review_history,
)
from backend.app.modules.expense.fx import report_currency_map, report_total
from backend.app.modules.expense.policy_fields import (
    apply_entry_policy_fields,
    refuse_receipt_on_mileage,
)
from backend.app.modules.expense.process_flags import (
    apply_affidavit_change,
    read_time_process_flags,
)
from backend.app.modules.expense.schemas import (
    EntryDeleteResponse,
    EntryReceipt,
    EntryReceiptRemoval,
    ExpenseEntryResponse,
    ExpenseReportCreate,
    ExpenseReportResponse,
    ExpenseUpdate,
    LinkReceiptRequest,
    MoveEntriesRequest,
    MoveEntriesResponse,
    ReceiptDeleteResponse,
    ReceiptLinkResponse,
    ReportDetailsUpdate,
    ReportListItem,
)
from backend.app.modules.receipt.schemas import PolicyVerdictSummary
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legal state-transition map (single source of truth for PATCH)
# ---------------------------------------------------------------------------

LEGAL_TRANSITIONS: dict[ExpenseReportStatus, frozenset[ExpenseReportStatus]] = {
    ExpenseReportStatus.draft: frozenset({ExpenseReportStatus.submitted}),
    ExpenseReportStatus.submitted: frozenset(
        {ExpenseReportStatus.approved, ExpenseReportStatus.rejected}
    ),
    ExpenseReportStatus.approved: frozenset({ExpenseReportStatus.reimbursed}),
    ExpenseReportStatus.rejected: frozenset({ExpenseReportStatus.draft}),
    ExpenseReportStatus.reimbursed: frozenset(),
}

# Edges driven ONLY by the approval workflow (PATCH / POST /approvals), applied
# through apply_workflow_transition — never by the owner's PATCH (C7R / P12):
#   submitted → approved | rejected   human decision, or agent auto-approve
#   submitted → draft                 changes requested: returned to the owner
#   approved  → rejected | draft      human reversal of an AUTO-approval
# There is no separate "returned" status: a returned report is an editable
# draft again; its approval (status changes_requested) carries the history and
# is re-queued as pending when the owner resubmits (approval.lifecycle).
WORKFLOW_TRANSITIONS: dict[ExpenseReportStatus, frozenset[ExpenseReportStatus]] = {
    ExpenseReportStatus.draft: frozenset(),
    ExpenseReportStatus.submitted: frozenset(
        {ExpenseReportStatus.approved, ExpenseReportStatus.rejected, ExpenseReportStatus.draft}
    ),
    ExpenseReportStatus.approved: frozenset(
        {ExpenseReportStatus.rejected, ExpenseReportStatus.draft}
    ),
    ExpenseReportStatus.rejected: frozenset(),
    ExpenseReportStatus.reimbursed: frozenset(),
}

# Owner-PATCH targets that are decision outcomes (or a finance action).  They
# stay in LEGAL_TRANSITIONS so the state machine documents them, but the owner
# may never set them: approve/reject are made through the approval workflow by
# someone other than the owner (Dev Rule 9, fail-safe); reimburse has no
# endpoint yet.
_PHASE4_GATED_TARGETS: frozenset[ExpenseReportStatus] = frozenset(
    {
        ExpenseReportStatus.approved,
        ExpenseReportStatus.rejected,
        ExpenseReportStatus.reimbursed,
    }
)

# ---------------------------------------------------------------------------
# Allow-listed filter / sort fields (deny-by-default)
# ---------------------------------------------------------------------------

REPORT_FILTER_FIELDS: set[str] = {
    "status",
    "owner_id",
    "trip_id",
    "currency",
    "withdrawn",  # Product gap 4 — withdrawn reports hidden by default
    *REPORT_EXTENDED_FILTER_FIELDS,  # Product gap 6 (US-F2 AC1) — approval/filters.py
}
REPORT_SORT_FIELDS: set[str] = {
    "created_at",
    "updated_at",
    "status",
    "total_amount",
    "submitted_at",
    "title",
}

_REPORT_FILTER_ENUM_FIELDS = {"status"}
_REPORT_FILTER_UUID_FIELDS = {"owner_id", "trip_id"}


def _apply_filters(stmt: sa.Select, filters: dict[str, str]) -> sa.Select:
    # Product gap 4: withdrawn reports are hidden unless asked for —
    # ``withdrawn=true`` lists only them, ``withdrawn=any`` lists everything.
    withdrawn = filters.get("withdrawn", "false").strip().lower()
    if withdrawn == "true":
        stmt = stmt.where(ExpenseReport.withdrawn_at.is_not(None))
    elif withdrawn == "false":
        stmt = stmt.where(ExpenseReport.withdrawn_at.is_(None))
    elif withdrawn != "any":
        raise BadRequestError("Invalid withdrawn filter. Valid: true, false, any.")
    for field, value in filters.items():
        if field == "withdrawn":
            continue
        if field == "status":
            try:
                stmt = stmt.where(ExpenseReport.status == ExpenseReportStatus(value))
            except ValueError as exc:
                valid = ", ".join(s.value for s in ExpenseReportStatus)
                raise BadRequestError(f"Invalid status '{value}'. Valid: {valid}.") from exc
        elif field in _REPORT_FILTER_UUID_FIELDS:
            try:
                col = getattr(ExpenseReport, field)
                stmt = stmt.where(col == uuid.UUID(value))
            except ValueError as exc:
                raise BadRequestError(f"Invalid UUID for field '{field}': '{value}'.") from exc
        else:
            col = getattr(ExpenseReport, field)
            stmt = stmt.where(col == value)
    return stmt


def _apply_sort(stmt: sa.Select, sort: list[tuple[str, str]]) -> sa.Select:
    for field, direction in sort:
        col = getattr(ExpenseReport, field)
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    return stmt


def _report_snapshot(report: ExpenseReport) -> dict:
    """JSON-safe snapshot of the report for audit before/after state."""
    return {
        "status": report.status.value,
        "version": report.version,
        "owner_id": str(report.owner_id),
        "title": report.title,
        "total_amount": str(report.total_amount),
        "submitted_at": str(report.submitted_at) if report.submitted_at else None,
    }


def _entry_snapshot(entry: Expense) -> dict:
    """JSON-safe snapshot of an expense entry for audit before/after state."""
    return {
        "report_id": str(entry.report_id),
        "category": entry.category.value,
        "amount": str(entry.amount),
        "currency": entry.currency,
        "expense_date": str(entry.expense_date),
        "status": entry.status.value,
        "is_itemized": entry.is_itemized,
        "vendor": entry.vendor,
        "description": entry.description,
        "justification": entry.justification,
        "receipt_affidavit_at": (
            str(entry.receipt_affidavit_at) if entry.receipt_affidavit_at else None
        ),
        # Product gap 2: attendees / ENT-04 disclosure / mileage trip.
        "attendee_count": entry.attendee_count,
        "disclosure": entry.disclosure,
        "mileage": entry.mileage,
    }


def _compute_total(session: Session, report_id: uuid.UUID) -> Decimal:
    """Sum all entry amounts for the given report (returns 0 if no entries).

    Product gap 8: the total is in the REPORT's currency — entries in that
    currency are summed as before (byte-identical); an entry in another one is
    converted (``report_total``).
    """
    result = session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(Expense.amount), 0)).where(
            Expense.report_id == report_id
        )
    )
    total = Decimal(str(result)) if result is not None else Decimal("0")
    return report_total(session, report_id, raw_total=total)


_REFRESH_FAILED_REF = "presubmit_refresh"


def refresh_presubmit_flags(session: Session, report: ExpenseReport) -> None:
    """Re-run the pre-submit policy check over *report*'s persisted entries (flush-only).

    C7R UAT: an entry edit, a justification or a move changes what the report
    claims, so the stored ``pre_submit_flags`` snapshot (what the submit gate and
    the readiness panel read) is rebuilt from the SAME evaluation approval time
    uses (``run_presubmit`` over ``draft_entries_for_expenses``, evidence owned
    by the report owner).  Each entry's ``flagged`` status follows its own
    blocking flags (by entry id).

    Runs strict inside a SAVEPOINT: if the policy engine cannot evaluate every
    entry, the previous flags are kept and an advisory ``not_evaluated`` note is
    added — the user may still submit (with a confirmation) and approval-time
    evaluation, which escalates anything it cannot evaluate, stays the gate.
    """
    entries = list(
        session.scalars(
            sa.select(Expense)
            .where(
                Expense.report_id == report.id,
                Expense.organization_id == report.organization_id,
            )
            .order_by(Expense.created_at, Expense.id)
        ).all()
    )
    context = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=report.owner_id,
    )
    try:
        with session.begin_nested():
            drafts = draft_entries_for_expenses(
                entries, session=session, organization_id=report.organization_id
            )
            draft_report = DraftReport(
                entries=drafts,
                total_amount=float(str(report.total_amount or 0)),
                currency=report.currency,
                trip_id=str(report.trip_id) if report.trip_id else None,
                title=report.title,
            )
            flags, _ = run_presubmit(
                draft_report,
                context=context,
                session=session,
                owner_id=report.owner_id,
                strict=True,
            )
    except Exception as exc:  # noqa: BLE001 — degrade to "not re-evaluated", never 500
        logger.warning(
            "expense: pre-submit refresh failed for report=%s (%s)", report.id, type(exc).__name__
        )
        existing = report.pre_submit_flags if isinstance(report.pre_submit_flags, dict) else {}
        kept = [
            f
            for f in (existing.get("flags") or [])
            if isinstance(f, dict) and f.get("entry_ref") != _REFRESH_FAILED_REF
        ]
        kept.append(
            PreSubmitFlag(
                entry_ref=_REFRESH_FAILED_REF,
                flag_type="not_evaluated",
                rule_id=None,
                rule_type=None,
                source_clause_ref=None,
                reason=(
                    "The pre-submit check could not be refreshed after your latest "
                    "change. Your approver's policy check re-evaluates every entry."
                ),
                requires_approver=None,
                observed={},
                severity="advisory",
            ).to_dict()
        )
        report.pre_submit_flags = {
            "flags": kept,
            "evaluated_entry_count": len(entries),
            "blocking_count": sum(1 for f in kept if f.get("severity") == "blocking"),
            "advisory_count": sum(1 for f in kept if f.get("severity") == "advisory"),
        }
        session.flush()
        return

    blocking_entry_ids = {f.entry_id for f in flags if f.severity == "blocking" and f.entry_id}
    for entry in entries:
        if str(entry.id) in blocking_entry_ids:
            entry.status = ExpenseStatus.flagged
        elif entry.status == ExpenseStatus.flagged:
            entry.status = ExpenseStatus.draft
    report.pre_submit_flags = build_flags_snapshot(flags, len(entries))
    session.flush()


def _load_report_for_write(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    *,
    allow_withdrawn: bool = False,
) -> ExpenseReport:
    """Load *report_id* ``FOR UPDATE`` for an owner-only DRAFT mutation.

    Unreadable / nonexistent → 404; readable but not owned → 403 (C7-01);
    owned but not draft → 409; withdrawn (product gap 4) → 409
    ``REPORT_WITHDRAWN`` unless *allow_withdrawn* (the move SOURCE only: a
    withdrawn report's lines are released to the owner's other drafts).  The row
    lock makes a concurrent submit wait, so a submitted report never holds a
    change its submit did not see.
    """
    stmt = sa.select(ExpenseReport).where(ExpenseReport.id == report_id)
    stmt = scope_query(
        stmt,
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
    require_owner(report.owner_id, user=current_user)
    if report.status != ExpenseReportStatus.draft:
        raise ConflictError("Only a draft expense report can be changed.")
    if not allow_withdrawn:
        refuse_withdrawn(report)
    return report


REPORT_WITHDRAWN = "REPORT_WITHDRAWN"
_WITHDRAWN_MESSAGE = (
    "This report was withdrawn and is read-only. Move its lines to another draft "
    "report if you still want to claim them."
)


def refuse_withdrawn(report: ExpenseReport) -> None:
    """409 ``REPORT_WITHDRAWN`` when the owner has withdrawn *report* (product gap 4).

    A withdrawn report keeps status ``draft`` (0016: ``withdrawn_at`` closes it),
    so every draft-only write checks this too — fail closed.
    """
    if report.withdrawn_at is not None:
        raise ConflictError(_WITHDRAWN_MESSAGE, code=REPORT_WITHDRAWN)


def _check_version(report: ExpenseReport, expected_version: int | None) -> None:
    """Fail fast (no mutation, no audit) on a stale If-Match version."""
    if expected_version is not None and expected_version != report.version:
        raise ConflictError("Expense report was modified by another request.")


def _bump_report_version(
    session: Session,
    report: ExpenseReport,
    *,
    expected_version: int | None,
    actor_id: uuid.UUID,
) -> None:
    """CAS-bump *report*'s version (If-Match semantics: stale → 409) and reload it.

    Flushes first: sessions run with autoflush off, and the reload below would
    otherwise discard pending ORM changes to the report.
    """
    session.flush()
    guarded_version_update(
        session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=expected_version if expected_version is not None else report.version,
        values={"updated_by": actor_id},
    )
    session.refresh(report)


def _approval_info(
    session: Session, reports: Sequence[ExpenseReport], *, with_reason: bool = False
) -> dict[uuid.UUID, tuple[str, str | None]]:
    """Map report id → (approval status, returned reason) (C7R / P12)."""
    return owner_approval_info(
        session, ApprovalSubjectType.expense_report, reports, with_reason=with_reason
    )


def _report_response(session: Session, report: ExpenseReport) -> ExpenseReportResponse:
    """Serialize a report with its owner-visible approval status + reason.

    Also carries the owner's display name (UAT2) so a reader who is not the
    owner (a manager via the team read scope) can be told whose report it is.
    """
    response = ExpenseReportResponse.model_validate(report)
    response.owner_name = session.scalar(
        sa.select(User.name).where(
            User.id == report.owner_id, User.organization_id == report.organization_id
        )
    )
    info = _approval_info(session, [report], with_reason=True).get(report.id)
    if info is not None:
        response.approval_status, response.approval_reason = info
    # Product gap 2 stage 2: a draft's EXP-01 / CARD-03 flags are as of today, not as
    # of its last edit (a stale draft shows it is stale without being touched).
    response.pre_submit_flags = read_time_process_flags(session, report)
    return response


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------


def create_report(
    session: Session,
    current_user: User,
    payload: ExpenseReportCreate,
    *,
    idempotency_key: str | None = None,
) -> ExpenseReportResponse:
    """Persist a new report (draft) + any entries, derive total_amount, audit, commit."""
    # Validate trip_id (no existence leak — 400 for nonexistent, out-of-scope AND
    # not-owned).  Linking is a write against the trip, so it uses the WRITE scope
    # (C7-01): a manager may read a report's trip but may not attach their own
    # report to it.
    if payload.trip_id is not None:
        trip_stmt = sa.select(Trip).where(Trip.id == payload.trip_id)
        trip_stmt = owner_scope_query(
            trip_stmt,
            user=current_user,
            ownership_column=Trip.traveler_id,
            org_column=Trip.organization_id,
        )
        accessible_trip = session.scalars(trip_stmt).first()
        if accessible_trip is None:
            raise BadRequestError("trip_id does not reference an accessible trip.")

    # Idempotency check — replay stored result if this key was seen before.
    if idempotency_key is not None:
        existing_id = idempotency_lookup(
            session,
            organization_id=current_user.organization_id,
            scope="expense_report.create",
            key=idempotency_key,
        )
        if existing_id is not None:
            existing_report = session.get(ExpenseReport, existing_id)
            if existing_report is not None and existing_report.owner_id != current_user.id:
                # Idempotency keys are org-scoped; a key first used by ANOTHER user
                # must never replay (= disclose) that user's report (C7-01 sweep).
                raise ConflictError("Idempotency key has already been used for another request.")
            if existing_report is None:
                raise ConflictError(
                    "Idempotency key was already consumed but the original record"
                    " is no longer available."
                )
            # ExpenseReportResponse excludes entries; if entries are added to the
            # response schema in future, this replay path must eagerly load them.
            return ExpenseReportResponse.model_validate(existing_report)

    report = ExpenseReport(
        organization_id=current_user.organization_id,
        owner_id=current_user.id,
        trip_id=payload.trip_id,
        title=payload.title,
        currency=payload.currency,
        status=ExpenseReportStatus.draft,
        total_amount=Decimal("0"),
        submitted_at=None,
        pre_submit_flags=None,
        created_by=current_user.id,
        updated_by=current_user.id,
    )
    session.add(report)
    session.flush()  # populate report.id for entries + audit

    for entry_data in payload.entries:
        entry = Expense(
            organization_id=current_user.organization_id,
            report_id=report.id,
            trip_segment_id=None,
            category=entry_data.category,
            amount=entry_data.amount,
            currency=entry_data.currency,
            expense_date=entry_data.expense_date,
            vendor=entry_data.vendor,
            description=entry_data.description,
            status=ExpenseStatus.draft,
            match_source=ExpenseMatchSource.manual,
            is_itemized=entry_data.is_itemized,
            created_by=current_user.id,
            updated_by=current_user.id,
        )
        session.add(entry)

    session.flush()  # populate entry ids + make entries visible to the sum query

    # Derive total_amount from entries (never from client)
    report.total_amount = _compute_total(session, report.id)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense_report.created",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state=None,
        after_state=_report_snapshot(report),
    )

    if idempotency_key is not None:
        idempotency_record(
            session,
            organization_id=current_user.organization_id,
            scope="expense_report.create",
            key=idempotency_key,
            entity_id=report.id,
        )

    session.commit()
    return ExpenseReportResponse.model_validate(report)


def list_reports(
    session: Session,
    current_user: User,
    pagination: PaginationParams,
    raw_filters: dict[str, str],
    raw_sort: str | None,
    *,
    mine: bool = False,
) -> Page[ReportListItem]:
    """Return a paginated, scoped list of expense reports visible to *current_user*.

    ``mine=True`` (the personal "My expenses" page) narrows the role's read scope
    to the reports *current_user* owns; the default keeps the full read scope.
    """
    filters = parse_filters(raw_filters, REPORT_FILTER_FIELDS)
    filters, extended = split_filters(filters, REPORT_EXTENDED_FILTER_FIELDS)
    sort = parse_sort(raw_sort, REPORT_SORT_FIELDS)

    base = sa.select(ExpenseReport)
    base = scope_query(
        base,
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    base = narrow_to_caller(
        base, user=current_user, ownership_column=ExpenseReport.owner_id, mine=mine
    )
    base = _apply_filters(base, filters)
    base = apply_report_extended_filters(
        base, extended, organization_id=current_user.organization_id
    )

    total: int = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0

    if sort:
        base = _apply_sort(base, sort)
    else:
        base = base.order_by(ExpenseReport.created_at.desc())

    base = base.offset(pagination.offset).limit(pagination.limit)
    rows = session.scalars(base).all()
    approval_info = _approval_info(session, rows)

    items: list[ReportListItem] = []
    for r in rows:
        item = ReportListItem.model_validate(r)
        info = approval_info.get(r.id)
        item.approval_status = info[0] if info is not None else None
        items.append(item)

    return Page.create(
        items=items,
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


def get_report(session: Session, current_user: User, report_id: uuid.UUID) -> ExpenseReportResponse:
    """Return a single report visible to *current_user*, or 404.

    Out-of-scope and nonexistent ids both return 404 — no existence leak.
    """
    stmt = sa.select(ExpenseReport).where(ExpenseReport.id == report_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    report = session.scalars(stmt).first()
    if report is None:
        raise NotFoundError("Expense report not found.")
    return _report_response(session, report)


def reported_match_source(
    stored: ExpenseMatchSource | None, linked_receipt_ids: Sequence[uuid.UUID]
) -> ExpenseMatchSource | None:
    """The match source an entry REPORTS, from its current links (follow-up).

    A receipt-sourced line whose receipts are all gone (removed, or detached and
    kept for audit) is no longer auto-matched: it reports ``None`` ("Unmatched").
    The stored column (how the line was created) is never rewritten here.
    """
    if stored == ExpenseMatchSource.receipt and not linked_receipt_ids:
        return None
    return stored


def entries_with_receipt_ids(
    session: Session,
    entries: Sequence[Expense],
    *,
    report: ExpenseReport | None = None,
    viewer: User | None = None,
) -> list[ExpenseEntryResponse]:
    """Serialize *entries*, attaching each entry's linked receipts.

    Receipt -> Expense is many-to-one; ``receipt_id`` is the earliest receipt (by
    created_at) and ``receipt_ids`` all of them, so the UI can fetch the scoped
    image link via GET /receipts/{id}, which re-applies RBAC + record scoping on
    its own. The lookup is tenant-guarded by the entries' organization_id. One
    query for the whole batch.  With *report* (the entries' parent), each entry
    also carries whether its owner may delete it now (UAT2), and with *viewer*
    too, whether that viewer may remove each of its receipts (UAT2 retest B).

    UAT2 defect F: ``receipts`` lists the same receipts with their machine
    reading, direct/shared link, whether each covers the line and whether it
    arrived after the report's last return — so a reviewer sees a correct
    receipt attached after a wrong one, not just the earliest.
    """
    receipts: dict[uuid.UUID, list[uuid.UUID]] = {}
    rows_by_id: dict[uuid.UUID, Receipt] = {}
    uploader: dict[uuid.UUID, uuid.UUID | None] = {}
    if entries:
        org_ids = {e.organization_id for e in entries}
        for row in session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.expense_id.in_([e.id for e in entries]),
                Receipt.organization_id.in_(org_ids),
            )
            .order_by(Receipt.created_at, Receipt.id)
        ).all():
            rows_by_id[row.id] = row
            uploader[row.id] = row.uploaded_by
            if row.expense_id is not None:
                receipts.setdefault(row.expense_id, []).append(row.id)
        # UAT2 hardening: a receipt SHARED with an entry (itemized siblings split
        # from one folio) backs it too — listed after the entry's own receipts.
        wanted = {e.id for e in entries}
        for shared in session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.organization_id.in_(org_ids),
                shared_with_any([e.id for e in entries]),
            )
            .order_by(Receipt.created_at, Receipt.id)
        ).all():
            rows_by_id[shared.id] = shared
            uploader[shared.id] = shared.uploaded_by
            for eid in shared_expense_ids(shared):
                if eid in wanted and shared.id not in receipts.get(eid, []):
                    receipts.setdefault(eid, []).append(shared.id)

    blocked: dict[uuid.UUID, str | None] = {}
    removal: dict[uuid.UUID, EntryReceiptRemoval] = {}
    if report is not None:
        blocked = _entry_delete_blocks(session, report, entries)
        if viewer is not None:
            removal = _receipt_removals(session, report, viewer, uploader)
    after_return = _receipts_after_return(session, entries, receipts)

    report_currencies = report_currency_map(session, entries, report=report)  # product gap 8
    out: list[ExpenseEntryResponse] = []
    for entry in entries:
        response = ExpenseEntryResponse.model_validate(entry)
        ids = receipts.get(entry.id, [])
        response.receipt_id = ids[0] if ids else None
        response.receipt_ids = list(ids)
        response.match_source = reported_match_source(entry.match_source, ids)
        response.receipts = [
            _entry_receipt(entry, rows_by_id[rid], after_return.get(entry.report_id, set()))
            for rid in ids
        ]
        if report is not None:
            reason = blocked.get(entry.id)
            response.deletable = reason is None
            response.delete_blocked_reason = reason
        response.receipt_removal = [removal[rid] for rid in ids if rid in removal]
        response.amount_conversion = amount_conversion(
            entry.amount, entry.currency, report_currencies.get(entry.report_id)
        )
        out.append(response)
    return out


def _receipts_after_return(
    session: Session,
    entries: Sequence[Expense],
    receipts: dict[uuid.UUID, list[uuid.UUID]],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Report id -> its receipts no reviewer saw (attached after the last return).

    ``unreviewed_receipt_ids`` per report (one query when the report was never
    returned; fail closed — unmarked — on a legacy step it cannot judge).
    """
    by_report: dict[tuple[uuid.UUID, uuid.UUID], set[uuid.UUID]] = {}
    for e in entries:
        ids = receipts.get(e.id)
        if ids:
            by_report.setdefault((e.organization_id, e.report_id), set()).update(ids)
    return {
        report_id: unreviewed_receipt_ids(
            session, organization_id=org_id, report_id=report_id, receipt_ids=ids
        )
        for (org_id, report_id), ids in by_report.items()
    }


def _entry_receipt(entry: Expense, receipt: Receipt, unseen: set[uuid.UUID]) -> EntryReceipt:
    """One receipt as the reviewer sees it on *entry* (UAT2 defect F)."""
    reading = machine_reading(receipt)
    fields = receipt.extracted_fields if isinstance(receipt.extracted_fields, dict) else {}
    currency = fields.get("currency")
    total = reading["amount"]
    raw_date = reading["date"]
    return EntryReceipt(
        receipt_id=receipt.id,
        link="direct" if receipt.expense_id == entry.id else "shared",
        vendor=reading["vendor"],
        receipt_date=date.fromisoformat(raw_date) if raw_date else None,
        amount=total,
        currency=currency.strip().upper()
        if isinstance(currency, str) and currency.strip()
        else None,
        covers_line=None
        if total is None
        else receipt_total_covers(total, Decimal(str(entry.amount))),
        attached_after_return=receipt.id in unseen,
    )


_RECEIPT_NOT_YOURS = "Only the person who uploaded this receipt can remove it."


def _receipt_removals(
    session: Session,
    report: ExpenseReport,
    viewer: User,
    uploader: dict[uuid.UUID, uuid.UUID | None],
) -> dict[uuid.UUID, EntryReceiptRemoval]:
    """Receipt id -> whether *viewer* may DELETE it from its line of *report* now.

    The read-only twin of ``delete_receipt``'s refusals, in the same order: not the
    report owner / uploader, kept for audit (marker, or on a report with approval
    history and seen by a reviewer — ``unreviewed_receipt_ids``), report not a
    draft.  The server re-checks everything under locks on the DELETE itself.
    """
    ids = list(uploader)
    if not ids:
        return {}
    org = report.organization_id
    kept = retained_receipt_ids(session, organization_id=org, receipt_ids=ids)
    if report_has_approval(session, organization_id=org, report_id=report.id):
        unseen = unreviewed_receipt_ids(
            session, organization_id=org, report_id=report.id, receipt_ids=ids
        )
        kept |= {rid for rid in ids if rid not in unseen}
    out: dict[uuid.UUID, EntryReceiptRemoval] = {}
    for rid in ids:
        reason: str | None = None
        if report.owner_id != viewer.id or uploader[rid] != viewer.id:
            reason = _RECEIPT_NOT_YOURS
        elif rid in kept:
            reason = RECEIPT_RETAINED_MESSAGE
        elif report.withdrawn_at is not None:
            reason = "This receipt backs an entry on a withdrawn report and can't be removed."
        elif report.status != ExpenseReportStatus.draft:
            reason = (
                f"This receipt backs an entry on a {report.status.value} report and "
                "can't be removed."
            )
        out[rid] = EntryReceiptRemoval(
            receipt_id=rid,
            removable=reason is None,
            kept_for_audit=reason == RECEIPT_RETAINED_MESSAGE,
            removal_blocked_reason=reason,
        )
    return out


def get_report_entries(
    session: Session, current_user: User, report_id: uuid.UUID
) -> list[ExpenseEntryResponse]:
    """Return entries for a report visible to *current_user*, or 404.

    Entries are only reachable through a scope-resolved parent report.
    """
    stmt = sa.select(ExpenseReport).where(ExpenseReport.id == report_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    report = session.scalars(stmt).first()
    if report is None:
        raise NotFoundError("Expense report not found.")

    entries = session.scalars(
        sa.select(Expense).where(Expense.report_id == report_id).order_by(Expense.created_at)
    ).all()
    return entries_with_receipt_ids(session, entries, report=report, viewer=current_user)


def transition_report(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    target_status: ExpenseReportStatus,
    *,
    expected_version: int | None = None,
) -> ExpenseReportResponse:
    """Validate + apply a status transition, audit, commit → ExpenseReportResponse.

    Unreadable/nonexistent → 404; readable but not owned → 403 (C7-01).
    Illegal transitions → 409 ConflictError, no state change, no audit row.

    The report is read ``FOR UPDATE`` (C7R UAT): entry edits / moves bump the
    report version while holding this lock, so a submit without If-Match waits
    for them and submits the edited report; with If-Match a stale version → 409.
    """
    stmt = sa.select(ExpenseReport).where(ExpenseReport.id == report_id)
    stmt = scope_query(
        stmt,
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

    # Write scope (C7-01): only the owner may submit/reopen their report.
    require_owner(report.owner_id, user=current_user)
    # Product gap 4: a withdrawn report is closed — never resubmitted.
    refuse_withdrawn(report)

    # Validate transition BEFORE any mutation (fail fast, no state change on illegal path)
    if target_status not in LEGAL_TRANSITIONS[report.status]:
        raise ConflictError(
            f"Cannot transition expense report from '{report.status.value}'"
            f" to '{target_status.value}'."
        )

    # Phase 4 authorization guard — fail safe (Dev Rule 9).
    # approve/reject/reimburse require per-role checks (manager ≠ self,
    # APPROVAL_DECIDE_REPORTS, finance-only reimburse) implemented in the
    # Phase 4 Approval module.  Blocking here prevents self-approval bypass
    # until those guards are wired up.
    if target_status in _PHASE4_GATED_TARGETS:
        raise ForbiddenError(
            "Approve and reject are decided through the approval workflow "
            "(/approvals), and reimburse is a finance action; none can be set "
            "on your own expense report."
        )

    # UAT2 (M12): a report with no entries is not a claim.  A report that has been
    # through approval is exempt: if its owner deleted every line of a returned
    # report it can neither be deleted (approval history) nor fixed, so it is
    # resubmitted empty and a human closes it.
    if target_status == ExpenseReportStatus.submitted:
        _check_version(report, expected_version)  # a stale tab learns that first
        entry_count = session.scalar(
            sa.select(sa.func.count(Expense.id)).where(
                Expense.report_id == report.id,
                Expense.organization_id == report.organization_id,
            )
        )
        if not entry_count and not report_has_approval(
            session, organization_id=report.organization_id, report_id=report.id
        ):
            raise ConflictError(
                "Add at least one expense before submitting this report.", code=REPORT_EMPTY
            )

    before = _report_snapshot(report)

    # Optimistic lock: if the caller supplied a version, use it as the CAS guard.
    # If absent, fall back to the current loaded version (always succeeds unless
    # a concurrent writer committed between our SELECT and UPDATE — in that case
    # fetchone() returns None → ConflictError, which is also correct).
    lock_version = expected_version if expected_version is not None else report.version

    update_values: dict[str, Any] = {
        "status": target_status,
        "updated_by": current_user.id,
    }
    if target_status == ExpenseReportStatus.submitted:
        update_values["submitted_at"] = datetime.now(timezone.utc)

    guarded_version_update(
        session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=lock_version,
        values=update_values,
    )
    session.refresh(report)

    after = _report_snapshot(report)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense_report.status_changed",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state=before,
        after_state=after,
    )

    if target_status == ExpenseReportStatus.draft:
        # UAT2 (M1): reopening a rejected report shows the engine's current
        # verdict, not the submit-time snapshot (flush-only, degrades).
        refresh_presubmit_flags(session, report)

    # C7R: submitting opens the report's approval in the same transaction — the
    # deterministic Approval Agent auto-approves or escalates it to an
    # independent approver; a resubmitted, returned report re-queues its
    # existing approval (P12).  An auto-approval moves the report to approved
    # through the workflow state machine (actor None — the agent decided).
    if target_status == ExpenseReportStatus.submitted:
        approval = open_approval_on_submission(
            session,
            organization_id=report.organization_id,
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=report.id,
            actor=current_user,
        )
        if approval.current_status == ApprovalStatus.auto_approved:
            apply_workflow_transition(session, report, ExpenseReportStatus.approved, actor_id=None)

    session.commit()
    return _report_response(session, report)


def apply_workflow_transition(
    session: Session,
    report: ExpenseReport,
    target_status: ExpenseReportStatus,
    *,
    actor_id: uuid.UUID | None,
) -> bool:
    """Apply an approval-workflow outcome to *report* (flush-only; caller commits).

    The ONLY path that sets a decision outcome on an expense report (C7R / P12).
    Authorization (scope, segregation of duties, decidable state) is the approval
    service's job; this function enforces the state machine:

    * already at *target_status*  → no-op, returns False (no audit row);
    * edge not in WORKFLOW_TRANSITIONS → ConflictError, no mutation, no audit;
    * otherwise CAS on the report version, one ``expense_report.status_changed``
      audit row (``actor_id`` None = the agent decided), returns True.

    Returning to ``draft`` (changes requested) clears ``submitted_at`` — the
    report is no longer submitted; the audit row keeps the old timestamp.
    """
    if report.status == target_status:
        return False
    if target_status not in WORKFLOW_TRANSITIONS[report.status]:
        raise ConflictError(
            f"Cannot move expense report from '{report.status.value}'"
            f" to '{target_status.value}' through the approval workflow."
        )

    before = _report_snapshot(report)
    update_values: dict[str, Any] = {"status": target_status, "updated_by": actor_id}
    if target_status == ExpenseReportStatus.draft:
        update_values["submitted_at"] = None

    guarded_version_update(
        session,
        ExpenseReport,
        entity_id=report.id,
        expected_version=report.version,
        values=update_values,
    )
    session.refresh(report)

    record_audit(
        session,
        organization_id=report.organization_id,
        action="expense_report.status_changed",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=actor_id,
        before_state=before,
        after_state=_report_snapshot(report),
    )
    if target_status == ExpenseReportStatus.draft:
        # UAT2 (M1): the owner must see the engine's CURRENT verdict on a returned
        # report — not the submit-time (or seeded) snapshot.  Flush-only, runs in
        # a SAVEPOINT and degrades; the transition above already bumped the version.
        refresh_presubmit_flags(session, report)
    return True


def _load_entry_for_write(
    session: Session,
    current_user: User,
    entry_id: uuid.UUID,
    *,
    expected_version: int | None,
    verb: str,
) -> tuple[Expense, ExpenseReport]:
    """Lock an entry's parent report then the entry for an owner-only DRAFT change.

    Unreadable / nonexistent → 404; readable but not owned → 403 (C7-01); the
    report not draft → 409; stale If-Match (report version) → 409; the entry
    moved away before the lock was taken → 409.
    """
    # Resolve entry through scope-resolved parent report (no direct entry queries)
    stmt = (
        sa.select(Expense)
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(Expense.id == entry_id)
    )
    # Apply scope via the parent report's ownership/org columns
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    entry = session.scalars(stmt).first()
    if entry is None:
        raise NotFoundError("Expense entry not found.")

    # Load the parent report to check ownership + editability.  Locked FOR UPDATE
    # (C7R review): a concurrent submit of the same report then waits for this
    # change, so a submitted report never holds a change its submit did not see.
    parent = session.scalars(
        sa.select(ExpenseReport)
        .where(ExpenseReport.id == entry.report_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()

    # Write scope (C7-01): only the parent report's owner may change its entries.
    require_owner(parent.owner_id if parent is not None else None, user=current_user)

    if parent is None or parent.status != ExpenseReportStatus.draft:
        raise ConflictError(
            f"Expense entries may only be {verb} while the report is in draft status."
        )
    refuse_withdrawn(parent)
    _check_version(parent, expected_version)

    # Re-read the entry under the parent's lock (C7R review): the read above was
    # unlocked, and a concurrent move may have re-parented the entry (and that
    # report may since have been submitted) before we acquired this lock.  Moves
    # lock the source report too, so once we hold it the entry's report_id is
    # stable — it must still be the report we locked and checked.
    locked_entry = session.scalars(
        sa.select(Expense)
        .where(Expense.id == entry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if locked_entry is None or locked_entry.report_id != parent.id:
        raise ConflictError(
            "Expense entry was moved to another report by another request; reload and retry."
        )
    return locked_entry, parent


def update_entry(
    session: Session,
    current_user: User,
    entry_id: uuid.UUID,
    payload: ExpenseUpdate,
    *,
    expected_version: int | None = None,
) -> ExpenseEntryResponse:
    """Update an expense entry — only allowed when the parent report is draft.

    Recomputes the parent report's total_amount atomically.
    Entry edits on a non-draft report → 409 ConflictError.
    Out-of-scope/nonexistent entry (via parent report scope) → 404.
    Readable but parent report not owned by the caller → 403 (C7-01).

    C7R UAT: entries carry no version of their own; the PARENT REPORT is the
    optimistic-lock aggregate.  ``expected_version`` (If-Match) is the report
    version the caller last saw — stale → 409, no change.  Every edit re-runs the
    pre-submit check (``refresh_presubmit_flags``) and bumps the report version,
    so a submit made against the pre-edit flags is rejected as stale.
    """
    entry, parent = _load_entry_for_write(
        session, current_user, entry_id, expected_version=expected_version, verb="edited"
    )

    before = _entry_snapshot(entry)

    # Apply only supplied fields.  Product gap 2: attendees / disclosure / mileage are
    # category-gated and a mileage amount is recomputed server-side (policy_fields).
    update_data = apply_entry_policy_fields(
        session, entry, payload.model_dump(exclude_unset=True), payload
    )
    # EXP-04: the affidavit flag is not a column — the server stamps when it was signed.
    signed = update_data.pop("receipt_affidavit", None)
    for field, value in update_data.items():
        setattr(entry, field, value)
    # After the other fields: signing is checked against the line AS EDITED, and a
    # change of the claim withdraws an earlier signature (process_flags).
    apply_affidavit_change(session, entry, signed)
    entry.updated_by = current_user.id

    session.flush()

    # Recompute parent total_amount atomically
    parent.total_amount = _compute_total(session, parent.id)
    parent.updated_by = current_user.id

    # Re-evaluate policy for the edited report (justification included), then
    # CAS-bump its version (stale If-Match → 409, whole edit rolled back).
    refresh_presubmit_flags(session, parent)
    _bump_report_version(
        session, parent, expected_version=expected_version, actor_id=current_user.id
    )
    refresh_duplicate_peers(
        session, report=parent, exclude_ids={parent.id}, actor_id=current_user.id
    )
    session.refresh(entry)

    after = _entry_snapshot(entry)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense.updated",
        entity_type="expense",
        entity_id=entry.id,
        actor_id=current_user.id,
        before_state=before,
        after_state=after,
    )

    session.commit()
    return entries_with_receipt_ids(session, [entry], report=parent, viewer=current_user)[0]


def rename_report(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    payload: ReportDetailsUpdate,
    *,
    expected_version: int | None = None,
) -> ExpenseReportResponse:
    """Rename a DRAFT report the caller owns (C7R UAT report assembly).

    404 unreadable, 403 not owner, 409 not draft or stale If-Match version.
    One ``expense_report.updated`` audit row (title before/after).  The policy
    check is unaffected by a title, so flags are not re-evaluated.
    """
    report = _load_report_for_write(session, current_user, report_id)
    _check_version(report, expected_version)
    if report.title == payload.title:
        return _report_response(session, report)

    before = _report_snapshot(report)
    report.title = payload.title
    _bump_report_version(
        session, report, expected_version=expected_version, actor_id=current_user.id
    )
    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense_report.updated",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state=before,
        after_state=_report_snapshot(report),
    )
    session.commit()
    return _report_response(session, report)


REPORT_UNDER_REVIEW = "REPORT_UNDER_REVIEW"


def _refuse_reviewed_reports(session: Session, *reports: ExpenseReport) -> None:
    """409 when any of *reports* was returned by a human reviewer (C7R SoD, move source).

    Checked under the reports' row locks: a returned report can only become
    "un-returned" by being resubmitted (status no longer draft), and its step
    history is append-only, so the answer cannot change before the commit.
    """
    reviewed = subjects_with_human_review_history(
        session,
        organization_id=reports[0].organization_id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_ids=[r.id for r in reports],
    )
    if reviewed:
        raise ConflictError(
            "Entries cannot be moved out of a report a reviewer has returned. "
            "Fix the entries on that report and resubmit it for review.",
            code=REPORT_UNDER_REVIEW,
        )


def _keep_released_receipts(
    session: Session, source: ExpenseReport, entries: Sequence[Expense], *, actor: User
) -> None:
    """Mark the reviewed receipts of lines leaving a WITHDRAWN report kept-for-audit.

    Product gap 4.  On the withdrawn report these receipts are frozen evidence
    (a reviewer saw them: ``unreviewed_receipt_ids``).  Once their line sits on a
    fresh draft with no approval history they would become correctable and
    deletable — so each gets the ``receipt.retained_for_audit`` marker while it
    stays LINKED: correction, deletion, unsharing and relinking then refuse it
    (``correction_locked_receipt_ids``, ``delete_receipt``, ``link``), and
    deleting the line detaches it instead of deleting it.  A receipt attached
    after the last return (never reviewed) stays free.  Flush-only.
    """
    ids = {e.id for e in entries}
    rows = session.scalars(
        sa.select(Receipt)
        .where(
            Receipt.organization_id == source.organization_id,
            sa.or_(Receipt.expense_id.in_(list(ids)), shared_with_any(list(ids))),
        )
        .order_by(Receipt.created_at, Receipt.id)
    ).all()
    if not rows:
        return
    receipt_ids = [r.id for r in rows]
    unseen = unreviewed_receipt_ids(
        session,
        organization_id=source.organization_id,
        report_id=source.id,
        receipt_ids=receipt_ids,
    )
    kept = retained_receipt_ids(
        session, organization_id=source.organization_id, receipt_ids=receipt_ids
    )
    for receipt in rows:
        if receipt.id in unseen or receipt.id in kept:
            continue
        record_audit(
            session,
            organization_id=source.organization_id,
            action=RECEIPT_RETAINED_ACTION,
            entity_type="receipt",
            entity_id=receipt.id,
            actor_id=actor.id,
            before_state={**_receipt_evidence(receipt), "expense_id": str(receipt.expense_id)},
            after_state={
                "expense_id": str(receipt.expense_id),
                "report_id": str(source.id),
                "reason": "line released from a withdrawn report",
            },
        )


def _release_lineage(
    session: Session, source: ExpenseReport, entries: Sequence[Expense]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Stamp each line leaving WITHDRAWN *source* with where it came from (phase 10).

    Sets ``released_from_report_id`` / ``released_from_approval_id`` /
    ``released_at`` (migration 0023) in the move's own transaction; the approval
    agent's re-file check then flags exactly these lines, whatever the owner
    edits later.  A line that already carries a lineage keeps its FIRST one (a
    second release can never rewrite where a reviewer first saw it).  Returns the
    ``released_from`` audit fragment per stamped line.  Flush-only.
    """
    approval_id = session.scalar(
        sa.select(Approval.id).where(
            Approval.organization_id == source.organization_id,
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id == source.id,
        )
    )
    now = datetime.now(timezone.utc)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for entry in entries:
        if entry.released_from_report_id is None:
            entry.released_from_report_id = source.id
            entry.released_from_approval_id = approval_id
            entry.released_at = now
        out[entry.id] = {
            "released_from": {
                "report_id": str(entry.released_from_report_id),
                "approval_id": (
                    str(entry.released_from_approval_id)
                    if entry.released_from_approval_id
                    else None
                ),
                "released_at": str(entry.released_at),
            }
        }
    return out


RECEIPT_SHARED = "RECEIPT_SHARED"


def _refuse_splitting_a_shared_receipt(
    session: Session, source: ExpenseReport, moving: set[uuid.UUID]
) -> None:
    """409 when a move would leave a SHARED receipt backing lines on two reports.

    UAT2 hardening: one receipt row may back several lines only on one report
    (itemized siblings).  Lines sharing a receipt move together or not at all.
    """
    if not moving:
        return
    candidates = session.scalars(
        sa.select(Receipt).where(
            Receipt.organization_id == source.organization_id,
            sa.or_(Receipt.expense_id.in_(list(moving)), shared_with_any(list(moving))),
        )
    ).all()
    on_source = set(
        session.scalars(
            sa.select(Expense.id).where(
                Expense.report_id == source.id,
                Expense.organization_id == source.organization_id,
            )
        ).all()
    )
    for receipt in candidates:
        group = set(backing_expense_ids(receipt)) & on_source
        if len(group) > 1 and not group <= moving:
            raise ConflictError(
                "One of these lines shares its receipt with another line of this report "
                "(an itemized receipt). Move those lines together, or remove the shared "
                "receipt from one of them first.",
                code=RECEIPT_SHARED,
            )


def move_entries(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    payload: MoveEntriesRequest,
    *,
    expected_version: int | None = None,
) -> MoveEntriesResponse:
    """Move entries from draft report *report_id* to another draft report (C7R UAT).

    Both reports must be readable (404), owned by the caller (403) and in draft
    (409).  Neither may have been returned by a human reviewer (409
    ``REPORT_UNDER_REVIEW``, segregation of duties): a returned report's entries
    stay on it — fixed there and resubmitted, which re-queues the SAME approval
    for a human — and are never laundered into a fresh report whose first
    submission the agent could auto-approve.  Every entry must currently be on
    the source report (404 otherwise — an entry id from elsewhere is not
    disclosed).  ``expected_version`` (If-Match)
    is the SOURCE report version (the report whose entries the caller selected).

    Atomically: entries re-parented (receipts stay linked to their entry), both
    totals re-derived, both pre-submit checks re-run, both versions bumped, one
    ``expense.moved`` audit row per entry.  Reports are locked in id order so two
    opposite moves cannot deadlock.
    """
    if payload.target_report_id == report_id:
        raise BadRequestError("Entries are already on this report.")
    entry_ids = list(dict.fromkeys(payload.entry_ids))

    locked = {
        rid: _load_report_for_write(session, current_user, rid, allow_withdrawn=rid == report_id)
        for rid in sorted({report_id, payload.target_report_id}, key=str)
    }
    source, target = locked[report_id], locked[payload.target_report_id]
    _check_version(source, expected_version)
    released = source.withdrawn_at is not None
    # SOURCE only: moving items OUT of a returned report could launder them past the
    # reviewer; moving items INTO one is safe (the target goes back to a human on
    # resubmission) and is how a reviewer's "add the missing line" request is met.
    # Product gap 4: a WITHDRAWN report is closed — its lines are released to the
    # owner's other drafts.  Laundering is covered at approval time instead: the
    # re-file check still matches every line a reviewer REJECTED (frozen return
    # snapshots + the withdrawal audit row), and reviewed receipts stay kept for
    # audit (``_keep_released_receipts``).
    if not released:
        _refuse_reviewed_reports(session, source)

    entries = list(
        session.scalars(
            sa.select(Expense)
            .where(
                Expense.id.in_(entry_ids),
                Expense.report_id == source.id,
                Expense.organization_id == source.organization_id,
            )
            .order_by(Expense.created_at, Expense.id)
        ).all()
    )
    if len(entries) != len(entry_ids):
        raise NotFoundError("Expense entry not found on this report.")
    _refuse_splitting_a_shared_receipt(session, source, {e.id for e in entries})
    if released:
        _keep_released_receipts(session, source, entries, actor=current_user)

    befores = {entry.id: _entry_snapshot(entry) for entry in entries}
    lineage = _release_lineage(session, source, entries) if released else {}
    for entry in entries:
        entry.report_id = target.id
        entry.updated_by = current_user.id
    session.flush()

    for report in (source, target):
        report.total_amount = _compute_total(session, report.id)
        report.updated_by = current_user.id
        if report.withdrawn_at is None:  # a withdrawn report's flags stay as closed
            refresh_presubmit_flags(session, report)
        _bump_report_version(session, report, expected_version=None, actor_id=current_user.id)
    # UAT2: a duplicate flag on ANOTHER draft that named the source (or the moved
    # line) is stale now — re-check those peers too.
    refresh_duplicate_peers(
        session, report=source, exclude_ids={source.id, target.id}, actor_id=current_user.id
    )

    # Audited AFTER the re-evaluation so after_state carries the entry's
    # re-derived flagged/draft status on its new report (C7R review).
    for entry in entries:
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="expense.moved",
            entity_type="expense",
            entity_id=entry.id,
            actor_id=current_user.id,
            before_state=befores[entry.id],
            after_state={**_entry_snapshot(entry), **lineage.get(entry.id, {})},
        )

    session.commit()
    return MoveEntriesResponse(
        source=_report_response(session, source),
        target=_report_response(session, target),
        moved_entry_ids=[e.id for e in entries],
    )


# ---------------------------------------------------------------------------
# UAT2 — attach receipts, delete entries / receipts / never-submitted reports
# ---------------------------------------------------------------------------
#
# Governance (see ai_agents.receipt_pipeline.retention):
#   * Writes are owner-only (404 unreadable → 403 not owner) and draft-only.
#   * A report that has EVER been sent for approval is never deleted (its approval
#     history, 0014 snapshots and the agent's open-return checks join on it).
#   * An entry that was on a report when a human returned it is deletable only
#     while every such return step froze the report's content (subject_snapshot,
#     0014): the approval agent's re-file check (_refiled_rejected_flags) then
#     still catches the deleted line if it is re-filed elsewhere, and the returned
#     report itself goes back to a human on resubmission.  Legacy (pre-0014)
#     return steps have no snapshot → such entries are refused (fail closed).
#   * Receipts that are or were evidence on a report with approval history are
#     kept: detached from a deleted entry (marker ``receipt.retained_for_audit``),
#     never hard-deleted and never linked to another entry.
#   * Every delete writes audit rows (the deleted row's FULL content) in the same
#     transaction; stored images are removed only AFTER the commit, best effort,
#     by the router (``purge_receipt_objects``).

REPORT_EMPTY = "REPORT_EMPTY"
REPORT_HAS_APPROVAL = "REPORT_HAS_APPROVAL"
RECEIPT_RETAINED = "RECEIPT_RETAINED"
RECEIPT_IN_USE = "RECEIPT_IN_USE"
RECEIPT_ALREADY_LINKED = "RECEIPT_ALREADY_LINKED"
RECEIPT_NOT_PROCESSED = "RECEIPT_NOT_PROCESSED"

_ENTRY_NOT_FROZEN = (
    "This entry was on the report when your reviewer returned it, and that review "
    "did not keep a copy of the report, so it can't be deleted. Correct it (or "
    "attach a receipt / add a justification) and resubmit the report."
)
_LOCK_ENTRY_ATTEMPTS = 3


def linked_entry_report_id(db: Session, *, user: User, expense_id: uuid.UUID) -> uuid.UUID | None:
    """The entry's CURRENT report id (unlocked read; the caller then locks it)."""
    return db.scalar(
        sa.select(Expense.report_id).where(
            Expense.id == expense_id,
            Expense.organization_id == user.organization_id,
        )
    )


def lock_linked_entry(
    db: Session, *, user: User, expense_id: uuid.UUID
) -> tuple[Expense, ExpenseReport] | Literal["moving"] | None:
    """Lock the entry's report, then the entry, both re-read fresh.

    Returns ``(entry, report)`` with the entry verified to still be on the locked
    report; ``None`` when the entry is gone or not on a report the caller owns
    (same org); ``"moving"`` when it was re-parented on every attempt.  The
    report lock comes first (report → entry), the same order as entry edits,
    moves and deletes, so these writers cannot deadlock.
    """
    for _ in range(_LOCK_ENTRY_ATTEMPTS):
        report_id = linked_entry_report_id(db, user=user, expense_id=expense_id)
        if report_id is None:
            return None
        report = db.scalar(
            sa.select(ExpenseReport)
            .where(
                ExpenseReport.id == report_id,
                ExpenseReport.organization_id == user.organization_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if report is None or report.owner_id != user.id:
            return None
        entry = db.scalar(
            sa.select(Expense)
            .where(
                Expense.id == expense_id,
                Expense.organization_id == user.organization_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry is None:
            return None
        if entry.report_id == report.id:
            return entry, report
    return "moving"


_DUPLICATE_FLAG_FILTER = {"flags": [{"flag_type": POSSIBLE_DUPLICATE}]}
# UAT2 hardening (revision): a WEAK receipt match on a peer report (advisory,
# escalating) names the other side too — it must clear when that side changes.
_POSSIBLE_REUSE_FLAG_FILTER = {
    "flags": [{"flag_type": "receipt_mismatch", "observed": {"receipt_check": "possible_reuse"}}]
}


def refresh_duplicate_peers(
    session: Session,
    *,
    report: ExpenseReport | None = None,
    organization_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    exclude_ids: set[uuid.UUID],
    actor_id: uuid.UUID,
    twins_of: Sequence[Expense] = (),
) -> None:
    """Re-check the owner's OTHER drafts whose stored flags carry a duplicate (flush-only).

    A duplicate is a blocking ``possible_duplicate`` or (UAT2 hardening) a weak
    receipt match (``receipt_mismatch`` / ``possible_reuse``).

    ``twins_of`` (UAT2 defect D): lines that were just ADDED.  A draft holding a
    line with the same date, amount and currency as one of them is re-checked
    too — its stored flags predate the new twin, so they carry no duplicate yet.
    (The pre-submit check itself decides whether the supplier matches.)

    UAT2: fixing one side of a duplicate (edit, move, delete, attach) never
    cleared the flag on the other report, whose stored snapshot kept naming a
    line that is gone or changed — a deadlock only an unrelated edit broke.  Each
    peer is locked with SKIP LOCKED (never waits, so no lock-order deadlock with a
    concurrent writer — which re-checks that report itself), re-evaluated, and
    its version bumped when the flags changed (a stale tab then gets a 409).
    No audit row: a flag refresh is not a user change.  The owner / org come
    from *report* or are passed explicitly (a deleted report).
    """
    org_id = (
        organization_id if organization_id is not None else getattr(report, "organization_id", None)
    )
    owner = owner_id if owner_id is not None else getattr(report, "owner_id", None)
    if org_id is None or owner is None:
        return
    twin_matches = [
        sa.select(Expense.id)
        .where(
            Expense.report_id == ExpenseReport.id,
            Expense.organization_id == org_id,
            Expense.id != twin.id,
            Expense.expense_date == twin.expense_date,
            Expense.amount == twin.amount,
            Expense.currency == twin.currency,
        )
        .exists()
        for twin in twins_of
        if twin.amount is not None and twin.expense_date is not None
    ]
    peers = session.scalars(
        sa.select(ExpenseReport)
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.owner_id == owner,
            ExpenseReport.status == ExpenseReportStatus.draft,
            ExpenseReport.withdrawn_at.is_(None),
            ExpenseReport.id.notin_(list(exclude_ids)) if exclude_ids else sa.true(),
            sa.or_(
                ExpenseReport.pre_submit_flags.op("@>")(sa.cast(_DUPLICATE_FLAG_FILTER, JSONB)),
                ExpenseReport.pre_submit_flags.op("@>")(
                    sa.cast(_POSSIBLE_REUSE_FLAG_FILTER, JSONB)
                ),
                *twin_matches,
            ),
        )
        .order_by(ExpenseReport.id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for peer in peers:
        before = peer.pre_submit_flags
        refresh_presubmit_flags(session, peer)
        if peer.pre_submit_flags != before:
            _bump_report_version(session, peer, expected_version=None, actor_id=actor_id)


def _human_return_steps(
    session: Session, report: ExpenseReport
) -> list[tuple[datetime | None, bool]]:
    """(decided_at, has snapshot) of every human reject / request_changes step of *report*."""
    # The snapshot is read back and checked in Python: an absent snapshot may be
    # SQL NULL (pre-0014 rows) or JSON null — only a frozen dict counts.
    rows = session.execute(
        sa.select(ApprovalStep.decided_at, ApprovalStep.subject_snapshot)
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .where(
            Approval.organization_id == report.organization_id,
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id == report.id,
            ApprovalStep.decision.in_(HUMAN_RETURN_DECISIONS),
        )
    ).all()
    return [(decided_at, isinstance(snapshot, dict)) for decided_at, snapshot in rows]


def _entry_delete_blocks(
    session: Session, report: ExpenseReport, entries: Sequence[Expense]
) -> dict[uuid.UUID, str | None]:
    """Entry id → why its owner may NOT delete it now (None = deletable).

    Fails closed: a report with approval history but no human return step on
    record refuses every delete; an entry created before (or at) a return step
    with no snapshot is refused.  Entries added after the last return are free.
    An entry MOVED into the report after a legacy return keeps its older
    created_at and is refused too (fail closed, documented).

    Allowing a snapshot-covered delete is weaker than move_entries (which never
    moves lines OUT of a returned report) and is safe only because (a) the
    returned report goes back to a human on resubmission, (b) while it is
    unresolved every other submission by the owner escalates
    (``_open_returned_subject_flags``) and (c) the snapshot lets
    ``_refiled_rejected_flags`` catch the deleted line re-filed elsewhere (date
    ±3 days, amount ± max($1, 2%)), and (d) the deleted line's receipt is kept
    for audit and a re-uploaded copy of it on any later claim raises an
    escalating ``receipt_mismatch`` flag naming the returned report
    (presubmit ``_receipt_reuse_scan``, UAT2 review) — so a re-file outside the
    date window but carrying the same paper receipt still reaches a human.
    """
    if not entries:
        return {}
    if report.status != ExpenseReportStatus.draft:
        reason = f"Entries can't be deleted from a {report.status.value} report."
        return {e.id: reason for e in entries}
    if report.withdrawn_at is not None:
        return {e.id: _WITHDRAWN_MESSAGE for e in entries}
    if not report_has_approval(
        session, organization_id=report.organization_id, report_id=report.id
    ):
        return {e.id: None for e in entries}
    steps = _human_return_steps(session, report)
    out: dict[uuid.UUID, str | None] = {}
    for entry in entries:
        if not steps:
            out[entry.id] = _ENTRY_NOT_FROZEN
            continue
        relevant = [
            frozen
            for decided_at, frozen in steps
            if decided_at is None or entry.created_at is None or entry.created_at <= decided_at
        ]
        out[entry.id] = None if all(relevant) else _ENTRY_NOT_FROZEN
    return out


def _receipt_evidence(receipt: Receipt) -> dict[str, Any]:
    fields = receipt.extracted_fields if isinstance(receipt.extracted_fields, dict) else {}
    return {
        "receipt_id": str(receipt.id),
        "ocr_status": getattr(receipt.ocr_status, "value", receipt.ocr_status),
        "amount": fields.get("amount"),
        "currency": fields.get("currency"),
        "date": fields.get("date"),
        "vendor": fields.get("vendor"),
        "field_provenance": receipt.field_provenance,
        "created_at": str(receipt.created_at) if receipt.created_at else None,
    }


def _entry_full_snapshot(
    entry: Expense, receipts: Sequence[Receipt], card_txn_ids: Sequence[uuid.UUID]
) -> dict[str, Any]:
    """The deleted row's FULL content for the audit trail (it is the only evidence left)."""
    return {
        **_entry_snapshot(entry),
        "entry_id": str(entry.id),
        "match_source": getattr(entry.match_source, "value", entry.match_source),
        "trip_segment_id": str(entry.trip_segment_id) if entry.trip_segment_id else None,
        "created_at": str(entry.created_at) if entry.created_at else None,
        "receipts": [_receipt_evidence(r) for r in receipts],
        "card_transaction_ids": [str(t) for t in card_txn_ids],
    }


def _discard_entry_evidence(
    session: Session,
    *,
    entry: Expense,
    report: ExpenseReport,
    actor: User,
    retain: bool,
) -> tuple[dict[str, Any], list[uuid.UUID], list[uuid.UUID], list[str]]:
    """Unlink / delete an entry's receipts and free its card transactions (flush-only).

    Returns (entry audit snapshot, deleted receipt ids, retained receipt ids,
    storage keys to purge AFTER commit).  ``retain`` (the report has approval
    history): receipts are detached and kept, marked ``receipt.retained_for_audit``.
    """
    receipts = list(
        session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.expense_id == entry.id,
                Receipt.organization_id == entry.organization_id,
            )
            .order_by(Receipt.created_at, Receipt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    txns = list(
        session.scalars(
            sa.select(CardTransaction)
            .where(
                CardTransaction.expense_id == entry.id,
                CardTransaction.organization_id == entry.organization_id,
            )
            .with_for_update()
        ).all()
    )
    # UAT2 hardening: receipts merely SHARED with this line (itemized siblings).
    sharers = list(
        session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.organization_id == entry.organization_id,
                Receipt.expense_id != entry.id,
                shared_with_any([entry.id]),
            )
            .order_by(Receipt.created_at, Receipt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    snapshot = _entry_full_snapshot(entry, [*receipts, *sharers], [t.id for t in txns])
    deleted: list[uuid.UUID] = []
    retained: list[uuid.UUID] = []
    keys: list[str] = []
    # A receipt that still backs ANOTHER line stays with that line: this line is
    # dropped from its share list, or — when this line is its primary link — the
    # next sibling becomes the primary.  Never deleted, never detached.
    for receipt in [*sharers, *receipts]:
        before = {
            "expense_id": str(receipt.expense_id) if receipt.expense_id else None,
            "shared_expense_ids": [str(e) for e in shared_expense_ids(receipt)],
        }
        others = [e for e in backing_expense_ids(receipt) if e != entry.id]
        if not others:
            continue
        receipt.expense_id = others[0]
        set_shared_expense_ids(receipt, others[1:])
        record_audit(
            session,
            organization_id=entry.organization_id,
            action="receipt.unshared",
            entity_type="receipt",
            entity_id=receipt.id,
            actor_id=actor.id,
            before_state=before,
            after_state={
                "expense_id": str(receipt.expense_id),
                "shared_expense_ids": [str(e) for e in shared_expense_ids(receipt)],
                "reason": "line deleted",
                "removed_expense_id": str(entry.id),
            },
        )
    receipts = [r for r in receipts if r.expense_id == entry.id]
    # Keep for audit only receipts a reviewer has seen; one attached after the
    # latest return is deleted with its line (no dead end, nothing relied on it).
    unreviewed = (
        unreviewed_receipt_ids(
            session,
            organization_id=entry.organization_id,
            report_id=report.id,
            receipt_ids=[r.id for r in receipts],
        )
        if retain
        else set()
    )
    # Product gap 4: a line released from a WITHDRAWN report keeps its reviewed
    # receipts marked for audit on whatever report it now sits on — never deleted.
    already_kept = retained_receipt_ids(
        session, organization_id=entry.organization_id, receipt_ids=[r.id for r in receipts]
    )
    for receipt in receipts:
        evidence = _receipt_evidence(receipt)
        if receipt.id in already_kept or (retain and receipt.id not in unreviewed):
            receipt.expense_id = None
            retained.append(receipt.id)
            record_audit(
                session,
                organization_id=entry.organization_id,
                action=RECEIPT_RETAINED_ACTION,
                entity_type="receipt",
                entity_id=receipt.id,
                actor_id=actor.id,
                before_state={**evidence, "expense_id": str(entry.id)},
                after_state={
                    "expense_id": None,
                    "report_id": str(report.id),
                    "reason": "entry deleted from a report with approval history",
                },
            )
        else:
            deleted.append(receipt.id)
            if receipt.image_url:
                keys.append(receipt.image_url)
            record_audit(
                session,
                organization_id=entry.organization_id,
                action="receipt.deleted",
                entity_type="receipt",
                entity_id=receipt.id,
                actor_id=actor.id,
                before_state={**evidence, "expense_id": str(entry.id)},
                after_state=None,
            )
            session.delete(receipt)
    for txn in txns:
        # The card charge is claimable again (re-matched by assembly).
        txn.expense_id = None
        txn.matched = False
    session.flush()
    return snapshot, deleted, retained, keys


def delete_entry(
    session: Session,
    current_user: User,
    entry_id: uuid.UUID,
    *,
    expected_version: int | None = None,
) -> tuple[EntryDeleteResponse, list[str]]:
    """Delete an expense entry of a DRAFT report the caller owns (UAT2).

    404 unreadable, 403 not owner, 409 report not draft / stale If-Match (report
    version) / reviewed entry whose return froze no snapshot (REPORT_UNDER_REVIEW).
    Receipts are deleted with the entry — or, when the report has approval
    history, detached and kept for audit.  Re-totals, re-runs the pre-submit
    check, bumps the version, re-checks duplicate peers, audits; commits.
    Returns the response and the storage keys the router purges after commit.
    """
    entry, parent = _load_entry_for_write(
        session, current_user, entry_id, expected_version=expected_version, verb="deleted"
    )
    reason = _entry_delete_blocks(session, parent, [entry]).get(entry.id)
    if reason is not None:
        raise ConflictError(reason, code=REPORT_UNDER_REVIEW)
    retain = report_has_approval(
        session, organization_id=parent.organization_id, report_id=parent.id
    )

    snapshot, deleted, retained, keys = _discard_entry_evidence(
        session, entry=entry, report=parent, actor=current_user, retain=retain
    )
    session.delete(entry)
    session.flush()

    parent.total_amount = _compute_total(session, parent.id)
    parent.updated_by = current_user.id
    refresh_presubmit_flags(session, parent)
    _bump_report_version(
        session, parent, expected_version=expected_version, actor_id=current_user.id
    )
    refresh_duplicate_peers(
        session, report=parent, exclude_ids={parent.id}, actor_id=current_user.id
    )

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense.deleted",
        entity_type="expense",
        entity_id=entry_id,
        actor_id=current_user.id,
        before_state=snapshot,
        after_state=None,
    )
    session.commit()
    return (
        EntryDeleteResponse(
            report=_report_response(session, parent),
            deleted_entry_id=entry_id,
            deleted_receipt_ids=deleted,
            retained_receipt_ids=retained,
        ),
        keys,
    )


def delete_report(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    *,
    expected_version: int | None = None,
) -> list[str]:
    """Delete a never-submitted DRAFT report the caller owns, with its entries (UAT2).

    404 unreadable, 403 not owner, 409 not draft / stale If-Match / the report
    has ANY approval history (REPORT_HAS_APPROVAL — its review history stays).
    Every entry and receipt is audited with its full content; returns the
    storage keys the router purges after the commit.
    """
    report = _load_report_for_write(session, current_user, report_id)
    _check_version(report, expected_version)
    org_id, owner_id = report.organization_id, report.owner_id
    if report_has_approval(session, organization_id=report.organization_id, report_id=report.id):
        raise ConflictError(
            "Submitted reports can't be deleted: this report has been through approval, "
            "so its review history stays with it. Fix it and resubmit instead.",
            code=REPORT_HAS_APPROVAL,
        )
    entries = list(
        session.scalars(
            sa.select(Expense)
            .where(
                Expense.report_id == report.id,
                Expense.organization_id == report.organization_id,
            )
            .order_by(Expense.created_at, Expense.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    keys: list[str] = []
    for entry in entries:
        snapshot, _deleted, _retained, entry_keys = _discard_entry_evidence(
            session, entry=entry, report=report, actor=current_user, retain=False
        )
        keys.extend(entry_keys)
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="expense.deleted",
            entity_type="expense",
            entity_id=entry.id,
            actor_id=current_user.id,
            before_state=snapshot,
            after_state=None,
        )
        session.delete(entry)
    session.flush()

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense_report.deleted",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=current_user.id,
        before_state={
            **_report_snapshot(report),
            "pre_submit_flags": report.pre_submit_flags,
            "trip_id": str(report.trip_id) if report.trip_id else None,
            "entry_ids": [str(e.id) for e in entries],
        },
        after_state=None,
    )
    session.delete(report)
    session.flush()
    refresh_duplicate_peers(
        session,
        organization_id=org_id,
        owner_id=owner_id,
        exclude_ids={report_id},
        actor_id=current_user.id,
    )
    session.commit()
    return keys


_REUSE_RECEIPT_CHECKS = frozenset({"possible_reuse", "kept_for_audit"})


def entry_flags(report: ExpenseReport, entry_id: uuid.UUID) -> list[dict[str, Any]]:
    """*entry_id*'s stored pre-submit flags, plus a report-level "not re-evaluated" note.

    The note (no entry id) means the check was NOT re-run — never "in policy".
    """
    snapshot = report.pre_submit_flags if isinstance(report.pre_submit_flags, dict) else {}
    return [
        f
        for f in (snapshot.get("flags") or [])
        if isinstance(f, dict)
        and (
            f.get("entry_id") == str(entry_id)
            or (f.get("flag_type") == "not_evaluated" and f.get("entry_id") is None)
        )
    ]


def duplicate_reason(flags: Sequence[dict[str, Any]]) -> str | None:
    """Why a line looks like a second claim (UAT2 defect D), or None.

    A blocking ``possible_duplicate`` wins; otherwise a ``receipt_mismatch`` whose
    receipt matches one backing another claim (``possible_reuse``) or one kept for
    audit.  The server's own flag reason — the client shows it verbatim.
    """
    for f in flags:
        if f.get("flag_type") == POSSIBLE_DUPLICATE and f.get("reason"):
            return str(f["reason"])
    for f in flags:
        observed = f.get("observed")
        if (
            f.get("flag_type") == "receipt_mismatch"
            and isinstance(observed, dict)
            and observed.get("receipt_check") in _REUSE_RECEIPT_CHECKS
            and f.get("reason")
        ):
            return str(f["reason"])
    return None


def _entry_verdict(report: ExpenseReport, entry_id: uuid.UUID) -> PolicyVerdictSummary:
    """The refreshed pre-submit verdict for ONE entry, from the report's stored flags."""
    flags = entry_flags(report, entry_id)
    blocking = sum(1 for f in flags if f.get("severity") == "blocking")
    advisory = sum(1 for f in flags if f.get("severity") == "advisory")
    return PolicyVerdictSummary(
        in_policy=blocking == 0
        and not any(f.get("flag_type") in ESCALATING_FLAG_TYPES for f in flags),
        blocking_count=blocking,
        advisory_count=advisory,
        flag_types=sorted({str(f.get("flag_type")) for f in flags if f.get("flag_type")}),
        requires_approver=next(
            (str(f["requires_approver"]) for f in flags if f.get("requires_approver")), None
        ),
        duplicate_reason=duplicate_reason(flags),
    )


def _single_receipt_inbox_lines(
    session: Session, current_user: User, expense_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of *expense_ids* that are releasable inbox auto-drafts with ONE receipt.

    Releasable = receipt-sourced line on the caller's never-reviewed draft inbox
    (``editable_inbox_expense_ids``); with a second receipt it is no longer just
    "the upload", so it is never deleted on the receipt's behalf.
    """
    editable = editable_inbox_expense_ids(
        session,
        organization_id=current_user.organization_id,
        user_id=current_user.id,
        expense_ids=expense_ids,
    )
    if not editable:
        return set()
    return {
        eid
        for eid in session.scalars(
            sa.select(Receipt.expense_id)
            .where(Receipt.expense_id.in_(editable))
            .group_by(Receipt.expense_id)
            .having(sa.func.count(Receipt.id) == 1)
        ).all()
        if eid is not None
    }


def _single_receipt_inbox_line(session: Session, current_user: User, expense_id: uuid.UUID) -> bool:
    return expense_id in _single_receipt_inbox_lines(session, current_user, {expense_id})


def _lock_inbox_line_nowait(
    session: Session, current_user: User, expense_id: uuid.UUID
) -> uuid.UUID | None:
    """Lock a releasable inbox line (report → entry) WITHOUT waiting (UAT2 review).

    Taken after the target report / entry locks and before the receipt lock.  The
    inbox report is a second report lock, so it never waits (a concurrent move
    between the two reports could otherwise deadlock): a busy inbox is a 409 the
    client simply retries.  Returns the line id when it is (still) releasable.
    """
    report_id = linked_entry_report_id(session, user=current_user, expense_id=expense_id)
    if report_id is None:
        return None
    try:
        with session.begin_nested():
            session.execute(
                sa.select(ExpenseReport.id)
                .where(
                    ExpenseReport.id == report_id,
                    ExpenseReport.organization_id == current_user.organization_id,
                )
                .with_for_update(nowait=True)
            )
            session.execute(
                sa.select(Expense.id)
                .where(
                    Expense.id == expense_id,
                    Expense.organization_id == current_user.organization_id,
                )
                .with_for_update(nowait=True)
            )
    except OperationalError as exc:
        raise ConflictError(
            "Your Receipt Inbox is being updated by another request; try again."
        ) from exc
    return expense_id if _single_receipt_inbox_line(session, current_user, expense_id) else None


def _release_inbox_line(
    session: Session, current_user: User, *, receipt: Receipt, target_entry_id: uuid.UUID
) -> None:
    """Delete *receipt*'s own Receipt-Inbox line so the receipt can back another entry.

    Same rules as smart-match's ``release_inbox_draft`` (never-reviewed draft
    inbox, receipt-sourced line) plus: the line has no other receipt.  The line's
    full content is audited (``expense.deleted``) — it is the only record left.
    """
    line_id = receipt.expense_id
    if line_id is None or not _single_receipt_inbox_line(session, current_user, line_id):
        return
    line = session.get(Expense, line_id)
    if line is None:
        return
    card_ids = list(
        session.scalars(sa.select(CardTransaction.id).where(CardTransaction.expense_id == line_id))
    )
    snapshot = _entry_full_snapshot(line, [receipt], card_ids)
    inbox = session.get(ExpenseReport, line.report_id)
    ctx = AgentContext(
        organization_id=current_user.organization_id,
        granted_permission_keys=frozenset(),
        user_id=current_user.id,
    )
    if not release_inbox_draft(session, context=ctx, receipt=receipt):
        return
    if card_ids:
        session.execute(
            sa.update(CardTransaction)
            .where(CardTransaction.id.in_(card_ids))
            .values(expense_id=None, matched=False)
        )
    if inbox is not None:
        _bump_report_version(session, inbox, expected_version=None, actor_id=current_user.id)
    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense.deleted",
        entity_type="expense",
        entity_id=line_id,
        actor_id=current_user.id,
        before_state=snapshot,
        after_state={
            "reason": "receipt attached to another expense",
            "receipt_id": str(receipt.id),
            "attached_to_entry_id": str(target_entry_id),
        },
    )


def _share_refusal(
    session: Session, *, receipt: Receipt, entry: Expense, report: ExpenseReport
) -> str | None:
    """Why *receipt* (linked to another line) may NOT also back *entry* (None = it may).

    UAT2 hardening: one receipt row may back several lines only when they are all
    on the same report and their summed amounts are covered by the receipt's
    machine-read total (``evidence.machine_total`` — never a typed total).  The
    report is locked by the caller, so the lines can't move meanwhile.
    """
    primary_report = session.scalar(
        sa.select(Expense.report_id).where(
            Expense.id == receipt.expense_id,
            Expense.organization_id == entry.organization_id,
        )
    )
    if primary_report != report.id:
        return (
            "This receipt is already attached to an expense on another report. A receipt "
            "can back only one claim — or be split across lines of the same report."
        )
    total = machine_total(receipt)
    if total is None:
        return (
            "This receipt is already attached to another line, and its total wasn't read "
            "from the receipt, so it can't be split across lines."
        )
    members = {*backing_expense_ids(receipt), entry.id}
    claimed = sum(
        (
            Decimal(str(amount))
            for amount in session.scalars(
                sa.select(Expense.amount).where(
                    Expense.id.in_(list(members)),
                    Expense.organization_id == entry.organization_id,
                )
            ).all()
        ),
        Decimal("0"),
    )
    if claimed > total:
        return (
            f"This receipt ({total:.2f}) is already attached to another line of this report "
            f"and doesn't cover this line too: together the lines would claim {claimed:.2f}. "
            "A receipt can be split across lines only up to its total."
        )
    return None


def link_receipt_locked(
    session: Session,
    current_user: User,
    *,
    receipt: Receipt,
    entry_id: uuid.UUID,
    expected_version: int | None,
    source: str,
    release_inbox_entry_id: uuid.UUID | None = None,
) -> tuple[ExpenseReport, Expense, PolicyVerdictSummary]:
    """Link *receipt* (the caller's, unlinked) to entry *entry_id* (flush-only; UAT2).

    The ONE short locked step of an attach: the entry's report then the entry are
    locked (``lock_linked_entry`` — the order every entry writer uses), then the
    receipt row.  Under those locks: the report must still be the caller's DRAFT
    (a submit that landed while OCR ran wins → 409), the If-Match report version
    must match, and the receipt must be the caller's, not linked to another entry
    (RECEIPT_ALREADY_LINKED — one receipt backs one claim; UAT2 hardening: except
    an itemized receipt split across lines of THIS report within its machine-read
    total, ``_share_refusal``) and not kept for audit (RECEIPT_RETAINED).  Then the
    link is set, the pre-submit check re-run, the version bumped, duplicate peers
    re-checked and an ``expense.receipt_attached`` audit row written.  The caller commits.

    ``release_inbox_entry_id`` (attach-existing only; its report and entry already
    locked by the caller, BEFORE this receipt lock): the receipt may still back
    that entry when it is the receipt's own auto-drafted Receipt-Inbox line —
    then the line is released (deleted, audited) and the receipt moves here.
    """
    locked = lock_linked_entry(session, user=current_user, expense_id=entry_id)
    if locked is None:
        raise NotFoundError("Expense not found or not accessible.")
    if locked == "moving":
        raise ConflictError(
            "Expense entry was moved to another report by another request; reload and retry."
        )
    entry, report = locked
    refuse_receipt_on_mileage(entry)  # product gap 2: REF-08, the amount is computed
    if report.status != ExpenseReportStatus.draft:
        raise ConflictError(
            "Receipts can only be attached to entries of a draft expense report "
            f"(this report is '{report.status.value}')."
        )
    refuse_withdrawn(report)
    _check_version(report, expected_version)

    fresh = session.scalar(
        sa.select(Receipt)
        .where(
            Receipt.id == receipt.id,
            Receipt.organization_id == current_user.organization_id,
            Receipt.uploaded_by == current_user.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if fresh is None:
        raise NotFoundError("Receipt not found or not accessible.")
    if (
        fresh.expense_id is not None
        and fresh.expense_id != entry.id
        and fresh.expense_id == release_inbox_entry_id
    ):
        _release_inbox_line(session, current_user, receipt=fresh, target_entry_id=entry.id)
    already = fresh.expense_id == entry.id or entry.id in shared_expense_ids(fresh)
    sharing = False
    if fresh.expense_id is not None and not already:
        # UAT2 hardening (owner decision 4): an itemized folio may back several
        # lines of ONE draft report, up to its machine-read total — anything
        # else is one receipt backing two claims.
        refusal = _share_refusal(session, receipt=fresh, entry=entry, report=report)
        if refusal is not None:
            raise ConflictError(refusal, code=RECEIPT_ALREADY_LINKED)
        sharing = True
    if receipt_retained_for_audit(
        session, organization_id=current_user.organization_id, receipt_id=fresh.id
    ):
        raise ConflictError(RECEIPT_RETAINED_MESSAGE, code=RECEIPT_RETAINED)

    if sharing:
        set_shared_expense_ids(fresh, [*shared_expense_ids(fresh), entry.id])
    elif not already:
        fresh.expense_id = entry.id
    session.flush()
    refresh_presubmit_flags(session, report)
    _bump_report_version(
        session, report, expected_version=expected_version, actor_id=current_user.id
    )
    refresh_duplicate_peers(
        session, report=report, exclude_ids={report.id}, actor_id=current_user.id
    )
    if not already:
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action=RECEIPT_ATTACHED_ACTION,
            entity_type="expense",
            entity_id=entry.id,
            actor_id=current_user.id,
            before_state={"report_id": str(report.id)},
            # Read back by retention.unreviewed_receipt_ids (legacy-return fallback).
            after_state={
                ATTACH_RECEIPT_ID_KEY: str(fresh.id),
                "report_id": str(report.id),
                "source": source,
                **({ATTACH_SHARED_KEY: str(fresh.expense_id)} if sharing else {}),
            },
        )
    session.refresh(entry)
    return report, entry, _entry_verdict(report, entry.id)


def link_existing_receipt(
    session: Session,
    current_user: User,
    entry_id: uuid.UUID,
    payload: LinkReceiptRequest,
    *,
    expected_version: int | None = None,
) -> ReceiptLinkResponse:
    """POST /expenses/{id}/receipts — attach one of the caller's unlinked receipts (UAT2).

    The entry is resolved like an edit (404 unreadable, 403 not owner, 409 not
    draft / stale version); the receipt must be the caller's (404), already read
    by OCR (409 RECEIPT_NOT_PROCESSED — a pending receipt has nothing to show a
    reviewer yet), unlinked and not kept for audit (``link_receipt_locked``).
    """
    # Resolve + authorize (404 / 403 / 409) the same way an edit does; this takes
    # the report then entry locks (canonical order), which link_receipt_locked
    # re-takes (a no-op in the same transaction) before locking the receipt row.
    _load_entry_for_write(
        session, current_user, entry_id, expected_version=expected_version, verb="given a receipt"
    )
    receipt = session.scalar(
        sa.select(Receipt).where(
            Receipt.id == payload.receipt_id,
            Receipt.organization_id == current_user.organization_id,
            Receipt.uploaded_by == current_user.id,
        )
    )
    if receipt is None:
        raise NotFoundError("Receipt not found or not accessible.")
    if receipt.ocr_status == OcrStatus.pending:
        raise ConflictError(
            "This receipt hasn't been read yet, so it can't be attached.",
            code=RECEIPT_NOT_PROCESSED,
        )
    # A receipt uploaded on the Receipts screen was auto-drafted into a Receipt
    # Inbox line; attaching it elsewhere moves it and releases that line (UAT2
    # review).  The inbox line is locked here, before the receipt row.
    release_id: uuid.UUID | None = None
    if receipt.expense_id is not None and receipt.expense_id != entry_id:
        release_id = _lock_inbox_line_nowait(session, current_user, receipt.expense_id)
    report, entry, verdict = link_receipt_locked(
        session,
        current_user,
        receipt=receipt,
        entry_id=entry_id,
        expected_version=expected_version,
        source="existing",
        release_inbox_entry_id=release_id,
    )
    session.commit()
    return ReceiptLinkResponse(
        report=_report_response(session, report),
        entry=entries_with_receipt_ids(session, [entry], report=report, viewer=current_user)[0],
        policy_verdict=verdict,
    )


def list_attachable_receipts(
    session: Session, current_user: User, *, for_expense_id: uuid.UUID | None = None
) -> list[Receipt]:
    """The caller's own receipts that may be attached to an entry (UAT2).

    Already read by OCR (processed or failed), not kept for audit, and either
    unlinked or still on their own auto-drafted Receipt-Inbox line (a receipt
    uploaded on the Receipts screen — attaching it releases that line; UAT2
    review); newest first, at most 50.  With *for_expense_id* (UAT2 hardening),
    receipts on OTHER lines of that entry's draft report whose total was read
    from the receipt come first: an itemized folio can be split across lines
    (the link re-checks that the lines stay within the receipt's total).  One
    already split onto that line is offered too (its response lists the line in
    ``shared_expense_ids``): re-attaching it is idempotent and re-runs the
    pre-submit check, so a stale "missing receipt" flag can be cleared from the
    flag's own "Attach receipt" action.
    """
    siblings: list[Receipt] = []
    if for_expense_id is not None:
        report_id = session.scalar(
            sa.select(Expense.report_id)
            .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
            .where(
                Expense.id == for_expense_id,
                Expense.organization_id == current_user.organization_id,
                ExpenseReport.organization_id == current_user.organization_id,
                ExpenseReport.owner_id == current_user.id,
                ExpenseReport.status == ExpenseReportStatus.draft,
                ExpenseReport.withdrawn_at.is_(None),
            )
        )
        if report_id is not None:
            siblings = [
                r
                for r in session.scalars(
                    sa.select(Receipt)
                    .join(Expense, Expense.id == Receipt.expense_id)
                    .where(
                        Receipt.organization_id == current_user.organization_id,
                        Receipt.uploaded_by == current_user.id,
                        Receipt.ocr_status == OcrStatus.processed,
                        Expense.report_id == report_id,
                        Expense.id != for_expense_id,
                    )
                    .order_by(Receipt.created_at.desc(), Receipt.id.desc())
                    .limit(30)
                ).all()
                if machine_total(r) is not None
            ]
    single_receipt_inbox_lines = (
        sa.select(Expense.id)
        .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
        .where(
            Expense.organization_id == current_user.organization_id,
            Expense.match_source == ExpenseMatchSource.receipt,
            ExpenseReport.organization_id == current_user.organization_id,
            ExpenseReport.owner_id == current_user.id,
            ExpenseReport.title == RECEIPT_INBOX_TITLE,
            ExpenseReport.status == ExpenseReportStatus.draft,
        )
    )
    rows = list(
        session.scalars(
            sa.select(Receipt)
            .where(
                Receipt.organization_id == current_user.organization_id,
                Receipt.uploaded_by == current_user.id,
                sa.or_(
                    Receipt.expense_id.is_(None),
                    Receipt.expense_id.in_(single_receipt_inbox_lines),
                ),
                Receipt.ocr_status != OcrStatus.pending,
            )
            .order_by(Receipt.created_at.desc(), Receipt.id.desc())
            .limit(80)
        ).all()
    )
    kept = retained_receipt_ids(
        session, organization_id=current_user.organization_id, receipt_ids=[r.id for r in rows]
    )
    linked = {r.expense_id for r in rows if r.expense_id is not None}
    releasable = _single_receipt_inbox_lines(session, current_user, linked)
    free = [
        r for r in rows if r.id not in kept and (r.expense_id is None or r.expense_id in releasable)
    ]
    sibling_ids = {r.id for r in siblings}
    return [*siblings, *(r for r in free if r.id not in sibling_ids)][:50]


def delete_receipt(
    session: Session,
    current_user: User,
    receipt_id: uuid.UUID,
    *,
    discard_drafted_entry: bool = False,
    from_expense_id: uuid.UUID | None = None,
) -> tuple[ReceiptDeleteResponse, list[str]]:
    """DELETE /receipts/{id} — delete one of the caller's receipts (UAT2).

    404 unreadable (scope on ``uploaded_by``), 403 a reader who is not the
    uploader.  Refused (409) when the receipt is or was evidence on a report with
    approval history (RECEIPT_RETAINED), or backs an entry on a report the caller
    cannot change (RECEIPT_IN_USE).  A receipt linked to an entry of a
    never-submitted draft is deleted and that report re-checked; the entry stays
    (RECEIPT-REQ reappears where it applies) — unless ``discard_drafted_entry``
    and the entry is the receipt's own auto-drafted line (receipt-sourced, no
    other receipt): then the entry is deleted with it (the upload is undone).
    A receipt SHARED by several lines of the report (itemized siblings, UAT2
    hardening): with ``from_expense_id`` naming one of them, only that line lets
    go of it (outcome ``unshared`` — the receipt stays for the others); without
    it, the receipt is deleted from every line.
    Returns the response and the storage keys to purge after commit.
    """
    stmt = scope_query(
        sa.select(Receipt).where(Receipt.id == receipt_id),
        user=current_user,
        ownership_column=Receipt.uploaded_by,
        org_column=Receipt.organization_id,
        session=session,
    )
    found = session.scalars(stmt).first()
    if found is None:
        raise NotFoundError("Receipt not found or not accessible.")
    require_owner(found.uploaded_by, user=current_user)

    report: ExpenseReport | None = None
    entry: Expense | None = None
    if found.expense_id is not None:
        locked = lock_linked_entry(session, user=current_user, expense_id=found.expense_id)
        if locked is None or locked == "moving":
            raise ConflictError(
                "This receipt backs an expense you can't change, so it can't be deleted.",
                code=RECEIPT_IN_USE,
            )
        entry, report = locked
        if report_has_approval(
            session, organization_id=report.organization_id, report_id=report.id
        ) and found.id not in unreviewed_receipt_ids(
            session,
            organization_id=report.organization_id,
            report_id=report.id,
            receipt_ids=[found.id],
        ):
            raise ConflictError(RECEIPT_RETAINED_MESSAGE, code=RECEIPT_RETAINED)
        if report.status != ExpenseReportStatus.draft or report.withdrawn_at is not None:
            raise ConflictError(
                "This receipt backs an entry on a "
                f"{'withdrawn' if report.withdrawn_at is not None else report.status.value} "
                "report and can't be deleted.",
                code=RECEIPT_IN_USE,
            )

    receipt = session.scalar(
        sa.select(Receipt)
        .where(Receipt.id == receipt_id, Receipt.organization_id == current_user.organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if receipt is None:
        raise NotFoundError("Receipt not found or not accessible.")
    if receipt.expense_id != (entry.id if entry is not None else None):
        raise ConflictError(
            "This receipt was attached or removed by another request; reload and retry."
        )
    # Kept for audit: detached evidence (UAT2), or — product gap 4 — reviewed
    # evidence still linked to a line released from a withdrawn report.
    if receipt_retained_for_audit(
        session, organization_id=receipt.organization_id, receipt_id=receipt.id
    ):
        raise ConflictError(RECEIPT_RETAINED_MESSAGE, code=RECEIPT_RETAINED)

    keys: list[str] = []
    backing = backing_expense_ids(receipt)
    if from_expense_id is not None and from_expense_id not in backing:
        # UAT2 hardening (revision): a stale / wrong line id must never fall
        # through to deleting the receipt from EVERY line it backs.
        raise ConflictError(
            "This receipt no longer backs that line; reload and retry.",
            code=RECEIPT_IN_USE,
        )
    if entry is not None and len(backing) > 1 and from_expense_id in backing:
        before = {
            "expense_id": str(receipt.expense_id),
            "shared_expense_ids": [str(e) for e in shared_expense_ids(receipt)],
        }
        others = [e for e in backing if e != from_expense_id]
        receipt.expense_id = others[0]
        set_shared_expense_ids(receipt, others[1:])
        session.flush()
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="receipt.unshared",
            entity_type="receipt",
            entity_id=receipt.id,
            actor_id=current_user.id,
            before_state=before,
            after_state={
                "expense_id": str(receipt.expense_id),
                "shared_expense_ids": [str(e) for e in shared_expense_ids(receipt)],
                "reason": "removed from one line by its owner",
                "removed_expense_id": str(from_expense_id),
            },
        )
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="expense.receipt_removed",
            entity_type="expense",
            entity_id=from_expense_id,
            actor_id=current_user.id,
            before_state={"receipt_id": str(receipt_id)},
            after_state={"receipt_id": None, "report_id": str(entry.report_id), "shared": True},
        )
        outcome = "unshared"
        unlinked_from: uuid.UUID | None = from_expense_id
    elif (
        discard_drafted_entry
        and entry is not None
        and report is not None
        # Never on a report with approval history: there a line can only go
        # through DELETE /expenses and its review-history guard (lock-scope review).
        and not report_has_approval(
            session, organization_id=report.organization_id, report_id=report.id
        )
        and entry.match_source == ExpenseMatchSource.receipt
        and session.scalar(
            sa.select(sa.func.count(Receipt.id)).where(Receipt.expense_id == entry.id)
        )
        == 1
    ):
        deleted_entry = entry.id
        snapshot, _deleted, _retained, keys = _discard_entry_evidence(
            session, entry=entry, report=report, actor=current_user, retain=False
        )
        session.delete(entry)
        session.flush()
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="expense.deleted",
            entity_type="expense",
            entity_id=deleted_entry,
            actor_id=current_user.id,
            before_state=snapshot,
            after_state=None,
        )
        outcome = "deleted_with_entry"
        unlinked_from = None
    else:
        evidence = _receipt_evidence(receipt)
        if receipt.image_url:
            keys.append(receipt.image_url)
        record_audit(
            session,
            organization_id=current_user.organization_id,
            action="receipt.deleted",
            entity_type="receipt",
            entity_id=receipt.id,
            actor_id=current_user.id,
            before_state={
                **evidence,
                "expense_id": str(entry.id) if entry is not None else None,
            },
            after_state=None,
        )
        session.delete(receipt)
        session.flush()
        if entry is not None:
            # Every line the receipt backed (its entry + shared siblings) loses it.
            for backed in backing or [entry.id]:
                record_audit(
                    session,
                    organization_id=current_user.organization_id,
                    action="expense.receipt_removed",
                    entity_type="expense",
                    entity_id=backed,
                    actor_id=current_user.id,
                    before_state={"receipt_id": str(receipt_id)},
                    after_state={"receipt_id": None, "report_id": str(entry.report_id)},
                )
        outcome = "deleted"
        unlinked_from = entry.id if entry is not None else None

    if report is not None:
        report.total_amount = _compute_total(session, report.id)
        report.updated_by = current_user.id
        refresh_presubmit_flags(session, report)
        _bump_report_version(session, report, expected_version=None, actor_id=current_user.id)
        refresh_duplicate_peers(
            session, report=report, exclude_ids={report.id}, actor_id=current_user.id
        )
    session.commit()
    return (
        ReceiptDeleteResponse(
            receipt_id=receipt_id,
            outcome=outcome,  # type: ignore[arg-type]
            expense_id=unlinked_from,
            report=_report_response(session, report) if report is not None else None,
        ),
        keys,
    )


def purge_receipt_objects(storage: Any, keys: Sequence[str]) -> None:
    """Best-effort removal of deleted receipts' stored images — call AFTER commit.

    Never before the commit (a rollback would leave rows pointing at deleted
    objects) and never failing the request: an orphaned object is acceptable.
    Logs only the exception type — never the key or a URL.
    """
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception as exc:  # noqa: BLE001 — orphaned object is acceptable
            logger.warning(
                "expense: stored receipt image could not be deleted (%s)", type(exc).__name__
            )
