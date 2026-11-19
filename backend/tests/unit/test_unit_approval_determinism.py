"""P7-T1 · items G + F — deterministic approval decision path (no LLM).

Targets ``backend.app.modules.approval.service.decide_approval`` and proves the
approval decision modules are LLM-free.

CURRENT, source-verified behavior (NOT the doc-ideal):
  * Decidable ONLY on {pending, auto_approved} (``_DECIDABLE_STATUSES``); any
    already-resolved status → ConflictError (409).
  * Invalid decision string → BadRequestError (400).
  * reject / request_changes without a (non-whitespace) reason →
    UnprocessableEntityError (422) — decide_approval delegates to the shared
    validate_required_reason helper, which strips and rejects blank reasons.
  * Stale expected_version → ConflictError (409) via guarded_version_update.
  * auto_approved→approved is allowed (US-M2 reverse-within-policy).

No-LLM proof mirrors ``ai_agents/tests/test_policy_agent.py:1032``:
``assert not hasattr(<module>, "get_llm_provider")``.

``derive_risk_level`` is NOT re-tested here — it is covered by
``backend/tests/test_approval_risk.py`` (7 tests); this file cites it for G's
risk-mapping rather than duplicating.

All decide_approval tests are live-DB (inherited ``db_session``; self-skips if
Postgres is unreachable). The actor is a finance_admin (org-wide scope) so the
scope-checked load isolates the decision logic from record-scoping (which is
covered in test_unit_rbac_scoping).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from backend.app.modules.approval import risk, service
from backend.app.modules.approval.service import _DECIDABLE_STATUSES, decide_approval
from database.models.approval import ApprovalStatus
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.identity import Organization, RoleKey, User


def _step_count(session: Session, approval_id: uuid.UUID) -> int:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(ApprovalStep)
            .where(ApprovalStep.approval_id == approval_id)
        )
        or 0
    )


def _audit_count(session: Session, entity_id: uuid.UUID) -> int:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_type == "approval",
                AuditLog.entity_id == entity_id,
            )
        )
        or 0
    )


@pytest.fixture()
def decider(db_session: Session, org_a: Organization) -> User:
    """An org-A finance_admin — org-wide scope so decide_approval can load any
    approval in the org, isolating the decision logic from record-scoping."""
    return f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)


# ===========================================================================
# No-LLM proof (item G) — deterministic modules never imported the provider
# ===========================================================================


def test_approval_service_has_no_llm_provider() -> None:
    """The approval service is a deterministic router — it must never have
    imported get_llm_provider (mirrors test_policy_agent.py:1032)."""
    assert not hasattr(service, "get_llm_provider")


def test_approval_risk_has_no_llm_provider() -> None:
    """Risk-level derivation is a pure function — no LLM provider import."""
    assert not hasattr(risk, "get_llm_provider")
    assert not hasattr(risk, "LlmProvider")


# ===========================================================================
# decide_approval — happy paths (deterministic step + audit)
# ===========================================================================


def test_decide_pending_to_approved_records_step_and_audit(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """pending→approved appends exactly one step, bumps version, writes one audit
    row, and returns status 'approved' — all deterministic, no LLM."""
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)
    assert _step_count(db_session, approval.id) == 0
    audits_before = _audit_count(db_session, approval.id)
    version_before = approval.version

    result = decide_approval(db_session, decider, approval.id, decision="approve", reason=None)

    assert result.current_status == "approved"
    assert _step_count(db_session, approval.id) == 1
    assert _audit_count(db_session, approval.id) == audits_before + 1
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.approved
    assert approval.version == version_before + 1
    # The recorded step is an 'approve' decision by this actor.
    step = db_session.scalars(
        sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
    ).first()
    assert step is not None
    assert step.decision == ApprovalDecision.approve
    assert step.approver_id == decider.id


def test_decide_auto_approved_to_approved_is_allowed(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """auto_approved is a decidable status (US-M2 reverse-within-policy): a human
    can act on an agent-auto-approved item."""
    approval = f.make_approval(
        db_session,
        org=org_a,
        current_status=ApprovalStatus.auto_approved,
        is_auto_approved=True,
    )

    result = decide_approval(db_session, decider, approval.id, decision="approve", reason=None)

    assert result.current_status == "approved"
    assert _step_count(db_session, approval.id) == 1


def test_decide_reject_with_reason_records_reason(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """reject WITH a reason succeeds and persists the reason on the step.

    The agent never auto-rejects — reject is always a recorded human step.
    """
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)

    result = decide_approval(
        db_session,
        decider,
        approval.id,
        decision="reject",
        reason="Out of policy: business class on a short-haul flight.",
    )

    assert result.current_status == "rejected"
    step = db_session.scalars(
        sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
    ).first()
    assert step is not None
    assert step.decision == ApprovalDecision.reject
    assert step.reason is not None and "Out of policy" in step.reason


# ===========================================================================
# decide_approval — guard rails (edge cases)
# ===========================================================================


@pytest.mark.parametrize(
    "resolved_status",
    [
        ApprovalStatus.approved,
        ApprovalStatus.rejected,
        ApprovalStatus.changes_requested,
    ],
)
def test_decide_on_resolved_approval_raises_conflict(
    db_session: Session,
    org_a: Organization,
    decider: User,
    resolved_status: ApprovalStatus,
) -> None:
    """Already-resolved approvals are not in _DECIDABLE_STATUSES → ConflictError,
    no new step, no audit row."""
    assert resolved_status not in _DECIDABLE_STATUSES  # guards the premise
    approval = f.make_approval(db_session, org=org_a, current_status=resolved_status)
    steps_before = _step_count(db_session, approval.id)
    audits_before = _audit_count(db_session, approval.id)

    with pytest.raises(ConflictError):
        decide_approval(db_session, decider, approval.id, decision="approve", reason=None)

    db_session.refresh(approval)
    assert approval.current_status == resolved_status  # unchanged
    assert _step_count(db_session, approval.id) == steps_before
    assert _audit_count(db_session, approval.id) == audits_before


def test_decide_invalid_decision_string_raises_bad_request(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """An unknown decision verb → BadRequestError (400), no step written."""
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)

    with pytest.raises(BadRequestError):
        decide_approval(db_session, decider, approval.id, decision="annihilate", reason=None)

    assert _step_count(db_session, approval.id) == 0


@pytest.mark.parametrize("decision", ["reject", "request_changes"])
@pytest.mark.parametrize("reason", [None, ""])
def test_decide_reject_without_reason_raises_unprocessable(
    db_session: Session,
    org_a: Organization,
    decider: User,
    decision: str,
    reason: str | None,
) -> None:
    """reject / request_changes with a None/empty reason → UnprocessableEntityError (422).

    decide_approval delegates to the shared ``validate_required_reason`` helper,
    which raises ``UnprocessableEntityError`` (422) for an absent or blank reason.

    Mutation target: removing the reason check lets this through (no error raised).
    """
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)

    with pytest.raises(UnprocessableEntityError):
        decide_approval(db_session, decider, approval.id, decision=decision, reason=reason)

    assert _step_count(db_session, approval.id) == 0


@pytest.mark.parametrize("decision", ["reject", "request_changes"])
def test_decide_reject_whitespace_reason_is_rejected(
    db_session: Session,
    org_a: Organization,
    decider: User,
    decision: str,
) -> None:
    """A whitespace-only reason is REJECTED for reject/request_changes.

    decide_approval delegates to ``validate_required_reason``, which strips the
    reason before checking, so "   " is treated as blank → UnprocessableEntityError
    (422) and no step is written.
    """
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)

    with pytest.raises(UnprocessableEntityError):
        decide_approval(db_session, decider, approval.id, decision=decision, reason="   ")

    assert _step_count(db_session, approval.id) == 0


def test_decide_stale_version_raises_conflict(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """A stale expected_version fails the CAS → ConflictError (409). A step row
    is appended before the CAS (append-only history), but the status transition
    is rolled back by get_db; status stays pending."""
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)
    stale = approval.version - 1

    with pytest.raises(ConflictError):
        decide_approval(
            db_session,
            decider,
            approval.id,
            decision="approve",
            reason=None,
            expected_version=stale,
        )

    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending  # not transitioned


def test_decide_out_of_scope_approval_raises_not_found(
    db_session: Session, org_a: Organization, org_b: Organization
) -> None:
    """An approval in org B is a 404 to an org-A decider (scope-checked load,
    no existence leak)."""
    decider_a = f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)
    approval_b = f.make_approval(db_session, org=org_b, current_status=ApprovalStatus.pending)

    with pytest.raises(NotFoundError):
        decide_approval(db_session, decider_a, approval_b.id, decision="approve", reason=None)


def test_request_changes_with_reason_succeeds(
    db_session: Session, org_a: Organization, decider: User
) -> None:
    """request_changes WITH a reason transitions to changes_requested — control
    for the missing-reason guard above."""
    approval = f.make_approval(db_session, org=org_a, current_status=ApprovalStatus.pending)

    result = decide_approval(
        db_session,
        decider,
        approval.id,
        decision="request_changes",
        reason="Please attach the itemized hotel folio.",
    )

    assert result.current_status == "changes_requested"
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.changes_requested
