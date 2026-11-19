"""Approval module router — approval trigger + read + decision endpoints.

Mounted at the API v1 root (no prefix) so paths match the design docs literally:
  POST  /api/v1/approvals
  GET   /api/v1/approvals                 (P5-T8 — role-scoped queue)
  GET   /api/v1/approvals/{approval_id}   (P5-T8 — decision detail)
  PATCH /api/v1/approvals/{approval_id}

Thin router — business logic lives in service.py (Dev Rule 5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ai_agents.orchestrator.envelopes import AgentResultStatus, Task
from ai_agents.orchestrator.orchestrator import Orchestrator
from backend.app.core.concurrency import if_match_version
from backend.app.core.database import get_db
from backend.app.core.errors import ConflictError
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rbac import require_permission
from backend.app.modules.ai.runtime import build_agent_context, get_orchestrator
from backend.app.modules.approval import export, filter_options, schemas, service
from config.constants.permissions import (
    APPROVAL_DECIDE_REPORTS,
    APPROVAL_READ,
    EXPENSE_READ_ORG,
)
from database.models.approval import Approval
from database.models.identity import User

router = APIRouter(tags=["approvals"])

# Query-string keys consumed by pagination/sort; excluded from filter parsing.
_PAGINATION_KEYS = {"page", "size", "sort"}


# ---------------------------------------------------------------------------
# POST /approvals — trigger the approval agent
# ---------------------------------------------------------------------------


@router.post("/approvals", response_model=schemas.ApprovalResponse, status_code=201)
def trigger_approval(
    body: schemas.ApprovalTriggerRequest,
    user: Annotated[User, Depends(require_permission(APPROVAL_DECIDE_REPORTS))],
    db: Annotated[Session, Depends(get_db)],
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
) -> schemas.ApprovalResponse:
    """Trigger the approval agent for a trip or expense report.

    Requires approval.decide.reports permission.
    The agent runs deterministically (no LLM) and either auto-approves or escalates.
    Returns 409 if an approval already exists for the given subject.
    The agent writes the audit row (auto_approved/escalated) — this endpoint does not.

    Pre-agent gate (C7-03 / C7-13): subject outside the caller's read scope → 404;
    caller owns the subject → 403 (no self-approval, any role); subject not in its
    submitted state (report ``submitted`` / trip ``pending_approval``) → 400
    INVALID_SUBJECT_STATE. An escalation that would route to nobody or to the
    submitter is re-routed to a finance_admin. An auto-approved subject (trip or
    expense report) is moved to ``approved`` in the same transaction.
    """
    # 0. Authorization + state gate BEFORE the agent writes anything.
    service.validate_trigger_subject(
        db, user=user, subject_type=body.subject_type, subject_id=body.subject_id
    )

    # 1. Build task envelope
    task = Task(
        context_type="approval",
        payload={
            "subject_type": body.subject_type.value,
            "subject_id": str(body.subject_id),
        },
        signals={"approval_required": True},
        task_type=None,
    )

    # 2. Build agent context (includes permission resolution)
    agent_ctx = build_agent_context(user, db)

    # 3. Run orchestrator (flush-only — endpoint owns the commit)
    outcome = orchestrator.handle_task(task, context=agent_ctx, session=db)

    # 4. Duplicate backstop.  The common re-trigger is already refused (409) by
    #    validate_trigger_subject; this branch covers the concurrent-trigger race
    #    where another request inserted the row after step 0 and the agent's
    #    persistence returned needs_review on IdempotencyError.
    if outcome.result.status == AgentResultStatus.needs_review:
        existing = db.scalars(
            sa.select(Approval).where(
                Approval.organization_id == user.organization_id,
                Approval.subject_type == body.subject_type,
                Approval.subject_id == body.subject_id,
            )
        ).first()
        if existing is not None:
            raise ConflictError("An approval already exists for this subject.")

    # 5. Independent-approver routing (C7-03), then the single commit (agent
    #    audit already flushed but not committed).
    persisted = db.scalars(
        sa.select(Approval).where(
            Approval.organization_id == user.organization_id,
            Approval.subject_type == body.subject_type,
            Approval.subject_id == body.subject_id,
        )
    ).first()
    if persisted is not None:
        service.ensure_independent_approver(db, persisted)
        service.apply_auto_approval_to_subject(db, persisted)
    db.commit()

    # 6. Fetch the persisted approval
    approval = db.scalars(
        sa.select(Approval).where(
            Approval.organization_id == user.organization_id,
            Approval.subject_type == body.subject_type,
            Approval.subject_id == body.subject_id,
        )
    ).first()

    if approval is None:
        # Should not happen if agent returned ok — surface as 409 for safety
        raise ConflictError("Approval could not be located after agent run.")

    return schemas.ApprovalResponse.model_validate(approval)


# ---------------------------------------------------------------------------
# GET /approvals — role-scoped approval queue (P5-T8)
# ---------------------------------------------------------------------------


@router.get("/approvals", response_model=Page[schemas.ApprovalListItem])
def list_approvals(
    request: Request,
    user: Annotated[User, Depends(require_permission(APPROVAL_READ))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(None, description="Sort spec, e.g. 'created_at:desc'."),
) -> Page[schemas.ApprovalListItem]:
    """List approvals scoped to the caller's authority (requires approval.read).

    manager → own + direct reports; finance_admin → org-wide (server-enforced via
    scope_query — the UI never client-filters for authority). Supports ?page,
    ?size, ?sort, and allow-listed filters (current_status, subject_type,
    is_auto_approved). Default order is prioritized (pending first).
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return service.list_approvals(db, user, pagination, raw_filters, sort)


# ---------------------------------------------------------------------------
# Product gaps 6 + 8 — filter options + finance CSV export.
# Declared BEFORE /approvals/{approval_id} so the literal paths win.
# ---------------------------------------------------------------------------


@router.get("/approvals/exception-types", response_model=list[filter_options.ExceptionTypeOption])
def list_exception_types(
    request: Request,
    user: Annotated[User, Depends(require_permission(APPROVAL_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> list[filter_options.ExceptionTypeOption]:
    """Canonical exception types with in-scope counts under the active filters.

    Takes the same filter params as GET /approvals (``exception_type`` itself is
    ignored: faceted counts); paging keys are ignored.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return filter_options.list_exception_types(db, user, raw_filters)


@router.get("/approvals/departments", response_model=list[str])
def list_departments(
    user: Annotated[User, Depends(require_permission(APPROVAL_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> list[str]:
    """Departments of the users in the caller's read scope (filter options)."""
    return filter_options.list_departments(db, user)


@router.get("/approvals/export.csv", response_class=StreamingResponse)
def export_approvals(
    request: Request,
    user: Annotated[User, Depends(require_permission(APPROVAL_READ))],
    _org_reader: Annotated[User, Depends(require_permission(EXPENSE_READ_ORG))],
    db: Annotated[Session, Depends(get_db)],
    sort: str | None = Query(None, description="Sort spec, e.g. 'created_at:desc'."),
) -> StreamingResponse:
    """Finance-only CSV export of the approval queue (approval.read + expense.read.org).

    Applies the SAME allow-listed filters, scope and order as GET /approvals;
    capped (a marker row + ``X-Export-Truncated: true`` when cut); every export
    is audit-logged (``approvals.exported``: who, filters, row count).
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return export.export_approvals_csv(db, user, raw_filters, sort)


# ---------------------------------------------------------------------------
# GET /approvals/{approval_id} — decision detail (P5-T8)
# ---------------------------------------------------------------------------


@router.get("/approvals/{approval_id}", response_model=schemas.ApprovalDetailResponse)
def get_approval(
    approval_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission(APPROVAL_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.ApprovalDetailResponse:
    """Fetch a single approval with full decision context (requires approval.read).

    Includes risk_summary, the derived risk_level, the decision history (steps),
    and the polymorphic subject context (expense_report + entries OR trip +
    segments). Returns 404 for both out-of-scope and nonexistent IDs.
    """
    return service.get_approval_detail(db, user, approval_id)


# ---------------------------------------------------------------------------
# PATCH /approvals/{approval_id} — record a human decision
# ---------------------------------------------------------------------------


@router.patch("/approvals/{approval_id}", response_model=schemas.ApprovalDetailResponse)
def decide_approval(
    approval_id: uuid.UUID,
    body: schemas.ApprovalDecisionRequest,
    user: Annotated[User, Depends(require_permission(APPROVAL_DECIDE_REPORTS))],
    db: Annotated[Session, Depends(get_db)],
    expected_version: Annotated[int | None, Depends(if_match_version)],
) -> schemas.ApprovalDetailResponse:
    """Record a human decision on an approval (requires approval.decide.reports).

    Decidable on pending OR auto_approved (US-M2 AC2 reverse-within-policy); other
    states → 409. reason is required (server-enforced) for reject/request_changes.
    Supply an If-Match header with the current version for optimistic locking; a
    stale version → 409. Scope is server-enforced via scope_query (manager →
    reports, finance_admin → org). The decision is written onto the subject in
    the same transaction: approve → ``approved``, reject → ``rejected``,
    request_changes → trip ``draft`` / expense report ``draft`` (returned to the
    owner; resubmitting — trip PATCH ``pending_approval``, report PATCH
    ``submitted`` — re-queues this approval as pending with a fresh,
    escalate-only risk assessment).
    Returns the refreshed decision detail.
    """
    return service.decide_approval(
        db,
        user,
        approval_id,
        decision=body.decision,
        reason=body.reason,
        expected_version=expected_version,
    )
