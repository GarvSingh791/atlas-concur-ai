"""Audit module router (#3b) — paginated org-wide audit-log read.

Mounted at /api/v1/audit (prefix added in backend/app/api/v1.py).
  GET /api/v1/audit  — paginated, filterable, sortable audit_log slice

Thin router — business logic lives in service.py (Dev Rule 5). Gated by
AUDIT_READ (finance_admin only); the audit_log has no ownership column, so the
service scopes by organization_id alone (no scope_query).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import require_permission
from backend.app.modules.audit import export, service
from backend.app.modules.audit.schemas import AuditLogItem
from config.constants.permissions import AUDIT_READ
from database.models.identity import User

router = APIRouter(tags=["audit"])

# Query-string keys consumed by pagination/sort; excluded from filter parsing.
_PAGINATION_KEYS = {"page", "size", "sort"}


@router.get("", response_model=Page[AuditLogItem])
def list_audit(
    request: Request,
    user: Annotated[User, Depends(require_permission(AUDIT_READ))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(None, description="Sort spec, e.g. 'created_at:desc'."),
) -> Page[AuditLogItem]:
    """List the organization's audit-log entries (requires audit.read).

    Org-wide scope (audit_log has no ownership column) — granted to finance_admin
    as the compliance reader. Supports ?page, ?size, ?sort (created_at), and
    allow-listed filters (action, entity_type, entity_id, actor_id). Default order
    is created_at desc (most recent first). actor_name is resolved server-side.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return service.list_audit(
        db,
        user=user,
        raw_filters=raw_filters,
        raw_sort=sort,
        pagination=pagination,
    )


@router.get("/export.csv", response_class=StreamingResponse)
def export_audit(
    request: Request,
    user: Annotated[User, Depends(require_permission(AUDIT_READ))],
    db: Annotated[Session, Depends(get_db)],
    sort: str | None = Query(None, description="Sort spec, e.g. 'created_at:desc'."),
) -> StreamingResponse:
    """Finance-only CSV export of the audit log (requires audit.read — finance_admin).

    Same org scope, allow-listed filters and order as GET /audit; capped (marker
    row + ``X-Export-Truncated: true`` when cut); audit-logged as ``audit.exported``.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return export.export_audit_csv(db, user, raw_filters, sort)
