"""Derived notifications (product gap 3 — the app-shell bell).

There is no event bus and no notifications table: the caller's feed is DERIVED on
every request from workflow state that already exists, and only the per-user
read state is persisted (``notification_reads``).

Sources (each a small, indexed, LIMITed query; nothing outside the caller's
scope is ever read):

* approvers (``approval.read``) — PENDING approvals routed to the caller
  (``risk_summary.routed_to_approver_id``) inside their ``scope_query`` read
  scope, never their own submissions → ``approval_pending``;
* finance admins — PENDING approvals escalated to human review
  (``risk_summary.route == "escalate"``) in the org, changed in the last 30
  days, not routed to them and not their own → ``approval_escalated``;
* owners — decisions (approval steps) on THEIR OWN submissions in the last 30
  days → ``approved`` / ``rejected`` / ``returned``; their reports that are
  ``reimbursed`` and trips that are ``completed`` (status changed in the last
  30 days) → ``reimbursed`` / ``completed``.  Both are set by the lifecycle
  track (finance reimburse; traveler / finance completion and the elapsed-trip
  sweep); a reimbursement is dated by ``reimbursed_at`` and names its reference.

Keys are deterministic so the same event keeps the same id across requests; a
re-queued approval (returned → resubmitted) gets a NEW key because the key
includes the approval's step count, so a resubmission notifies again.

The merged feed is newest-first (ties broken by key, so ordering is stable) and
capped at ``FEED_CAP`` items.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import get_user_permissions, scope_query
from backend.app.modules.approval.grade_routing import approval_scope
from backend.app.modules.notifications.schemas import NotificationItem
from config.constants.permissions import APPROVAL_READ
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import RoleKey, User
from database.models.notification_read import NOTIFICATION_KEY_MAX_LENGTH, NotificationRead
from database.models.trip import Trip, TripStatus

FEED_CAP = 50
WINDOW = timedelta(days=30)
_BODY_MAX = 200

_DECISION_KIND: dict[ApprovalDecision, str] = {
    ApprovalDecision.approve: "approved",
    ApprovalDecision.auto_approve: "approved",
    ApprovalDecision.reject: "rejected",
    ApprovalDecision.request_changes: "returned",
}


@dataclass(frozen=True)
class _Item:
    key: str
    kind: str
    title: str
    body: str
    subject_type: str
    subject_id: uuid.UUID
    link: str
    created_at: datetime


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _BODY_MAX else text[: _BODY_MAX - 1] + "…"


def _trip_title(trip: Trip) -> str:
    return f"{trip.origin} → {trip.primary_destination}"


def report_link(report_id: uuid.UUID) -> str:
    return f"/expenses?report={report_id}"


def trip_link(trip_id: uuid.UUID) -> str:
    return f"/trips?trip={trip_id}"


def approval_link(approval_id: uuid.UUID) -> str:
    return f"/approvals?approval={approval_id}"


def _step_counts(session: Session, approval_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not approval_ids:
        return {}
    rows = session.execute(
        sa.select(ApprovalStep.approval_id, sa.func.count())
        .where(ApprovalStep.approval_id.in_(approval_ids))
        .group_by(ApprovalStep.approval_id)
    ).all()
    return {aid: int(n) for aid, n in rows}


def _subject_titles(
    session: Session, organization_id: uuid.UUID, approvals: list[Approval]
) -> dict[uuid.UUID, str]:
    """approval id → subject display title (tenant-guarded, batched)."""
    report_ids = [
        a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.expense_report
    ]
    trip_ids = [a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.trip]
    titles: dict[uuid.UUID, str] = {}
    if report_ids:
        for rid, title in session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.title).where(
                ExpenseReport.id.in_(report_ids), ExpenseReport.organization_id == organization_id
            )
        ).all():
            titles[rid] = title
    if trip_ids:
        for trip in session.scalars(
            sa.select(Trip).where(Trip.id.in_(trip_ids), Trip.organization_id == organization_id)
        ):
            titles[trip.id] = _trip_title(trip)
    return {
        a.id: titles.get(a.subject_id, "Untitled")
        for a in approvals
        if a.subject_id in titles  # a dangling subject is skipped (fail closed)
    }


def _submitter_names(session: Session, approvals: list[Approval]) -> dict[uuid.UUID, str]:
    ids = {a.submitter_id for a in approvals}
    if not ids:
        return {}
    return {
        uid: name
        for uid, name in session.execute(sa.select(User.id, User.name).where(User.id.in_(ids)))
    }


def _approval_items(
    session: Session, user: User, approvals: list[Approval], *, kind: str, title: str
) -> list[_Item]:
    counts = _step_counts(session, [a.id for a in approvals])
    subjects = _subject_titles(session, user.organization_id, approvals)
    names = _submitter_names(session, approvals)
    items: list[_Item] = []
    for a in approvals:
        subject = subjects.get(a.id)
        if subject is None:
            continue
        who = names.get(a.submitter_id, "Someone")
        noun = "trip" if a.subject_type == ApprovalSubjectType.trip else "expense report"
        items.append(
            _Item(
                key=f"{kind}:{a.id}:{counts.get(a.id, 0)}",
                kind=kind,
                title=title,
                body=_clip(f"{who}'s {noun}: {subject}"),
                subject_type="approval",
                subject_id=a.id,
                link=approval_link(a.id),
                created_at=a.updated_at,
            )
        )
    return items


def _pending_for_approver(session: Session, user: User) -> list[_Item]:
    # Follow-up (grades): approval_scope also reaches an approval routed to a manager
    # by grade (an SVP above the submitter's direct manager).
    stmt = approval_scope(
        sa.select(Approval).where(
            Approval.current_status == ApprovalStatus.pending,
            Approval.submitter_id != user.id,
            Approval.risk_summary["routed_to_approver_id"].astext == str(user.id),
        ),
        user=user,
        session=session,
    )
    approvals = list(
        session.scalars(stmt.order_by(Approval.updated_at.desc(), Approval.id).limit(FEED_CAP))
    )
    return _approval_items(
        session, user, approvals, kind="approval_pending", title="Approval waiting on you"
    )


def _escalated_for_finance(session: Session, user: User, since: datetime) -> list[_Item]:
    routed = Approval.risk_summary["routed_to_approver_id"].astext
    stmt = scope_query(
        sa.select(Approval).where(
            Approval.current_status == ApprovalStatus.pending,
            Approval.submitter_id != user.id,
            Approval.risk_summary["route"].astext == "escalate",
            sa.or_(routed.is_(None), routed != str(user.id)),
            Approval.updated_at >= since,
        ),
        user=user,
        ownership_column=Approval.submitter_id,
        org_column=Approval.organization_id,
        session=session,
    )
    approvals = list(
        session.scalars(stmt.order_by(Approval.updated_at.desc(), Approval.id).limit(FEED_CAP))
    )
    return _approval_items(
        session, user, approvals, kind="approval_escalated", title="Escalated for review"
    )


def _decisions_for_owner(session: Session, user: User, since: datetime) -> list[_Item]:
    rows = session.execute(
        sa.select(ApprovalStep, Approval)
        .join(Approval, Approval.id == ApprovalStep.approval_id)
        .where(
            Approval.organization_id == user.organization_id,
            Approval.submitter_id == user.id,
            ApprovalStep.created_at >= since,
            ApprovalStep.decision.in_(list(_DECISION_KIND)),
        )
        .order_by(ApprovalStep.created_at.desc(), ApprovalStep.id)
        .limit(FEED_CAP)
    ).all()
    if not rows:
        return []
    approvals = [a for _s, a in rows]
    # Ownership re-check on the live subject (fail closed): only subjects the
    # caller owns in their own org produce an item.
    report_ids = [
        a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.expense_report
    ]
    trip_ids = [a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.trip]
    owned_reports: dict[uuid.UUID, str] = {}
    if report_ids:
        owned_reports = {
            rid: title
            for rid, title in session.execute(
                sa.select(ExpenseReport.id, ExpenseReport.title).where(
                    ExpenseReport.id.in_(report_ids),
                    ExpenseReport.organization_id == user.organization_id,
                    ExpenseReport.owner_id == user.id,
                )
            )
        }
    owned_trips = (
        {
            t.id: _trip_title(t)
            for t in session.scalars(
                sa.select(Trip).where(
                    Trip.id.in_(trip_ids),
                    Trip.organization_id == user.organization_id,
                    Trip.traveler_id == user.id,
                )
            )
        }
        if trip_ids
        else {}
    )
    items: list[_Item] = []
    for step, approval in rows:
        is_trip = approval.subject_type == ApprovalSubjectType.trip
        subject = (owned_trips if is_trip else owned_reports).get(approval.subject_id)
        if subject is None:
            continue
        kind = _DECISION_KIND[step.decision]
        noun = "Trip" if is_trip else "Expense report"
        title = {
            "approved": (
                f"{noun} auto-approved"
                if step.decision == ApprovalDecision.auto_approve
                else f"{noun} approved"
            ),
            "rejected": f"{noun} rejected",
            "returned": f"{noun} returned for changes",
        }[kind]
        body = subject
        if step.reason and kind != "approved":
            body = f"{subject} — {step.reason}"
        items.append(
            _Item(
                key=f"decision:{step.id}",
                kind=kind,
                title=title,
                body=_clip(body),
                subject_type="trip" if is_trip else "expense_report",
                subject_id=approval.subject_id,
                link=trip_link(approval.subject_id)
                if is_trip
                else report_link(approval.subject_id),
                created_at=step.created_at,
            )
        )
    return items


def _closed_for_owner(session: Session, user: User, since: datetime) -> list[_Item]:
    items: list[_Item] = []
    # Product-gaps integration: the lifecycle track records WHEN a report was
    # reimbursed (``reimbursed_at``, 0016) — that is the event time; a row without
    # it (pre-0016 data) falls back to ``updated_at``.
    paid_at = sa.func.coalesce(ExpenseReport.reimbursed_at, ExpenseReport.updated_at)
    for report in session.scalars(
        sa.select(ExpenseReport)
        .where(
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id == user.id,
            ExpenseReport.status == ExpenseReportStatus.reimbursed,
            paid_at >= since,
        )
        .order_by(paid_at.desc(), ExpenseReport.id)
        .limit(FEED_CAP)
    ):
        reference = report.reimbursement_reference
        body = f"{report.title} · {reference}" if reference else report.title
        happened = report.reimbursed_at
        items.append(
            _Item(
                key=f"report_reimbursed:{report.id}",
                kind="reimbursed",
                title="Expense report reimbursed",
                body=_clip(body),
                subject_type="expense_report",
                subject_id=report.id,
                link=report_link(report.id),
                created_at=happened if isinstance(happened, datetime) else report.updated_at,
            )
        )
    for trip in session.scalars(
        sa.select(Trip)
        .where(
            Trip.organization_id == user.organization_id,
            Trip.traveler_id == user.id,
            Trip.status == TripStatus.completed,
            Trip.updated_at >= since,
        )
        .order_by(Trip.updated_at.desc(), Trip.id)
        .limit(FEED_CAP)
    ):
        items.append(
            _Item(
                key=f"trip_completed:{trip.id}",
                kind="completed",
                title="Trip completed",
                body=_clip(_trip_title(trip)),
                subject_type="trip",
                subject_id=trip.id,
                link=trip_link(trip.id),
                created_at=trip.updated_at,
            )
        )
    return items


def _derive(session: Session, user: User) -> list[_Item]:
    """The caller's capped, newest-first feed (unique keys)."""
    since = datetime.now(timezone.utc) - WINDOW
    items: list[_Item] = []
    if APPROVAL_READ in get_user_permissions(user, session):
        items.extend(_pending_for_approver(session, user))
        if user.role == RoleKey.finance_admin:
            items.extend(_escalated_for_finance(session, user, since))
    items.extend(_decisions_for_owner(session, user, since))
    items.extend(_closed_for_owner(session, user, since))
    unique: dict[str, _Item] = {}
    for item in items:
        unique.setdefault(item.key, item)
    ordered = sorted(unique.values(), key=lambda i: i.key)
    ordered.sort(key=lambda i: i.created_at, reverse=True)
    return ordered[:FEED_CAP]


def _read_keys(session: Session, user: User, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return set(
        session.scalars(
            sa.select(NotificationRead.notification_key).where(
                NotificationRead.user_id == user.id,
                NotificationRead.notification_key.in_(keys),
            )
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_notifications(
    session: Session, user: User, *, offset: int, limit: int
) -> tuple[list[NotificationItem], int]:
    """Return (page of items, total in the capped feed)."""
    feed = _derive(session, user)
    page = feed[offset : offset + limit]
    read = _read_keys(session, user, [i.key for i in page])
    return (
        [
            NotificationItem(
                id=i.key,
                kind=i.kind,  # type: ignore[arg-type]
                title=i.title,
                body=i.body,
                subject_type=i.subject_type,  # type: ignore[arg-type]
                subject_id=i.subject_id,
                link=i.link,
                created_at=i.created_at,
                read=i.key in read,
            )
            for i in page
        ],
        len(feed),
    )


def unread_count(session: Session, user: User) -> int:
    feed = _derive(session, user)
    keys = [i.key for i in feed]
    return len(keys) - len(_read_keys(session, user, keys))


def _insert_reads(session: Session, user: User, keys: list[str]) -> None:
    if not keys:
        return
    session.execute(
        pg_insert(NotificationRead)
        .values([{"user_id": user.id, "notification_key": k} for k in keys])
        .on_conflict_do_nothing(index_elements=["user_id", "notification_key"])
    )


def mark_read(session: Session, user: User, key: str) -> int:
    """Mark ONE of the caller's current notifications read (404 for any other key)."""
    if not key or len(key) > NOTIFICATION_KEY_MAX_LENGTH:
        raise NotFoundError("Notification not found.")
    if key not in {i.key for i in _derive(session, user)}:
        raise NotFoundError("Notification not found.")
    _insert_reads(session, user, [key])
    session.commit()
    return 1


def mark_all_read(session: Session, user: User) -> int:
    """Mark every item in the caller's current feed read; prune stale read rows.

    Pruning removes only the caller's read rows older than the window whose key
    is no longer in the feed (it can never be shown again), so the table stays
    bounded per user without ever resurrecting a visible item as unread.
    """
    keys = [i.key for i in _derive(session, user)]
    already = _read_keys(session, user, keys)
    fresh = [k for k in keys if k not in already]
    _insert_reads(session, user, fresh)
    prune = sa.delete(NotificationRead).where(
        NotificationRead.user_id == user.id,
        NotificationRead.read_at < datetime.now(timezone.utc) - WINDOW,
    )
    if keys:
        prune = prune.where(NotificationRead.notification_key.notin_(keys))
    session.execute(prune)
    session.commit()
    return len(fresh)
