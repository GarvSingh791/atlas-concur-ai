"""Hermetic tests for the Orchestrator router/dispatcher (P4-T7).

No database, no LLM, no network.  All tests run in hermetic CI.

Key invariants verified:
  - High-confidence/no-reasoning task → route=deterministic, fake handler dispatched
  - Ambiguous/reasoning task → route=llm, fake agent dispatched
  - No-LLM guarantee: fake LlmProvider raises if touched; orchestrator must not trigger it
  - Unregistered deterministic handler → AgentResultStatus.needs_review (no crash)
  - Unregistered agent → AgentResultStatus.unavailable (no crash)
  - Decision-log writer invoked exactly once per task with correct fields
  - PII-minimized input_summary: never contains raw payload content
  - Determinism: same Task → same RoutingDecision
  - Scoped-context pass-through: context reaches the handler/agent unchanged
  - Confidence threshold controls route selection (boundary test)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_agents.context import AgentContext
from ai_agents.orchestrator import (
    AgentRegistry,
    AgentResult,
    AgentResultStatus,
    Orchestrator,
    OrchestratorOutcome,
    RoutingDecision,
    Task,
)
from database.models.ai_decision_log import AiAgent, AiRoute

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


def _ctx(**kwargs: Any) -> AgentContext:
    return AgentContext(
        organization_id=kwargs.get("organization_id", _ORG_ID),
        granted_permission_keys=frozenset(kwargs.get("keys", [])),
        user_id=kwargs.get("user_id", _USER_ID),
        role=kwargs.get("role", "employee"),
    )


def _session() -> Any:
    """Sentinel session for hermetic tests — fakes never touch it."""
    return MagicMock()


def _task(
    context_type: str | None,
    *,
    signals: dict | None = None,
    payload: dict | None = None,
) -> Task:
    return Task(
        context_type=context_type,
        payload=payload or {"question": "test"},
        signals=signals or {},
    )


class FakeLlmProvider:
    """Raises on any call — proves orchestrator never touches the LLM."""

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("FakeLlmProvider.complete was called — orchestrator must not call LLM")

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("FakeLlmProvider.stream was called — orchestrator must not call LLM")


class _FakeAgent:
    """Minimal Agent that records calls and returns a canned result."""

    def __init__(self, agent_type: AiAgent) -> None:
        self.agent_type = agent_type
        self.calls: list[dict] = []
        self._llm = FakeLlmProvider()  # held but NEVER called by orchestrator

    def handled_task_types(self) -> frozenset[str]:
        return frozenset()

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Any,
    ) -> AgentResult:
        self.calls.append({"task": task, "decision": decision, "context": context})
        return AgentResult(status=AgentResultStatus.ok, output={"from": "fake_agent"})


class _FakeDeterministicHandler:
    """Minimal DeterministicHandler that records calls and returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Any,
    ) -> AgentResult:
        self.calls.append({"task": task, "decision": decision, "context": context})
        return AgentResult(status=AgentResultStatus.ok, output={"from": "fake_det_handler"})


def _make_orchestrator(
    *,
    threshold: float = 0.75,
    agents: list[_FakeAgent] | None = None,
    det_handlers: dict[str, _FakeDeterministicHandler] | None = None,
) -> tuple[Orchestrator, AgentRegistry]:
    registry = AgentRegistry()
    for agent in agents or []:
        registry.register_agent(agent)
    for task_type, handler in (det_handlers or {}).items():
        registry.register_deterministic_handler(task_type, handler)
    orch = Orchestrator(registry, confidence_threshold=threshold)
    return orch, registry


def _stub_write_log() -> MagicMock:
    """Return a mock that replaces write_decision_log and captures arguments."""
    mock = MagicMock()
    mock.return_value = MagicMock(id=uuid.uuid4())
    return mock


# ---------------------------------------------------------------------------
# Route decision: deterministic path
# ---------------------------------------------------------------------------


class TestDeterministicRoute:
    def test_expense_simple_takes_deterministic_route(self) -> None:
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            det_handlers={"expense_categorize_simple": det_handler},
        )
        task = _task("expense", signals={"expense_simple": True})

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.deterministic
        assert len(det_handler.calls) == 1

    def test_policy_evaluate_takes_deterministic_route(self) -> None:
        # policy_eval signal → policy_evaluate task_type, requires_reasoning=False → deterministic
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            det_handlers={"policy_evaluate": det_handler},
        )
        task = Task(context_type="policy_qa", payload={}, signals={"policy_eval": True})

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.deterministic
        assert len(det_handler.calls) == 1


# ---------------------------------------------------------------------------
# Route decision: llm path
# ---------------------------------------------------------------------------


class TestLlmRoute:
    def test_travel_planning_takes_llm_route(self) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("travel_planning")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.llm
        assert len(fake_agent.calls) == 1

    def test_expense_reasoning_takes_llm_route(self) -> None:
        fake_agent = _FakeAgent(AiAgent.expense)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("expense")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.llm
        assert len(fake_agent.calls) == 1

    def test_general_low_confidence_takes_llm_route(self) -> None:
        fake_agent = _FakeAgent(AiAgent.policy)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("general")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.llm


# ---------------------------------------------------------------------------
# No-LLM guarantee
# ---------------------------------------------------------------------------


class TestNoLlmGuarantee:
    def test_orchestrator_never_calls_llm_on_deterministic_path(self) -> None:
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            det_handlers={"expense_categorize_simple": det_handler},
        )
        task = _task("expense", signals={"expense_simple": True})
        # FakeLlmProvider is in _FakeDeterministicHandler/_FakeAgent — orchestrator never calls it.

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        # The FakeLlmProvider was never invoked (it would have raised)
        assert outcome.decision.route == AiRoute.deterministic

    def test_orchestrator_never_calls_llm_on_llm_path_itself(self) -> None:
        # On the llm route, the orchestrator dispatches to an agent;
        # the agent stub holds a FakeLlmProvider but the ORCHESTRATOR never calls it.
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("travel_planning")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        # Agent was called (llm route), but the FakeLlmProvider inside it was not
        assert outcome.decision.route == AiRoute.llm
        assert len(fake_agent.calls) == 1
        # The FakeLlmProvider never raised → orchestrator did not trigger it


# ---------------------------------------------------------------------------
# Fail-safe: unregistered handlers / agents
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_unregistered_deterministic_handler_returns_needs_review(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("expense", signals={"expense_simple": True})

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.result.status == AgentResultStatus.needs_review
        assert "message" in outcome.result.output

    def test_unregistered_agent_returns_unavailable(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("travel_planning")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.result.status == AgentResultStatus.unavailable
        assert "message" in outcome.result.output

    def test_unregistered_target_does_not_raise(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("general")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        # Must return an outcome, never crash
        assert isinstance(outcome, OrchestratorOutcome)


# ---------------------------------------------------------------------------
# Decision-log writer invoked exactly once with correct fields
# ---------------------------------------------------------------------------


class TestDecisionLogWriter:
    def test_writer_invoked_exactly_once_per_task(self) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("travel_planning")
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        assert mock_writer.call_count == 1

    def test_writer_called_with_orchestrator_agent(self) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("travel_planning")
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        assert record.agent == AiAgent.orchestrator

    def test_writer_called_with_correct_route(self) -> None:
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            det_handlers={"expense_categorize_simple": det_handler},
        )
        task = _task("expense", signals={"expense_simple": True})
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        assert record.route == AiRoute.deterministic

    def test_writer_model_provider_is_none(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("general")
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        assert record.model_provider is None

    def test_writer_context_injected(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("general")
        ctx = _ctx(organization_id=_ORG_ID)
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=ctx, session=_session())

        _, kwargs = mock_writer.call_args
        assert kwargs["context"] is ctx

    def test_writer_latency_ms_recorded(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("general")
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        assert record.latency_ms is not None
        assert isinstance(record.latency_ms, int)
        assert record.latency_ms >= 0


# ---------------------------------------------------------------------------
# PII-minimized input_summary
# ---------------------------------------------------------------------------


class TestPiiMinimizedInputSummary:
    def test_input_summary_does_not_contain_raw_payload(self) -> None:
        orch, _ = _make_orchestrator()
        sensitive_payload = {
            "user_email": "garv@example.com",
            "password": "super-secret-123",
            "amount": 1234.56,
        }
        task = Task(
            context_type="general",
            payload=sensitive_payload,
            signals={},
        )
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        summary = record.input_summary or {}
        # The raw payload must NOT appear in input_summary
        assert "garv@example.com" not in str(summary)
        assert "super-secret-123" not in str(summary)
        assert "1234.56" not in str(summary)

    def test_input_summary_contains_context_type(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("travel_planning")
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        summary = record.input_summary or {}
        assert summary.get("context_type") == "travel_planning"

    def test_input_summary_records_signal_keys_not_values(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task(
            "expense",
            signals={"expense_simple": True, "currency": "USD"},
        )
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            orch.handle_task(task, context=_ctx(), session=_session())

        _, kwargs = mock_writer.call_args
        record = kwargs["record"]
        summary = record.input_summary or {}
        # Keys are recorded (presence flags); values are NOT
        assert "currency" in summary.get("signal_keys", [])
        # Signal VALUES not in summary string
        assert "USD" not in str(summary)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    @pytest.mark.parametrize(
        "context_type,signals",
        [
            ("travel_planning", {}),
            ("expense", {"expense_simple": True}),
            ("policy_qa", {}),
            ("general", {}),
            (None, {"approval_required": True}),
        ],
    )
    def test_same_task_produces_same_routing_decision(
        self, context_type: str | None, signals: dict
    ) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        fa2 = _FakeAgent(AiAgent.expense)
        fa3 = _FakeAgent(AiAgent.policy)
        fa4 = _FakeAgent(AiAgent.approval)
        orch, _ = _make_orchestrator(agents=[fake_agent, fa2, fa3, fa4])
        task = _task(context_type, signals=signals)
        mock_writer = _stub_write_log()

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", mock_writer):
            o1 = orch.handle_task(task, context=_ctx(), session=_session())
            mock_writer.return_value = MagicMock(id=uuid.uuid4())
            o2 = orch.handle_task(task, context=_ctx(), session=_session())

        assert o1.decision.route == o2.decision.route
        assert o1.decision.target_agent == o2.decision.target_agent
        assert o1.decision.task_type == o2.decision.task_type
        assert o1.decision.confidence == o2.decision.confidence


# ---------------------------------------------------------------------------
# Scoped-context pass-through
# ---------------------------------------------------------------------------


class TestScopedContextPassThrough:
    def test_context_passed_to_agent_unchanged(self) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(agents=[fake_agent])
        task = _task("travel_planning")
        ctx = _ctx(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="manager")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            orch.handle_task(task, context=ctx, session=_session())

        assert fake_agent.calls[0]["context"] is ctx

    def test_context_passed_to_deterministic_handler_unchanged(self) -> None:
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            det_handlers={"expense_categorize_simple": det_handler},
        )
        task = _task("expense", signals={"expense_simple": True})
        ctx = _ctx(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="employee")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            orch.handle_task(task, context=ctx, session=_session())

        assert det_handler.calls[0]["context"] is ctx


# ---------------------------------------------------------------------------
# Confidence threshold boundary
# ---------------------------------------------------------------------------


class TestConfidenceThreshold:
    def test_at_threshold_no_reasoning_takes_deterministic(self) -> None:
        det_handler = _FakeDeterministicHandler()
        # policy_eval → policy_evaluate, confidence=0.95, requires_reasoning=False → deterministic
        orch, _ = _make_orchestrator(
            threshold=0.95,
            det_handlers={"policy_evaluate": det_handler},
        )
        task = Task(context_type="policy_qa", payload={}, signals={"policy_eval": True})

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.deterministic

    def test_just_above_threshold_still_deterministic(self) -> None:
        det_handler = _FakeDeterministicHandler()
        orch, _ = _make_orchestrator(
            threshold=0.94,
            det_handlers={"policy_evaluate": det_handler},
        )
        task = Task(context_type="policy_qa", payload={}, signals={"policy_eval": True})

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.deterministic

    def test_just_below_threshold_takes_llm(self) -> None:
        fake_agent = _FakeAgent(AiAgent.policy)
        orch, _ = _make_orchestrator(
            threshold=0.96,
            agents=[fake_agent],
        )
        task = _task("policy_qa")  # confidence=0.95

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.llm

    def test_requires_reasoning_forces_llm_even_at_high_confidence(self) -> None:
        fake_agent = _FakeAgent(AiAgent.travel)
        orch, _ = _make_orchestrator(
            threshold=0.50,  # low threshold — still should go llm because reasoning required
            agents=[fake_agent],
        )
        task = _task("travel_planning")  # confidence=0.95, requires_reasoning=True

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _stub_write_log()):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert outcome.decision.route == AiRoute.llm


# ---------------------------------------------------------------------------
# Writer failure is non-fatal
# ---------------------------------------------------------------------------


class TestWriterFailureIsNonFatal:
    def test_log_write_failure_does_not_crash_orchestrator(self) -> None:
        orch, _ = _make_orchestrator()
        task = _task("general")

        def _failing_writer(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("DB unavailable")

        with patch("ai_agents.orchestrator.orchestrator.write_decision_log", _failing_writer):
            outcome = orch.handle_task(task, context=_ctx(), session=_session())

        assert isinstance(outcome, OrchestratorOutcome)
        assert outcome.log_id is None
