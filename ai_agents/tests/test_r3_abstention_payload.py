"""Hermetic tests for the R3 abstention (silent) AgentResult payload.

No database, no LLM API, no network. Mirrors the established pattern in
test_policy_agent.py (_RaisingProvider + patched GroundedRetriever.retrieve +
patched write_decision_log).

Gates covered (I_R3_6 / V2 / V6):
  • The LLM provider is NEVER invoked on the silent path.
  • confidence is None.
  • rationale names the org ("Acme Corp" default).
  • escalation.message contains the primary contact ("your team lead").
  • escalation.secondary == "Travel Desk".
  • near_topic: non-empty str citing the near-miss doc when a suggestion exists,
    None when there is no near-miss suggestion.
  • answer_type == "silent" and answer is None.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

from ai_agents.agents import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AiAgent, AiRoute, RoutingDecision, Task
from ai_agents.rag.retrieval import (
    GroundedRetrieval,
    GroundingStatus,
    NearTopicSuggestion,
)
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Raising provider — proves the LLM is never touched on the silent path.
# (Local copy of the _RaisingProvider pattern from test_policy_agent.py.)
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Raises AssertionError if ever called — proves LLM is not touched."""

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on the silent path")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called on the silent path")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_QA_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="test",
)


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        granted_permission_keys=frozenset(),
        user_id=_USER_ID,
    )


def _qa_task(question: str = "What is the per diem in NYC?") -> Task:
    return Task(context_type="policy_qa", payload={"question": question})


def _silent_with_suggestion() -> GroundedRetrieval:
    """Case A — a near-miss suggestion is present."""
    return GroundedRetrieval(
        query="What is the per diem in NYC?",
        status=GroundingStatus.silent,
        clauses=(),
        citations=(),
        confidence=None,
        reason="below_threshold: top similarity 0.12 < threshold 0.20",
        threshold=0.20,
        near_topic_suggestion=NearTopicSuggestion(
            chunk_text="Per diem in NYC is $79.",
            policy_name="TEP-FY26 Global T&E Policy",
            clause_ref="4.2.1",
        ),
    )


def _silent_no_suggestion() -> GroundedRetrieval:
    """Case B — no near-miss suggestion."""
    return GroundedRetrieval(
        query="What is the policy on emotional support animals?",
        status=GroundingStatus.silent,
        clauses=(),
        citations=(),
        confidence=None,
        reason="below_threshold: top similarity 0.03 < threshold 0.20",
        threshold=0.20,
        near_topic_suggestion=None,
    )


# ---------------------------------------------------------------------------
# Case A — silent payload WITH a near-topic suggestion
# ---------------------------------------------------------------------------


class TestR3SilentPayloadWithSuggestion:
    def _run(self) -> object:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("What is the per diem in NYC?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_silent_with_suggestion(),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            # If _RaisingProvider is called, this raises AssertionError → test fails.
            return agent.handle(task, _QA_DECISION, context=_ctx(), session=MagicMock())

    def test_provider_never_called(self) -> None:
        # Reaching the assertion at all means the raising provider was never invoked.
        result = self._run()
        assert result is not None

    def test_confidence_is_none(self) -> None:
        result = self._run()
        assert result.confidence is None

    def test_rationale_names_org(self) -> None:
        result = self._run()
        assert (
            result.output["rationale"]
            == "This query is not covered by the Acme Corp T&E documents."
        )

    def test_escalation_message_and_secondary(self) -> None:
        result = self._run()
        assert "your team lead" in result.output["escalation"]["message"]
        assert result.output["escalation"]["secondary"] == "Travel Desk"

    def test_near_topic_non_empty_str_with_refs(self) -> None:
        result = self._run()
        near_topic = result.output["near_topic"]
        assert isinstance(near_topic, str)
        assert near_topic != ""
        assert "TEP-FY26 Global T&E Policy" in near_topic
        assert "4.2.1" in near_topic

    def test_answer_type_silent_answer_none(self) -> None:
        result = self._run()
        assert result.output["answer_type"] == "silent"
        assert result.output["answer"] is None


# ---------------------------------------------------------------------------
# Case B — silent payload WITHOUT a near-topic suggestion
# ---------------------------------------------------------------------------


class TestR3SilentPayloadNoSuggestion:
    def _run(self) -> object:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("What is the policy on emotional support animals?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_silent_no_suggestion(),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            return agent.handle(task, _QA_DECISION, context=_ctx(), session=MagicMock())

    def test_provider_never_called(self) -> None:
        result = self._run()
        assert result is not None

    def test_confidence_is_none(self) -> None:
        result = self._run()
        assert result.confidence is None

    def test_rationale_names_org(self) -> None:
        result = self._run()
        assert (
            result.output["rationale"]
            == "This query is not covered by the Acme Corp T&E documents."
        )

    def test_escalation_message_and_secondary(self) -> None:
        result = self._run()
        assert "your team lead" in result.output["escalation"]["message"]
        assert result.output["escalation"]["secondary"] == "Travel Desk"

    def test_near_topic_is_none(self) -> None:
        result = self._run()
        assert result.output["near_topic"] is None

    def test_answer_type_silent_answer_none(self) -> None:
        result = self._run()
        assert result.output["answer_type"] == "silent"
        assert result.output["answer"] is None
