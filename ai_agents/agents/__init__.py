"""Policy Agent package — exports + registration helper (P4-T8)."""

from __future__ import annotations

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.agents.policy_eval_handler import PolicyEvalHandler
from ai_agents.orchestrator.registry import AgentRegistry
from services.llm_provider.interface import LlmProvider

__all__ = ["PolicyAgent", "PolicyEvalHandler", "register_policy"]

_POLICY_EVAL_TASK_TYPE = "policy_evaluate"


def register_policy(registry: AgentRegistry, *, provider: LlmProvider | None = None) -> None:
    """Register Cap 1 (deterministic eval) and Cap 2+3 (agent) with the registry."""
    registry.register_deterministic_handler(_POLICY_EVAL_TASK_TYPE, PolicyEvalHandler())
    registry.register_agent(PolicyAgent(provider=provider))
