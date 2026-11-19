"""Read-only ops status endpoints — finance admins only.

* GET /api/v1/ops/rate-limits (phase 11): the configured limits, the number of
  tracked counters and how many requests each policy has throttled since the
  process started.  It never returns an identity, hash, email, IP, token or secret.
* GET /api/v1/ops/demo-reset (phase 12): the scheduled demo reset — on/off and
  why, next / last reset, in progress, failure counters, last error (an
  exception type or a refusal reason, never a secret).  The counts of the last
  reset come from its ``demo.reset`` audit row and are returned ONLY to a
  finance admin of the demo organisation itself (tenant scoping).  There is no
  "reset now" — resets run on the schedule or by the operator tool.

Deny-by-default: any other role gets the uniform 403.
"""

from __future__ import annotations

from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.demo_reset_scheduler import (
    RESET_ACTION,
    DemoResetScheduler,
    ScheduleConfig,
    demo_org_name,
)
from backend.app.core.errors import ForbiddenError
from backend.app.core.rate_limit import RateLimiter
from backend.app.core.security import get_current_user
from database.models.audit_log import AuditLog
from database.models.identity import Organization, RoleKey

router = APIRouter(prefix="/ops", tags=["ops"])


def _require_finance(current_user: Any) -> None:
    if getattr(current_user, "role", None) != RoleKey.finance_admin:
        raise ForbiddenError("You do not have permission to perform this action.")


@router.get("/rate-limits")
def rate_limit_status(
    request: Request,
    current_user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_finance(current_user)
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        return {"enabled": False, "throttled": {}}
    return limiter.snapshot()


def _last_reset_result(db: Session, organization_id: Any) -> dict[str, Any] | None:
    """Counts of the newest demo.reset of the CALLER's organisation, when that
    organisation is the demo org (None otherwise)."""
    row = db.execute(
        sa.select(AuditLog.created_at, AuditLog.after_state)
        .join(Organization, Organization.id == AuditLog.organization_id)
        .where(
            AuditLog.organization_id == organization_id,
            Organization.name == demo_org_name(),
            AuditLog.action == RESET_ACTION,
            AuditLog.entity_type == "organization",
            AuditLog.entity_id == organization_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    state = row.after_state or {}
    return {
        "at": row.created_at.isoformat(),
        "deleted": dict(state.get("deleted") or {}),
        "restored": dict(state.get("restored") or {}),
        "storage_objects_to_delete": state.get("storage_objects_to_delete"),
    }


@router.get("/demo-reset")
def demo_reset_status(
    request: Request,
    current_user: Annotated[Any, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    _require_finance(current_user)
    scheduler = getattr(request.app.state, "demo_reset_scheduler", None)
    if not isinstance(scheduler, DemoResetScheduler):
        scheduler = DemoResetScheduler(ScheduleConfig(enabled=False, reason="not started"))
    body = scheduler.status_view()
    body["last_result"] = _last_reset_result(db, current_user.organization_id)
    return body
