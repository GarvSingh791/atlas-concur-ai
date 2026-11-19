"""Typed envelopes for the orchestrator/router layer (P4-T7).

Task          — incoming request envelope (opaque payload, classification signals).
Classification — deterministic classifier output; authoritative task_type + confidence.
RoutingDecision — what route + agent the orchestrator chose (logged as M-R4).
AgentResult   — what a handler/agent returns to the orchestrator.
OrchestratorOutcome — the full result returned to the caller.

Design decisions:
- All envelopes are frozen dataclasses for immutability (same idiom as AgentContext).
- Task.payload is opaque to the orchestrator (agents know their own I/O schemas).
- Task.task_type is an OPTIONAL caller hint; RoutingDecision.task_type is authoritative.
- AgentResult.citations reuses the Citation type from ai_agents.guardrails.citations.
- LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ai_agents.guardrails.citations import Citation
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome


class AgentResultStatus(str, enum.Enum):
    """Outcome category of an agent/handler dispatch."""

    ok = "ok"
    """Handler ran successfully."""
    unavailable = "unavailable"
    """No agent/handler registered for the requested target (fail-safe)."""
    needs_review = "needs_review"
    """Handled with low confidence / default; human review recommended."""


@dataclass(frozen=True)
class Task:
    """Incoming request envelope submitted to the orchestrator.

    context_type is the primary routing signal; signals carries additional
    classification hints (e.g., approval_required flag, task-specific keys).
    payload is opaque to the orchestrator — agents own their own I/O schemas.
    task_type is an optional caller hint (the classifier may override it).
    """

    context_type: str | None
    """ConversationContextType value (travel_planning/expense/policy_qa/general/None)."""
    payload: Mapping[str, Any]
    """Opaque content for the downstream agent; orchestrator does not inspect it."""
    signals: Mapping[str, Any] = field(default_factory=dict)
    """Extra classification hints (e.g., {'approval_required': True})."""
    task_type: str | None = None
    """Optional caller-declared task type hint; superseded by classifier output."""


@dataclass(frozen=True)
class Classification:
    """Output of the deterministic intent classifier.

    task_type here is authoritative and propagates to RoutingDecision + the log row.
    requires_reasoning=True forces the llm route even when confidence is high.
    """

    target_agent: AiAgent
    task_type: str
    confidence: float
    """Classification confidence in [0, 1]."""
    requires_reasoning: bool
    """True when the task semantically requires agent LLM reasoning (→ forces llm route)."""


@dataclass(frozen=True)
class RoutingDecision:
    """The orchestrator's routing choice for one task.

    Persisted as one ai_decision_logs row (agent='orchestrator').
    """

    target_agent: AiAgent
    route: AiRoute
    task_type: str
    confidence: float
    rationale: str
    """Human-readable explanation of why this route + agent were chosen."""


@dataclass(frozen=True)
class AgentResult:
    """What a registered agent or deterministic handler returns.

    citations reuses the Citation type from ai_agents.guardrails.citations so
    agents can return grounded evidence without coupling to a separate type.
    """

    status: AgentResultStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    citations: tuple[Citation, ...] = ()
    confidence: float | None = None
    reflection_outcome: ReflectionOutcome | None = None


@dataclass(frozen=True)
class OrchestratorOutcome:
    """The full result returned to the caller of Orchestrator.handle_task().

    log_id is the UUID of the persisted ai_decision_logs row; None only if the
    write failed (which is non-fatal — routing still proceeds).
    """

    decision: RoutingDecision
    result: AgentResult
    log_id: uuid.UUID | None
