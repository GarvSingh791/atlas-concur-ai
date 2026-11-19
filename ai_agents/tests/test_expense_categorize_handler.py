"""Hermetic tests for ExpenseCategorizeHandler (P8 C1, FR-X3, NFR-R1, M-R4).

No database, no LLM API, no network.  write_decision_log is patched; the
session is a MagicMock.  Covers: deterministic route with zero provider calls,
decision-log route/agent/task_type, LLM fallback on low confidence, degraded
path on provider error / no provider / unparseable output, PII minimization,
and end-to-end dispatch through Orchestrator + register_expense.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent import (
    EXPENSE_CATEGORIZE_TASK_TYPE,
    ExpenseCategorizeHandler,
    register_expense,
)
from ai_agents.agents.expense_agent.categorize_handler import LLM_CATEGORY_CONFIDENCE
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse

_PATCH_TARGET = "ai_agents.agents.expense_agent.categorize_handler.write_decision_log"

_DECISION = RoutingDecision(
    target_agent=AiAgent.expense,
    route=AiRoute.deterministic,
    task_type=EXPENSE_CATEGORIZE_TASK_TYPE,
    confidence=0.95,
    rationale="context_type=expense expense_simple",
)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Raises AssertionError if ever called — proves the LLM is not touched."""

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on this path")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called on this path")


class _ProviderErrorProvider(LlmProvider):
    """Always raises ProviderError — simulates LLM unavailability (NFR-R1)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.calls += 1
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        raise ProviderError("LLM unavailable", code="PROVIDER_UNAVAILABLE")


class _ScriptedProvider(LlmProvider):
    """Returns a fixed text and records every prompt it receives."""

    model = "test-stub"

    def __init__(self, text: str) -> None:
        self._text = text
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.append(messages[-1].content)
        return LlmResponse(text=self._text, model=self.model)

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="test-c1",
    )


def _task(payload: dict[str, Any]) -> Task:
    return Task(context_type="expense", payload=payload, signals={"expense_simple": True})


def _run(
    handler: ExpenseCategorizeHandler, payload: dict[str, Any]
) -> tuple[Any, DecisionRecord, MagicMock]:
    with patch(_PATCH_TARGET) as mock_log:
        result = handler.handle(
            _task(payload),
            _DECISION,
            context=_ctx(),
            session=MagicMock(spec=Session),
        )
    assert mock_log.call_count == 1, "exactly one expense decision-log row per call"
    record: DecisionRecord = mock_log.call_args.kwargs["record"]
    return result, record, mock_log


def _summary(record: DecisionRecord) -> dict[str, Any]:
    assert record.output_summary is not None
    return record.output_summary


# Low-confidence payload: brand + keyword disagree → conflict (0.50).
_LOW_CONF_PAYLOAD = {"merchant": "Hilton Garden", "description": "dinner with client"}


# ---------------------------------------------------------------------------
# High confidence → deterministic, zero provider calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"merchant": "Marriott"}, "lodging"),
        ({"vendor": "Delta Air Lines"}, "airfare"),
        ({"merchant": "Uber"}, "ground_transport"),
        ({"description": "taxi", "amount": 25.0}, "ground_transport"),
        ({"question": "Team dinner"}, "meals"),
        ({"mcc": 5812}, "meals"),
        ({"mcc": "7011"}, "lodging"),
        ({"expense_type": "flight"}, "airfare"),
    ],
)
def test_high_confidence_is_deterministic_zero_llm(payload: dict, expected: str) -> None:
    handler = ExpenseCategorizeHandler(provider=_RaisingProvider())
    result, record, _ = _run(handler, payload)

    assert result.status == AgentResultStatus.ok
    assert result.output["category"] == expected
    assert result.output["route"] == "deterministic"
    assert result.output["needs_review"] is False
    assert result.output["degraded"] is False

    assert record.route == AiRoute.deterministic
    assert record.model_provider is None
    assert record.agent == AiAgent.expense
    assert record.task_type == EXPENSE_CATEGORIZE_TASK_TYPE
    assert _summary(record)["llm_attempted"] is False
    assert _summary(record)["threshold"] == 0.80


def test_threshold_override_forces_escalation() -> None:
    provider = _ScriptedProvider(json.dumps({"category": "lodging"}))
    handler = ExpenseCategorizeHandler(provider=provider, threshold=0.99)
    result, record, _ = _run(handler, {"merchant": "Marriott"})
    assert provider.calls == 1
    assert record.route == AiRoute.llm
    assert result.output["category"] == "lodging"


# ---------------------------------------------------------------------------
# Low confidence → one LLM call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"category": "meals"}',
        '```json\n{"category": "meals"}\n```',
        '{"category": "MEALS"}',
    ],
)
def test_low_confidence_llm_success(text: str) -> None:
    provider = _ScriptedProvider(text)
    handler = ExpenseCategorizeHandler(provider=provider)
    result, record, _ = _run(handler, _LOW_CONF_PAYLOAD)

    assert provider.calls == 1
    assert result.status == AgentResultStatus.ok
    assert result.output["category"] == "meals"
    assert result.output["route"] == "llm"
    assert result.output["needs_review"] is False
    assert result.confidence == LLM_CATEGORY_CONFIDENCE

    assert record.route == AiRoute.llm
    assert record.model_provider == "test-stub"
    assert _summary(record)["llm_attempted"] is True
    assert _summary(record)["degraded"] is False
    assert _summary(record)["rule_confidence"] < 0.80


def test_unknown_merchant_escalates() -> None:
    provider = _ScriptedProvider('{"category": "other"}')
    handler = ExpenseCategorizeHandler(provider=provider)
    result, record, _ = _run(handler, {"merchant": "Acme Widgets", "amount": 12.5})
    assert provider.calls == 1
    assert result.output["category"] == "other"
    assert record.route == AiRoute.llm
    assert "12.50" in provider.prompts[0]


def test_llm_prompt_carries_no_ids() -> None:
    provider = _ScriptedProvider('{"category": "meals"}')
    handler = ExpenseCategorizeHandler(provider=provider)
    conv_id = str(uuid.uuid4())
    ref_id = str(uuid.uuid4())
    _run(handler, {**_LOW_CONF_PAYLOAD, "context_ref_id": ref_id, "conversation_id": conv_id})
    assert conv_id not in provider.prompts[0]
    assert ref_id not in provider.prompts[0]


# ---------------------------------------------------------------------------
# Degraded paths (NFR-R1)
# ---------------------------------------------------------------------------


def test_provider_error_degrades_to_rule_guess() -> None:
    provider = _ProviderErrorProvider()
    handler = ExpenseCategorizeHandler(provider=provider)
    result, record, _ = _run(handler, _LOW_CONF_PAYLOAD)

    assert provider.calls == 1
    assert result.status == AgentResultStatus.needs_review
    assert result.output["category"] == "lodging"  # rule best guess (brand)
    assert result.output["degraded"] is True
    assert result.output["needs_review"] is True

    assert record.route == AiRoute.llm
    assert record.model_provider is None
    assert _summary(record)["degraded"] is True
    assert _summary(record)["llm_attempted"] is True


def test_no_provider_degrades_without_call() -> None:
    handler = ExpenseCategorizeHandler(provider=None)
    result, record, _ = _run(handler, _LOW_CONF_PAYLOAD)
    assert result.status == AgentResultStatus.needs_review
    assert result.output["category"] == "lodging"
    assert result.output["degraded"] is True
    assert record.model_provider is None
    assert _summary(record)["llm_attempted"] is False


def test_no_provider_high_confidence_still_ok() -> None:
    handler = ExpenseCategorizeHandler(provider=None)
    result, record, _ = _run(handler, {"merchant": "Lyft"})
    assert result.status == AgentResultStatus.ok
    assert record.route == AiRoute.deterministic


@pytest.mark.parametrize(
    "text",
    [
        "Sure! It looks like a meal.",
        '{"category": "food"}',
        '["meals"]',
        '{"cat": "meals"}',
        '{"category": 3}',
        "",
    ],
)
def test_unparseable_or_out_of_enum_is_needs_review(text: str) -> None:
    provider = _ScriptedProvider(text)
    handler = ExpenseCategorizeHandler(provider=provider)
    result, record, _ = _run(handler, _LOW_CONF_PAYLOAD)
    assert provider.calls == 1
    assert result.status == AgentResultStatus.needs_review
    assert result.output["category"] == "lodging"
    assert result.output["degraded"] is True
    assert record.model_provider is None


# ---------------------------------------------------------------------------
# PII minimization
# ---------------------------------------------------------------------------


def test_decision_log_has_no_merchant_or_description_text() -> None:
    handler = ExpenseCategorizeHandler(provider=_ProviderErrorProvider())
    payload = {
        "merchant": "Zyxwq Bistro Hilton",
        "description": "dinner with Jane Doe jane@example.com",
    }
    _, record, _ = _run(handler, payload)

    assert record.input_summary == {
        "has_merchant": True,
        "has_description": True,
        "has_mcc": False,
        "has_expense_type": False,
    }
    assert all(isinstance(v, bool) for v in record.input_summary.values())
    blob = json.dumps(
        {
            "input": record.input_summary,
            "tools": record.tools_used,
            "output": record.output_summary,
        }
    ).lower()
    for fragment in ("zyxwq", "jane", "example.com", "dinner with"):
        assert fragment not in blob


# ---------------------------------------------------------------------------
# Conversation message (best effort)
# ---------------------------------------------------------------------------


def test_conversation_message_appended_when_conversation_id_present() -> None:
    handler = ExpenseCategorizeHandler(provider=_RaisingProvider())
    conv_id = str(uuid.uuid4())
    target = "ai_agents.agents.expense_agent.categorize_handler.append_assistant_message"
    with patch(target) as mock_append:
        _run(handler, {"merchant": "Marriott", "conversation_id": conv_id})
    mock_append.assert_called_once()
    kwargs = mock_append.call_args.kwargs
    assert kwargs["conversation_id"] == conv_id
    assert "lodging" in kwargs["content"]


@pytest.mark.parametrize(
    ("mcc", "expected"),
    [("5812", True), (5812, True), ("abc", False), ("123", False), (True, False), (None, False)],
)
def test_has_mcc_reflects_only_a_usable_mcc(mcc: object, expected: bool) -> None:
    handler = ExpenseCategorizeHandler(provider=_RaisingProvider())
    _, record, _ = _run(handler, {"merchant": "Marriott", "mcc": mcc})
    assert record.input_summary is not None
    assert record.input_summary["has_mcc"] is expected


def test_conversation_message_failure_rolls_back_savepoint_and_still_logs() -> None:
    """A failing append is isolated in a SAVEPOINT; the decision log is still written."""
    handler = ExpenseCategorizeHandler(provider=_RaisingProvider())
    session = MagicMock(spec=Session)
    nested = session.begin_nested.return_value
    target = "ai_agents.agents.expense_agent.categorize_handler.append_assistant_message"
    with (
        patch(target, side_effect=RuntimeError("flush failed")),
        patch(_PATCH_TARGET) as mock_log,
    ):
        result = handler.handle(
            _task({"merchant": "Marriott", "conversation_id": str(uuid.uuid4())}),
            _DECISION,
            context=_ctx(),
            session=session,
        )
    session.begin_nested.assert_called_once()
    nested.__exit__.assert_called_once()
    assert nested.__exit__.call_args.args[0] is RuntimeError
    assert mock_log.call_count == 1
    assert result.status == AgentResultStatus.ok


def test_conversation_message_failure_is_swallowed() -> None:
    handler = ExpenseCategorizeHandler(provider=_RaisingProvider())
    target = "ai_agents.agents.expense_agent.categorize_handler.append_assistant_message"
    with patch(target, side_effect=RuntimeError("boom")):
        result, _, _ = _run(handler, {"merchant": "Marriott", "conversation_id": "not-a-uuid"})
    assert result.status == AgentResultStatus.ok


# ---------------------------------------------------------------------------
# End to end through the orchestrator
# ---------------------------------------------------------------------------


def test_register_expense_registers_handler() -> None:
    registry = AgentRegistry()
    register_expense(registry, provider=_RaisingProvider())
    assert EXPENSE_CATEGORIZE_TASK_TYPE in registry.registered_deterministic_task_types
    assert isinstance(
        registry.get_deterministic_handler(EXPENSE_CATEGORIZE_TASK_TYPE), ExpenseCategorizeHandler
    )
    assert AiAgent.expense in registry.registered_agent_types


def test_orchestrator_end_to_end_simple_categorize() -> None:
    registry = AgentRegistry()
    register_expense(registry, provider=_RaisingProvider())
    orch = Orchestrator(registry)
    task = Task(
        context_type="expense", signals={"expense_simple": True}, payload={"merchant": "Uber"}
    )
    with (
        patch(_PATCH_TARGET) as handler_log,
        patch("ai_agents.orchestrator.orchestrator.write_decision_log") as orch_log,
    ):
        outcome = orch.handle_task(task, context=_ctx(), session=MagicMock(spec=Session))

    assert outcome.decision.route == AiRoute.deterministic
    assert outcome.decision.task_type == EXPENSE_CATEGORIZE_TASK_TYPE
    assert outcome.result.status == AgentResultStatus.ok
    assert outcome.result.output["category"] == "ground_transport"
    assert handler_log.call_count == 1
    assert orch_log.call_count == 1
    assert orch_log.call_args.kwargs["record"].agent == AiAgent.orchestrator
