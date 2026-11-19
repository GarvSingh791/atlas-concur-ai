"""Orchestrator — the front door of Atlas's intelligence plane (P4-T7).

Classifies → routes → dispatches → logs.  Makes ZERO LLM calls itself.

handle_task() contract:
  1. Classify the Task with the deterministic IntentClassifier.
  2. Decide route: deterministic if confidence >= threshold AND NOT requires_reasoning;
     llm otherwise.
  3. Dispatch to the appropriate registered handler/agent (via AgentRegistry).
     If nothing is registered → fail safe (unavailable/needs_review status, no crash).
  4. Write exactly ONE ai_decision_logs row (agent='orchestrator') recording the
     routing decision for the M-R4 metric.
  5. Return an OrchestratorOutcome to the caller.  The caller owns session.commit().

No-LLM guarantee (structural):
  - Orchestrator never imports LlmProvider.
  - model_provider is always None in the DecisionRecord.
  - The only LLM path lives INSIDE a future agent; route='llm' merely names it.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, latency_timer, write_decision_log
from ai_agents.orchestrator.classifier import Classification, IntentClassifier
from ai_agents.orchestrator.envelopes import (
    AgentResult,
    AgentResultStatus,
    OrchestratorOutcome,
    RoutingDecision,
    Task,
)
from ai_agents.orchestrator.registry import AgentRegistry
from config.settings import get_settings
from database.models.ai_decision_log import AiAgent, AiRoute

logger = logging.getLogger(__name__)


class Orchestrator:
    """Deterministic router + dispatcher for the Atlas AI system (FR-A1).

    Args:
        registry: The AgentRegistry to dispatch through.
        classifier: Optional IntentClassifier override (default: IntentClassifier()).
        confidence_threshold: Override the env-driven ROUTING_CONFIDENCE_THRESHOLD.
            Use in tests to assert boundary behaviour without touching settings.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        classifier: IntentClassifier | None = None,
        *,
        confidence_threshold: float | None = None,
    ) -> None:
        self._registry = registry
        self._classifier = classifier or IntentClassifier()
        if confidence_threshold is not None:
            self._threshold = confidence_threshold
        else:
            self._threshold = get_settings().ROUTING_CONFIDENCE_THRESHOLD

    # ------------------------------------------------------------------ public

    def handle_task(
        self,
        task: Task,
        *,
        context: AgentContext,
        session: Session,
    ) -> OrchestratorOutcome:
        """Classify, route, dispatch, and log one task.

        Args:
            task: The incoming request envelope.
            context: Injected AgentContext (org/user/permissions).
            session: Caller-owned SQLAlchemy session; orchestrator does NOT commit.

        Returns:
            OrchestratorOutcome with the routing decision, agent result, and log id.
        """
        with latency_timer() as ms:
            classification = self._classifier.classify(task)
            decision = self._make_routing_decision(classification)
            result = self._dispatch(task, decision, context=context, session=session)

        log_id = self._write_log(
            task=task,
            decision=decision,
            result=result,
            latency_ms=ms[0],
            context=context,
            session=session,
        )

        logger.info(
            "orchestrator route=%s agent=%s task_type=%s conf=%.3f org=%s",
            decision.route.value,
            decision.target_agent.value,
            decision.task_type,
            decision.confidence,
            context.organization_id,
        )

        return OrchestratorOutcome(decision=decision, result=result, log_id=log_id)

    # ----------------------------------------------------------------- private

    def _make_routing_decision(self, cls: Classification) -> RoutingDecision:
        use_deterministic = cls.confidence >= self._threshold and not cls.requires_reasoning
        route = AiRoute.deterministic if use_deterministic else AiRoute.llm

        if use_deterministic:
            rationale = (
                f"confidence {cls.confidence:.3f} >= threshold {self._threshold:.3f} "
                f"and task does not require reasoning"
            )
        else:
            reason = (
                "requires agent reasoning"
                if cls.requires_reasoning
                else f"confidence {cls.confidence:.3f} < threshold {self._threshold:.3f}"
            )
            rationale = f"llm route: {reason}"

        return RoutingDecision(
            target_agent=cls.target_agent,
            route=route,
            task_type=cls.task_type,
            confidence=cls.confidence,
            rationale=rationale,
        )

    def _dispatch(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        if decision.route == AiRoute.deterministic:
            handler = self._registry.get_deterministic_handler(decision.task_type)
            if handler is None:
                logger.warning(
                    "orchestrator: no deterministic handler for task_type=%s; "
                    "returning needs_review",
                    decision.task_type,
                )
                return AgentResult(
                    status=AgentResultStatus.needs_review,
                    output={
                        "message": (
                            f"No deterministic handler registered for "
                            f"task_type={decision.task_type!r}. "
                            "Please route to an agent or register a handler."
                        )
                    },
                )
            return handler.handle(task, decision, context=context, session=session)

        # llm route → dispatch to registered agent
        agent = self._registry.get_agent(decision.target_agent)
        if agent is None:
            logger.warning(
                "orchestrator: no agent registered for agent_type=%s; " "returning unavailable",
                decision.target_agent.value,
            )
            return AgentResult(
                status=AgentResultStatus.unavailable,
                output={
                    "message": (
                        f"Agent {decision.target_agent.value!r} is not yet available. "
                        "This capability is under construction."
                    )
                },
            )
        return agent.handle(task, decision, context=context, session=session)

    def _write_log(
        self,
        *,
        task: Task,
        decision: RoutingDecision,
        result: AgentResult,
        latency_ms: int,
        context: AgentContext,
        session: Session,
    ) -> uuid.UUID | None:
        # input_summary: minimized — context_type + signal presence flags only.
        # NEVER include raw payload (PII risk).
        input_summary = {
            "context_type": task.context_type,
            "signal_keys": sorted(task.signals.keys()),
            "has_payload": bool(task.payload),
            "caller_task_type_hint": task.task_type,
        }
        output_summary = {
            "target_agent": decision.target_agent.value,
            "route": decision.route.value,
            "task_type": decision.task_type,
            "confidence": round(decision.confidence, 3),
            "rationale": decision.rationale,
            "result_status": result.status.value,
        }
        record = DecisionRecord(
            agent=AiAgent.orchestrator,
            task_type=decision.task_type,
            route=decision.route,
            model_provider=None,  # orchestrator never calls LLM
            input_summary=input_summary,
            output_summary=output_summary,
            confidence=decision.confidence,
            latency_ms=latency_ms,
        )
        try:
            entry = write_decision_log(session, context=context, record=record)
            return entry.id
        except Exception:
            logger.exception("orchestrator: failed to write decision log; continuing")
            return None
