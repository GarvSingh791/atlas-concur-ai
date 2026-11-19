"""R6 gate-calibration hermetic tests (no DB, no LLM).

Root cause: the citation-contract confidence floor was a single fixed 0.70, but the confidence
SIGNAL changed scale between paths (R3).  On the CALIBRATED path the signal is a Platt reranker
probability gated at tau* (~0.039) — its observed max is ~0.25, so a 0.70 floor is unsatisfiable
and produces a 100% false-block.  The fix gates on the SAME operating point the answerability gate
already validated: tau* on the calibrated path, the historical 0.70 on the cosine/local path
(``resolve_citation_threshold``, mirroring the R6-T2 confidence_source split).

Layers:
  * (A) unit — resolve_citation_threshold scale-aware selection.
  * (B) unit — validate_grounding at the calibrated floor (confidence + citation-PRESENCE).
  * (C) unit — check_entailment at the lowered 0.55 NLI threshold (paraphrase passes, drift blocks).
  * (D) integration — ROOT CAUSE FIXED: a 0.11 calibrated answer now asserts at floor=tau*.
  * (E) integration — SAFETY: an uncited calibrated answer is still withheld (citation-PRESENCE).
  * (F) integration — AUDIT SHAPE UNCHANGED: the gate/nli audit reads retrieval.*, not the floor.

Harness mirrors test_r5_nli_grounding / test_r6_audit_completeness (scripted provider + patched
GroundedRetriever.retrieve + captured write_decision_log record).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    Citation,
    CitationKind,
    GroundedResult,
    resolve_citation_threshold,
    validate_grounding,
)
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.grounding import check_entailment, extract_cited_sentences
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

# The calibrated operating point the abstention gate validated on the keyed Novita path.
_TAU_STAR = 0.039299

_LEAF = "Business class is only allowed on flights over 10 hours."
# A leaf detail used for the (C) entailment overlap arithmetic (distinct from the marker text).
_NLI_LEAF = "Business class is only allowed on flights over ten hours."


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
    return Citation(
        kind=CitationKind.clause_ref,
        ref=ref,
        detail=detail,
        source="Atlas T&E Policy",
        document_type="policy",
    )


# --------------------------------------------------------------- (A) resolve_citation_threshold


class TestResolveCitationThreshold:
    def test_calibrated_returns_tau_star(self) -> None:
        # Calibrated path with a validated operating point -> gate on tau*, not the 0.70 floor.
        assert resolve_citation_threshold("calibrated", _TAU_STAR) == _TAU_STAR

    def test_cosine_returns_default_floor(self) -> None:
        # Cosine/local path -> byte-identical historical 0.70 behaviour.
        assert resolve_citation_threshold("cosine", None) == DEFAULT_CITATION_THRESHOLD
        assert resolve_citation_threshold("cosine", None) == 0.70

    def test_none_source_returns_default_floor(self) -> None:
        assert resolve_citation_threshold(None, None) == 0.70

    def test_calibrated_without_tau_star_falls_back_to_default(self) -> None:
        # Calibrated source but no operating point -> conservative default, never a stray tau*.
        assert resolve_citation_threshold("calibrated", None) == 0.70


# -------------------------------------------- (B) validate_grounding at the calibrated floor


class TestValidateGroundingAtCalibratedFloor:
    def _result(self, *, confidence: float, citations: tuple[Citation, ...]) -> GroundedResult:
        return GroundedResult(
            claim="Business class eligibility",
            value="Business class is allowed on flights over 10 hours.",
            citations=citations,
            confidence=confidence,
            asserted=False,
        )

    def test_above_tau_with_citation_is_asserted(self) -> None:
        # 0.11 >= tau* (0.039299) and >=1 citation -> asserted.  Under the OLD 0.70 floor this was
        # a false-block (0.11 < 0.70).
        r = validate_grounding(
            self._result(confidence=0.11, citations=(_cite(),)), threshold=_TAU_STAR
        )
        assert r.asserted is True

    def test_below_tau_is_not_asserted(self) -> None:
        # 0.02 < tau* -> still withheld: the calibrated floor genuinely bites below the gate point.
        r = validate_grounding(
            self._result(confidence=0.02, citations=(_cite(),)), threshold=_TAU_STAR
        )
        assert r.asserted is False

    def test_zero_citations_is_not_asserted_even_above_tau(self) -> None:
        # Above tau* but ZERO citations -> citation-PRESENCE still bites despite the low floor.
        r = validate_grounding(self._result(confidence=0.11, citations=()), threshold=_TAU_STAR)
        assert r.asserted is False


# -------------------------------------------- (C) check_entailment at the lowered 0.55 threshold


class TestCheckEntailmentAtLoweredThreshold:
    def test_paraphrase_overlap_passes_at_055_but_fails_at_070(self) -> None:
        # Prose has 5 distinct content tokens {business, class, flights, pilots, require}; exactly 3
        # (business, class, flights) appear in the cited leaf -> _token_support = 3/5 = 0.6.  A
        # faithful paraphrase like this clears the lowered 0.55 floor but not the old 0.70 floor.
        cite = _cite(detail=_NLI_LEAF)
        cited = extract_cited_sentences(
            "Business class flights require pilots [T&E Policy §AIR-CABIN].", (cite,)
        )
        assert cited.sentences[0].citations != ()  # the marker verified against the citation
        assert check_entailment(cited, threshold=0.55).min_score == 0.6
        assert check_entailment(cited, threshold=0.55).entailed is True
        assert check_entailment(cited, threshold=0.70).entailed is False

    def test_drift_sentence_with_zero_overlap_still_blocks_at_055(self) -> None:
        # A drift sentence sharing no content token with its cited leaf scores 0.0 -> the lowered
        # 0.55 floor still catches a fully-unsupported claim (MIN aggregation retained).
        cite = _cite(detail=_NLI_LEAF)
        drift = extract_cited_sentences(
            "Managers approve reimbursements quarterly indeed [T&E Policy §AIR-CABIN].", (cite,)
        )
        r = check_entailment(drift, threshold=0.55)
        assert r.min_score == 0.0
        assert r.entailed is False


# ------------------------------------------------------------------- integration seam (D), (E), (F)

_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="t",
)

# Same shape as test_r5_nli_grounding's _GROUNDED, but the CALIBRATED trust surface: confidence=0.11
# on the Platt scale, gated at tau* (0.039299) instead of the cosine 0.92/0.20.
_GROUNDED_CALIBRATED = GroundedRetrieval(
    query="Is business class allowed?",
    status=GroundingStatus.grounded,
    clauses=(
        GroundedClause(
            clause_ref="AIR-CABIN", chunk_text=_LEAF, similarity=0.92, document_type="policy"
        ),
    ),
    citations=(_cite(),),
    confidence=0.11,
    reason=None,
    threshold=_TAU_STAR,
    confidence_source="calibrated",
    tau_star=_TAU_STAR,
)

# Test-F-only fixture: identical to _GROUNDED_CALIBRATED except retrieval.threshold is a DISTINCT
# sentinel (0.5) that differs from tau_star (0.039299) AND from the computed citation floor
# (resolve_citation_threshold("calibrated", 0.039299) == 0.039299).  This decouples the three
# values so the audit assertion can NUMERICALLY prove gate["threshold"] reads retrieval.threshold,
# not the floor.  The sentinel is deliberately artificial: on the real calibrated path
# retrieval.threshold == tau_star.
_GROUNDED_CALIBRATED_DISTINCT_THRESHOLD = GroundedRetrieval(
    query="Is business class allowed?",
    status=GroundingStatus.grounded,
    clauses=(
        GroundedClause(
            clause_ref="AIR-CABIN", chunk_text=_LEAF, similarity=0.92, document_type="policy"
        ),
    ),
    citations=(_cite(),),
    confidence=0.11,
    reason=None,
    threshold=0.5,
    confidence_source="calibrated",
    tau_star=_TAU_STAR,
)


def _run(
    answer: str, *, retrieval: GroundedRetrieval = _GROUNDED_CALIBRATED
) -> tuple[Any, list[DecisionRecord]]:
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
        patch(
            "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
            return_value=retrieval,
        ),
        patch("ai_agents.agents.policy_agent.write_decision_log", side_effect=_capture),
    ):
        result = agent.handle(task, _DECISION, context=ctx, session=MagicMock())
    return result, records


class TestCalibratedGateIntegration:
    def test_d_root_cause_fixed_low_calibrated_answer_now_asserts(self) -> None:
        # ROOT CAUSE: a 0.11 confidence answer that BOTH gates (per-sentence citation + entailment)
        # otherwise accept.  Under the OLD 0.70 floor this answer was withheld (0.11 < 0.70);
        # it now asserts because the floor is tau* (0.039299) on the calibrated path.
        result, records = _run(f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].")
        assert result.status is AgentResultStatus.ok
        assert result.output["answer_type"] == "grounded"
        assert result.output["asserted"] is True
        assert result.output["answer"] is not None
        assert len(records) == 1  # Fence 6 / Rule 11: exactly one write on the grounded path

    def test_e_safety_uncited_calibrated_answer_is_still_withheld(self) -> None:
        # SAFETY (ans-142 shape): a calibrated answer with NO citation marker.  citation-PRESENCE
        # catches it regardless of the low floor -> needs_review, answer withheld, one write.
        result, records = _run("Business class is always fine for everyone.")
        assert result.status is AgentResultStatus.needs_review
        assert result.output["answer_type"] != "grounded"
        assert result.output["answer_type"] == "needs_review"
        assert result.output["asserted"] is False
        assert result.output["answer"] is None
        assert len(records) == 1

    def test_f_audit_shape_unchanged_reads_retrieval_not_the_floor(self) -> None:
        # AUDIT: the gate block reflects retrieval.* (source/signal/threshold/tau_star), NOT the
        # computed citation floor -> R6-T2/R6-T3 trust surface is untouched by this fix.
        # This fixture sets retrieval.threshold to a DISTINCT sentinel (0.5) that differs from both
        # tau_star (0.039299) and the computed citation floor (0.039299), so the assertion below
        # NUMERICALLY proves gate["threshold"] reads retrieval.threshold (0.5), NOT the floor
        # (0.039299).  The sentinel is deliberately artificial: on the real calibrated path
        # retrieval.threshold == tau_star.
        _, records = _run(
            f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN].",
            retrieval=_GROUNDED_CALIBRATED_DISTINCT_THRESHOLD,
        )
        rec = records[0]
        tools_used = rec.tools_used
        assert tools_used is not None
        assert tools_used["GroundedRetriever"]["gate"] == {
            "source": "calibrated",
            "signal": 0.11,
            "threshold": 0.5,
            "tau_star": _TAU_STAR,
        }
        output_summary = rec.output_summary
        assert output_summary is not None
        assert output_summary["nli_result"] is not None
        nli = output_summary["nli_result"]
        assert isinstance(nli, dict)
        assert set(nli) == {"entailed", "min_score", "scores", "threshold"}
