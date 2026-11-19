"""C7-06 / C7-10 — Policy Q&A needs_review contract + lexical grounding rescue (hermetic).

Shared contract (backend side), for ``answer_type == "needs_review"``:
  - ``answer`` = the critic-supported, cited draft text (cited + entailed sentences only),
    else ``None``; ``needs_review`` = True; ``answer_structured`` kept.
  - silent / grounded shapes unchanged (no ``needs_review`` key).

Regression proof: on the pre-fix code every test in ``TestNeedsReviewContract`` that expects a
non-None ``answer`` or a ``needs_review`` key fails (the agent emitted ``answer=None`` for every
non-grounded result and never set ``needs_review``); ``test_sub_threshold_lexically_relevant_
chunk_is_kept`` fails because the cull dropped every dense chunk below 0.20 that the sparse
path had not flagged.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import config.settings as cs
from ai_agents.agents.policy_agent import PolicyAgent, presentable_policy_output
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind, GroundedResult
from ai_agents.guardrails.reflection import Critique, reflect
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.lexical import content_stems, is_lexically_relevant
from ai_agents.rag.retrieval import (
    GroundedClause,
    GroundedRetrieval,
    GroundedRetriever,
    GroundingStatus,
)
from ai_agents.tools.policy_retrieval import RetrievalIntent, RetrievalResult, RetrievedChunk
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

_HOTEL_CAP_TEXT = (
    "Hotel nightly rate caps exclude taxes. Tier-1 cities (New York, San Francisco, London, "
    "Tokyo, Paris, Zurich): USD 250 per night. Domestic US cities (non-Tier-1): USD 180 per "
    "night."
)


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


def _retrieval(confidence: float, source: str = "cosine") -> GroundedRetrieval:
    """A grounded retrieval on the cosine path (staging: local embedder, no reranker)."""
    return GroundedRetrieval(
        query="What is the nightly hotel rate cap for Tier-1 cities?",
        status=GroundingStatus.grounded,
        clauses=(
            GroundedClause(
                clause_ref="HOTEL-CAP",
                chunk_text=_HOTEL_CAP_TEXT,
                similarity=0.36,
                document_type=None,
            ),
        ),
        citations=(
            Citation(
                kind=CitationKind.clause_ref,
                ref="HOTEL-CAP",
                detail=_HOTEL_CAP_TEXT,
                source="Atlas Global Travel & Expense Policy",
            ),
        ),
        confidence=confidence,
        reason=None,
        threshold=0.20,
        confidence_source=source,
    )


_SILENT = GroundedRetrieval(
    query="What is the weather in Paris?",
    status=GroundingStatus.silent,
    clauses=(),
    citations=(),
    confidence=None,
    reason="below_threshold: top similarity 0.1 < threshold 0.2",
    threshold=0.20,
)

_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="t",
)

# The draft the staging LLM produced for the Tier-1 question (C7-06 evidence out_conv.txt).
_CITED_DRAFT = "Tier-1 cities have a cap of USD 250 per night [Policy §HOTEL-CAP]."


def _run(retrieval: GroundedRetrieval, *responses: str) -> tuple[Any, list[Any]]:
    records: list[Any] = []
    agent = PolicyAgent(provider=_ScriptedProvider(*responses))
    ctx = AgentContext(
        organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
    )
    with (
        patch("ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=retrieval),
        patch(
            "ai_agents.agents.policy_agent.write_decision_log",
            side_effect=lambda s, *, context, record: records.append(record) or MagicMock(),
        ),
    ):
        result = agent.handle(
            Task(context_type="policy_qa", payload={"question": retrieval.query}),
            _DECISION,
            context=ctx,
            session=MagicMock(),
        )
    return result, records


@pytest.fixture(autouse=True)
def _local_rerank_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The cosine-path citation floor depends on the configured reranker (C7R): pin the
    # local/staging one (no calibrated gate) unless a test overrides it.
    monkeypatch.setenv("RERANK_PROVIDER", "local")
    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


class TestNeedsReviewContract:
    def test_critic_supported_cited_draft_below_cosine_floor_is_provisional_answer(self) -> None:
        # Staging C7-06: critic SUPPORTED, fully cited, but cosine confidence 0.52 is below the
        # local cosine floor (0.55; 0.52 == top similarity 0.232, just past the 0.20 gate).
        # C7R: was 0.60, which is now inside the question-relevance band (see
        # test_local_cosine_floor_requires_question_relevance_below_070) — the below-floor
        # provisional path is still reachable, at a lower confidence.
        result, records = _run(_retrieval(0.52), _CITED_DRAFT, "SUPPORTED\nok.")
        out = result.output
        assert result.status is AgentResultStatus.needs_review
        assert out["answer_type"] == "needs_review"  # NEVER presented as grounded
        assert out["asserted"] is False
        assert out["needs_review"] is True
        assert out["answer"] == _CITED_DRAFT
        assert out["answer_structured"]["full_text"] == _CITED_DRAFT
        assert [c.ref for c in result.citations] == ["HOTEL-CAP"]
        json.dumps(out)  # SSE result frame stays json-safe
        assert len(records) == 1  # still exactly one decision-log write
        assert records[0].output_summary["answer_type"] == "needs_review"
        assert records[0].output_summary["provisional_answer"] is True

    def test_critic_rejected_draft_is_not_surfaced(self) -> None:
        result, _ = _run(_retrieval(0.60), _CITED_DRAFT, "UNSUPPORTED\nnot in clauses.")
        assert result.output["answer_type"] == "needs_review"
        assert result.output["answer"] is None
        assert result.output["needs_review"] is True

    def test_critic_contradicted_draft_is_not_surfaced(self) -> None:
        result, _ = _run(_retrieval(0.60), _CITED_DRAFT, "CONTRADICTED\nwrong amount.")
        assert result.output["answer"] is None
        assert result.output["needs_review"] is True

    def test_uncited_draft_is_not_surfaced_out_of_corpus_abstention_holds(self) -> None:
        # An out-of-corpus question that slipped past the weak cosine gate: the LLM cannot cite a
        # clause, so there is NO cited draft -> no answer, even if the critic waves it through.
        # C7R (out-of-corpus classification): the model DECLINED ("does not cover"), so this is
        # now classified as an abstention (silent, "not covered" + escalation) rather than a
        # "needs review" answer — still no answer text, no citations.
        result, records = _run(
            _retrieval(0.55),
            "The provided policy does not cover the weather.",
            "SUPPORTED\nok.",
        )
        assert result.output["answer_type"] == "silent"
        assert result.output["answer"] is None
        assert "needs_review" not in result.output  # silent shape unchanged
        assert result.citations == ()
        assert records[0].output_summary["decision"] == "abstain"
        assert records[0].output_summary["withheld_reason"] == "model_declined"

    def test_uncited_non_refusal_draft_stays_needs_review_withheld(self) -> None:
        # An uncited draft that does NOT decline is not evidence the topic is out of corpus (the
        # model may have ignored the citation rule) -> needs_review, nothing shown.
        result, records = _run(_retrieval(0.55), "Hotels are capped somewhere.", "SUPPORTED\nok.")
        assert result.output["answer_type"] == "needs_review"
        assert result.output["answer"] is None
        assert records[0].output_summary["withheld_reason"] == "no_verified_citation"

    def test_entailment_failing_cited_sentence_is_dropped_the_verified_one_shown(self) -> None:
        # Cited, critic-supported, but one cited clause does not support its claim.
        # C7R decision (sentence-level provisional answers, as documented in the C7 phase log):
        # the unsupported sentence is DROPPED and the verified one is shown — the dropped claim
        # carries no qualifier, so removing it cannot invert the shown sentence's meaning (a
        # dropped CAVEAT still withholds everything: see the next test).  The previous
        # all-or-nothing code returned answer=None here.
        sentence_1 = "Tier-1 cities have a cap of USD 250 per night [Policy §HOTEL-CAP]."
        draft = f"{sentence_1} Employees may always upgrade to suites [Policy §HOTEL-CAP]."
        result, records = _run(_retrieval(0.60), draft, "SUPPORTED\nok.")
        assert result.output["answer"] == sentence_1
        assert result.output["answer_structured"]["full_text"] == sentence_1
        assert len(result.output["answer_structured"]["sentences"]) == 1
        assert result.output["needs_review"] is True
        assert result.output["answer_type"] == "needs_review"
        assert records[0].output_summary["provisional_answer"] is True
        assert records[0].output_summary["withheld_reason"] == "partial_provisional"
        assert records[0].output_summary["shown_sentences"] == [0]

    def test_uncited_caveat_is_never_silently_dropped(self) -> None:
        # Review finding: "cap ... [§HOTEL-CAP]. This cap does not apply to Tier-1 cities." —
        # surfacing only the cited sentence shows an unqualified cap with the exception removed.
        # Now the whole draft is withheld.  On the previous code ``answer`` was sentence 1 only.
        # C7R keeps this: a DROPPED sentence carrying a qualifier ("not") withholds everything.
        draft = (
            "Hotel stays are capped at USD 250 per night [Policy §HOTEL-CAP]. "
            "This cap does not apply to Tier-1 cities."
        )
        result, _ = _run(_retrieval(0.60), draft, "SUPPORTED\nok.")
        assert result.output["answer"] is None
        assert result.output["answer_type"] == "needs_review"

    def test_cited_critic_supported_out_of_corpus_draft_is_provisional_never_grounded(
        self,
    ) -> None:
        # Pins the intended behaviour the review asked about: a draft that passed the weak cosine
        # gate (confidence ~0.59, below 0.70) and is FULLY cited, entailed by the cited clause
        # text and SUPPORTED by the critic — but does not answer the question asked (Domestic
        # cap for a Tier-1 question) — is shown as a provisional needs_review answer and is
        # NEVER labelled grounded.  C7R: below 0.70 on the local cosine path "grounded" also
        # needs the answer-vs-question relevance gate; this draft never addresses "Tier-1".
        draft = "Domestic US cities have a cap of USD 180 per night [Policy §HOTEL-CAP]."
        result, records = _run(_retrieval(0.59), draft, "SUPPORTED\nok.")
        out = result.output
        assert out["answer_type"] == "needs_review"
        assert out["asserted"] is False
        assert out["needs_review"] is True
        assert out["answer"] == draft
        assert result.status is AgentResultStatus.needs_review
        assert records[0].output_summary["withheld_reason"] == "question_not_covered"
        assert "tier" in records[0].output_summary["question_relevance"]["unaddressed"]

    def test_grounded_shape_unchanged(self) -> None:
        result, _ = _run(_retrieval(0.92), _CITED_DRAFT, "SUPPORTED\nok.")
        out = result.output
        assert result.status is AgentResultStatus.ok
        assert out["answer_type"] == "grounded"
        assert out["answer"] == _CITED_DRAFT
        assert "needs_review" not in out  # grounded shape unchanged

    def test_silent_shape_unchanged(self) -> None:
        result, _ = _run(_SILENT, "should never be called")
        out = result.output
        assert out["answer_type"] == "silent"
        assert out["answer"] is None
        assert "needs_review" not in out

    def test_local_cosine_floor_requires_question_relevance_below_070(self) -> None:
        # Data decision (C7-06, revised C7R): WITHOUT a relevance check 0.70 is the only cosine
        # operating point with zero out-of-corpus pass-through, so it is kept for every answer
        # that is not shown to cover the question.  On the local reranker an answer in
        # [0.55, 0.70) is grounded only when the answer and its cited clause cover the question;
        # below 0.55 it is provisional whatever it says.
        relevant = "The nightly hotel rate cap for Tier-1 cities is USD 250 [Policy §HOTEL-CAP]."
        result, records = _run(_retrieval(0.56), relevant, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "grounded"
        assert records[0].output_summary["question_relevance"]["relevant"] is True
        result, records = _run(_retrieval(0.54), relevant, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "needs_review"
        assert records[0].output_summary["withheld_reason"] == "below_confidence_floor"
        # The terser draft never says what the cap is FOR ("hotel", "rate"): provisional.
        result, records = _run(_retrieval(0.60), _CITED_DRAFT, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "needs_review"
        assert result.output["answer"] == _CITED_DRAFT
        assert records[0].output_summary["withheld_reason"] == "question_not_covered"
        result, records = _run(_retrieval(0.70), _CITED_DRAFT, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "grounded"
        assert records[0].output_summary["question_relevance"] is None  # not evaluated >= 0.70

    @pytest.mark.parametrize("provider", ["huggingface", "novita", "cohere"])
    def test_cosine_fallback_of_a_calibrated_reranker_keeps_070(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        # A calibrated reranker that fell back to cosine (reranker down / tau* missing) keeps
        # the fail-closed 0.70 floor: 0.69 stays needs_review even for a relevant answer.
        monkeypatch.setenv("RERANK_PROVIDER", provider)
        monkeypatch.setenv("RERANK_API_KEY", "test-key")
        cs.get_settings.cache_clear()
        result, records = _run(_retrieval(0.69), _CITED_DRAFT, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "needs_review"
        assert records[0].output_summary["withheld_reason"] == "below_confidence_floor"
        result, _ = _run(_retrieval(0.70), _CITED_DRAFT, "SUPPORTED\nok.")
        assert result.output["answer_type"] == "grounded"


class TestReflectCriticSupported:
    def _hyp(self, confidence: float, citations: tuple[Citation, ...]) -> Any:
        seed = GroundedResult(
            claim="c", value="v", citations=citations, confidence=confidence, asserted=False
        )
        hyp = MagicMock()
        hyp.seed.return_value = seed
        hyp.regenerate.return_value = seed
        return hyp

    _CIT = (Citation(kind=CitationKind.clause_ref, ref="X"),)

    def test_supported_but_below_floor_is_rejected_with_critic_supported(self) -> None:
        r = reflect(
            hypothesizer=self._hyp(0.5, self._CIT),
            critic=lambda h: Critique(verdict="supported", rationale="ok"),
            threshold=0.7,
        )
        assert r.outcome is ReflectionOutcome.rejected
        assert r.critic_supported is True

    def test_passed_is_critic_supported(self) -> None:
        r = reflect(
            hypothesizer=self._hyp(0.9, self._CIT),
            critic=lambda h: Critique(verdict="supported", rationale="ok"),
        )
        assert r.outcome is ReflectionOutcome.passed
        assert r.critic_supported is True

    @pytest.mark.parametrize("verdict", ["unsupported", "contradicted"])
    def test_critic_rejection_is_not_critic_supported(self, verdict: str) -> None:
        r = reflect(
            hypothesizer=self._hyp(0.9, self._CIT),
            critic=lambda h: Critique(verdict=verdict, rationale="no"),  # type: ignore[arg-type]
        )
        assert r.outcome is ReflectionOutcome.rejected
        assert r.critic_supported is False


# ---------------------------------------------------------------------------
# C7-10 — lexical rescue in the grounding cull
# ---------------------------------------------------------------------------


def _chunk(ref: str, sim: float, text: str, *, sparse: bool = False) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref=ref,
        chunk_text=text,
        similarity=sim,
        policy_name="Atlas Global Travel & Expense Policy",
        from_sparse=sparse,
    )


class _Fixed:
    def __init__(self, result: RetrievalResult) -> None:
        self._r = result

    def retrieve(self, intent: RetrievalIntent, *, context: Any, session: Any) -> RetrievalResult:
        return self._r


def _ctx() -> AgentContext:
    return AgentContext(organization_id=uuid.uuid4(), granted_permission_keys=frozenset())


_HOTEL_PREF_TEXT = (
    "Preferred hotel partners are Marriott International and Hilton. Travelers should book "
    "within the Marriott or Hilton portfolio when properties are available at the applicable "
    "capped rate in the destination city."
)


class TestLexicalStems:
    def test_symmetric_inflection_normalization(self) -> None:
        assert content_stems("nightly hotel caps") == content_stems("night hotels cap")
        assert content_stems("expenses receipts") == content_stems("expense receipt")
        assert content_stems("Tier-1 cities") == content_stems("tier 1 city")

    def test_stop_words_dropped(self) -> None:
        assert content_stems("What is the") == frozenset()

    def test_short_and_non_plural_words_kept(self) -> None:
        assert "class" in content_stems("business class")
        assert "bus" in content_stems("bus")

    def test_relevance_needs_majority_and_two_concepts(self) -> None:
        assert is_lexically_relevant("What is the nightly hotel cap?", _HOTEL_CAP_TEXT, "HOTEL-CAP")
        assert not is_lexically_relevant("What is the weather in Paris?", _HOTEL_CAP_TEXT)
        assert not is_lexically_relevant("hotel?", _HOTEL_CAP_TEXT)  # one concept only


class TestLexicalRescueInGrounding:
    def test_sub_threshold_lexically_relevant_chunk_is_kept(self) -> None:
        # Staging C7-10: "nightly hotel cap" -> HOTEL-PREF 0.270 passes the gate, HOTEL-CAP 0.142
        # is the answer but sits below the 0.20 cull and the sparse path did not flag it.
        result = RetrievalResult(
            query="What is the nightly hotel cap?",
            chunks=(
                _chunk("HOTEL-PREF", 0.270, _HOTEL_PREF_TEXT),
                _chunk("HOTEL-CAP", 0.142, _HOTEL_CAP_TEXT),
            ),
            max_dense_similarity=0.270,
        )
        out = GroundedRetriever(retriever=_Fixed(result), threshold=0.20).retrieve(
            "What is the nightly hotel cap?", context=_ctx(), session=MagicMock()
        )
        assert out.status is GroundingStatus.grounded
        assert [c.clause_ref for c in out.clauses] == ["HOTEL-PREF", "HOTEL-CAP"]
        assert [c.ref for c in out.citations] == ["HOTEL-PREF", "HOTEL-CAP"]

    def test_unrelated_sub_threshold_chunk_still_dropped(self) -> None:
        result = RetrievalResult(
            query="What is the nightly hotel cap?",
            chunks=(
                _chunk("HOTEL-PREF", 0.270, _HOTEL_PREF_TEXT),
                _chunk("MEALS-PD", 0.15, "Daily meal allowance: USD 75 per day."),
            ),
            max_dense_similarity=0.270,
        )
        out = GroundedRetriever(retriever=_Fixed(result), threshold=0.20).retrieve(
            "What is the nightly hotel cap?", context=_ctx(), session=MagicMock()
        )
        assert [c.clause_ref for c in out.clauses] == ["HOTEL-PREF"]

    def test_lexical_relevance_never_changes_the_abstention_gate(self) -> None:
        # Max dense cosine below the gate -> SILENT, even though a chunk is lexically relevant.
        result = RetrievalResult(
            query="What is the nightly hotel cap?",
            chunks=(_chunk("HOTEL-CAP", 0.142, _HOTEL_CAP_TEXT),),
            max_dense_similarity=0.142,
        )
        out = GroundedRetriever(retriever=_Fixed(result), threshold=0.20).retrieve(
            "What is the nightly hotel cap?", context=_ctx(), session=MagicMock()
        )
        assert out.status is GroundingStatus.silent
        assert out.clauses == ()

    def test_staging_stub_corpus_reproduction_with_the_real_local_embedder(self) -> None:
        """End-to-end over the REAL local embedder + the stub clauses staging serves.

        Reproduces the staging cosines (HOTEL-CAP 0.142 < 0.20) and shows HOTEL-CAP now reaches
        the grounding context while the stored document vectors are untouched.
        """
        from database.seeds.policy_content import CLAUSES
        from services.embeddings_provider.local import embed

        q = "What is the nightly hotel cap?"
        qv = np.array(embed(q))
        scored = sorted(
            (
                (float(qv @ np.array(embed(c["chunk_text"]))), c["clause_ref"], c["chunk_text"])
                for c in CLAUSES
            ),
            reverse=True,
        )
        cos = {ref: s for s, ref, _ in scored}
        assert cos["HOTEL-CAP"] == pytest.approx(0.142, abs=0.002)  # the staging number
        top5 = tuple(_chunk(ref, s, text) for s, ref, text in scored[:5])
        result = RetrievalResult(query=q, chunks=top5, max_dense_similarity=scored[0][0])
        out = GroundedRetriever(retriever=_Fixed(result), threshold=0.20).retrieve(
            q, context=_ctx(), session=MagicMock()
        )
        assert out.status is GroundingStatus.grounded
        assert "HOTEL-CAP" in [c.clause_ref for c in out.clauses]


class TestPresentablePolicyOutput:
    """Review fix (C7-06/C7-09): a WITHHELD needs_review draft never leaks via side channels."""

    _CIT = Citation(kind=CitationKind.clause_ref, ref="HOTEL-CAP", detail="d", source="p")

    def test_withheld_draft_drops_structured_and_citations(self) -> None:
        out, cits = presentable_policy_output(
            {
                "answer_type": "needs_review",
                "answer": None,
                "needs_review": True,
                "answer_structured": {"full_text": "withheld draft"},
            },
            (self._CIT,),
        )
        assert out["answer_structured"] is None
        assert cits == ()
        assert "withheld draft" not in json.dumps(out)

    def test_provisional_and_grounded_and_silent_pass_through(self) -> None:
        for output in (
            {"answer_type": "needs_review", "answer": "x [P §HOTEL-CAP].", "needs_review": True},
            {"answer_type": "grounded", "answer": "x"},
            {"answer_type": "silent", "answer": None},
            {"trip_id": "t", "shortlist": []},  # non-policy agent output
        ):
            out, cits = presentable_policy_output(output, (self._CIT,))
            assert out == output
            assert cits == (self._CIT,)

    def test_agent_withheld_result_through_the_boundary(self) -> None:
        result, _ = _run(_retrieval(0.60), _CITED_DRAFT, "UNSUPPORTED\nnot in clauses.")
        assert result.citations  # the agent result keeps them (audit)
        out, cits = presentable_policy_output(result.output, result.citations)
        assert cits == ()
        assert out["answer_structured"] is None
