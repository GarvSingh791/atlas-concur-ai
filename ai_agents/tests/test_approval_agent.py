"""Unit tests for the Approval Agent (P4-T12) — no DB required.

Covers:
  - routing.decide_route: all gate conditions + boundary values
  - evaluate.build_risk_summary: serialisation shape
  - evaluate.assess_expense_report and assess_trip logic (via mocked subjects)
  - persistence.persist_approval: idempotency error + both routes (mocked session)
  - agent.ApprovalAgent.handle: full pipeline with mocked DB calls
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.agents.approval_agent.evaluate import (
    RiskAssessment,
    SubjectLoadError,
    build_risk_summary,
    load_subject,
)
from ai_agents.agents.approval_agent.persistence import IdempotencyError
from ai_agents.agents.approval_agent.routing import (
    LOW_RISK_AUTO_APPROVE_MAX_USD,
    decide_route,
)
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from config.constants.permissions import APPROVAL_DECIDE_REPORTS
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.approval import ApprovalStatus, ApprovalSubjectType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
MANAGER_ID = uuid.uuid4()
SUBJECT_ID = uuid.uuid4()


def _ctx(*, with_perm: bool = True) -> AgentContext:
    keys = frozenset({APPROVAL_DECIDE_REPORTS}) if with_perm else frozenset()
    return AgentContext(
        organization_id=ORG_ID,
        user_id=USER_ID,
        granted_permission_keys=keys,
        correlation_id="unit-test",
    )


def _decision() -> RoutingDecision:
    return RoutingDecision(
        target_agent=AiAgent.approval,
        route=AiRoute.deterministic,
        task_type="approval_decision",
        confidence=0.95,
        rationale="unit-test",
    )


def _task(subject_type: str, subject_id: str) -> Task:
    return Task(
        context_type="approval",
        payload={"subject_type": subject_type, "subject_id": subject_id},
        signals={"approval_required": True},
    )


def _assessment(
    *,
    subject_in_policy: bool = True,
    blocking_flag_count: int = 0,
    requires_approver: bool = False,
    subject_total: float | None = 200.0,
    subject_type: ApprovalSubjectType = ApprovalSubjectType.expense_report,
) -> RiskAssessment:
    return RiskAssessment(
        subject_type=subject_type,
        subject_id=SUBJECT_ID,
        submitter_id=USER_ID,
        manager_id=MANAGER_ID,
        subject_in_policy=subject_in_policy,
        blocking_flag_count=blocking_flag_count,
        requires_approver=requires_approver,
        subject_total=subject_total,
        flags_snapshot=[],
        # C7-05: auto-approval now requires an EXPLICIT compliance verdict from
        # the fresh approval-time evaluation; mirror subject_in_policy here.
        is_policy_compliant=subject_in_policy,
    )


# ---------------------------------------------------------------------------
# routing.decide_route tests
# ---------------------------------------------------------------------------


class TestDecideRoute:
    def test_all_green_auto_approves(self) -> None:
        assert decide_route(_assessment()) == "auto_approve"

    def test_not_in_policy_escalates(self) -> None:
        assert decide_route(_assessment(subject_in_policy=False)) == "escalate"

    def test_blocking_flag_escalates(self) -> None:
        assert decide_route(_assessment(blocking_flag_count=1)) == "escalate"

    def test_requires_approver_escalates(self) -> None:
        assert decide_route(_assessment(requires_approver=True)) == "escalate"

    def test_unknown_total_escalates(self) -> None:
        assert decide_route(_assessment(subject_total=None)) == "escalate"

    def test_at_threshold_auto_approves(self) -> None:
        a = _assessment(subject_total=LOW_RISK_AUTO_APPROVE_MAX_USD)
        assert decide_route(a) == "auto_approve"

    def test_above_threshold_escalates(self) -> None:
        a = _assessment(subject_total=LOW_RISK_AUTO_APPROVE_MAX_USD + 0.01)
        assert decide_route(a) == "escalate"

    def test_zero_amount_auto_approves(self) -> None:
        assert decide_route(_assessment(subject_total=0.0)) == "auto_approve"

    def test_threshold_constant_is_500(self) -> None:
        assert LOW_RISK_AUTO_APPROVE_MAX_USD == 500.00


# ---------------------------------------------------------------------------
# evaluate.build_risk_summary tests
# ---------------------------------------------------------------------------


class TestBuildRiskSummary:
    def test_auto_approve_keys(self) -> None:
        summary = build_risk_summary(_assessment(), route="auto_approve")
        assert summary["route"] == "auto_approve"
        assert summary["subject_in_policy"] is True
        assert summary["blocking_flag_count"] == 0
        assert summary["requires_approver"] is False
        assert summary["subject_total_usd"] == 200.0
        assert "routed_to_approver_id" not in summary

    def test_escalate_keys(self) -> None:
        a = _assessment(subject_in_policy=False)
        summary = build_risk_summary(a, route="escalate")
        assert summary["route"] == "escalate"
        assert "routed_to_approver_id" in summary
        assert summary["routed_to_approver_id"] == str(MANAGER_ID)

    def test_escalate_no_manager(self) -> None:
        a = RiskAssessment(
            subject_type=ApprovalSubjectType.expense_report,
            subject_id=SUBJECT_ID,
            submitter_id=USER_ID,
            manager_id=None,
            subject_in_policy=False,
            blocking_flag_count=1,
            requires_approver=False,
            subject_total=600.0,
            flags_snapshot=[],
        )
        summary = build_risk_summary(a, route="escalate")
        assert summary["routed_to_approver_id"] is None


# ---------------------------------------------------------------------------
# evaluate.load_subject tests (mocked session)
# ---------------------------------------------------------------------------


class TestLoadSubject:
    def test_expense_report_found(self) -> None:
        mock_session = MagicMock()
        mock_report = MagicMock()
        mock_report.organization_id = ORG_ID
        mock_session.get.return_value = mock_report

        result = load_subject(ApprovalSubjectType.expense_report, SUBJECT_ID, ORG_ID, mock_session)
        assert result is mock_report

    def test_expense_report_not_found_raises(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with pytest.raises(SubjectLoadError, match="not found"):
            load_subject(ApprovalSubjectType.expense_report, SUBJECT_ID, ORG_ID, mock_session)

    def test_expense_report_wrong_org_raises(self) -> None:
        mock_session = MagicMock()
        mock_report = MagicMock()
        mock_report.organization_id = uuid.uuid4()  # different org
        mock_session.get.return_value = mock_report
        with pytest.raises(SubjectLoadError, match="different organisation"):
            load_subject(ApprovalSubjectType.expense_report, SUBJECT_ID, ORG_ID, mock_session)

    def test_trip_found(self) -> None:
        mock_session = MagicMock()
        mock_trip = MagicMock()
        mock_trip.organization_id = ORG_ID
        mock_session.get.return_value = mock_trip

        result = load_subject(ApprovalSubjectType.trip, SUBJECT_ID, ORG_ID, mock_session)
        assert result is mock_trip

    def test_trip_not_found_raises(self) -> None:
        mock_session = MagicMock()
        mock_session.get.return_value = None
        with pytest.raises(SubjectLoadError, match="not found"):
            load_subject(ApprovalSubjectType.trip, SUBJECT_ID, ORG_ID, mock_session)

    def test_trip_wrong_org_raises(self) -> None:
        mock_session = MagicMock()
        mock_trip = MagicMock()
        mock_trip.organization_id = uuid.uuid4()
        mock_session.get.return_value = mock_trip
        with pytest.raises(SubjectLoadError, match="different organisation"):
            load_subject(ApprovalSubjectType.trip, SUBJECT_ID, ORG_ID, mock_session)


# ---------------------------------------------------------------------------
# agent.ApprovalAgent.handle unit tests (all mocked)
# ---------------------------------------------------------------------------

from ai_agents.agents.approval_agent.agent import ApprovalAgent  # noqa: E402


def _make_approval_mock(
    *,
    status: ApprovalStatus = ApprovalStatus.auto_approved,
    is_auto_approved: bool = True,
) -> MagicMock:
    mock = MagicMock()
    mock.id = uuid.uuid4()
    mock.current_status = status
    mock.is_auto_approved = is_auto_approved
    return mock


class TestApprovalAgentHandle:
    """Pipeline tests with all DB calls mocked."""

    def _run(
        self,
        payload: dict,
        *,
        with_perm: bool = True,
        subject_found: bool = True,
        subject_org_ok: bool = True,
        assessment: RiskAssessment | None = None,
        route: str = "auto_approve",
        idempotent: bool = False,
    ) -> Any:
        agent = ApprovalAgent(audit_writer=None)
        ctx = _ctx(with_perm=with_perm)
        mock_session = MagicMock()
        decision = _decision()

        if assessment is None:
            assessment = _assessment()

        mock_subject = MagicMock()
        mock_subject.organization_id = ORG_ID if subject_org_ok else uuid.uuid4()
        mock_session.get.return_value = mock_subject if subject_found else None

        auto = route == "auto_approve"
        mock_approval = _make_approval_mock(
            status=ApprovalStatus.auto_approved if auto else ApprovalStatus.pending,
            is_auto_approved=auto,
        )

        with (
            patch(
                "ai_agents.agents.approval_agent.agent.load_subject",
                side_effect=(
                    SubjectLoadError("not found")
                    if not subject_found
                    else lambda *a, **kw: mock_subject
                ),
            ),
            patch(
                "ai_agents.agents.approval_agent.agent.assess_risk",
                return_value=assessment,
            ),
            patch(
                "ai_agents.agents.approval_agent.agent.decide_route",
                return_value=route,
            ),
            patch(
                "ai_agents.agents.approval_agent.agent.persist_approval",
                side_effect=(
                    IdempotencyError("already exists")
                    if idempotent
                    else lambda *a, **kw: mock_approval
                ),
            ),
            patch("ai_agents.agents.approval_agent.agent.write_decision_log"),
        ):
            task = Task(
                context_type="approval",
                payload=payload,
                signals={"approval_required": True},
            )
            return agent.handle(task, decision, context=ctx, session=mock_session)

    def test_auto_approve_returns_ok(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
            route="auto_approve",
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["route"] == "auto_approve"

    def test_escalate_returns_ok(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
            route="escalate",
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["route"] == "escalate"

    def test_invalid_subject_type_needs_review(self) -> None:
        result = self._run({"subject_type": "unknown_type", "subject_id": str(SUBJECT_ID)})
        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True

    def test_invalid_subject_id_needs_review(self) -> None:
        result = self._run({"subject_type": "expense_report", "subject_id": "not-a-uuid"})
        assert result.status == AgentResultStatus.needs_review

    def test_missing_permission_needs_review(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
            with_perm=False,
        )
        assert result.status == AgentResultStatus.needs_review

    def test_subject_not_found_needs_review(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
            subject_found=False,
        )
        assert result.status == AgentResultStatus.needs_review

    def test_idempotency_needs_review(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
            idempotent=True,
        )
        assert result.status == AgentResultStatus.needs_review

    def test_trip_subject_type_accepted(self) -> None:
        result = self._run(
            {"subject_type": "trip", "subject_id": str(SUBJECT_ID)},
            route="escalate",
            assessment=_assessment(subject_type=ApprovalSubjectType.trip),
        )
        assert result.status == AgentResultStatus.ok

    def test_confidence_is_1(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
        )
        assert result.confidence == 1.0

    def test_reflection_outcome_none(self) -> None:
        result = self._run(
            {"subject_type": "expense_report", "subject_id": str(SUBJECT_ID)},
        )
        assert result.reflection_outcome is None
