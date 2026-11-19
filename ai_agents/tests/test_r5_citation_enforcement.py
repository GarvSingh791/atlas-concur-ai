"""Hermetic tests for R5-T2 STRICT citation enforcement (PolicyQaHypothesizer + agent).

No DB, no LLM.  Proves the sourcing is strict — answer citations come ONLY from the LLM's
verified inline [Label §ref] markers (no fallback) — and that the grounded AgentResult.output
is JSON-serializable (the streaming path does json.dumps(dict(output))).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.agents.policy_qa import PolicyQaHypothesizer
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind, validate_grounding
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.grounding import CitedAnswer
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError, ProviderUnavailableError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage


class _ScriptedProvider(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:  # noqa: E501
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(text=text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1))

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:  # noqa: E501
        yield from []


class _ProviderErrorProvider(LlmProvider):
    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:  # noqa: E501
        raise ProviderUnavailableError("down")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:  # noqa: E501
        raise ProviderError("down")


def _clause() -> GroundedClause:
    return GroundedClause(
        clause_ref="AIR-01",
        chunk_text="Economy for short flights.",
        similarity=0.9,
        document_type="policy",
    )


def _cite() -> Citation:
    return Citation(
        kind=CitationKind.clause_ref,
        ref="AIR-01",
        detail="Economy for short flights.",
        source="Atlas T&E Policy",
    )


def _hypothesizer(provider: LlmProvider) -> PolicyQaHypothesizer:
    return PolicyQaHypothesizer(
        provider=provider, clauses=(_clause(),), question="q", citations=(_cite(),), confidence=0.9
    )


class TestStrictSeed:
    def test_valid_marker_yields_verified_citation(self) -> None:
        r = _hypothesizer(_ScriptedProvider("Economy is required [T&E Policy §AIR-01].")).seed()
        assert isinstance(r.value, CitedAnswer)
        assert r.value.full_text == "Economy is required [T&E Policy §AIR-01]."
        assert [c.ref for c in r.citations] == ["AIR-01"]
        assert not r.value.needs_review

    def test_uncited_answer_has_no_citations_no_fallback(self) -> None:
        # STRICT: prose without an inline marker → ZERO citations (no fallback to retrieval).
        r = _hypothesizer(_ScriptedProvider("Business class is always fine.")).seed()
        assert isinstance(r.value, CitedAnswer)
        assert r.citations == ()
        assert r.value.needs_review

    def test_provider_error_keeps_falsy_empty_value(self) -> None:
        r = _hypothesizer(_ProviderErrorProvider()).seed()
        assert r.value == ""  # falsy → PolicyQaCritic's empty short-circuit still fires
        assert r.citations == ()

    def test_uncited_seed_is_not_asserted(self) -> None:
        r = _hypothesizer(_ScriptedProvider("Business class is always fine.")).seed()
        assert validate_grounding(r).asserted is False  # confidence high but 0 citations

    def test_cited_seed_can_assert(self) -> None:
        r = _hypothesizer(_ScriptedProvider("Economy is required [T&E Policy §AIR-01].")).seed()
        assert validate_grounding(r).asserted is True

    def test_partially_cited_seed_flags_needs_review(self) -> None:
        # One cited sentence + one uncited sentence: one verified citation, but needs_review flags
        # that not every sentence is grounded (the per-sentence strict signal).
        r = _hypothesizer(
            _ScriptedProvider(
                "Economy is required [T&E Policy §AIR-01]. You also get a free chauffeur."
            )
        ).seed()
        assert isinstance(r.value, CitedAnswer)
        assert [c.ref for c in r.citations] == ["AIR-01"]
        assert r.value.needs_review is True


_GROUNDED = GroundedRetrieval(
    query="Is business class allowed?",
    status=GroundingStatus.grounded,
    clauses=(
        GroundedClause(
            clause_ref="AIR-CABIN",
            chunk_text="Business class is only allowed on flights over 10 hours.",
            similarity=0.92,
            document_type="policy",
        ),
    ),
    citations=(
        Citation(
            kind=CitationKind.clause_ref,
            ref="AIR-CABIN",
            detail="Business class is only allowed on flights over 10 hours.",
            source="Atlas T&E Policy",
        ),
    ),
    confidence=0.92,
    reason=None,
    threshold=0.20,
)


class TestAgentJsonSafety:
    def test_grounded_output_is_json_serializable(self) -> None:
        # R5-T3: EXTRACTIVE answer (tokens present in the cited leaf text) so the entailment gate
        # passes — this test asserts grounded-output SHAPE / JSON-safety, not an NLI verdict.
        provider = _ScriptedProvider(
            "Business class is only allowed on flights over 10 hours [T&E Policy §AIR-CABIN].",
            "SUPPORTED\nFollows directly from the clause.",
        )
        agent = PolicyAgent(provider=provider)
        task = Task(context_type="policy_qa", payload={"question": "business class?"})
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.llm,
            task_type="policy_grounded_lookup",
            confidence=0.95,
            rationale="t",
        )
        ctx = AgentContext(
            organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
        )
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=_GROUNDED
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()),
        ):
            result = agent.handle(task, decision, context=ctx, session=MagicMock())

        assert result.output["asserted"] is True
        assert isinstance(result.output["answer"], str)  # not a CitedAnswer object
        assert "[T&E Policy §AIR-CABIN]" in result.output["answer"]
        assert isinstance(result.output["answer_structured"], dict)
        # The streaming path serializes the whole output dict — must not raise.
        json.dumps(dict(result.output))
        assert any(c.ref == "AIR-CABIN" for c in result.citations)

    def test_partially_cited_answer_is_downgraded_to_needs_review(self) -> None:
        # STRICT per-sentence: one cited + one uncited sentence → NOT presented as grounded.
        provider = _ScriptedProvider(
            "Business class needs VP approval [T&E Policy §AIR-CABIN]. "
            "You also get free lounge access.",
            "SUPPORTED\nok.",
        )
        agent = PolicyAgent(provider=provider)
        task = Task(context_type="policy_qa", payload={"question": "business class?"})
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.llm,
            task_type="policy_grounded_lookup",
            confidence=0.95,
            rationale="t",
        )
        ctx = AgentContext(
            organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
        )
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=_GROUNDED
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()),
        ):
            result = agent.handle(task, decision, context=ctx, session=MagicMock())

        assert result.status is AgentResultStatus.needs_review
        assert result.output["answer_type"] == "needs_review"
        assert result.output["asserted"] is False
        assert result.output["answer"] is None  # uncited sentence is NOT presented as grounded
        structured = result.output["answer_structured"]  # breakdown retained for human review
        assert structured["needs_review"] is True
        assert [s["needs_review"] for s in structured["sentences"]] == [False, True]
        json.dumps(dict(result.output))  # still json-safe
