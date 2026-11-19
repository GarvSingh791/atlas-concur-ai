"""C7R — local cosine citation floor + answer-vs-question relevance gate (hermetic).

Defect: on the cosine/local path (local embedder + local reranker: local Docker AND staging) the
citation floor was a fixed 0.70 while the local embedder's in-corpus top similarities give
confidences of 0.56-0.69, so most verified answers were never grounded.  A first fix set the
cosine floor to 0.5 — the clamp minimum of ``retrieval._confidence``, i.e. no floor at all — and
an off-topic question ("What life-insurance coverage does the company provide?") came back
GROUNDED on a true, cited sentence about business TRAVEL insurance.

Contract pinned here:

  * ``resolve_citation_threshold``: tau* on the calibrated path (unchanged); 0.55 on the LOCAL
    reranker's cosine path (a real bar: top similarity 0.28 at the 0.20 gate); 0.70 for every
    other cosine path — including a calibrated reranker that fell back to cosine.
  * Below 0.70 (local cosine only) a verified answer is grounded only when the answer AND its
    cited clauses cover every subject concept of the question (``question_relevance``), and the
    question names at least two (vague "What's the cap?" never qualifies).  Otherwise the
    verified text is shown provisionally (needs_review, reason ``question_not_covered``).
    The same holds when a figure in the question is absent from the answer, or when any
    sentence opens with a yes/no verdict ("Yes," / "No,"): nothing verifies a verdict against
    the question's premise, so below 0.70 it is never grounded.
  * A bare "Yes." / "No." sentence must agree with the statement it introduces and never
    answers a wh-question, including a preposition-led one ("By when ...") (support gate,
    every path).

Regression proof: with ``resolve_citation_threshold`` returning 0.5 for the cosine path and no
relevance gate (the rejected attempt), ``test_off_topic_life_insurance_is_never_grounded`` and
``test_vague_question_is_never_grounded`` fail (grounded), as do the calibrated-fallback and
below-floor tests; with the old fixed 0.70 floor, ``test_relevant_answer_in_band_is_grounded``
fails (needs_review); without ``support._check_bare_answers`` the cited-bare-"Yes" and
wh-question cases fail (supported / grounded); without the ``polar_verdict`` /
``unmatched_numbers`` checks the wrong-premise cases in ``TestPolarVerdictBelowFloor`` are
GROUNDED (the second review's finding).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config.settings as cs
from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    LOCAL_COSINE_CITATION_THRESHOLD,
    Citation,
    CitationKind,
    resolve_citation_threshold,
)
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.grounding import check_entailment, extract_cited_sentences
from ai_agents.rag.retrieval import (
    GroundedClause,
    GroundedRetrieval,
    GroundingStatus,
    _confidence,
)
from ai_agents.rag.support import has_polar_verdict, question_relevance
from database.models.ai_decision_log import AiAgent, AiRoute
from database.seeds.corpus import policy as corpus_policy
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

_UNITS: dict[str, str] = {u.id: u.text for u in corpus_policy._UNITS}
_SOURCE = corpus_policy.DOCUMENT.title
_SUPPORTED = "SUPPORTED\nThe answer follows from the cited clauses."
_TAU_STAR = 0.039299
_GATE = 0.20  # RETRIEVAL_SIMILARITY_THRESHOLD


def _label(ref: str) -> str:
    return f"[T&E Policy §{ref}]"


@pytest.fixture(autouse=True)
def _local_rerank_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RERANK_PROVIDER", "local")
    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


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


def _citation(ref: str) -> Citation:
    return Citation(
        kind=CitationKind.clause_ref,
        ref=ref,
        detail=_UNITS[ref],
        source=_SOURCE,
        document_type="policy",
    )


def _retrieval(
    question: str,
    refs: tuple[str, ...],
    confidence: float,
    *,
    source: str = "cosine",
    tau_star: float | None = None,
) -> GroundedRetrieval:
    return GroundedRetrieval(
        query=question,
        status=GroundingStatus.grounded,
        clauses=tuple(
            GroundedClause(clause_ref=r, chunk_text=_UNITS[r], similarity=0.4, document_type=None)
            for r in refs
        ),
        citations=tuple(_citation(r) for r in refs),
        confidence=confidence,
        reason=None,
        threshold=_GATE,
        confidence_source=source,
        tau_star=tau_star,
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="t",
)


def _run(retrieval: GroundedRetrieval, draft: str, critic: str = _SUPPORTED) -> tuple[Any, Any]:
    records: list[Any] = []

    def _capture(session: Any, *, context: Any, record: Any) -> MagicMock:
        records.append(record)
        return MagicMock()

    agent = PolicyAgent(provider=_ScriptedProvider(draft, critic))
    ctx = AgentContext(
        organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None
    )
    with (
        patch("ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=retrieval),
        patch("ai_agents.agents.policy_agent.write_decision_log", side_effect=_capture),
    ):
        result = agent.handle(
            Task(context_type="policy_qa", payload={"question": retrieval.query}),
            _DECISION,
            context=ctx,
            session=MagicMock(),
        )
    assert len(records) == 1  # still exactly one decision-log write
    return result, records[0].output_summary


# The exact draft the rejected attempt labelled GROUNDED for una-014 (live Gemini, local path).
_LIFE_Q = "What life-insurance coverage does the company provide?"
_LIFE_DRAFT = f"The company provides business travel insurance {_label('INTL-04')}."
_HOTEL_Q = "What is the nightly hotel rate cap in New York?"
_HOTEL_DRAFT = f"The nightly hotel rate cap in New York is $300 {_label('LOD-01')}."


# ------------------------------------------------------------------- resolve_citation_threshold


class TestResolveCitationThreshold:
    def test_calibrated_path_is_unchanged(self) -> None:
        for provider in (None, "local", "novita", "huggingface"):
            assert (
                resolve_citation_threshold("calibrated", _TAU_STAR, rerank_provider=provider)
                == _TAU_STAR
            )

    def test_local_cosine_path_uses_the_calibrated_local_floor(self) -> None:
        assert resolve_citation_threshold("cosine", None, rerank_provider="local") == 0.55
        assert LOCAL_COSINE_CITATION_THRESHOLD == 0.55

    @pytest.mark.parametrize("provider", ["novita", "huggingface", "cohere", None])
    def test_cosine_fallback_of_any_other_reranker_fails_closed(self, provider: str | None) -> None:
        # Reranker down / tau* missing on a calibrated deployment -> keep the historical 0.70.
        assert (
            resolve_citation_threshold("cosine", None, rerank_provider=provider)
            == DEFAULT_CITATION_THRESHOLD
        )

    def test_unknown_source_and_calibrated_without_tau_fail_closed(self) -> None:
        assert resolve_citation_threshold(None, None, rerank_provider="local") == 0.70
        assert resolve_citation_threshold("calibrated", None, rerank_provider="local") == 0.70

    def test_local_floor_is_a_real_bar_not_the_gate_image(self) -> None:
        # The rejected attempt used 0.5 == _confidence(gate) == the clamp minimum: a no-op.
        assert _confidence(_GATE, _GATE) == 0.5 < LOCAL_COSINE_CITATION_THRESHOLD
        assert _confidence(0.27, _GATE) < LOCAL_COSINE_CITATION_THRESHOLD
        assert _confidence(0.29, _GATE) > LOCAL_COSINE_CITATION_THRESHOLD


# ---------------------------------------------------------------------------- question relevance


class TestQuestionRelevance:
    def test_life_insurance_answered_with_travel_insurance_is_not_relevant(self) -> None:
        rel = question_relevance(_LIFE_Q, _LIFE_DRAFT, [_UNITS["INTL-04"]])
        assert rel.relevant is False
        assert "lif" in rel.uncovered and "lif" in rel.unaddressed

    def test_echoing_the_subject_does_not_help_when_the_clause_lacks_it(self) -> None:
        draft = f"Life insurance is provided as business travel insurance {_label('INTL-04')}."
        rel = question_relevance(_LIFE_Q, draft, [_UNITS["INTL-04"]])
        assert rel.relevant is False
        assert "lif" in rel.uncovered

    @pytest.mark.parametrize(
        "question",
        ["What's the cap?", "Is it reimbursable?", "Can you approve it?", "Is that allowed?"],
    )
    def test_vague_question_is_never_relevant(self, question: str) -> None:
        draft = f"The Tier 1 hotel cap is $300 per night and it is reimbursable {_label('LOD-01')}."
        assert question_relevance(question, draft, [_UNITS["LOD-01"]]).relevant is False

    def test_answer_about_another_subject_in_the_same_clause_is_not_relevant(self) -> None:
        # LOD-01 covers every tier; an answer about Tier 2 never addresses "Tier 1 ... New York".
        q = "What is the nightly hotel cap in New York?"
        draft = f"Tier 2 cities have a nightly cap of $200 {_label('LOD-01')}."
        rel = question_relevance(q, draft, [_UNITS["LOD-01"]])
        assert rel.relevant is False
        assert {"new", "york"} <= set(rel.unaddressed)

    def test_question_scaffolding_is_not_required(self) -> None:
        q = "How many days do I have to submit my hotel receipts, and is that allowed?"
        rel = question_relevance(
            q, "Submit hotel receipts within 30 days.", ["submit hotel receipts 30 days"]
        )
        assert "many" not in rel.concepts and "permit" not in rel.concepts
        assert rel.relevant is True

    def test_relevant_answer(self) -> None:
        rel = question_relevance(_HOTEL_Q, _HOTEL_DRAFT, [_UNITS["LOD-01"]])
        assert rel.relevant is True, rel


# ----------------------------------------------------------------------------- agent level


class TestAgentLocalCosineBand:
    def test_relevant_answer_in_band_is_grounded(self) -> None:
        # 0.56-0.69 is where staging's real in-corpus answers land.
        for confidence in (0.56, 0.62, 0.69):
            result, summary = _run(
                _retrieval(_HOTEL_Q, ("LOD-01", "LOD-02"), confidence), _HOTEL_DRAFT
            )
            assert result.output["answer_type"] == "grounded", summary
            assert result.status is AgentResultStatus.ok
            assert summary["question_relevance"]["relevant"] is True
            assert "needs_review" not in result.output

    def test_below_local_floor_is_provisional(self) -> None:
        # 0.54 == top similarity ~0.264: a real retrieval state between the gate and the floor.
        result, summary = _run(_retrieval(_HOTEL_Q, ("LOD-01",), 0.54), _HOTEL_DRAFT)
        assert result.output["answer_type"] == "needs_review"
        assert result.output["answer"] == _HOTEL_DRAFT
        assert summary["withheld_reason"] == "below_confidence_floor"
        assert summary["question_relevance"] is None

    def test_off_topic_life_insurance_is_never_grounded(self) -> None:
        # The rejected attempt's una-014 result: true, cited, entailed, critic-SUPPORTED — and
        # not an answer.  Confidence 0.564 is the live local-path value.
        result, summary = _run(_retrieval(_LIFE_Q, ("INTL-04", "INTL-01"), 0.564), _LIFE_DRAFT)
        out = result.output
        assert out["answer_type"] == "needs_review", summary
        assert out["asserted"] is False
        assert result.status is AgentResultStatus.needs_review
        assert summary["withheld_reason"] == "question_not_covered"
        assert summary["question_relevance"]["relevant"] is False
        # The verified sentence is shown as a provisional needs_review answer (as before).
        assert out["answer"] == _LIFE_DRAFT
        assert summary["sentence_support"][0]["supported"] is True

    @pytest.mark.parametrize(
        ("question", "confidence"),
        [("What's the cap?", 0.688), ("Is it reimbursable?", 0.616), ("Can you approve it?", 0.6)],
    )
    def test_vague_question_is_never_grounded(self, question: str, confidence: float) -> None:
        # Live local-path confidences of una-033 / una-034 (fully cited, verified drafts).
        draft = f"The Tier 1 nightly cap is $300 {_label('LOD-01')}."
        result, summary = _run(_retrieval(question, ("LOD-01",), confidence), draft)
        assert result.output["answer_type"] == "needs_review", summary
        assert summary["withheld_reason"] == "question_not_covered"

    def test_relevance_is_not_evaluated_at_or_above_070(self) -> None:
        # Historical behaviour kept: 0.70+ is decided by the support gate + critic alone.
        result, summary = _run(_retrieval(_HOTEL_Q, ("LOD-01",), 0.70), _HOTEL_DRAFT)
        assert result.output["answer_type"] == "grounded"
        assert summary["question_relevance"] is None

    @pytest.mark.parametrize("provider", ["huggingface", "novita"])
    def test_calibrated_reranker_fallen_back_to_cosine_keeps_070(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        monkeypatch.setenv("RERANK_PROVIDER", provider)
        monkeypatch.setenv("RERANK_API_KEY", "test-key")
        cs.get_settings.cache_clear()
        result, summary = _run(_retrieval(_HOTEL_Q, ("LOD-01",), 0.69), _HOTEL_DRAFT)
        assert result.output["answer_type"] == "needs_review"
        assert summary["withheld_reason"] == "below_confidence_floor"
        assert summary["question_relevance"] is None

    def test_calibrated_path_never_runs_the_relevance_gate(self) -> None:
        # tau* path: confidence is a Platt probability (always < 0.70) — unchanged by C7R even
        # for a question the relevance gate would reject.
        retrieval = _retrieval(
            "What's the cap?", ("LOD-01",), 0.11, source="calibrated", tau_star=_TAU_STAR
        )
        result, summary = _run(retrieval, f"The Tier 1 nightly cap is $300 {_label('LOD-01')}.")
        assert result.output["answer_type"] == "grounded"
        assert summary["question_relevance"] is None

    @pytest.mark.parametrize(
        "draft",
        [
            # wrong amount (number guard)
            f"The nightly hotel rate cap in New York is $450 {_label('LOD-01')}.",
            # one uncited sentence (strict per-sentence rule)
            f"{_HOTEL_DRAFT} Suites are always allowed.",
            # citation to a clause that was never retrieved
            f"The nightly hotel rate cap in New York is $300 {_label('LOD-99')}.",
        ],
        ids=["wrong-number", "uncited-sentence", "unretrieved-ref"],
    )
    def test_in_band_gates_still_block_unverified_drafts(self, draft: str) -> None:
        result, _ = _run(_retrieval(_HOTEL_Q, ("LOD-01",), 0.62), draft)
        assert result.output["answer_type"] != "grounded"
        assert result.output["asserted"] is False

    def test_in_band_critic_rejection_still_blocks(self) -> None:
        result, _ = _run(
            _retrieval(_HOTEL_Q, ("LOD-01",), 0.62), _HOTEL_DRAFT, "UNSUPPORTED\nnot in clauses."
        )
        assert result.output["answer_type"] == "needs_review"
        assert result.output["answer"] is None


# ------------------------------------------------------------------------------ bare Yes / No


_MINIBAR_Q = "Are minibar charges reimbursable?"


class TestBareYesNo:
    @pytest.mark.parametrize(
        "draft",
        [
            # The reviewer's regression case (label written after the full stop).
            f"Yes. {_label('LOD-04')} Minibar charges are not reimbursable {_label('LOD-04')}.",
            # The bare "Yes" carries its own citation: it used to score 1.0 and pass.
            f"Yes {_label('LOD-04')}. Minibar charges are not reimbursable {_label('LOD-04')}.",
        ],
        ids=["label-after-stop", "cited-bare-yes"],
    )
    @pytest.mark.parametrize("confidence", [0.62, 0.92], ids=["in-band", "above-070"])
    def test_yes_contradicting_a_negative_statement_is_never_grounded(
        self, draft: str, confidence: float
    ) -> None:
        result, summary = _run(_retrieval(_MINIBAR_Q, ("LOD-04",), confidence), draft)
        assert result.output["answer_type"] != "grounded", summary["sentence_support"]
        assert result.output["asserted"] is False
        assert summary["sentence_support"][0]["supported"] is False
        # Never shown on its own: at most the verified negative sentence survives.
        assert not (result.output["answer"] or "").startswith("Yes")

    def test_agreeing_bare_no_is_supported(self) -> None:
        draft = f"No {_label('LOD-04')}. Minibar charges are not reimbursable {_label('LOD-04')}."
        result, summary = _run(_retrieval(_MINIBAR_Q, ("LOD-04",), 0.92), draft)
        assert result.output["answer_type"] == "grounded", summary["sentence_support"]

    def test_bare_answer_to_a_wh_question_fails(self) -> None:
        q = "What is the nightly hotel cap in New York?"
        cited = extract_cited_sentences(
            f"Yes {_label('LOD-01')}. New York is Tier 1 at $300 per night {_label('LOD-01')}.",
            (_citation("LOD-01"),),
        )
        verdicts = check_entailment(cited, threshold=0.55, question=q).sentences
        assert verdicts[0].supported is False and "polarity" in verdicts[0].failed
        assert verdicts[1].supported is True

    def test_bare_yes_without_a_following_statement_fails(self) -> None:
        cited = extract_cited_sentences(f"Yes {_label('LOD-04')}.", (_citation("LOD-04"),))
        verdict = check_entailment(cited, threshold=0.55, question=_MINIBAR_Q).sentences[0]
        assert verdict.supported is False

    def test_preposition_led_wh_question_is_a_wh_question(self) -> None:
        # "By when ...", "For how long ...": a bare "Yes" answers none of them.
        cited = extract_cited_sentences(
            f"Yes {_label('EXP-01')}. Submit expense reports within 30 days {_label('EXP-01')}.",
            (_citation("EXP-01"),),
        )
        for q in (
            "By when must I submit an expense report?",
            "For how long can I wait to submit my report?",
            "Within how many days must I submit a report?",
        ):
            verdicts = check_entailment(cited, threshold=0.55, question=q).sentences
            assert verdicts[0].supported is False and "polarity" in verdicts[0].failed, q


# ------------------------------------------------------- yes/no verdicts below 0.70 (C7R review 2)


_BIZ_Q = "Can I book business class for a 2-hour flight?"
_BIZ_DRAFT = f"Yes, business class is permitted for flights over 10 hours {_label('AIR-03')}."
_PREMIUM_Q = "Is premium economy allowed on a 2 hour flight?"
_PREMIUM_DRAFT = (
    f"Yes, premium economy is permitted for flights 6–10 hours with manager approval "
    f"{_label('AIR-02')}."
)
_DEADLINE_Q = "By when must I submit an expense report?"  # live ans-120
_DEADLINE_DRAFT = (
    f"Yes, you must submit an expense report within 30 days of trip completion "
    f"{_label('EXP-01')}."
)


class TestPolarVerdictBelowFloor:
    """The relevance gate compares concepts; a "Yes," also asserts the question's PREMISE.

    Reviewer reproduction (PolicyAgent.handle, local cosine 0.60, critic SUPPORTED): both
    wrong-premise "Yes" answers were GROUNDED.  At HEAD (fixed 0.70 floor) they were
    needs_review — the C7R band must not ground them.
    """

    @pytest.mark.parametrize(
        ("question", "draft", "ref"),
        [
            (_BIZ_Q, _BIZ_DRAFT, "AIR-03"),
            (_PREMIUM_Q, _PREMIUM_DRAFT, "AIR-02"),
            (_DEADLINE_Q, _DEADLINE_DRAFT, "EXP-01"),
        ],
        ids=["business-2h", "premium-2h", "by-when-yes"],
    )
    @pytest.mark.parametrize("confidence", [0.56, 0.60, 0.69])
    def test_leading_verdict_in_band_is_never_grounded(
        self, question: str, draft: str, ref: str, confidence: float
    ) -> None:
        result, summary = _run(_retrieval(question, (ref,), confidence), draft)
        out = result.output
        assert out["answer_type"] == "needs_review", summary
        assert out["asserted"] is False
        assert result.status is AgentResultStatus.needs_review
        assert summary["withheld_reason"] == "question_not_covered"
        assert summary["question_relevance"]["relevant"] is False
        assert summary["question_relevance"]["polar_verdict"] is True

    def test_wrong_premise_without_yes_is_caught_by_the_question_figure(self) -> None:
        # Drop the "Yes," — the question's "2" (hours) is still never addressed by the answer.
        draft = f"Business class is permitted for flights over 10 hours {_label('AIR-03')}."
        result, summary = _run(_retrieval(_BIZ_Q, ("AIR-03",), 0.60), draft)
        assert result.output["answer_type"] == "needs_review", summary
        rel = summary["question_relevance"]
        assert rel["relevant"] is False and rel["unmatched_numbers"] == ["2"]
        assert rel["polar_verdict"] is False

    def test_question_figure_distinguishes_2_from_6_hours(self) -> None:
        # The reviewer's minor gap: "7 hour" vs "2 hour" used to get the same verdict.
        draft = f"Premium economy is permitted for flights 6–10 hours {_label('AIR-02')}."
        two = question_relevance(_PREMIUM_Q, draft, [_UNITS["AIR-02"]])
        assert two.relevant is False and two.unmatched_numbers == ("2",)
        exact = question_relevance(
            "Is premium economy allowed on a 6 hour flight?", draft, [_UNITS["AIR-02"]]
        )
        assert exact.unmatched_numbers == ()

    def test_same_answer_without_the_verdict_is_grounded(self) -> None:
        # Positive control: the wh-question answered plainly stays grounded in band.
        draft = (
            f"You must submit an expense report within 30 days of trip completion "
            f"{_label('EXP-01')}."
        )
        result, summary = _run(_retrieval(_DEADLINE_Q, ("EXP-01",), 0.559), draft)
        assert result.output["answer_type"] == "grounded", summary
        assert summary["question_relevance"]["relevant"] is True

    def test_verdict_at_or_above_070_is_unchanged(self) -> None:
        # Historical behaviour: 0.70+ is decided by the support gate + critic alone.
        result, summary = _run(_retrieval(_DEADLINE_Q, ("EXP-01",), 0.75), _DEADLINE_DRAFT)
        assert result.output["answer_type"] == "grounded"
        assert summary["question_relevance"] is None

    @pytest.mark.parametrize(
        ("answer", "polar"),
        [
            ("Yes, minibar charges are reimbursable.", True),
            ("yes minibar charges are reimbursable.", True),
            ("No, minibar charges are not reimbursable.", True),
            ("No. Minibar charges are not reimbursable.", True),
            (f"{_label('LOD-04')} No — minibar charges are not reimbursable.", True),
            ("Minibar charges are not reimbursable. Yes, even on client trips.", True),
            # "No" as a determiner is not a verdict.
            ("No receipt is needed below $75.", False),
            ("Nothing else applies; yesterday's rate is used.", False),
        ],
    )
    def test_has_polar_verdict(self, answer: str, polar: bool) -> None:
        assert has_polar_verdict(answer) is polar
