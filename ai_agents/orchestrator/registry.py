"""AgentRegistry — maps agent types and deterministic task handlers for dispatch (P4-T7).

Two registration faces (one registry object, approved by Garv):
  register_agent()                → keyed by Agent.agent_type (AiAgent enum)
  register_deterministic_handler()→ keyed by task_type string

Ships EMPTY of real agents in P4-T7.  Agents register here as they are built
(P4-T8 Policy → T12 Approval).  Tests register fakes.

Lookups return None for unknown keys → orchestrator fails safe (never crashes).

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

from ai_agents.orchestrator.contracts import Agent, DeterministicHandler
from database.models.ai_decision_log import AiAgent


class AgentRegistry:
    """Thread-unsafe registry (single-process use; no concurrent writes expected).

    The registry is not a singleton — the orchestrator owns it.  Tests construct
    a fresh registry per test to avoid cross-test state leakage.
    """

    def __init__(self) -> None:
        self._agents: dict[AiAgent, Agent] = {}
        self._det_handlers: dict[str, DeterministicHandler] = {}

    # ------------------------------------------------------------------ agents

    def register_agent(self, agent: Agent) -> None:
        """Register an agent for its declared agent_type.

        Overwrites any previously registered agent for the same type (allows
        re-registration in tests without raising).
        """
        self._agents[agent.agent_type] = agent

    def get_agent(self, agent_type: AiAgent) -> Agent | None:
        """Return the registered agent for agent_type, or None if unregistered."""
        return self._agents.get(agent_type)

    # ------------------------------------------- deterministic task handlers

    def register_deterministic_handler(self, task_type: str, handler: DeterministicHandler) -> None:
        """Register a deterministic handler for a specific task_type string.

        Overwrites any previously registered handler for the same task_type.
        """
        self._det_handlers[task_type] = handler

    def get_deterministic_handler(self, task_type: str) -> DeterministicHandler | None:
        """Return the registered deterministic handler for task_type, or None."""
        return self._det_handlers.get(task_type)

    # ----------------------------------------------------------------- helpers

    @property
    def registered_agent_types(self) -> frozenset[AiAgent]:
        return frozenset(self._agents)

    @property
    def registered_deterministic_task_types(self) -> frozenset[str]:
        return frozenset(self._det_handlers)
