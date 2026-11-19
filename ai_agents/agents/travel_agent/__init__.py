"""Travel Agent package — exports TravelAgent and registration helper (P4-T9)."""

from __future__ import annotations

from ai_agents.agents.travel_agent.agent import TravelAgent
from ai_agents.orchestrator.registry import AgentRegistry
from services.llm_provider.interface import LlmProvider

__all__ = ["TravelAgent", "register_travel"]


def register_travel(registry: AgentRegistry, *, provider: LlmProvider | None = None) -> None:
    """Register TravelAgent with the given AgentRegistry."""
    registry.register_agent(TravelAgent(provider=provider))
