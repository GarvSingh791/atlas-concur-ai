"""Notifications router (product gap 3 — app-shell bell).

  GET  /api/v1/notifications               caller's derived feed (newest first, capped, paged)
  GET  /api/v1/notifications/unread-count  {"unread": n}
  POST /api/v1/notifications/read-all      mark the caller's whole current feed read
  POST /api/v1/notifications/{id}/read     mark one of the caller's items read (404 otherwise)

Any authenticated user: the feed only ever contains the caller's own items
(their approvals to decide, decisions on their own submissions), so there is no
permission gate beyond authentication — scoping happens in the service.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.security import get_current_user
from backend.app.modules.notifications import service
from backend.app.modules.notifications.schemas import (
    MarkReadResponse,
    NotificationItem,
    UnreadCountResponse,
)
from database.models.identity import User

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationItem])
def list_notifications(
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
) -> Page[NotificationItem]:
    user: User = current_user
    items, total = service.list_notifications(
        db, user, offset=pagination.offset, limit=pagination.limit
    )
    return Page.create(items=items, total=total, page=pagination.page, size=pagination.size)


@router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count(
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UnreadCountResponse:
    return UnreadCountResponse(unread=service.unread_count(db, current_user))


@router.post("/read-all", response_model=MarkReadResponse)
def mark_all_read(
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MarkReadResponse:
    return MarkReadResponse(marked=service.mark_all_read(db, current_user))


@router.post("/{notification_id}/read", response_model=MarkReadResponse)
def mark_read(
    notification_id: Annotated[str, Path(max_length=200)],
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MarkReadResponse:
    return MarkReadResponse(marked=service.mark_read(db, current_user, notification_id))
