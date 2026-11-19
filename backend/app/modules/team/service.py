"""Team service (Product gap 5, PRD US-M3) — direct reports + their T&E posture.

"Team" has ONE definition, the existing manager read scope:
  * manager       → ``rbac.direct_report_ids`` (one level, same org) — the very
                    helper scope_query uses, so the Team page can never show a
                    person whose trips/reports the manager could not read;
  * finance_admin → every user in the organization (finance's org-wide read
                    scope, the same as scope_query's finance branch);
  * any other role → 403 (fail closed; employees are also refused at the router).

Every aggregate is filtered by organization_id AND the member id set.  A member
outside the caller's team is a 404 (no existence leak).  The drill-down lists
reuse the existing trip / report list services (scope_query + an owner filter),
so the items are exactly what /trips and /expense-reports would return.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.core.rbac import direct_report_ids
from backend.app.modules.expense import service as expense_service
from backend.app.modules.team.schemas import TeamMemberDetail, TeamMemberSummary
from backend.app.modules.travel import service as travel_service
from database.models.approval import Approval, ApprovalStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.trip import Trip, TripStatus

TEAM_FILTER_FIELDS: set[str] = {"q"}
TEAM_SORT_FIELDS: set[str] = {
    "name",
    "department",
    "open_trips",
    "pending_approvals",
    "spend_mtd",
    "spend_90d",
}

OPEN_TRIP_STATUSES = (TripStatus.draft, TripStatus.pending_approval, TripStatus.approved)
SPEND_STATUSES = (ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed)
SPEND_WINDOW_DAYS = 90
_MAX_QUERY_LEN = 100


def team_member_ids(session: Session, user: User) -> set[uuid.UUID]:
    """The caller's team, by the existing read-scope rule (see module doc)."""
    if user.role == RoleKey.manager:
        return direct_report_ids(user, session)
    if user.role == RoleKey.finance_admin:
        return set(
            session.scalars(sa.select(User.id).where(User.organization_id == user.organization_id))
        )
    raise ForbiddenError("Only managers and finance administrators have a team view.")


def _base_currency(session: Session, user: User) -> str:
    currency = session.scalar(
        sa.select(Organization.base_currency).where(Organization.id == user.organization_id)
    )
    return currency or "USD"


def _counts(session: Session, stmt: sa.Select[Any]) -> dict[Any, int]:
    return {row[0]: int(row[1]) for row in session.execute(stmt).all()}


def _summaries(
    session: Session, user: User, members: list[User], now: datetime
) -> list[TeamMemberSummary]:
    if not members:
        return []
    org_id = user.organization_id
    ids = [m.id for m in members]
    currency = _base_currency(session, user)

    open_trips = _counts(
        session,
        sa.select(Trip.traveler_id, sa.func.count(Trip.id))
        .where(
            Trip.organization_id == org_id,
            Trip.traveler_id.in_(ids),
            Trip.status.in_(OPEN_TRIP_STATUSES),
        )
        .group_by(Trip.traveler_id),
    )

    status_rows = session.execute(
        sa.select(ExpenseReport.owner_id, ExpenseReport.status, sa.func.count(ExpenseReport.id))
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.owner_id.in_(ids),
            # Product-gaps integration: a WITHDRAWN report keeps status draft
            # (gap 4) but is closed — it is not an open draft of the member.
            ExpenseReport.withdrawn_at.is_(None),
        )
        .group_by(ExpenseReport.owner_id, ExpenseReport.status)
    ).all()
    by_status: dict[uuid.UUID, dict[str, int]] = {
        i: {s.value: 0 for s in ExpenseReportStatus} for i in ids
    }
    for owner_id, status, count in status_rows:
        by_status[owner_id][status.value] = int(count)

    pending = _counts(
        session,
        sa.select(Approval.submitter_id, sa.func.count(Approval.id))
        .where(
            Approval.organization_id == org_id,
            Approval.submitter_id.in_(ids),
            Approval.current_status == ApprovalStatus.pending,
        )
        .group_by(Approval.submitter_id),
    )

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    window_start = now - timedelta(days=SPEND_WINDOW_DAYS)
    spend_date = sa.func.coalesce(ExpenseReport.submitted_at, ExpenseReport.created_at)
    in_window = sa.and_(spend_date >= window_start, spend_date <= now)
    base_ccy = ExpenseReport.currency == currency
    zero = sa.literal(0, type_=ExpenseReport.total_amount.type)
    spend_rows = session.execute(
        sa.select(
            ExpenseReport.owner_id,
            sa.func.coalesce(
                sa.func.sum(
                    sa.case(
                        (sa.and_(base_ccy, spend_date >= month_start), ExpenseReport.total_amount),
                        else_=zero,
                    )
                ),
                0,
            ),
            sa.func.coalesce(
                sa.func.sum(sa.case((base_ccy, ExpenseReport.total_amount), else_=zero)), 0
            ),
            sa.func.count(ExpenseReport.id).filter(sa.not_(base_ccy)),
        )
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.owner_id.in_(ids),
            ExpenseReport.status.in_(SPEND_STATUSES),
            in_window,
        )
        .group_by(ExpenseReport.owner_id)
    ).all()
    spend: dict[uuid.UUID, tuple[Decimal, Decimal, int]] = {
        row[0]: (Decimal(str(row[1])), Decimal(str(row[2])), int(row[3])) for row in spend_rows
    }

    out: list[TeamMemberSummary] = []
    for m in members:
        mtd, d90, other = spend.get(m.id, (Decimal("0"), Decimal("0"), 0))
        out.append(
            TeamMemberSummary(
                id=m.id,
                name=m.name,
                department=m.department,
                role=m.role.value,
                status=m.status.value,
                open_trips=open_trips.get(m.id, 0),
                reports_by_status=by_status[m.id],
                pending_approvals=pending.get(m.id, 0),
                spend_mtd=mtd.quantize(Decimal("0.01")),
                spend_90d=d90.quantize(Decimal("0.01")),
                currency=currency,
                other_currency_reports=other,
            )
        )
    return out


def _load_members(session: Session, user: User, ids: set[uuid.UUID]) -> list[User]:
    if not ids:
        return []
    return list(
        session.scalars(
            sa.select(User).where(User.organization_id == user.organization_id, User.id.in_(ids))
        ).all()
    )


def _sort_key(field: str) -> Any:
    def key(s: TeamMemberSummary) -> Any:
        if field == "name":
            return s.name.lower()
        if field == "department":
            return (s.department or "").lower()
        return getattr(s, field)

    return key


def list_members(
    session: Session,
    user: User,
    pagination: PaginationParams,
    raw_filters: dict[str, str],
    raw_sort: str | None,
    *,
    now: datetime | None = None,
) -> Page[TeamMemberSummary]:
    """Paginated team summaries; ?q (name/department substring), ?sort (allow-listed)."""
    filters = parse_filters(raw_filters, TEAM_FILTER_FIELDS)
    sort = parse_sort(raw_sort, TEAM_SORT_FIELDS)
    query = filters.get("q", "").strip().lower()
    if len(query) > _MAX_QUERY_LEN:
        raise BadRequestError(f"Search text is limited to {_MAX_QUERY_LEN} characters.")

    members = _load_members(session, user, team_member_ids(session, user))
    if query:
        members = [
            m for m in members if query in m.name.lower() or query in (m.department or "").lower()
        ]
    summaries = _summaries(session, user, members, now or datetime.now(timezone.utc))

    # Deterministic base order (name, id); stable sorts applied last-key-first.
    summaries.sort(key=lambda s: (s.name.lower(), str(s.id)))
    for field, direction in reversed(sort):
        summaries.sort(key=_sort_key(field), reverse=direction == "desc")

    page_items = summaries[pagination.offset : pagination.offset + pagination.limit]
    return Page.create(
        items=page_items, total=len(summaries), page=pagination.page, size=pagination.size
    )


def get_member(
    session: Session,
    user: User,
    member_id: uuid.UUID,
    *,
    trips_page: PaginationParams,
    reports_page: PaginationParams,
    now: datetime | None = None,
) -> TeamMemberDetail:
    """One team member's summary + trips + reports.  Outside the team → 404."""
    if member_id not in team_member_ids(session, user):
        raise NotFoundError("Team member not found.")
    members = _load_members(session, user, {member_id})
    if not members:
        raise NotFoundError("Team member not found.")
    summary = _summaries(session, user, members, now or datetime.now(timezone.utc))[0]

    trips = travel_service.list_trips(
        session, user, trips_page, {"traveler_id": str(member_id)}, None
    )
    reports = expense_service.list_reports(
        session, user, reports_page, {"owner_id": str(member_id)}, None
    )
    return TeamMemberDetail(member=summary, trips=trips, reports=reports)
