"""Approval lifecycle hooks driven by the SUBJECT's owner (C7R / P12).

Kept free of the approval *service* (which imports the expense service) so the
expense service can call it without an import cycle: this module depends only
on core helpers and models.

One approval exists per subject (the ApprovalAgent is idempotent on
``(org, subject_type, subject_id)`` and POST /approvals 409s on a second
trigger).  The owner's SUBMIT opens it (``open_approval_on_submission``): the
deterministic Approval Agent runs in the submit transaction as a system
principal, so a submitted report / trip always reaches a reviewer without anyone
calling POST /approvals (which remains for backward compatibility and 409s once
the approval exists).  When a decision sent an expense report back to its owner
(``changes_requested`` → report ``draft``; or ``rejected`` → owner reopens to
``draft``) and the owner resubmits it, that SAME approval is re-queued as
``pending`` for a fresh human decision.  The decision history (steps) and the
original agent audit rows are preserved; the reopen is audited as
``approval.reopened``.

A returned report is an editable draft, so the agent's stored risk snapshot
(route, policy verdict, flags, total) describes contents that may no longer
exist.  The reopen therefore RE-RUNS the Approval Agent's deterministic risk
assessment (no LLM) over the subject's CURRENT entries, overwrites
``risk_summary`` with a forced ``escalate`` route (a human already sent it back),
and re-applies the independent-approver routing (C7-03).  The previous snapshot
is kept in the audit row's ``before_state``.

``ensure_independent_approver`` lives here (re-exported by the approval
service) so this module can apply it without importing the approval service.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.approval_agent.evaluate import (
    assess_risk,
    build_risk_summary,
    load_subject,
)
from ai_agents.orchestrator.envelopes import Task
from backend.app.core.audit import record_audit
from backend.app.core.concurrency import guarded_version_update
from backend.app.modules.ai import runtime as ai_runtime
from backend.app.modules.ai.runtime import build_agent_context, build_system_agent_context
from backend.app.modules.approval.grade_routing import route_by_grade
from config.constants.permissions import APPROVAL_DECIDE_REPORTS
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.identity import RoleKey, User, UserStatus

logger = logging.getLogger(__name__)

# Approval outcomes that hand the subject back to its owner.  A resubmission
# re-queues them; pending / approved / auto_approved approvals are left alone.
REOPENABLE_ON_RESUBMISSION: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.changes_requested, ApprovalStatus.rejected}
)


# Human decisions that hand a subject back to its owner.  A step with one of
# these in an approval's history means a reviewer has already looked at (and
# not accepted) the subject's contents — including a reversed auto-approval,
# which is recorded as a human reject / request_changes step on top of the
# agent's auto_approve step.
HUMAN_RETURN_DECISIONS: frozenset[ApprovalDecision] = frozenset(
    {ApprovalDecision.reject, ApprovalDecision.request_changes}
)


def subjects_with_human_review_history(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """The subset of *subject_ids* a human reviewer has returned (C7R SoD).

    A subject qualifies when its approval is CURRENTLY returned
    (changes_requested / rejected) or its decision history holds ANY human
    reject / request_changes step (past returns survive a resubmission, and a
    reversed auto-approval is such a step).  Used to keep a returned report's
    contents bound to it: they may be fixed and resubmitted (which re-queues the
    SAME approval for a human), but never moved to a fresh report whose first
    submission the agent could auto-approve.  Tenant-guarded by organization.
    """
    if not subject_ids:
        return set()
    human_return = (
        sa.select(ApprovalStep.id)
        .where(
            ApprovalStep.approval_id == Approval.id,
            ApprovalStep.decision.in_(HUMAN_RETURN_DECISIONS),
        )
        .exists()
    )
    rows = session.scalars(
        sa.select(Approval.subject_id).where(
            Approval.organization_id == organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id.in_(list(subject_ids)),
            sa.or_(
                Approval.current_status.in_(REOPENABLE_ON_RESUBMISSION),
                human_return,
            ),
        )
    ).all()
    return set(rows)


def subject_under_human_rereview(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> bool:
    """True while a human reviewer has this subject sent back or awaiting re-decision (C7R SoD).

    That is: its approval is currently changes_requested, or pending again with a human
    reject / request_changes step in its history.  While true the owner may not withdraw
    the subject (e.g. cancel a trip): it would launder the return and strand an approval
    nobody can decide.
    """
    human_return = (
        sa.select(ApprovalStep.id)
        .where(
            ApprovalStep.approval_id == Approval.id,
            ApprovalStep.decision.in_(HUMAN_RETURN_DECISIONS),
        )
        .exists()
    )
    found = session.scalar(
        sa.select(Approval.id).where(
            Approval.organization_id == organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
            sa.or_(
                Approval.current_status == ApprovalStatus.changes_requested,
                sa.and_(Approval.current_status == ApprovalStatus.pending, human_return),
            ),
        )
    )
    return found is not None


def owner_approval_info(
    session: Session,
    subject_type: ApprovalSubjectType,
    subjects: Sequence[Any],
    *,
    with_reason: bool = False,
) -> dict[uuid.UUID, tuple[str, str | None]]:
    """Map subject id → (approval status, returned reason) — one batched query.

    The owner has no approval.read, so an expense report or trip carries its own
    approval status (C7R / P12).  The reason is the latest human decision's
    reason, exposed only while the approval is changes_requested / rejected (a
    re-queued, pending approval must not show a stale reason).  *subjects* need
    ``id`` and ``organization_id``; tenant-guarded by organization.
    """
    if not subjects:
        return {}
    rows = session.execute(
        sa.select(Approval.id, Approval.subject_id, Approval.current_status).where(
            Approval.subject_type == subject_type,
            Approval.subject_id.in_([s.id for s in subjects]),
            Approval.organization_id.in_({s.organization_id for s in subjects}),
        )
    ).all()
    reasons: dict[uuid.UUID, str | None] = {}
    returned = [aid for aid, _sid, st in rows if st in REOPENABLE_ON_RESUBMISSION]
    if with_reason and returned:
        steps = session.execute(
            sa.select(ApprovalStep.approval_id, ApprovalStep.reason)
            .where(
                ApprovalStep.approval_id.in_(returned),
                ApprovalStep.approver_id.isnot(None),
            )
            .order_by(ApprovalStep.approval_id, ApprovalStep.step_order.desc())
        ).all()
        for approval_id, reason in steps:
            reasons.setdefault(approval_id, reason)
    return {sid: (st.value, reasons.get(aid)) for aid, sid, st in rows}


def _finance_fallback_approver(
    session: Session, *, organization_id: uuid.UUID, exclude: uuid.UUID
) -> uuid.UUID | None:
    """Deterministically pick an active finance_admin in the org other than *exclude*."""
    return session.scalars(
        sa.select(User.id)
        .where(
            User.organization_id == organization_id,
            User.role == RoleKey.finance_admin,
            User.status == UserStatus.active,
            User.id != exclude,
        )
        .order_by(User.created_at, User.id)
    ).first()


def ensure_independent_approver(session: Session, approval: Approval) -> None:
    """Never leave an escalated approval routed to nobody or to its submitter (C7-03).

    When the agent routed a pending approval to the submitter's manager but the
    submitter has no manager (null) or is their own manager (manager_id == self),
    re-route it to an active finance_admin who is not the submitter.  Flush-only;
    the caller owns the commit.  Auto-approved rows carry no human approver.
    """
    if approval.current_status != ApprovalStatus.pending:
        return
    summary = dict(approval.risk_summary or {})
    routed = summary.get("routed_to_approver_id")
    if routed is None or str(routed) == str(approval.submitter_id):
        fallback = _finance_fallback_approver(
            session, organization_id=approval.organization_id, exclude=approval.submitter_id
        )
        summary["routed_to_approver_id"] = str(fallback) if fallback is not None else None
        summary["routing_fallback"] = "finance_admin"
        approval.risk_summary = summary  # reassign so the JSONB change is tracked
        session.flush()
    # Follow-up (grades): a flag that names an approver GRADE (SVP / VP) routes the
    # approval to an approver of that grade (grade_routing).
    route_by_grade(session, approval)


def _submitter_manager(session: Session, submitter_id: uuid.UUID) -> str | None:
    """The submitter's manager id (the agent's escalation target), or None."""
    manager_id = session.scalar(sa.select(User.manager_id).where(User.id == submitter_id))
    return str(manager_id) if manager_id is not None else None


def _not_evaluated_summary(
    session: Session,
    *,
    subject_type: ApprovalSubjectType,
    submitter_id: uuid.UUID,
    rationale: str,
) -> dict[str, Any]:
    """Explicit "not evaluated" risk snapshot, routed for a HUMAN decision.

    Unknown, not "out of policy": nothing was evaluated (C7R review).  The route
    is escalate, so a human decides; the UI shows "Not evaluated".
    """
    return {
        "route": "escalate",
        "subject_type": subject_type.value,
        "subject_in_policy": None,
        "is_policy_compliant": None,
        "evaluation_failed": True,
        "blocking_flag_count": 0,
        "requires_approver": False,
        "subject_total_usd": None,
        "flags": [],
        "policy_rationale": [rationale],
        "routed_to_approver_id": _submitter_manager(session, submitter_id),
    }


def _reassess_risk_summary(
    session: Session,
    *,
    actor: User,
    submitter_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> dict[str, Any]:
    """Fresh, deterministic risk snapshot of the subject's CURRENT contents.

    Same evaluation the Approval Agent runs on trigger (fresh policy evaluation
    over the current entries, merged with stored pre-submit flags), but the
    route is forced to ``escalate``: a resubmission is always a human decision.
    Should the evaluation itself fail, the verdict fields are replaced by an
    explicit "not evaluated" snapshot so no stale in-policy verdict survives.
    """
    resubmitted_at = datetime.now(timezone.utc).isoformat()
    try:
        # Read-only evaluation inside a SAVEPOINT: a DB error here rolls back to
        # it, leaving the resubmission transaction usable for the fallback.
        with session.begin_nested():
            context = build_agent_context(actor, session)
            subject = load_subject(subject_type, subject_id, actor.organization_id, session)
            assessment = assess_risk(subject, subject_type, context=context, session=session)
            summary = build_risk_summary(assessment, route="escalate")
    except Exception as exc:  # defensive: never keep the stale verdict
        logger.warning(
            "reopen_on_resubmission: risk re-assessment failed for %s %s: %s",
            subject_type.value,
            subject_id,
            exc,
        )
        summary = _not_evaluated_summary(
            session,
            subject_type=subject_type,
            submitter_id=submitter_id,
            rationale="Risk re-assessment after resubmission failed; human review required.",
        )
    summary["resubmitted_at"] = resubmitted_at
    summary["reassessed_on_resubmission"] = True
    return summary


def reopen_on_resubmission(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
    actor: User,
) -> Approval | None:
    """Re-queue the subject's returned approval as ``pending`` (flush-only).

    Returns the reopened approval, or None when the subject has no approval or
    its approval is not in a returned state.  The reopened row is a human-review
    item: ``is_auto_approved`` is cleared (the agent's original auto-approve step
    stays in the history), ``risk_summary`` is RE-COMPUTED over the current
    contents (route ``escalate``, ``resubmitted_at`` +
    ``reassessed_on_resubmission`` markers) and the approval is routed to an
    independent approver (C7-03).
    """
    approval = session.scalars(
        sa.select(Approval)
        .where(
            Approval.organization_id == organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
        .with_for_update()
    ).first()
    if approval is None or approval.current_status not in REOPENABLE_ON_RESUBMISSION:
        return None

    before = {
        "current_status": approval.current_status.value,
        "is_auto_approved": approval.is_auto_approved,
        "risk_summary": approval.risk_summary,
    }
    summary = _reassess_risk_summary(
        session,
        actor=actor,
        submitter_id=approval.submitter_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    guarded_version_update(
        session,
        Approval,
        entity_id=approval.id,
        expected_version=approval.version,
        values={
            "current_status": ApprovalStatus.pending,
            "is_auto_approved": False,
            "risk_summary": summary,
        },
    )
    session.refresh(approval)
    ensure_independent_approver(session, approval)
    record_audit(
        session,
        organization_id=organization_id,
        action="approval.reopened",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=actor.id,
        before_state=before,
        after_state={
            "current_status": approval.current_status.value,
            "is_auto_approved": approval.is_auto_approved,
            "risk_summary": approval.risk_summary,
            "reason": f"{subject_type.value} resubmitted by its owner",
        },
    )
    return approval


# ---------------------------------------------------------------------------
# Opening the approval on the owner's submission (C7R — submit → approval)
# ---------------------------------------------------------------------------


def _find_approval(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> Approval | None:
    return session.scalars(
        sa.select(Approval).where(
            Approval.organization_id == organization_id,
            Approval.subject_type == subject_type,
            Approval.subject_id == subject_id,
        )
    ).first()


def _run_approval_agent(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
) -> Approval | None:
    """Run the deterministic Approval Agent (no LLM) for a just-submitted subject.

    The SAME orchestrator path POST /approvals uses (Task → ApprovalAgent →
    persist_approval, with the agent's own ``approval.auto_approved`` /
    ``approval.escalated`` audit row and decision logs), run as a SYSTEM
    principal: the submitter holds no approval.decide.reports and must never
    decide their own subject.  The grant is that one key, for this one run.

    Runs inside a SAVEPOINT so an agent/DB failure cannot poison the submit
    transaction.  Returns the persisted approval, or None when the agent did not
    persist one (an exception, or a needs_review outcome) — the caller falls back.
    """
    task = Task(
        context_type="approval",
        payload={"subject_type": subject_type.value, "subject_id": str(subject_id)},
        signals={"approval_required": True},
        task_type=None,
    )
    context = build_system_agent_context(
        organization_id, permission_keys=(APPROVAL_DECIDE_REPORTS,)
    )
    try:
        with session.begin_nested():
            outcome = ai_runtime.get_orchestrator().handle_task(
                task, context=context, session=session
            )
    except Exception:  # fail safe: the submit still succeeds (see the caller)
        logger.exception(
            "open_approval_on_submission: approval agent failed for %s %s",
            subject_type.value,
            subject_id,
        )
        return None
    approval = _find_approval(
        session,
        organization_id=organization_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if approval is None:
        logger.warning(
            "open_approval_on_submission: approval agent persisted no approval for %s %s "
            "(result status %s)",
            subject_type.value,
            subject_id,
            outcome.result.status.value,
        )
    return approval


def _open_fallback_approval(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
    submitter_id: uuid.UUID,
) -> Approval:
    """Fail-safe: a PENDING, human-review approval when the agent could not run.

    The subject is never auto-approved without an evaluation, and never left
    without an approval (which would strand it): it lands ``pending`` with an
    explicit "not evaluated" risk snapshot, routed to the submitter's manager or
    (via ensure_independent_approver) a finance_admin, and is visible in the
    org-wide finance queue.  Audited as ``approval.escalated`` (system actor).
    """
    summary = _not_evaluated_summary(
        session,
        subject_type=subject_type,
        submitter_id=submitter_id,
        rationale="Automatic risk assessment on submission failed; human review required.",
    )
    approval = Approval(
        organization_id=organization_id,
        subject_type=subject_type,
        subject_id=subject_id,
        submitter_id=submitter_id,
        current_status=ApprovalStatus.pending,
        is_auto_approved=False,
        risk_summary=summary,
    )
    session.add(approval)
    session.flush()
    record_audit(
        session,
        organization_id=organization_id,
        action="approval.escalated",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=None,
        before_state=None,
        after_state={
            "status": approval.current_status.value,
            "is_auto_approved": False,
            "subject_type": subject_type.value,
            "subject_id": str(subject_id),
            "evaluation_failed": True,
        },
    )
    return approval


def open_approval_on_submission(
    session: Session,
    *,
    organization_id: uuid.UUID,
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
    actor: User,
) -> Approval:
    """Open (or re-queue) the subject's approval as part of the owner's submit.

    Called by the expense / travel services in the SAME transaction as the
    owner's submit transition (flush-only; the caller commits), so a submitted
    subject never exists without an approval a reviewer can act on:

    * a RETURNED approval (changes_requested / rejected) → re-queued as pending
      (reopen_on_resubmission) — never duplicated;
    * any other existing approval → left untouched and returned;
    * no approval yet → the Approval Agent runs (system principal, no LLM) and
      auto-approves or escalates exactly as POST /approvals would.  An agent
      failure falls back to a pending, not-evaluated approval (fail safe).

    A new approval is routed to an independent approver (C7-03 — never the
    submitter).  The CALLER moves an ``auto_approved`` subject to ``approved``
    through its own workflow state machine (this module cannot import the
    subject services: import cycle).
    """
    reopened = reopen_on_resubmission(
        session,
        organization_id=organization_id,
        subject_type=subject_type,
        subject_id=subject_id,
        actor=actor,
    )
    if reopened is not None:
        return reopened
    existing = _find_approval(
        session,
        organization_id=organization_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if existing is not None:
        return existing

    session.flush()  # the submitted state must be visible to the agent's reads
    approval = _run_approval_agent(
        session,
        organization_id=organization_id,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    if approval is None:
        approval = _open_fallback_approval(
            session,
            organization_id=organization_id,
            subject_type=subject_type,
            subject_id=subject_id,
            submitter_id=actor.id,
        )
    ensure_independent_approver(session, approval)
    return approval
