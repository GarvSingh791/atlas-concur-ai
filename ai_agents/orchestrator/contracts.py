"""Dispatch contracts for the orchestrator layer (P4-T7).

Agent              — the protocol every specialized agent (P4-T8+) must implement.
DeterministicHandler — the protocol for deterministic (no-LLM) task handlers.

Both protocols mirror the established (intent, *, context, session) idiom used by
GroundingTool and GroundedRetriever; the caller injects AgentContext + Session unchanged
(scoped-context pass-through, NFR-SEC5/SEC6).

Design decisions:
- @runtime_checkable so isinstance() works in the registry for safety checks.
- Agent carries an agent_type class attribute so the registry can key on it without
  requiring the agent to be registered under a separately specified key.
- DeterministicHandler has no agent_type — deterministic handlers are registered
  by task_type string, not by AiAgent enum (they may serve multiple task types).
- Neither protocol imports LlmProvider — the orchestrator never calls the LLM;
  LLM usage lives entirely inside specialized agents.
- LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResult, RoutingDecision, Task
from database.models.ai_decision_log import AiAgent


@runtime_checkable
class Agent(Protocol):
    """Minimum stable contract every specialized agent (P4-T8+) implements.

    The orchestrator dispatches via this protocol; agents need not inherit from
    it — structural conformance (duck-typing) is sufficient due to @runtime_checkable.

    Attributes:
        agent_type: Identifies which AiAgent enum slot this agent fills.  The
            registry uses this as the dispatch key.

    Methods:
        handled_task_types: Returns the set of task_type strings this agent can serve.
            Used for registry validation; empty set means the agent handles all
            task_types routed to it (agent decides internally).
        handle: Execute the task and return an AgentResult.  The agent owns its own
            LLM calls, tool usage, and reflection (P4-T3) internally.
    """

    agent_type: AiAgent

    def handled_task_types(self) -> frozenset[str]:
        """Task types this agent is prepared to serve.

        An empty frozenset signals "all task types routed to me".
        The registry does not enforce this; it is advisory for documentation.
        """
        ...

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        """Execute the task and return an AgentResult.

        Args:
            task: The original request envelope (payload opaque to orchestrator).
            decision: The routing decision that selected this agent (for logging inside
                the agent if needed).
            context: Injected AgentContext — must be passed through to any tool calls
                or sub-operations unchanged.
            session: Caller-owned SQLAlchemy session.  Agent must NOT commit; it only
                flushes or reads within this session.
        """
        ...


@runtime_checkable
class DeterministicHandler(Protocol):
    """Protocol for deterministic (no-LLM) task handlers.

    Deterministic handlers are registered by task_type string in the registry.
    They answer by rule evaluation, data lookup, or computation.  A handler may be
    *deterministic-first*: when its own rule confidence is too low it may escalate
    internally to a bounded LLM call (e.g. ExpenseCategorizeHandler, P8 C1).  Such a
    handler owns that escalation end to end — it injects its own LlmProvider, degrades
    to its rule answer on provider failure (NFR-R1), and writes its own decision-log
    row carrying the route that actually ran (deterministic vs llm).  The orchestrator
    itself still never calls the LLM.
    They share the same (task, decision, *, context, session) signature as Agent.handle
    to keep dispatch uniform.
    """

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        """Execute the task deterministically and return an AgentResult.

        Args:
            task: The original request envelope.
            decision: The routing decision that selected this handler.
            context: Injected AgentContext — pass through to any tool calls unchanged.
            session: Caller-owned SQLAlchemy session; handler must NOT commit.
        """
        ...
