"""Expense module router — expense report + entry endpoints.

Mounted at the API v1 root (no prefix) so paths match the design docs literally:
  POST  /api/v1/expense-reports
  GET   /api/v1/expense-reports
  GET   /api/v1/expense-reports/{report_id}
  PATCH /api/v1/expense-reports/{report_id}        (state transition)
  GET   /api/v1/expense-reports/{report_id}/entries
  PATCH /api/v1/expenses/{entry_id}               (entry update)
  PATCH /api/v1/expense-reports/{report_id}/details      (rename a draft)
  POST  /api/v1/expense-reports/{report_id}/entries/move (move entries between drafts)
  DELETE /api/v1/expenses/{entry_id}               (delete a draft entry, UAT2)
  DELETE /api/v1/expense-reports/{report_id}       (delete a never-submitted draft, UAT2)
  POST  /api/v1/expenses/{entry_id}/receipts       (attach an existing receipt, UAT2)
  POST  /api/v1/expense-reports/{report_id}/mileage (add a server-computed mileage entry)
  GET   /api/v1/mileage-rate                      (the MIL-01 rate, for the live estimate)

Thin router — all business logic lives in service.py (Dev Rule 5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from backend.app.core.concurrency import if_match_version
from backend.app.core.database import get_db
from backend.app.core.idempotency import idempotency_key_header
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import require_permission
from backend.app.modules.expense import policy_fields, schemas, service
from config.constants.permissions import (
    EXPENSE_CREATE_OWN,
    EXPENSE_READ_OWN,
    EXPENSE_UPDATE_OWN,
)
from database.models.identity import User
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import StorageProvider

router = APIRouter(tags=["expenses"])

# Query-string keys consumed by pagination/sort/scope; excluded from filter parsing.
_RESERVED_KEYS = {"page", "size", "sort", "mine"}


@router.post("/expense-reports", response_model=schemas.ExpenseReportResponse, status_code=201)
def create_report(
    body: schemas.ExpenseReportCreate,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_CREATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    idem_key: Annotated[str | None, Depends(idempotency_key_header)],
) -> schemas.ExpenseReportResponse:
    """Create a new expense report in draft status (requires expense.create.own permission).

    total_amount is derived from submitted entries — never accepted from the client.
    If trip_id is provided it must reference a trip accessible to the caller.
    Supports idempotent retries via the Idempotency-Key header.
    """
    return service.create_report(db, current_user, body, idempotency_key=idem_key)


@router.get("/expense-reports", response_model=Page[schemas.ReportListItem])
def list_reports(
    request: Request,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(
        None, description="Sort spec, e.g. 'created_at:desc' or '-total_amount'."
    ),
    mine: bool = Query(
        False,
        description=(
            "true → only reports the caller owns (the personal 'My expenses' view). "
            "Default: the caller's full role read scope."
        ),
    ),
) -> Page[schemas.ReportListItem]:
    """List expense reports scoped by role (requires expense.read.own permission).

    employee→own, manager→own+reports, finance_admin→org-wide.  ``?mine=true``
    narrows any role to its own reports (UAT2 — personal pages always send it).
    Supports ?page, ?size, ?sort, and allow-listed filter fields.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _RESERVED_KEYS}
    return service.list_reports(db, current_user, pagination, raw_filters, sort, mine=mine)


@router.get("/expense-reports/{report_id}", response_model=schemas.ExpenseReportResponse)
def get_report(
    report_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.ExpenseReportResponse:
    """Fetch a single expense report by ID (requires expense.read.own permission).

    Returns 404 for both out-of-scope and nonexistent IDs.
    """
    return service.get_report(db, current_user, report_id)


@router.patch("/expense-reports/{report_id}", response_model=schemas.ExpenseReportResponse)
def transition_report(
    report_id: uuid.UUID,
    body: schemas.ReportTransition,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.ExpenseReportResponse:
    """Transition an expense report's status (requires expense.update.own permission).

    Legal transitions per LEGAL_TRANSITIONS map.  Illegal → 409 Conflict.
    approved / rejected / reimbursed are never owner-settable (403): decisions
    come from the approval workflow.  submitted_at is set automatically when the
    report moves to submitted; resubmitting a returned report re-queues its
    approval as pending.  Supply an If-Match header with the current version for
    optimistic locking.
    """
    return service.transition_report(
        db, current_user, report_id, body.status, expected_version=expected_version
    )


@router.get(
    "/expense-reports/{report_id}/entries",
    response_model=list[schemas.ExpenseEntryResponse],
)
def get_report_entries(
    report_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_READ_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> list[schemas.ExpenseEntryResponse]:
    """Return entries for an expense report (requires expense.read.own permission).

    Returns 404 if the parent report is out of scope or nonexistent.
    """
    return service.get_report_entries(db, current_user, report_id)


@router.patch("/expenses/{entry_id}", response_model=schemas.ExpenseEntryResponse)
def update_entry(
    entry_id: uuid.UUID,
    body: schemas.ExpenseUpdate,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.ExpenseEntryResponse:
    """Update an expense entry (requires expense.update.own permission).

    Only allowed while the parent report is in draft status — else 409.
    Parent report total_amount is recomputed atomically and its pre-submit check
    re-run (justification included).  Optional If-Match carries the PARENT
    REPORT version (entries have none); stale → 409.  The report version bumps.
    """
    return service.update_entry(db, current_user, entry_id, body, expected_version=expected_version)


@router.patch(
    "/expense-reports/{report_id}/details",
    response_model=schemas.ExpenseReportResponse,
)
def rename_report(
    report_id: uuid.UUID,
    body: schemas.ReportDetailsUpdate,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.ExpenseReportResponse:
    """Rename a draft expense report (owner only; If-Match report version)."""
    return service.rename_report(
        db, current_user, report_id, body, expected_version=expected_version
    )


@router.post(
    "/expense-reports/{report_id}/entries/move",
    response_model=schemas.MoveEntriesResponse,
)
def move_entries(
    report_id: uuid.UUID,
    body: schemas.MoveEntriesRequest,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.MoveEntriesResponse:
    """Move entries of draft report *report_id* to another draft report of the caller.

    If-Match carries the SOURCE report version.  Both reports are re-totalled and
    re-checked; 404 unreadable, 403 not owner, 409 not draft / stale version.
    """
    return service.move_entries(
        db, current_user, report_id, body, expected_version=expected_version
    )


@router.delete("/expenses/{entry_id}", response_model=schemas.EntryDeleteResponse)
def delete_entry(
    entry_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageProvider, Depends(get_storage_provider)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.EntryDeleteResponse:
    """Delete an entry of a draft report the caller owns (UAT2).

    If-Match carries the PARENT REPORT version.  404 unreadable, 403 not owner,
    409 not draft / stale version / an entry a reviewer saw whose return kept no
    snapshot (REPORT_UNDER_REVIEW).  Receipts go with the entry — or are kept
    for audit when the report has been through approval.  The report is
    re-totalled and re-checked; stored images are removed after the commit.
    """
    response, keys = service.delete_entry(
        db, current_user, entry_id, expected_version=expected_version
    )
    service.purge_receipt_objects(storage, keys)
    return response


@router.delete("/expense-reports/{report_id}", status_code=204)
def delete_report(
    report_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageProvider, Depends(get_storage_provider)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> Response:
    """Delete a never-submitted draft report the caller owns, with its entries (UAT2).

    409 REPORT_HAS_APPROVAL once the report has any approval history (its review
    history stays with it), 409 not draft / stale If-Match, 404 / 403 as usual.
    """
    keys = service.delete_report(db, current_user, report_id, expected_version=expected_version)
    service.purge_receipt_objects(storage, keys)
    return Response(status_code=204)


@router.post("/expenses/{entry_id}/receipts", response_model=schemas.ReceiptLinkResponse)
def link_receipt(
    entry_id: uuid.UUID,
    body: schemas.LinkReceiptRequest,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.ReceiptLinkResponse:
    """Attach one of the caller's unlinked, OCR-read receipts to a draft entry (UAT2).

    If-Match carries the PARENT REPORT version.  409 RECEIPT_ALREADY_LINKED (a
    receipt backs one claim), RECEIPT_RETAINED (kept for audit),
    RECEIPT_NOT_PROCESSED, not draft / stale version; 404 / 403 as usual.  The
    report's pre-submit check is re-run and its version bumped.
    """
    return service.link_existing_receipt(
        db, current_user, entry_id, body, expected_version=expected_version
    )


@router.get("/mileage-rate", response_model=schemas.MileageRateResponse)
def get_mileage_rate(
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.MileageRateResponse:
    """The MIL-01 rate the server charges the caller's organisation (read-only).

    Feeds the Add-mileage dialog's live estimate; gated like the POST that uses
    it.  The amount is never taken from the client.
    """
    return policy_fields.mileage_rate_response(db, current_user)


@router.post(
    "/expense-reports/{report_id}/mileage",
    response_model=schemas.ExpenseEntryResponse,
    status_code=201,
)
def add_mileage(
    report_id: uuid.UUID,
    body: schemas.MileageEntryCreate,
    current_user: Annotated[User, Depends(require_permission(EXPENSE_UPDATE_OWN))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
    idempotency_key: Annotated[str | None, Depends(idempotency_key_header)],
) -> schemas.ExpenseEntryResponse:
    """Add a personal-vehicle mileage entry to a draft report (product gap 2).

    Owner only; the amount is computed server-side (business miles x the MIL-01
    rate) — a sent amount that differs is a 422.  If-Match: the report version.
    Integration hardening: If-Match or an Idempotency-Key is REQUIRED (428 without
    either), so a double submit can never add the trip twice — the second request
    is a stale-version 409, or replays the first entry for the same key.
    """
    return policy_fields.add_mileage_entry(
        db,
        current_user,
        report_id,
        body,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )
