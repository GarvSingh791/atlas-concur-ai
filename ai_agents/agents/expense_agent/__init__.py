"""Expense Agent package — exports ExpenseAgent, the simple-categorize handler and
the registration helper (P4-T10, P8 C1)."""

from __future__ import annotations

from ai_agents.agents.expense_agent.agent import ExpenseAgent
from ai_agents.agents.expense_agent.categorize_handler import (
    EXPENSE_CATEGORIZE_TASK_TYPE,
    ExpenseCategorizeHandler,
)
from ai_agents.orchestrator.registry import AgentRegistry
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmProvider

__all__ = [
    "EXPENSE_CATEGORIZE_TASK_TYPE",
    "ExpenseAgent",
    "ExpenseCategorizeHandler",
    "register_expense",
]


def register_expense(registry: AgentRegistry, *, provider: LlmProvider | None = None) -> None:
    """Register ExpenseAgent and the expense_categorize_simple handler.

    Both share one resolved provider (ExpenseAgent's previous default of
    get_llm_provider() is preserved when ``provider`` is None).
    """
    llm = provider if provider is not None else get_llm_provider()
    registry.register_agent(ExpenseAgent(provider=llm))
    registry.register_deterministic_handler(
        EXPENSE_CATEGORIZE_TASK_TYPE, ExpenseCategorizeHandler(provider=llm)
    )
