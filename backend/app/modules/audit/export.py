"""Finance CSV export of the audit log (Product gap 8) — GET /audit/export.csv.

Same org scope, allow-listed filters (action, entity_type, entity_id, actor_id)
and order as GET /audit.  Columns are what the audit table shows (time, actor
name, action, entity type, entity id) — the before/after JSON snapshots are not
exported.  Capped, injection-safe (core/csv_export.py) and itself audit-logged
(``audit.exported``) AFTER the rows are read, so the file never contains its
own export event.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.core import csv_export
from backend.app.core.audit import record_audit
from backend.app.modules.audit import service
from database.models.identity import User

AUDIT_EXPORT_ACTION = "audit.exported"
EXPORT_ENTITY_TYPE = "export"

AUDIT_CSV_HEADER: tuple[str, ...] = ("Time (UTC)", "Actor", "Action", "Entity type", "Entity ID")


def export_audit_csv(
    session: Session, user: User, raw_filters: dict[str, str], raw_sort: str | None
) -> StreamingResponse:
    cap = csv_export.EXPORT_MAX_ROWS
    stmt = service.ordered(service.scoped_filtered_audit(user, raw_filters), raw_sort)
    logs = list(session.scalars(stmt.limit(cap + 1)).all())
    truncated = len(logs) > cap
    logs = logs[:cap]

    actor_ids = {log.actor_id for log in logs if log.actor_id is not None}
    names: dict[uuid.UUID, str] = {}
    if actor_ids:
        names = {
            u.id: u.name
            for u in session.scalars(
                sa.select(User).where(
                    User.id.in_(actor_ids), User.organization_id == user.organization_id
                )
            )
        }

    rows: list[list[object]] = [
        [
            csv_export.timestamp_cell(log.created_at),
            names.get(log.actor_id, "") if log.actor_id else "System",
            log.action,
            log.entity_type,
            csv_export.Raw(str(log.entity_id)),
        ]
        for log in logs
    ]

    record_audit(
        session,
        organization_id=user.organization_id,
        action=AUDIT_EXPORT_ACTION,
        entity_type=EXPORT_ENTITY_TYPE,
        entity_id=uuid.uuid4(),
        actor_id=user.id,
        after_state={
            "export": "audit",
            "filters": dict(sorted(raw_filters.items())),
            "sort": raw_sort,
            "row_count": len(rows),
            "truncated": truncated,
        },
    )
    session.commit()

    return csv_export.csv_response(
        filename=csv_export.export_filename("audit-log"),
        header=AUDIT_CSV_HEADER,
        rows=rows,
        truncated=truncated,
        cap=cap,
    )
