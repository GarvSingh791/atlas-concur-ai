"""Persistence helpers for the Travel Agent (P4-T9, FR-T7).

apply_trip_selection    — adds a selection to the conversation's ONE draft trip
    (creates it on the first selection; UAT-2 Issue 4) via database.models (no
    backend imports).  session.flush() only; the endpoint commits.
persist_trip_selection  — thin wrapper returning the Trip.

append_assistant_message — appends an assistant ConversationMessage carrying the
    tool_calls trace and citations; only called when conversation_id is supplied
    in the payload.

LAYERING LAW: this module must never import anything from backend/.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.process_checks import selection_change
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.context import AgentContext
from ai_agents.guardrails.actions import AgentAction, check_action
from ai_agents.guardrails.citations import Citation
from ai_agents.tools.process_rules import Finding
from database.models.approval import Approval, ApprovalSubjectType
from database.models.conversation import Conversation, ConversationContextType
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trip + segment persistence
# ---------------------------------------------------------------------------


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# Planned hotel stay times (UTC): standard check-in / check-out.
_CHECK_IN_TIME = time(15, 0)
_CHECK_OUT_TIME = time(11, 0)


def _stay_nights(start: date, end: date) -> int:
    """Nights of the planned stay — min. one, the same formula as ``_estimated_cost``."""
    return max((end - start).days, 1)


def _option_to_segment(
    opt: RankedOption,
    trip_id: uuid.UUID,
    *,
    check_in: date | None = None,
    nights: int | None = None,
) -> TripSegment:
    """Build a TripSegment from a selected RankedOption.

    Hotel (Issue 6): ``amount`` is the STAY total (nightly rate x nights, the same
    figure ``_estimated_cost`` counts and a folio charge shows) and ``details``
    carries nightly_rate_usd / nights / check-in / check-out, so approval-time
    re-evaluation compares the nightly rate with the nightly cap.  With a check-in
    date the segment gets start (15:00) / end (check-out 11:00) datetimes; nights
    >= 1 keeps end >= start (CHECK end_datetime_gte_start_datetime).
    """
    if opt.segment_type == "flight":
        return TripSegment(
            trip_id=trip_id,
            segment_type=SegmentType.flight,
            supplier=opt.supplier,
            amount=opt.amount_usd,
            currency="USD",
            in_policy=opt.in_policy,
            policy_notes=opt.policy_reason if not opt.in_policy else None,
            details={
                "option_id": opt.option_id,
                "cabin_class": opt.cabin_class,
                "duration_hours": opt.duration_hours,
                "is_direct": opt.is_direct,
                "sustainability_score": opt.sustainability_score,
            },
        )
    # hotel
    rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
    stay = nights if nights is not None and nights >= 1 else 1
    details: dict[str, Any] = {
        "option_id": opt.option_id,
        "room_type": opt.room_type,
        "city": opt.city,
        "sustainability_score": opt.sustainability_score,
        "nightly_rate_usd": rate,
        "nights": stay,
    }
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if check_in is not None:
        check_out = check_in + timedelta(days=stay)
        details["check_in_date"] = check_in.isoformat()
        details["check_out_date"] = check_out.isoformat()
        start_dt = datetime.combine(check_in, _CHECK_IN_TIME, tzinfo=timezone.utc)
        end_dt = datetime.combine(check_out, _CHECK_OUT_TIME, tzinfo=timezone.utc)
    return TripSegment(
        trip_id=trip_id,
        segment_type=SegmentType.hotel,
        supplier=opt.supplier,
        location=opt.city,
        start_datetime=start_dt,
        end_datetime=end_dt,
        amount=(Decimal(str(rate)) * stay).quantize(Decimal("0.01")),
        currency="USD",
        in_policy=opt.in_policy,
        policy_notes=opt.policy_reason if not opt.in_policy else None,
        details=details,
    )


def _estimated_cost(selected: list[RankedOption], start: date, end: date) -> Decimal | None:
    """Total USD cost of the selected options (None when nothing priced was chosen).

    A flight's ``amount_usd`` is the fare; a hotel's is its NIGHTLY rate, so it is
    multiplied by the stay length (min. one night).  Without this the saved trip
    had no estimated_cost and the approval risk summary carried no amount.
    """
    if not selected:
        return None
    nights = _stay_nights(start, end)
    total = Decimal("0")
    for opt in selected:
        if opt.segment_type == "flight":
            total += Decimal(str(opt.amount_usd))
        else:
            rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
            total += Decimal(str(rate)) * nights
    return total.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# One planning conversation -> one draft trip (UAT-2 Issue 4)
# ---------------------------------------------------------------------------


class SelectionUnavailable(Exception):
    """None of the selected option ids is in the current shortlist."""


TripAction = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class TripSelectionResult:
    """What a planner selection did to the conversation's draft trip.

    ``previous_trip`` is set when the conversation already had a planner trip that
    this selection could NOT be added to — it is no longer a draft, or it is for a
    different route / departure date — and a new draft trip was started instead.

    ``end_date_change`` (old, new) is set when ONLY the return date changed and the
    draft was updated in place (UAT2 defect E).  ``stale_hotel`` is then True when
    the trip keeps a hotel leg that still covers the OLD stay (it was not
    re-selected): the hotel is never silently re-priced, the reply flags it.
    """

    trip: Trip
    action: TripAction
    added: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    previous_trip: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] = field(default_factory=dict)
    segments: tuple[dict[str, Any], ...] = ()
    end_date_change: tuple[date, date] | None = None
    stale_hotel: bool = False
    # Product gap 2 stage 2 (PTA-05): the selection changes an approved / under-review
    # trip of the same route — the new draft is linked to it (``changes_trip_id``) and
    # ``reapproval`` is the clause's finding when the estimate rose > 10% (else None).
    changes_trip_id: str | None = None
    reapproval: Finding | None = None
    reapproval_checked: bool = False


def trip_label(
    origin: Any, destination: Any, start: Any, end: Any, purpose: str | None = None
) -> str:
    """Human label of a trip: ``ORD → JFK, 2026-11-17 – 2026-11-20 (Client meeting)``."""
    start_s = start.isoformat() if isinstance(start, date) else str(start or "")
    end_s = end.isoformat() if isinstance(end, date) else str(end or "")
    when = start_s if not end_s or end_s == start_s else f"{start_s} – {end_s}"
    label = f"{origin or '?'} → {destination or '?'}, {when}"
    return f"{label} ({purpose})" if purpose else label


def _label_of(trip: Trip) -> str:
    return trip_label(
        trip.origin, trip.primary_destination, trip.start_date, trip.end_date, trip.purpose
    )


def _option_snapshot(opt: RankedOption) -> dict[str, Any]:
    return {
        "option_id": opt.option_id,
        "segment_type": opt.segment_type,
        "supplier": opt.supplier,
        "amount_usd": opt.amount_usd,
        "in_policy": opt.in_policy,
        "policy_reason": opt.policy_reason,
        "clause_refs": list(opt.clause_refs),
        "explanation": opt.explanation,
        # Product gap 2 stage 2: the AIR-06 baseline of the search this option came from
        # (server-written, read back at trip approval) and the notices shown on it.
        "lowest_fare_usd": opt.lowest_fare_usd,
        "policy_notices": [dict(n) for n in opt.policy_notices],
    }


def _select_options(shortlist: list[RankedOption], selected_ids: list[str]) -> list[RankedOption]:
    """Selected options in shortlist order, at most ONE per segment type (highest ranked)."""
    wanted = set(selected_ids)
    picked: dict[str, RankedOption] = {}
    for opt in shortlist:
        if opt.option_id not in wanted:
            continue
        if opt.segment_type in picked:
            logger.info(
                "persist_trip_selection: ignoring extra %s option %s (one per type)",
                opt.segment_type,
                opt.option_id,
            )
            continue
        picked[opt.segment_type] = opt
    return list(picked.values())


def _seg_option_id(seg: TripSegment) -> str | None:
    details = seg.details if isinstance(seg.details, dict) else {}
    raw = details.get("option_id")
    return str(raw) if raw else None


def _seg_type(seg: TripSegment) -> str:
    st = seg.segment_type
    return str(st.value if hasattr(st, "value") else st)


def _seg_details(seg: TripSegment) -> dict[str, Any]:
    return seg.details if isinstance(seg.details, dict) else {}


def _segment_rows(segments: list[TripSegment]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in segments:
        details = _seg_details(s)
        row: dict[str, Any] = {
            "segment_type": _seg_type(s),
            "supplier": s.supplier,
            "option_id": _seg_option_id(s),
            "amount": str(s.amount) if s.amount is not None else None,
            "nights": details.get("nights"),
        }
        if row["segment_type"] == "hotel":
            row["check_in_date"] = details.get("check_in_date")
            row["check_out_date"] = details.get("check_out_date")
        rows.append(row)
    return rows


def _hotel_covers_stay(seg: TripSegment, check_in: date, nights: int) -> bool:
    """True when a hotel leg already covers the planned stay (same check-in + nights)."""
    details = _seg_details(seg)
    if details.get("nights") != nights:
        return False
    recorded = details.get("check_in_date")
    return recorded is None or recorded == check_in.isoformat()


def _snapshot(trip: Trip, segments: list[TripSegment]) -> dict[str, Any]:
    """JSON-safe audit snapshot of a trip header + its segments."""

    def _d(v: Any) -> Any:
        return v.isoformat() if isinstance(v, date) else v

    return {
        "origin": trip.origin,
        "primary_destination": trip.primary_destination,
        "start_date": _d(trip.start_date),
        "end_date": _d(trip.end_date),
        "purpose": trip.purpose,
        "estimated_cost": str(trip.estimated_cost) if trip.estimated_cost is not None else None,
        "currency": trip.currency,
        "version": trip.version,
        "segments": _segment_rows(segments),
    }


def _trip_segments(session: Session, trip_id: uuid.UUID) -> list[TripSegment]:
    return list(
        session.scalars(
            sa.select(TripSegment)
            .where(TripSegment.trip_id == trip_id)
            .order_by(TripSegment.created_at, TripSegment.id)
        ).all()
    )


def _segments_cost(trip_currency: str, segments: list[TripSegment]) -> Decimal | None:
    """Sum of the segment amounts — None if anything is non-USD or unpriced."""
    if not segments or (trip_currency or "").upper() != "USD":
        return None
    total = Decimal("0")
    for seg in segments:
        if seg.amount is None or (seg.currency or "USD").upper() != "USD":
            return None
        total += Decimal(str(seg.amount))
    return total.quantize(Decimal("0.01"))


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _same_route_and_start(trip: Trip, intent: Any, start: date) -> bool:
    """Same origin, destination AND departure date — only then may a selection join
    this trip (a different return date alone is applied to it in place)."""
    return (
        _norm(trip.origin) == _norm(getattr(intent, "origin", None))
        and _norm(trip.primary_destination) == _norm(getattr(intent, "destination", None))
        and trip.start_date == start
    )


def _planner_trip_of(trip: Trip | None, conversation_id: uuid.UUID) -> bool:
    """True when the planner created ``trip`` in this conversation."""
    if trip is None or not isinstance(trip.recommendation_context, dict):
        return False
    return str(trip.recommendation_context.get("conversation_id") or "") == str(conversation_id)


def _lock_conversation(session: Session, intent: Any, context: AgentContext) -> Conversation | None:
    """The caller's own conversation row, locked FOR UPDATE (serialises selections).

    populate_existing: the router already loaded this row into the identity map;
    a request that waited on the lock must see the context_ref_id the first one
    committed, not the stale cached value.
    """
    raw = getattr(intent, "conversation_id", None)
    if not raw or context.user_id is None:
        return None
    try:
        conv_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None
    return session.scalars(
        sa.select(Conversation)
        .where(
            Conversation.id == conv_id,
            Conversation.user_id == context.user_id,
            Conversation.organization_id == context.organization_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _working_trip(session: Session, conv: Conversation, context: AgentContext) -> Trip | None:
    """The trip the planner created in THIS conversation (latest), locked FOR UPDATE.

    Trusted only when it is the caller's own trip, in the caller's org, and was
    created by the planner in this conversation (recommendation_context
    .conversation_id).  A conversation opened on a trip page (context_ref_id =
    that trip, possibly a teammate's or a hand-made draft) is therefore never
    followed into a trip the planner did not create here.
    """
    if conv.context_type != ConversationContextType.travel_planning:
        return None
    base = sa.select(Trip).where(
        Trip.organization_id == context.organization_id,
        Trip.traveler_id == context.user_id,
    )
    if conv.context_ref_id is not None:
        ref = session.scalars(
            base.where(Trip.id == conv.context_ref_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if _planner_trip_of(ref, conv.id):
            return ref
    return session.scalars(
        base.where(Trip.recommendation_context["conversation_id"].astext == str(conv.id))
        .order_by(Trip.created_at.desc(), Trip.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _approval_status_of(session: Session, trip: Trip) -> str | None:
    """The trip's approval state, if it was ever submitted (None = never submitted).

    A trip the approver returned ("request changes") goes back to ``status=draft``;
    the only sign is its approval row in ``changes_requested``.
    """
    status = session.scalar(
        sa.select(Approval.current_status)
        .where(
            Approval.organization_id == trip.organization_id,
            Approval.subject_type == ApprovalSubjectType.trip,
            Approval.subject_id == trip.id,
        )
        .order_by(Approval.created_at.desc(), Approval.id.desc())
        .limit(1)
    )
    if status is None:
        return None
    return str(status.value if hasattr(status, "value") else status)


def _editable_status(session: Session, trip: Trip) -> str | None:
    """None when the planner may add legs to ``trip``; else the state that forbids it.

    Only a never-submitted draft is editable.  A draft with an approval row was
    returned for changes (or otherwise decided) — owner decision 3: never mutate
    it; the traveler edits and resubmits it from Trips.
    """
    if trip.status != TripStatus.draft:
        return str(trip.status.value if hasattr(trip.status, "value") else trip.status)
    approval = _approval_status_of(session, trip)
    if approval is None:
        return None
    return "returned" if approval == "changes_requested" else approval


def _may_repoint(session: Session, conv: Conversation) -> bool:
    """Write context_ref_id only when it is empty or already one of THIS planner's trips."""
    if conv.context_ref_id is None:
        return True
    ref = session.get(Trip, conv.context_ref_id)
    return _planner_trip_of(ref, conv.id)


def apply_trip_selection(
    *,
    intent: Any,  # TripIntent — typed as Any to avoid circular import
    shortlist: list[RankedOption],
    selected_ids: list[str],
    reflection_outcome_value: str | None,
    context: AgentContext,
    session: Session,
) -> TripSelectionResult:
    """Add the selected option(s) to the conversation's draft trip; flush, never commit.

    One planning conversation -> one draft trip:
      * the first selection creates a draft trip and points the conversation at it;
      * a later selection with the SAME route and dates adds that leg to it, or
        replaces the leg of the same type (re-picking the same option is a no-op);
      * UAT2 defect E: when ONLY the return date changed (same route and
        departure date) the same draft is updated in place — its end date moves,
        the flight leg is kept, a re-selected hotel replaces the hotel leg with
        the nights of the new stay, and a hotel leg that was NOT re-selected is
        left as it was and flagged (``stale_hotel``), never silently re-priced;
      * if that trip is no longer a draft (incl. a draft RETURNED for changes —
        it has an approval row), or the route / departure date changed, the trip
        is never touched: a NEW draft trip is started and the conversation is
        repointed (``previous_trip`` says why).
    The conversation and trip rows are locked FOR UPDATE, so a double-click
    cannot create two trips or duplicate a leg.  Without a conversation every
    call creates a trip (legacy API path).
    """
    selected = _select_options(shortlist, selected_ids)
    if not selected:
        raise SelectionUnavailable(
            "That option is no longer available for the current trip details. "
            "Pick one of the options shown."
        )

    departure_date = _parse_date(intent.departure_date)
    if departure_date is None:
        raise ValueError("departure_date is required for trip creation")
    end_date = _parse_date(intent.return_date) or departure_date
    stay_nights = _stay_nights(departure_date, end_date)
    currency = intent.currency or "USD"

    conv = _lock_conversation(session, intent, context)
    if conv is not None and conv.context_type != ConversationContextType.travel_planning:
        conv = None  # only a planning conversation owns a working trip
    previous: dict[str, Any] | None = None
    target: Trip | None = None
    if conv is not None:
        current = _working_trip(session, conv, context)
        if current is not None:
            status = current.status.value if hasattr(current.status, "value") else current.status
            locked_state = _editable_status(session, current)
            if locked_state is not None:
                previous = {"reason": "not_draft"}
                status = locked_state
            elif not _same_route_and_start(current, intent, departure_date):
                previous = {"reason": "different_trip"}
            else:
                target = current
            if previous is not None:
                previous.update(
                    {"id": str(current.id), "status": str(status), "label": _label_of(current)}
                )

    if target is None:
        return _create_trip(
            intent=intent,
            selected=selected,
            reflection_outcome_value=reflection_outcome_value,
            context=context,
            session=session,
            conv=conv,
            previous=previous,
            departure_date=departure_date,
            end_date=end_date,
            stay_nights=stay_nights,
            currency=currency,
        )
    return _update_trip(
        trip=target,
        intent=intent,
        selected=selected,
        reflection_outcome_value=reflection_outcome_value,
        context=context,
        session=session,
        departure_date=departure_date,
        end_date=end_date,
        stay_nights=stay_nights,
    )


def _create_trip(
    *,
    intent: Any,
    selected: list[RankedOption],
    reflection_outcome_value: str | None,
    context: AgentContext,
    session: Session,
    conv: Conversation | None,
    previous: dict[str, Any] | None,
    departure_date: date,
    end_date: date,
    stay_nights: int,
    currency: str,
) -> TripSelectionResult:
    rec_ctx: dict[str, Any] = {
        "selected_option_ids": [opt.option_id for opt in selected],
        "reflection_outcome": reflection_outcome_value,
        "agent_task": "trip_planning",
        "selected_options": [_option_snapshot(opt) for opt in selected],
    }
    if conv is not None:
        rec_ctx["conversation_id"] = str(conv.id)
    if previous is not None:
        rec_ctx["related_trip_id"] = previous["id"]
    # PTA-05 (product gap 2 stage 2): an approved / under-review trip is never changed
    # in place; a selection for the SAME route becomes a new draft that CHANGES it.
    changes = _changed_trip(session, previous, intent, context)
    if changes is not None:
        rec_ctx["changes_trip_id"] = str(changes.id)

    # Option prices are USD; never store a USD sum as a cost in another currency.
    estimated_cost = (
        _estimated_cost(selected, departure_date, end_date) if currency.upper() == "USD" else None
    )
    trip = Trip(
        id=uuid.uuid4(),
        organization_id=context.organization_id,
        traveler_id=context.user_id,
        origin=intent.origin or "",
        primary_destination=intent.destination or "",
        start_date=departure_date,
        end_date=end_date,
        estimated_cost=estimated_cost,
        currency=currency,
        purpose=getattr(intent, "purpose", None),
        status=TripStatus.draft,
        recommendation_context=rec_ctx,
        created_by=context.user_id,
        updated_by=context.user_id,
    )
    session.add(trip)
    session.flush()  # populate server-side defaults (created_at, etc.)

    for opt in selected:
        session.add(_option_to_segment(opt, trip.id, check_in=departure_date, nights=stay_nights))
    session.flush()

    if conv is not None and _may_repoint(session, conv):
        conv.context_ref_id = trip.id
        session.flush()

    segments = _trip_segments(session, trip.id)
    checked, reapproval = (
        selection_change(
            session,
            organization_id=context.organization_id,
            previous=changes,
            new_estimate=trip.estimated_cost,
            new_types={opt.segment_type for opt in selected},
        )
        if changes is not None
        else (False, None)
    )
    logger.info(
        "persist_trip_selection: trip %s created for user %s (%d segment(s))",
        trip.id,
        context.user_id,
        len(segments),
    )
    return TripSelectionResult(
        trip=trip,
        action="created",
        added=tuple(opt.segment_type for opt in selected),
        previous_trip=previous,
        before=None,
        after=_snapshot(trip, segments),
        segments=tuple(_segment_rows(segments)),
        changes_trip_id=str(changes.id) if changes is not None else None,
        reapproval=reapproval,
        reapproval_checked=checked,
    )


# Trip states whose approved estimate a change must be compared with (PTA-05).
_CHANGEABLE_STATES = frozenset({"approved", "pending_approval"})


def _changed_trip(
    session: Session, previous: dict[str, Any] | None, intent: Any, context: AgentContext
) -> Trip | None:
    """The approved / under-review trip a new draft CHANGES (PTA-05), else None.

    Only when the planner refused to change the conversation's trip because it is
    approved or awaiting approval AND the new selection is for the same route (origin
    and destination): a different route is another trip, not a change.
    """
    if previous is None or previous.get("reason") != "not_draft":
        return None
    if str(previous.get("status")) not in _CHANGEABLE_STATES:
        return None
    try:
        prior_id = uuid.UUID(str(previous.get("id")))
    except (ValueError, TypeError):
        return None
    prior = session.scalars(
        sa.select(Trip).where(
            Trip.id == prior_id,
            Trip.organization_id == context.organization_id,
            Trip.traveler_id == context.user_id,
        )
    ).first()
    if prior is None:
        return None
    same_route = _norm(prior.origin) == _norm(getattr(intent, "origin", None)) and _norm(
        prior.primary_destination
    ) == _norm(getattr(intent, "destination", None))
    return prior if same_route else None


def _update_trip(
    *,
    trip: Trip,
    intent: Any,
    selected: list[RankedOption],
    reflection_outcome_value: str | None,
    context: AgentContext,
    session: Session,
    departure_date: date,
    end_date: date,
    stay_nights: int,
) -> TripSelectionResult:
    existing = _trip_segments(session, trip.id)
    before = _snapshot(trip, existing)
    # Only the return date changed (the caller checked route + departure date).
    old_end = trip.end_date
    end_change = (old_end, end_date) if isinstance(old_end, date) and old_end != end_date else None
    to_add: list[RankedOption] = []
    to_delete: list[TripSegment] = []
    added: list[str] = []
    replaced: list[str] = []
    for opt in selected:
        same_type = [s for s in existing if _seg_type(s) == opt.segment_type]
        if (
            len(same_type) == 1
            and _seg_option_id(same_type[0]) == opt.option_id
            and (
                opt.segment_type != "hotel"
                or _hotel_covers_stay(same_type[0], departure_date, stay_nights)
            )
        ):
            continue  # already on the trip (a hotel: for this very stay)
        to_delete.extend(same_type)
        to_add.append(opt)
        (replaced if same_type else added).append(opt.segment_type)

    if not to_add and end_change is None:
        return TripSelectionResult(
            trip=trip,
            action="unchanged",
            before=before,
            after=before,
            segments=tuple(_segment_rows(existing)),
        )

    # Changing an existing trip is an UPDATE: the agent must not be a wider write
    # path than PATCH /trips (trip.update.own).
    check_action(context, AgentAction.TRIP_UPDATE)

    for seg in to_delete:
        session.delete(seg)
    session.flush()
    for opt in to_add:
        session.add(_option_to_segment(opt, trip.id, check_in=departure_date, nights=stay_nights))
    session.flush()
    session.expire(trip, ["segments"])

    segments = _trip_segments(session, trip.id)
    old_ctx = trip.recommendation_context if isinstance(trip.recommendation_context, dict) else {}
    old_snaps = {
        str(s.get("option_id")): s
        for s in (old_ctx.get("selected_options") or [])
        if isinstance(s, dict)
    }
    new_snaps = {opt.option_id: _option_snapshot(opt) for opt in selected}
    current_ids = [oid for oid in (_seg_option_id(s) for s in segments) if oid]
    # A NEW dict — the JSONB column has no MutableDict, in-place edits are lost.
    trip.recommendation_context = {
        **old_ctx,
        "selected_option_ids": current_ids,
        "selected_options": [
            new_snaps.get(oid) or old_snaps.get(oid) or {"option_id": oid} for oid in current_ids
        ],
        "reflection_outcome": reflection_outcome_value,
    }
    trip.estimated_cost = _segments_cost(trip.currency, segments)
    if end_change is not None:
        trip.end_date = end_date
    purpose = getattr(intent, "purpose", None)
    if purpose:
        trip.purpose = purpose
    trip.version = (trip.version or 1) + 1
    trip.updated_by = context.user_id
    session.flush()

    # A hotel leg kept from before a return-date change still covers the OLD
    # stay: flag it (the reply says so) rather than silently re-pricing it.
    stale_hotel = end_change is not None and any(
        _seg_type(s) == "hotel" and not _hotel_covers_stay(s, departure_date, stay_nights)
        for s in segments
    )

    logger.info(
        "persist_trip_selection: trip %s updated (added=%s replaced=%s end_date=%s)",
        trip.id,
        added,
        replaced,
        end_change,
    )
    return TripSelectionResult(
        trip=trip,
        action="updated",
        added=tuple(added),
        replaced=tuple(replaced),
        before=before,
        after=_snapshot(trip, segments),
        segments=tuple(_segment_rows(segments)),
        end_date_change=end_change,
        stale_hotel=stale_hotel,
    )


def persist_trip_selection(
    *,
    intent: Any,  # TripIntent — typed as Any to avoid circular import
    shortlist: list[RankedOption],
    selected_ids: list[str],
    reflection_outcome_value: str | None,
    context: AgentContext,
    session: Session,
) -> Trip:
    """Write Trip + TripSegments; session.flush(); never commits.

    Thin wrapper over :func:`apply_trip_selection` returning the trip.  Without a
    conversation id every call creates a new draft trip.
    """
    return apply_trip_selection(
        intent=intent,
        shortlist=shortlist,
        selected_ids=selected_ids,
        reflection_outcome_value=reflection_outcome_value,
        context=context,
        session=session,
    ).trip


# ---------------------------------------------------------------------------
# Conversation message persistence
# ---------------------------------------------------------------------------


def load_prior_intent(*, conversation_id: str, session: Session) -> dict[str, Any] | None:
    """Return the intent slots recorded on this conversation's latest planner turn.

    Every TravelAgent turn stores ``tool_calls["intent"]`` on its assistant
    message; the next turn merges the user's new message onto it, so a bare
    answer to a clarify question ("Chicago (ORD)") keeps the slots already given.
    Returns None when there is no prior planner turn.
    """
    conv_uuid = uuid.UUID(conversation_id)
    rows = session.scalars(
        sa.select(ConversationMessage.tool_calls)
        .where(
            ConversationMessage.conversation_id == conv_uuid,
            ConversationMessage.role == MessageRole.assistant,
        )
        .order_by(ConversationMessage.sequence.desc())
        .limit(10)
    ).all()
    for tool_calls in rows:
        if isinstance(tool_calls, dict) and isinstance(tool_calls.get("intent"), dict):
            return dict(tool_calls["intent"])
    return None


def append_assistant_message(
    *,
    conversation_id: str,
    content: str,
    tool_calls_trace: dict[str, Any],
    citations: tuple[Citation, ...],
    session: Session,
) -> ConversationMessage:
    """Append an assistant ConversationMessage at the next sequence; no commit."""
    conv_uuid = uuid.UUID(conversation_id)

    max_seq: int | None = session.scalar(
        sa.select(sa.func.max(ConversationMessage.sequence)).where(
            ConversationMessage.conversation_id == conv_uuid
        )
    )
    next_seq = (max_seq or 0) + 1

    msg = ConversationMessage(
        conversation_id=conv_uuid,
        role=MessageRole.assistant,
        content=content,
        tool_calls=tool_calls_trace or None,
        citations=({"citations": [c.to_dict() for c in citations]} if citations else None),
        sequence=next_seq,
    )
    session.add(msg)
    session.flush()
    return msg
