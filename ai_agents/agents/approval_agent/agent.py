"""ApprovalAgent — deterministic approval routing agent (P4-T12).

Implements the Agent protocol (P4-T7 contracts.py).
Registered as AiAgent.approval in the AgentRegistry by register_approval().

Pipeline (100% deterministic — no LLM):
  1. Parse & validate payload     — deterministic
  2. Load subject + org-scope     — deterministic (DB read)
  3. Permission gate              — deterministic (NFR-SEC6)
  4. Assess risk                  — deterministic (fresh PolicyEvalTool run over
                                    the report's entries + stored flags, C7-05)
  5. Decide route                 — deterministic (binary gate)
  6. Persist (flush-only)         — deterministic (idempotency check → write)
  7. Write decision log           — always

reflection_outcome=None — agent is 100% deterministic; no LLM critic loop.
model_provider=None     — no LLM call on this agent.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.approval_agent.evaluate import (
    RiskAssessment,
    SubjectLoadError,
    assess_risk,
    load_subject,
)
from ai_agents.agents.approval_agent.persistence import (
    AuditWriter,
    IdempotencyError,
    persist_approval,
)
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.guardrails.actions import AgentAction, check_action
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.approval import ApprovalSubjectType

logger = logging.getLogger(__name__)


class ApprovalAgent:
    """Deterministic approval routing agent implementing the Agent protocol (P4-T7).

    100% deterministic — no LLM call, no reflection loop.
    Registered as AiAgent.approval.
    """

    agent_type: AiAgent = AiAgent.approval

    def __init__(self, audit_writer: AuditWriter | None = None) -> None:
        self._audit_writer = audit_writer

    def handled_task_types(self) -> frozenset[str]:
        return frozenset({"approval_decision"})

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = dict(task.payload)

        # ── 1. PARSE & VALIDATE PAYLOAD ───────────────────────────────────────
        subject_type_raw = payload.get("subject_type")
        subject_id_raw = payload.get("subject_id")

        try:
            subject_type = ApprovalSubjectType(subject_type_raw)
        except (ValueError, KeyError):
            return self._needs_review(
                f"Invalid subject_type: {subject_type_raw!r}",
                decision,
                t0,
                payload,
                context,
                session,
            )

        try:
            subject_id = uuid.UUID(str(subject_id_raw))
        except (ValueError, TypeError, AttributeError):
            return self._needs_review(
                f"Invalid subject_id: {subject_id_raw!r}",
                decision,
                t0,
                payload,
                context,
                session,
            )

        # ── 2. LOAD SUBJECT (org-scope check) ────────────────────────────────
        try:
            subject = load_subject(subject_type, subject_id, context.organization_id, session)
        except SubjectLoadError as exc:
            return self._needs_review(
                str(exc),
                decision,
                t0,
                payload,
                context,
                session,
            )

        # ── 3. PERMISSION GATE (NFR-SEC6) ─────────────────────────────────────
        try:
            check_action(context, AgentAction.APPROVAL_DECIDE)
        except GuardrailBlocked as exc:
            return self._needs_review(
                str(exc),
                decision,
                t0,
                payload,
                context,
                session,
            )

        # ── 4. ASSESS RISK ────────────────────────────────────────────────────
        assessment: RiskAssessment = assess_risk(
            subject, subject_type, context=context, session=session
        )

        # ── 5. DECIDE ROUTE ───────────────────────────────────────────────────
        route = decide_route(assessment)

        # ── 6. PERSIST (flush-only; idempotency check inside) ─────────────────
        try:
            approval = persist_approval(
                session,
                subject_type=subject_type,
                subject_id=subject_id,
                submitter_id=assessment.submitter_id,
                context=context,
                route=route,
                assessment=assessment,
                audit_writer=self._audit_writer,
            )
        except IdempotencyError as exc:
            return self._needs_review(
                str(exc),
                decision,
                t0,
                payload,
                context,
                session,
            )

        # ── 7. DECISION LOG ───────────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.approval,
                task_type=decision.task_type,
                route=AiRoute.deterministic,
                model_provider=None,
                input_summary={
                    "subject_type": subject_type.value,
                    "subject_id": str(subject_id),
                },
                tools_used={},
                output_summary={
                    "route": route,
                    "approval_id": str(approval.id),
                    "status": approval.current_status.value,
                    "subject_in_policy": assessment.subject_in_policy,
                    "is_policy_compliant": assessment.is_policy_compliant,
                    "blocking_flag_count": assessment.blocking_flag_count,
                    "requires_approver": assessment.requires_approver,
                    "subject_total_usd": assessment.subject_total,
                },
                confidence=1.0,
                reflection_outcome=None,
                latency_ms=latency_ms,
            ),
        )

        # ── RESULT ────────────────────────────────────────────────────────────
        output: dict[str, Any] = {
            "route": route,
            "approval_id": str(approval.id),
            "status": approval.current_status.value,
            "subject_type": subject_type.value,
            "subject_id": str(subject_id),
            "subject_in_policy": assessment.subject_in_policy,
            "is_policy_compliant": assessment.is_policy_compliant,
            "blocking_flag_count": assessment.blocking_flag_count,
        }
        if assessment.policy_rationale:
            output["policy_rationale"] = list(assessment.policy_rationale)
        if route == "escalate" and assessment.manager_id is not None:
            output["routed_to_approver_id"] = str(assessment.manager_id)

        return AgentResult(
            status=AgentResultStatus.ok,
            output=output,
            citations=(),
            confidence=1.0,
            reflection_outcome=None,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _needs_review(
        self,
        reason: str,
        decision: RoutingDecision,
        t0: float,
        payload: dict[str, Any],
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        """Write a decision log and return needs_review without any DB writes."""
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            write_decision_log(
                session,
                context=context,
                record=DecisionRecord(
                    agent=AiAgent.approval,
                    task_type=decision.task_type,
                    route=AiRoute.deterministic,
                    model_provider=None,
                    input_summary={
                        "subject_type": payload.get("subject_type"),
                        "subject_id": str(payload.get("subject_id", "")),
                    },
                    tools_used={},
                    output_summary={"denied": True, "reason": reason},
                    confidence=None,
                    reflection_outcome=None,
                    latency_ms=latency_ms,
                ),
            )
        except Exception as log_exc:
            logger.warning("ApprovalAgent: decision log write failed: %s", log_exc)

        return AgentResult(
            status=AgentResultStatus.needs_review,
            output={"denied": True, "reason": reason},
            citations=(),
            confidence=None,
            reflection_outcome=None,
        )
