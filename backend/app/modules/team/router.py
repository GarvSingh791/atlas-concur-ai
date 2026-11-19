"""Team module router (Product gap 5) — mounted at /api/v1/team.

  GET /api/v1/team/members              paginated team summaries (?q, ?sort)
  GET /api/v1/team/members/{user_id}    summary + the member's trips + reports

Read-only.  Gated on the TEAM read permissions (deny-by-default): a manager
holds ``expense.read.reports`` (direct reports), finance_admin holds
``expense.read.org`` (org-wide); an employee holds neither → 403.  The service
then derives the team from the existing scope rule (rbac.direct_report_ids /
org) and answers 404 for anyone outside it.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.errors import ForbiddenError
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import get_user_permissions
from backend.app.core.security import get_current_user
from backend.app.modules.team import service
from backend.app.modules.team.schemas import TeamMemberDetail, TeamMemberSummary
from config.constants.permissions import EXPENSE_READ_ORG, EXPENSE_READ_REPORTS
from database.models.identity import User

router = APIRouter(tags=["team"])

TEAM_READ_PERMISSIONS: frozenset[str] = frozenset({EXPENSE_READ_REPORTS, EXPENSE_READ_ORG})

_PAGINATION_KEYS = {"page", "size", "sort"}


def require_team_reader(
    current_user: Annotated[Any, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
) -> User:
    """Pass if the DB-loaded user holds ANY team read permission; 403 otherwise."""
    user: User = current_user
    if not (get_user_permissions(user, session) & TEAM_READ_PERMISSIONS):
        raise ForbiddenError("You do not have permission to perform this action.")
    return user


@router.get("/members", response_model=Page[TeamMemberSummary])
def list_members(
    request: Request,
    user: Annotated[User, Depends(require_team_reader)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(None, description="e.g. '-spend_90d' or 'pending_approvals:desc'."),
) -> Page[TeamMemberSummary]:
    """The caller's team with trips / reports / pending / spend (manager → direct reports)."""
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return service.list_members(db, user, pagination, raw_filters, sort)


@router.get("/members/{user_id}", response_model=TeamMemberDetail)
def get_member(
    user_id: uuid.UUID,
    user: Annotated[User, Depends(require_team_reader)],
    db: Annotated[Session, Depends(get_db)],
    trips_page: int = Query(1, description="1-based page of the member's trips."),
    reports_page: int = Query(1, description="1-based page of the member's reports."),
    size: int | None = Query(None, description="Items per page for both lists."),
) -> TeamMemberDetail:
    """One team member: summary + trips + reports (read-only).  Not in the team → 404."""
    return service.get_member(
        db,
        user,
        user_id,
        trips_page=PaginationParams.create(page=trips_page, size=size),
        reports_page=PaginationParams.create(page=reports_page, size=size),
    )
