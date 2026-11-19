"""Audit module service (#3b) — paginated read over the append-only audit_log.

Org-wide read: audit_log has NO ownership column, so the query is scoped by
organization_id alone (NOT scope_query). The endpoint is gated by AUDIT_READ,
which is granted to finance_admin only (the org-wide compliance reader).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.errors import BadRequestError
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.modules.audit.schemas import AuditLogItem
from database.models.audit_log import AuditLog
from database.models.identity import User

# Allow-listed filter / sort fields (deny-by-default via parse_filters/parse_sort).
AUDIT_FILTER_FIELDS: set[str] = {"action", "entity_type", "entity_id", "actor_id"}
AUDIT_SORT_FIELDS: set[str] = {"created_at"}


def _coerce_uuid(field: str, value: str) -> uuid.UUID:
    """Coerce a filter value to UUID, raising BadRequestError on a malformed id."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BadRequestError(f"Invalid UUID value '{value}' for filter '{field}'.") from exc


def _apply_filters(stmt: sa.Select, filters: dict[str, str]) -> sa.Select:
    for field, value in filters.items():
        if field == "action":
            stmt = stmt.where(AuditLog.action == value)
        elif field == "entity_type":
            stmt = stmt.where(AuditLog.entity_type == value)
        elif field == "entity_id":
            stmt = stmt.where(AuditLog.entity_id == _coerce_uuid(field, value))
        elif field == "actor_id":
            stmt = stmt.where(AuditLog.actor_id == _coerce_uuid(field, value))
    return stmt


def _apply_sort(stmt: sa.Select, sort: list[tuple[str, str]]) -> sa.Select:
    for field, direction in sort:
        col = getattr(AuditLog, field)
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    return stmt


def _to_items(session: Session, logs: list[AuditLog]) -> list[AuditLogItem]:
    """Resolve actor names (batched) into response rows."""
    if not logs:
        return []

    actor_ids = {log.actor_id for log in logs if log.actor_id is not None}
    name_map: dict[uuid.UUID, str] = {}
    if actor_ids:
        name_map = {
            u.id: u.name for u in session.scalars(sa.select(User).where(User.id.in_(actor_ids)))
        }

    return [
        AuditLogItem(
            id=log.id,
            action=log.action,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            actor_id=log.actor_id,
            actor_name=name_map.get(log.actor_id) if log.actor_id else None,
            before_state=log.before_state,
            after_state=log.after_state,
            created_at=log.created_at,
        )
        for log in logs
    ]


def scoped_filtered_audit(user: User, raw_filters: dict[str, str]) -> sa.Select:
    """Org-scoped audit_log SELECT with the allow-listed filters (list + CSV export)."""
    filters = parse_filters(raw_filters, AUDIT_FILTER_FIELDS)
    base = sa.select(AuditLog).where(AuditLog.organization_id == user.organization_id)
    return _apply_filters(base, filters)


def ordered(stmt: sa.Select, raw_sort: str | None) -> sa.Select:
    """Apply ?sort (created_at only) or the default newest-first order."""
    sort = parse_sort(raw_sort, AUDIT_SORT_FIELDS)
    return _apply_sort(stmt, sort) if sort else stmt.order_by(AuditLog.created_at.desc())


def list_audit(
    session: Session,
    *,
    user: User,
    raw_filters: dict[str, str],
    raw_sort: str | None,
    pagination: PaginationParams,
) -> Page[AuditLogItem]:
    """Return a paginated, org-scoped slice of the audit_log for *user*'s org.

    Scope is organization_id only (audit_log has no ownership column). Supports
    allow-listed filters (action, entity_type, entity_id, actor_id) and sort
    (created_at). Default order is created_at desc (most recent first).
    """
    sort = parse_sort(raw_sort, AUDIT_SORT_FIELDS)
    base = scoped_filtered_audit(user, raw_filters)

    total: int = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0

    base = _apply_sort(base, sort) if sort else base.order_by(AuditLog.created_at.desc())
    base = base.offset(pagination.offset).limit(pagination.limit)
    logs = list(session.scalars(base).all())

    return Page.create(
        items=_to_items(session, logs),
        total=total,
        page=pagination.page,
        size=pagination.size,
    )
