"""Hermetic tests for R5-T3 deterministic NLI/entailment grounding (no DB, no LLM).

Two layers:
  * ``check_entailment`` unit sub-cases (a)-(c) + edges — pure token-overlap on a CitedAnswer.
  * PolicyAgent gate sub-cases (d)-(e) — an answer that passes the R5-T2 citation gate but FAILS
    entailment is downgraded to needs_review (answer withheld), with the verdict riding the SINGLE
    audit write (Fence 6 / Rule 11 — exactly one write_decision_log on the grounded path).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.grounding import (
    CitedAnswer,
    CitedSentence,
    check_entailment,
    extract_cited_sentences,
)
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

_LEAF = "Business class is only allowed on flights over 10 hours."


class _ScriptedProvider(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(text=text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1))

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _cite(ref: str = "AIR-CABIN", detail: str = _LEAF) -> Citation:
    return Citation(kind=CitationKind.clause_ref, ref=ref, detail=detail, source="Atlas T&E Policy")


# --------------------------------------------------------------------------- check_entailment unit


class TestCheckEntailmentUnit:
    def test_a_fully_cited_extractive_answer_is_entailed(self) -> None:
        # (a) every sentence's tokens are present in the cited leaf text → entailed.
        cited = extract_cited_sentences(f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].", (_cite(),))
        r = check_entailment(cited, threshold=0.70)
        assert r.entailed is True
        assert r.min_score >= 0.70
        assert r.threshold == 0.70

    def test_b_cited_chunk_missing_sentence_tokens_fails(self) -> None:
        # (b) sentence cites a real chunk whose leaf text does NOT contain its claim tokens → fail.
        cite = _cite(detail="Hotel nightly cap is 200 dollars.")
        cited = extract_cited_sentences(
            "Employees may always fly first class for any trip [T&E Policy §AIR-CABIN].", (cite,)
        )
        r = check_entailment(cited, threshold=0.70)
        assert r.entailed is False
        assert r.min_score < 0.70

    def test_c_uncited_sentence_scores_zero_and_fails(self) -> None:
        # (c) an uncited sentence (no valid marker) has empty context → score 0.0 → not entailed.
        cited = extract_cited_sentences("Business class is always fine.", (_cite(),))
        assert cited.sentences[0].citations == ()  # uncited
        r = check_entailment(cited)
        assert r.scores[0] == 0.0
        assert r.entailed is False

    def test_marker_tokens_are_stripped_before_scoring(self) -> None:
        # The [Label §ref] marker tokens (t/e/policy/air/cabin) must not count against the score:
        # a verbatim-extractive answer entailing its own leaf text scores 1.0, not <1.0.
        cited = extract_cited_sentences(f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].", (_cite(),))
        assert check_entailment(cited).min_score == 1.0

    def test_empty_answer_is_vacuously_entailed(self) -> None:
        assert check_entailment(CitedAnswer(sentences=())).entailed is True
        assert check_entailment(CitedAnswer(sentences=())).min_score == 1.0

    def test_min_score_is_the_weakest_sentence(self) -> None:
        # One entailed + one unsupported sentence → whole answer not entailed; min_score = weak one.
        cited = CitedAnswer(
            sentences=(
                CitedSentence(text=f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].", citations=(_cite(),)),
                CitedSentence(
                    text="You also get a free chauffeur [T&E Policy §AIR-CABIN].",
                    citations=(_cite(),),
                ),
            )
        )
        r = check_entailment(cited, threshold=0.70)
        assert r.entailed is False
        assert r.min_score == min(r.scores)
        assert len(r.scores) == 2

    def test_to_dict_is_json_shaped(self) -> None:
        d = check_entailment(
            extract_cited_sentences("x [T&E Policy §AIR-CABIN].", (_cite(),))
        ).to_dict()
        assert set(d) == {"entailed", "min_score", "scores", "threshold"}
        assert isinstance(d["scores"], list)


# --------------------------------------------------------------------- PolicyAgent NLI gate (d),(e)

_GROUNDED = GroundedRetrieval(
    query="Is business class allowed?",
    status=GroundingStatus.grounded,
    clauses=(
        GroundedClause(
            clause_ref="AIR-CABIN", chunk_text=_LEAF, similarity=0.92, document_type="policy"
        ),
    ),
    citations=(_cite(),),
    confidence=0.92,
    reason=None,
    threshold=0.20,
)
_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="t",
)


def _run(answer: str) -> tuple[Any, list[DecisionRecord]]:
    provider = _ScriptedProvider(answer, "SUPPORTED\nok.")
    agent = PolicyAgent(provider=provider)
    task = Task(context_type="policy_qa", payload={"question": "business class?"})
    ctx = AgentContext(
        organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
    )
    records: list[DecisionRecord] = []

    def _capture(session: Any, *, context: Any, record: DecisionRecord) -> None:
        records.append(record)

    with (
        patch("ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=_GROUNDED),
        patch("ai_agents.agents.policy_agent.write_decision_log", side_effect=_capture),
    ):
        result = agent.handle(task, _DECISION, context=ctx, session=MagicMock())
    return result, records


class TestAgentNliGate:
    def test_d_strict_pass_but_nli_fail_is_downgraded_single_write(self) -> None:
        # Fully cited (strict gate passes) but the claim tokens are absent from the cited leaf
        # text → entailment fails → needs_review, answer withheld, nli_result on the ONE write.
        result, records = _run(
            "Employees may always fly first class for any trip [T&E Policy §AIR-CABIN]."
        )
        assert result.status is AgentResultStatus.needs_review
        assert result.output["answer_type"] == "needs_review"
        assert result.output["asserted"] is False
        assert result.output["answer"] is None
        assert result.output["answer_structured"] is not None  # per-sentence breakdown retained
        assert len(records) == 1  # Fence 6 / Rule 11: exactly one write on the grounded path
        nli = records[0].output_summary["nli_result"]
        assert nli is not None and nli["entailed"] is False
        assert nli["min_score"] < nli["threshold"]

    def test_e_both_gates_pass_is_grounded_single_write(self) -> None:
        # Extractive answer: strict gate AND entailment both pass → grounded, nli_result.entailed.
        result, records = _run(f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].")
        assert result.status is AgentResultStatus.ok
        assert result.output["answer_type"] == "grounded"
        assert result.output["asserted"] is True
        assert isinstance(result.output["answer"], str)
        assert len(records) == 1
        nli = records[0].output_summary["nli_result"]
        assert nli is not None and nli["entailed"] is True
