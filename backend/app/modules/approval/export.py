"""Finance CSV export of the approval queue (Product gap 8) — GET /approvals/export.csv.

Applies EXACTLY the list's scope + filters (``service.scoped_filtered_approvals``)
and its order, so the file is what the filtered queue shows.  Finance-only at
the router (org-wide reader), tenant-scoped by scope_query, capped
(``csv_export.EXPORT_MAX_ROWS``) and audit-logged (who, filters, row count).

Columns are the on-screen queue fields only — no internal ids, no emails.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.app.core import csv_export
from backend.app.core.audit import record_audit
from backend.app.core.pagination import parse_sort
from backend.app.modules.approval import service
from backend.app.modules.approval.filters import exception_labels
from database.models.approval_step import ApprovalStep
from database.models.identity import User

APPROVALS_EXPORT_ACTION = "approvals.exported"
EXPORT_ENTITY_TYPE = "export"

APPROVAL_CSV_HEADER: tuple[str, ...] = (
    "Submitted at (UTC)",
    "Status",
    "Subject type",
    "Subject",
    "Submitter",
    "Department",
    "Amount",
    "Currency",
    "Risk level",
    "Auto-approved",
    "Exception types",
    "Last decided at (UTC)",
)


def _last_decided(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    if not ids:
        return {}
    rows = session.execute(
        sa.select(ApprovalStep.approval_id, sa.func.max(ApprovalStep.decided_at))
        .where(ApprovalStep.approval_id.in_(ids))
        .group_by(ApprovalStep.approval_id)
    ).all()
    return {r[0]: r[1] for r in rows}


def export_approvals_csv(
    session: Session,
    user: User,
    raw_filters: dict[str, str],
    raw_sort: str | None,
) -> StreamingResponse:
    """Build, audit-log and stream the filtered approvals CSV for *user*."""
    cap = csv_export.EXPORT_MAX_ROWS
    sort = parse_sort(raw_sort, service.APPROVAL_SORT_FIELDS)
    stmt = service.scoped_filtered_approvals(session, user, raw_filters)
    stmt = service._apply_sort(stmt, sort) if sort else service._apply_default_order(stmt)
    approvals = list(session.scalars(stmt.limit(cap + 1)).all())

    truncated = len(approvals) > cap
    approvals = approvals[:cap]
    items = service._to_list_items(session, approvals)
    decided = _last_decided(session, [a.id for a in approvals])

    rows: list[list[object]] = [
        [
            csv_export.timestamp_cell(it.created_at),
            it.display_status or it.current_status,
            it.subject_type,
            it.subject_title,
            it.submitter_name,
            it.submitter_department,
            csv_export.money_cell(it.subject_amount),
            it.currency,
            it.risk_level,
            csv_export.bool_cell(it.is_auto_approved),
            "; ".join(exception_labels(it.exception_types)),
            csv_export.timestamp_cell(decided.get(it.id)),
        ]
        for it in items
    ]

    record_audit(
        session,
        organization_id=user.organization_id,
        action=APPROVALS_EXPORT_ACTION,
        entity_type=EXPORT_ENTITY_TYPE,
        entity_id=uuid.uuid4(),
        actor_id=user.id,
        after_state={
            "export": "approvals",
            "filters": dict(sorted(raw_filters.items())),
            "sort": raw_sort,
            "row_count": len(rows),
            "truncated": truncated,
        },
    )
    session.commit()

    return csv_export.csv_response(
        filename=csv_export.export_filename("approvals"),
        header=APPROVAL_CSV_HEADER,
        rows=rows,
        truncated=truncated,
        cap=cap,
    )
