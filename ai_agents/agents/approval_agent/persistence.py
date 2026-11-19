"""Approval persistence helpers (P4-T12).

persist_approval — write one Approval row (+ one ApprovalStep on auto_approve path)
                   and one AuditLog row via the injected audit_writer port.

Idempotency: if an Approval already exists for (org, subject_type, subject_id),
returns it unchanged (no second write).

Flush-only contract: every session.flush(); never session.commit().
The caller (P4-T13 endpoint) owns the commit.

Audit port (Decision C): record_audit lives in backend/app/core/audit.py which
ai_agents must never import.  The caller injects a compatible callable so this
module stays backend-free.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.approval_agent.evaluate import RiskAssessment, build_risk_summary
from ai_agents.agents.approval_agent.routing import LOW_RISK_AUTO_APPROVE_MAX_USD
from ai_agents.context import AgentContext
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep

logger = logging.getLogger(__name__)


class IdempotencyError(Exception):
    """Raised when an Approval already exists for the given (org, subject_type, subject_id)."""


class AuditWriter(Protocol):
    """Port matching backend.app.core.audit.record_audit's signature."""

    def __call__(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any] | None,
    ) -> Any: ...


def _build_auto_approve_rationale(assessment: RiskAssessment) -> str:
    total_str = (
        f"{float(assessment.subject_total):.2f} USD"
        if assessment.subject_total is not None
        else "unknown"
    )
    return (
        f"Auto-approved: subject is fully in policy, "
        f"zero blocking flags, "
        f"no approver required by policy, "
        f"total {total_str} ≤ threshold "
        f"{LOW_RISK_AUTO_APPROVE_MAX_USD:.2f} USD."
    )


def persist_approval(
    session: Session,
    *,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
    submitter_id: uuid.UUID,
    context: AgentContext,
    route: str,
    assessment: RiskAssessment,
    audit_writer: AuditWriter | None = None,
) -> Approval:
    """Write Approval (+ optional ApprovalStep) and audit row; no commit.

    Args:
        session: Caller-owned session.  Only flush() is called; never commit().
        subject_type: ApprovalSubjectType.trip or .expense_report.
        subject_id: UUID of the subject (no FK in DB — app-layer integrity).
        submitter_id: User who submitted the subject.
        context: Injected AgentContext (carries org_id).
        route: "auto_approve" | "escalate".
        assessment: RiskAssessment produced by assess_risk().
        audit_writer: Injected AuditWriter port (None skips audit write).

    Returns:
        The flushed (not committed) Approval row.

    Raises:
        IdempotencyError: An Approval already exists for this (org, subject_type, subject_id).
    """
    org_id = context.organization_id

    # ── 1. Idempotency check ─────────────────────────────────────────────────
    existing = session.scalar(
        sa.select(Approval).where(
            Approval.organization_id == org_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    )
    if existing is not None:
        raise IdempotencyError(
            f"Approval already exists for {subject_type.value}:{subject_id} "
            f"(id={existing.id}, status={existing.current_status.value})"
        )

    # ── 2. Build Approval row ─────────────────────────────────────────────────
    is_auto_approved = route == "auto_approve"
    status = ApprovalStatus.auto_approved if is_auto_approved else ApprovalStatus.pending
    risk_summary = build_risk_summary(assessment, route=route)

    approval = Approval(
        organization_id=org_id,
        subject_type=subject_type,
        subject_id=subject_id,
        submitter_id=submitter_id,
        current_status=status,
        is_auto_approved=is_auto_approved,
        risk_summary=risk_summary,
    )
    session.add(approval)
    session.flush()

    # ── 3. Auto-approve path: write the step row ──────────────────────────────
    if is_auto_approved:
        now = datetime.now(timezone.utc)
        step = ApprovalStep(
            approval_id=approval.id,
            approver_id=None,  # system action — no human actor
            step_order=1,
            decision=ApprovalDecision.auto_approve,
            reason=_build_auto_approve_rationale(assessment),
            decided_at=now,
        )
        session.add(step)
        session.flush()

    # ── 4. Audit via injected port ────────────────────────────────────────────
    if audit_writer is not None:
        action = "approval.auto_approved" if is_auto_approved else "approval.escalated"
        audit_writer(
            session,
            organization_id=org_id,
            action=action,
            entity_type="approval",
            entity_id=approval.id,
            actor_id=None,  # system-initiated (agent acts as system)
            before_state=None,
            after_state={
                "status": status.value,
                "is_auto_approved": is_auto_approved,
                "subject_type": subject_type.value,
                "subject_id": str(subject_id),
            },
        )

    logger.info(
        "persist_approval: %s %s/%s route=%s status=%s",
        "auto_approved" if is_auto_approved else "escalated",
        subject_type.value,
        subject_id,
        route,
        status.value,
    )
    return approval
