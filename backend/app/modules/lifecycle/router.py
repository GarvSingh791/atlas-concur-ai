"""Lifecycle router — reimbursement, withdrawal and trip completion (product gaps 1, 4, 8).

Mounted at the API v1 root (no prefix) so paths sit next to their subjects:
  GET  /api/v1/reimbursements/queue              approved reports finance may reimburse
  POST /api/v1/expense-reports/{report_id}/reimburse   finance: mark reimbursed (simulated)
  POST /api/v1/expense-reports/{report_id}/withdraw    owner: withdraw a RETURNED report
  POST /api/v1/trips/{trip_id}/withdraw                owner: withdraw a RETURNED trip
  POST /api/v1/trips/{trip_id}/complete                traveler / finance: approved -> completed

Thin router — all business logic lives in service.py (Dev Rule 5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from backend.app.core.concurrency import if_match_version
from backend.app.core.database import get_db
from backend.app.core.rbac import require_permission
from backend.app.modules.expense.schemas import ExpenseReportResponse
from backend.app.modules.lifecycle import schemas, service
from backend.app.modules.travel.schemas import TripResponse
from config.constants.permissions import (
    EXPENSE_REIMBURSE_ORG,
    EXPENSE_UPDATE_OWN,
    TRIP_READ_OWN,
    TRIP_UPDATE_OWN,
)
from database.models.identity import User

router = APIRouter(tags=["lifecycle"])


@router.get("/reimbursements/queue", response_model=schemas.ReimbursementQueue)
def reimbursement_queue(
    current_user: Annotated[User, Depends(require_permission(EXPENSE_REIMBURSE_ORG))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.ReimbursementQueue:
    """Approved expense reports awaiting (simulated) reimbursement — finance only."""
    return service.reimbursement_queue(db, current_user)


@router.post("/expense-reports/{report_id}/reimburse", response_model=ExpenseReportResponse)
def reimburse_report(
    report_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_REIMBURSE_ORG))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
    body: Annotated[schemas.ReimburseRequest | None, Body()] = None,
) -> ExpenseReportResponse:
    """Mark an approved report reimbursed — simulated, no real money moves (finance only).

    403 without expense.reimburse.org or on the caller's own report; 409 already
    reimbursed / not approved / no approved approval / total mismatch / stale If-Match.
    """
    return service.reimburse_report(
        db,
        current_user,
        report_id,
        note=body.note if body is not None else None,
        expected_version=expected_version,
    )


@router.post("/expense-reports/{report_id}/withdraw", response_model=ExpenseReportResponse)
def withdraw_report(
    report_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
    body: Annotated[schemas.WithdrawRequest | None, Body()] = None,
) -> ExpenseReportResponse:
    """Withdraw a report a reviewer returned to you (owner only; cannot be undone)."""
    return service.withdraw_report(
        db,
        current_user,
        report_id,
        reason=body.reason if body is not None else None,
        expected_version=expected_version,
    )


@router.post("/trips/{trip_id}/withdraw", response_model=TripResponse)
def withdraw_trip(
    trip_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(TRIP_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    body: Annotated[schemas.WithdrawRequest | None, Body()] = None,
) -> TripResponse:
    """Withdraw a trip a reviewer returned to you (owner only; cannot be undone)."""
    return service.withdraw_trip(
        db, current_user, trip_id, reason=body.reason if body is not None else None
    )


@router.post("/trips/{trip_id}/complete", response_model=TripResponse)
def complete_trip(
    trip_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(TRIP_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> TripResponse:
    """Mark an approved trip whose end date has passed as completed (traveler or finance)."""
    return service.complete_trip(db, current_user, trip_id)
