"""Travel service — business logic for trips + trip_segments.

Security invariants:
  1. Every query is scope_query'd before execution — tenant isolation first, then role.
  2. Segments are only reachable through a scope-resolved parent trip (no direct queries).
  3. Illegal state transitions are rejected before any mutation; no audit row is written.
  4. All audit writes go through record_audit() in the same transaction as the mutation.
  5. Mutations are OWNER-ONLY for every role (C7-01): read scope finds the record
     (404 when unreadable), require_owner then rejects a non-owner with 403.
  6. approved/rejected are approval-workflow outcomes, never owner PATCH targets
     (C7-02) — the traveler cannot approve their own trip.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.concurrency import guarded_version_update
from backend.app.core.errors import ConflictError, ForbiddenError, NotFoundError
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.core.rbac import narrow_to_caller, require_owner, scope_query
from backend.app.modules.approval.lifecycle import (
    open_approval_on_submission,
    owner_approval_info,
    subject_under_human_rereview,
)
from backend.app.modules.travel.schemas import (
    SegmentResponse,
    TripCreate,
    TripListItem,
    TripResponse,
)
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment

# ---------------------------------------------------------------------------
# Legal state-transition map (single source of truth for PATCH)
# ---------------------------------------------------------------------------

LEGAL_TRANSITIONS: dict[TripStatus, frozenset[TripStatus]] = {
    TripStatus.draft: frozenset({TripStatus.pending_approval, TripStatus.cancelled}),
    TripStatus.pending_approval: frozenset(
        {TripStatus.approved, TripStatus.rejected, TripStatus.cancelled}
    ),
    TripStatus.approved: frozenset({TripStatus.completed, TripStatus.cancelled}),
    TripStatus.rejected: frozenset(),
    TripStatus.completed: frozenset(),
    TripStatus.cancelled: frozenset(),
}

# Targets that represent an approval DECISION.  They stay in LEGAL_TRANSITIONS
# (so the state machine documents them and illegal moves keep returning 409), but
# the owner PATCH endpoint may never set them: decisions are made only through the
# approval workflow (POST/PATCH /approvals) by someone other than the traveler.
APPROVER_ONLY_TARGETS: frozenset[TripStatus] = frozenset({TripStatus.approved, TripStatus.rejected})

SUBJECT_UNDER_REVIEW = "SUBJECT_UNDER_REVIEW"
TRIP_NOT_ENDED = "TRIP_NOT_ENDED"


def today_utc() -> date:
    """The server's calendar date (UTC) — the clock trip completion is judged by."""
    return datetime.now(timezone.utc).date()


def refuse_unfinished_trip(trip: Trip) -> None:
    """409 ``TRIP_NOT_ENDED`` unless *trip* has ended (end_date <= today, UTC).

    Product gap 8: approved -> completed records that the travel happened; a
    trip that has not ended yet cannot be completed (fail closed on a missing
    end date).
    """
    end = trip.end_date
    if not isinstance(end, date) or end > today_utc():
        raise ConflictError(
            "A trip can be marked completed only after its end date.", code=TRIP_NOT_ENDED
        )


# ---------------------------------------------------------------------------
# Allow-listed filter / sort fields (deny-by-default)
# ---------------------------------------------------------------------------

TRIP_FILTER_FIELDS: set[str] = {"status", "origin", "primary_destination", "traveler_id"}
TRIP_SORT_FIELDS: set[str] = {
    "start_date",
    "end_date",
    "status",
    "origin",
    "primary_destination",
    "created_at",
    "updated_at",
}

_FILTER_COLUMNS = {
    "origin": Trip.origin,
    "primary_destination": Trip.primary_destination,
}


def _apply_filters(stmt: sa.Select, filters: dict[str, str]) -> sa.Select:
    from backend.app.core.errors import BadRequestError

    for field, value in filters.items():
        if field == "status":
            try:
                stmt = stmt.where(Trip.status == TripStatus(value))
            except ValueError as exc:
                valid = ", ".join(s.value for s in TripStatus)
                raise BadRequestError(f"Invalid status '{value}'. Valid: {valid}.") from exc
        elif field == "traveler_id":
            try:
                stmt = stmt.where(Trip.traveler_id == uuid.UUID(value))
            except ValueError as exc:
                raise BadRequestError(f"Invalid traveler_id '{value}'.") from exc
        else:
            col = _FILTER_COLUMNS[field]
            stmt = stmt.where(col == value)
    return stmt


def _apply_sort(stmt: sa.Select, sort: list[tuple[str, str]]) -> sa.Select:
    for field, direction in sort:
        col = getattr(Trip, field)
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    return stmt


def _trip_snapshot(trip: Trip) -> dict:
    """Serialisable snapshot for audit before/after state."""
    return {
        "status": trip.status.value,
        "version": trip.version,
        "traveler_id": str(trip.traveler_id),
        "origin": trip.origin,
        "primary_destination": trip.primary_destination,
    }


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------


def _trip_response(session: Session, trip: Trip) -> TripResponse:
    """Serialize a trip with its traveler-visible approval status + reason (C7R).

    Also carries the traveler's display name (UAT2) so a reader who is not the
    traveler (a manager via the team read scope) can be told whose trip it is.
    """
    response = TripResponse.model_validate(trip)
    response.traveler_name = session.scalar(
        sa.select(User.name).where(
            User.id == trip.traveler_id, User.organization_id == trip.organization_id
        )
    )
    info = owner_approval_info(session, ApprovalSubjectType.trip, [trip], with_reason=True).get(
        trip.id
    )
    if info is not None:
        response.approval_status, response.approval_reason = info
    return response


def create_trip(session: Session, current_user: User, payload: TripCreate) -> TripResponse:
    """Persist a new trip (draft) + any segments, audit, commit → TripResponse."""
    trip = Trip(
        organization_id=current_user.organization_id,
        traveler_id=current_user.id,
        origin=payload.origin,
        primary_destination=payload.primary_destination,
        start_date=payload.start_date,
        end_date=payload.end_date,
        purpose=payload.purpose,
        estimated_cost=payload.estimated_cost,
        budget_amount=payload.budget_amount,
        currency=payload.currency,
        status=TripStatus.draft,
        recommendation_context=None,
        created_by=current_user.id,
        updated_by=current_user.id,
    )
    session.add(trip)
    session.flush()  # populate trip.id for segments + audit

    for seg_data in payload.segments:
        segment = TripSegment(
            trip_id=trip.id,
            segment_type=seg_data.segment_type,
            supplier=seg_data.supplier,
            start_datetime=seg_data.start_datetime,
            end_datetime=seg_data.end_datetime,
            location=seg_data.location,
            amount=seg_data.amount,
            currency=seg_data.currency,
            in_policy=seg_data.in_policy,
            policy_notes=seg_data.policy_notes,
            details=seg_data.details,
        )
        session.add(segment)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="trip.created",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=current_user.id,
        before_state=None,
        after_state=_trip_snapshot(trip),
    )

    session.commit()
    return TripResponse.model_validate(trip)


def list_trips(
    session: Session,
    current_user: User,
    pagination: PaginationParams,
    raw_filters: dict[str, str],
    raw_sort: str | None,
    *,
    mine: bool = False,
) -> Page[TripListItem]:
    """Return a paginated, scoped list of trips visible to *current_user*.

    ``mine=True`` (the personal "My Trips" page) narrows the role's read scope to
    the trips *current_user* travels on; the default keeps the full read scope.
    """
    filters = parse_filters(raw_filters, TRIP_FILTER_FIELDS)
    sort = parse_sort(raw_sort, TRIP_SORT_FIELDS)

    base = sa.select(Trip)
    base = scope_query(
        base,
        user=current_user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    base = narrow_to_caller(base, user=current_user, ownership_column=Trip.traveler_id, mine=mine)
    base = _apply_filters(base, filters)

    total: int = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0

    if sort:
        base = _apply_sort(base, sort)
    else:
        base = base.order_by(Trip.created_at.desc())

    base = base.offset(pagination.offset).limit(pagination.limit)
    rows = session.scalars(base).all()

    info = owner_approval_info(session, ApprovalSubjectType.trip, rows, with_reason=True)
    items = []
    for t in rows:
        item = TripListItem.model_validate(t)
        if t.id in info:
            item.approval_status, item.approval_reason = info[t.id]
        items.append(item)
    return Page.create(
        items=items,
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


def get_trip(session: Session, current_user: User, trip_id: uuid.UUID) -> TripResponse:
    """Return a single trip visible to *current_user*, or 404.

    Out-of-scope and nonexistent ids both return 404 — no existence leak.
    """
    stmt = sa.select(Trip).where(Trip.id == trip_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    trip = session.scalars(stmt).first()
    if trip is None:
        raise NotFoundError("Trip not found.")
    return _trip_response(session, trip)


def get_trip_segments(
    session: Session, current_user: User, trip_id: uuid.UUID
) -> list[SegmentResponse]:
    """Return segments for a trip visible to *current_user*, or 404.

    Segments are only reachable through a scope-resolved parent trip.
    """
    # Resolve parent trip first — raises 404 if out of scope or nonexistent.
    stmt = sa.select(Trip).where(Trip.id == trip_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    trip = session.scalars(stmt).first()
    if trip is None:
        raise NotFoundError("Trip not found.")

    segs = session.scalars(
        sa.select(TripSegment)
        .where(TripSegment.trip_id == trip_id)
        .order_by(TripSegment.created_at)
    ).all()
    return [SegmentResponse.model_validate(s) for s in segs]


def transition_trip(
    session: Session,
    current_user: User,
    trip_id: uuid.UUID,
    target_status: TripStatus,
) -> TripResponse:
    """Validate + apply a status transition, audit, commit → TripResponse.

    Unreadable/nonexistent → 404.  Readable but not owned (manager on a direct
    report's trip, finance org-wide) → 403 (C7-01).  Illegal transitions → 409
    ConflictError.  Legal but approver-only targets (approved/rejected) → 403
    (C7-02).  No state change and no audit row on any rejected path.
    """
    stmt = sa.select(Trip).where(Trip.id == trip_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    # Row lock: serialise against a concurrent approval decision writing the
    # trip outcome (approval.service.decide_approval locks the same row).
    trip = session.scalars(stmt.with_for_update(of=Trip)).first()
    if trip is None:
        raise NotFoundError("Trip not found.")

    # Write scope (C7-01): readable is not writable — only the traveler mutates.
    require_owner(trip.traveler_id, user=current_user)

    # Validate transition BEFORE any mutation (fail fast, no state change on illegal path)
    if target_status not in LEGAL_TRANSITIONS[trip.status]:
        raise ConflictError(
            f"Cannot transition trip from '{trip.status.value}' to '{target_status.value}'."
        )

    # C7-02: approve/reject are approval-workflow decisions, never a traveler PATCH.
    if target_status in APPROVER_ONLY_TARGETS:
        raise ForbiddenError("Trips are approved or rejected only through the approval workflow.")

    # Product gap 8: completing a trip is only possible once it has ended.
    if target_status == TripStatus.completed:
        refuse_unfinished_trip(trip)

    # C7R SoD: a trip a reviewer sent back (or that awaits their re-decision) cannot be
    # cancelled — it would launder the return and strand an approval nobody can decide.
    if target_status == TripStatus.cancelled and subject_under_human_rereview(
        session,
        organization_id=trip.organization_id,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
    ):
        raise ConflictError(
            "This trip is with a reviewer who returned it. Fix and resubmit it, or wait for "
            "their decision, before cancelling.",
            code=SUBJECT_UNDER_REVIEW,
        )

    before = _trip_snapshot(trip)

    trip.status = target_status
    trip.version = trip.version + 1
    trip.updated_by = current_user.id

    after = _trip_snapshot(trip)

    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="trip.status_changed",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=current_user.id,
        before_state=before,
        after_state=after,
    )

    # C7R: submitting opens the trip's approval in the same transaction — the
    # deterministic Approval Agent auto-approves or escalates it to an
    # independent approver; resubmitting a trip the approver returned (changes
    # requested) re-queues its existing approval instead.  An auto-approval
    # moves the trip to approved (actor None — the agent decided).
    if target_status == TripStatus.pending_approval:
        session.flush()
        approval = open_approval_on_submission(
            session,
            organization_id=trip.organization_id,
            subject_type=ApprovalSubjectType.trip,
            subject_id=trip.id,
            actor=current_user,
        )
        if approval.current_status == ApprovalStatus.auto_approved:
            apply_workflow_status(session, trip, TripStatus.approved, actor_id=None)

    session.commit()
    return _trip_response(session, trip)


def apply_workflow_status(
    session: Session, trip: Trip, target: TripStatus, *, actor_id: uuid.UUID | None
) -> None:
    """Apply an approval-workflow outcome to *trip* (flush-only; caller commits).

    CAS the trip to *target* (no-op when already there) + one
    ``trip.status_changed`` audit row; ``actor_id`` None = the agent decided.
    Authorization is the approval workflow's job (C7-02).
    """
    if trip.status == target:
        return
    before = {"status": trip.status.value, "version": trip.version}
    guarded_version_update(
        session,
        Trip,
        entity_id=trip.id,
        expected_version=trip.version,
        values={"status": target, "updated_by": actor_id},
    )
    session.refresh(trip)
    record_audit(
        session,
        organization_id=trip.organization_id,
        action="trip.status_changed",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=actor_id,
        before_state=before,
        after_state={"status": trip.status.value, "version": trip.version},
    )
