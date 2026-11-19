"""P6-T4 support — spy provider, orchestrator builder, and context helper.

Not a test file (no pytest magic).  Imported by test_p6_t4_integration_db.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from ai_agents.agents import register_policy
from ai_agents.agents.expense_agent import register_expense
from ai_agents.agents.travel_agent import register_travel
from ai_agents.context import AgentContext
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmResponse, ToolSpec
from services.llm_provider.local import LocalLlmProvider


class SpyLlmProvider(LocalLlmProvider):
    """LocalLlmProvider subclass that counts complete()/stream() calls.

    The spy delegates every call to super(), so it produces the same canned
    responses as the real local stub.  The call counters are the anti-cheat:
    asserting spy.total_calls == 0 proves a deterministic path was taken.
    """

    model = "local-stub-spy"

    def __init__(self) -> None:
        self.complete_calls: int = 0
        self.stream_calls: int = 0

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> LlmResponse:
        self.complete_calls += 1
        return super().complete(messages, tools=tools, tool_choice=tool_choice)

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Iterator[LlmChunk]:
        self.stream_calls += 1
        yield from super().stream(messages, tools=tools, tool_choice=tool_choice)

    @property
    def total_calls(self) -> int:
        return self.complete_calls + self.stream_calls


def build_spied_orchestrator(spy: SpyLlmProvider) -> Orchestrator:
    """Build an Orchestrator with SpyLlmProvider injected into all agents.

    Mirrors backend.app.modules.ai.runtime.build_orchestrator() but replaces
    the production LlmProvider with the spy for call-count verification.
    Approval agent is omitted — not exercised in T4.
    """
    registry = AgentRegistry()
    register_policy(registry, provider=spy)
    register_travel(registry, provider=spy)
    register_expense(registry, provider=spy)
    return Orchestrator(registry)


def make_ctx(
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    permissions: frozenset[str] | None = None,
) -> AgentContext:
    """Minimal AgentContext for T4 test invocations."""
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=permissions or frozenset(),
        user_id=user_id,
    )
