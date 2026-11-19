"""Hermetic tests for the AI composition root (P4-T13, Slice 0).

All tests are hermetic — no database required, no @pytest.mark.db.
They exercise the AgentRegistry state that build_orchestrator() produces,
verifying that all four agents and the policy_evaluate deterministic handler
are registered, and that the cached singleton behaves correctly.
"""

from __future__ import annotations

import pytest

from ai_agents.orchestrator.registry import AgentRegistry
from backend.app.modules.ai.runtime import build_orchestrator
from database.models.ai_decision_log import AiAgent


@pytest.fixture(autouse=True)
def clear_orchestrator_cache():
    """Clear the lru_cache before and after each test to avoid cross-test leakage."""
    build_orchestrator.cache_clear()
    yield
    build_orchestrator.cache_clear()


def test_build_orchestrator_returns_orchestrator():
    """build_orchestrator() returns an Orchestrator instance."""
    from ai_agents.orchestrator.orchestrator import Orchestrator

    orch = build_orchestrator()
    assert isinstance(orch, Orchestrator)


def test_all_agents_registered():
    """All four agent types are present in the registry after build_orchestrator()."""
    orch = build_orchestrator()
    registered = orch._registry.registered_agent_types
    assert AiAgent.policy in registered
    assert AiAgent.travel in registered
    assert AiAgent.expense in registered
    assert AiAgent.approval in registered


def test_policy_eval_handler_registered():
    """The policy_evaluate deterministic handler is wired up."""
    orch = build_orchestrator()
    assert "policy_evaluate" in orch._registry.registered_deterministic_task_types


def test_expense_categorize_handler_registered():
    """The expense_categorize_simple deterministic handler is wired up (P8 C1)."""
    orch = build_orchestrator()
    assert "expense_categorize_simple" in orch._registry.registered_deterministic_task_types


def test_get_agent_unknown_returns_none():
    """An empty registry returns None for any agent type (fail-safe)."""
    registry = AgentRegistry()
    assert registry.get_agent(AiAgent.policy) is None


def test_build_orchestrator_is_cached():
    """Repeated calls to build_orchestrator() return the same object."""
    orch1 = build_orchestrator()
    orch2 = build_orchestrator()
    assert orch1 is orch2
