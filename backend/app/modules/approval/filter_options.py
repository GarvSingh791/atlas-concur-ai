"""Filter options for the approvals filter bar (Product gap 6).

GET /approvals/exception-types — the canonical exception types (approval/filters.py)
with how many approvals IN THE CALLER'S SCOPE carry each one.  The count runs
the exact predicate the ``exception_type`` filter applies, so a count and the
filtered list can never disagree.  Follow-up: the endpoint takes the same filter
params as GET /approvals and counts within that filtered set (faceted — the
``exception_type`` param itself is ignored, so each option says how many rows
choosing it would show); filters only AND onto the scope.

GET /approvals/departments — the distinct departments of the users the caller
may read (scope_query on users: manager → self + direct reports, finance → org).
"""

from __future__ import annotations

from collections.abc import Mapping

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.core.rbac import scope_query
from backend.app.modules.approval.filters import EXCEPTION_TYPES, approval_exception_predicate
from backend.app.modules.approval.service import scoped_filtered_approvals
from database.models.approval import Approval
from database.models.identity import User


class ExceptionTypeOption(BaseModel):
    value: str
    label: str
    kind: str  # "flag" | "rule" | "segment"
    count: int  # approvals in the caller's scope that carry this type


def list_exception_types(
    session: Session, user: User, raw_filters: Mapping[str, str] | None = None
) -> list[ExceptionTypeOption]:
    others = {k: v for k, v in (raw_filters or {}).items() if k != "exception_type"}
    base = scoped_filtered_approvals(session, user, others).with_only_columns(Approval.id)
    options: list[ExceptionTypeOption] = []
    for etype in EXCEPTION_TYPES.values():
        stmt = base.where(approval_exception_predicate(etype))
        count = session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery())) or 0
        options.append(
            ExceptionTypeOption(value=etype.value, label=etype.label, kind=etype.kind, count=count)
        )
    return options


def list_departments(session: Session, user: User) -> list[str]:
    stmt = scope_query(
        sa.select(User.department).where(User.department.isnot(None)),
        user=user,
        ownership_column=User.id,
        org_column=User.organization_id,
        session=session,
    )
    values = {d.strip() for d in session.scalars(stmt.distinct()) if d and d.strip()}
    return sorted(values, key=str.lower)
