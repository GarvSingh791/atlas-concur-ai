"""PolicyAgent — grounded Q&A (Cap 2) + rule-authoring assist (Cap 3) (P4-T8).

Registered as AiAgent.policy in the AgentRegistry.
Dispatches to Cap 2 (Q&A) or Cap 3 (authoring) based on decision.task_type.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.policy_authoring import (
    AuthoringFailure,
    RuleAuthoringCritic,
    RuleAuthoringHypothesizer,
    draft_rule_title,
    load_corpus_clause_refs,
    persist_draft_rule,
)
from ai_agents.agents.policy_qa import PolicyQaCritic, PolicyQaHypothesizer
from ai_agents.agents.policy_rule_semantics import describe_rule, sanitize_reason
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.guardrails.actions import AgentAction, check_action
from ai_agents.guardrails.citations import (
    DEFAULT_CITATION_THRESHOLD,
    Citation,
    resolve_citation_threshold,
)
from ai_agents.guardrails.reflection import ReflectionResult, reflect
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from ai_agents.rag.grounding import (
    CitedAnswer,
    EntailmentResult,
    check_entailment,
    section_citations,
)
from ai_agents.rag.retrieval import (
    GroundedRetrieval,
    GroundedRetriever,
    GroundingStatus,
    NearTopicSuggestion,
)
from ai_agents.rag.support import QuestionRelevance, is_refusal, question_relevance
from config.settings import get_settings
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmProvider

logger = logging.getLogger(__name__)

_TASK_AUTHORING = "policy_authoring"


def _retrieval_audit_payload(retrieval: GroundedRetrieval) -> dict[str, Any]:
    """R6-T3 (audit completeness): the structured IDENTITY + NUMERICS of the retrieval stage
    for the SINGLE decision-log write — the reranked candidate set (document_id + unit_id +
    fused_rank + rerank_score) and the answerability gate signal + its source.  NO chunk_text
    (identities/scores only — the row stays small + queryable); minimize() preserves UUIDs,
    clause refs, and numeric scores.  Recorded on BOTH the grounded and the abstain write."""
    audit = retrieval.retrieval_audit or ()
    return {
        "candidate_count": len(audit),
        "candidates": [
            {
                "document_id": ca.document_id,
                "unit_id": ca.unit_id,
                "fused_rank": ca.fused_rank,
                "rerank_score": ca.rerank_score,
            }
            for ca in audit
        ],
        # The gate signal that decided answer/abstain + WHICH signal it was.  ``source`` mirrors
        # the R6-T2 confidence_source: "cosine" on the local/CI path (tau_star None), "calibrated"
        # on the keyed Novita path (tau_star = 0.039299).  ``signal`` is None on the silent path.
        "gate": {
            "source": retrieval.confidence_source,
            "signal": retrieval.confidence,
            "threshold": retrieval.threshold,
            "tau_star": retrieval.tau_star,
        },
    }


def _silent_output(reason: str | None, near_topic: NearTopicSuggestion | None) -> dict[str, Any]:
    """The user-facing abstention shape (retrieval gate OR no-citable-answer classification)."""
    settings = get_settings()
    org = settings.ORGANIZATION_NAME or "your company's"
    near_topic_msg = (
        f"The documents don't cover that, but {near_topic.policy_name} has a section on "
        f"{near_topic.clause_ref} that might be related — would you like that instead?"
        if near_topic is not None
        else None
    )
    return {
        "answer_type": "silent",
        "answer": None,
        "confidence": None,
        "confidence_source": None,
        "rationale": f"This query is not covered by the {org} T&E documents.",
        "threshold": None,  # calibrated τ* — always None on the silent path
        "escalation": {
            "message": f"For guidance, contact {settings.ESCALATION_CONTACT_PRIMARY}.",
            "secondary": settings.ESCALATION_CONTACT_SECONDARY,
        },
        "near_topic": near_topic_msg,
        "reason": reason,
    }


# Reason codes recorded (internal audit only) for a non-grounded answer.
REASON_NO_CITATION = "no_verified_citation"  # draft cites no retrieved clause -> withheld
REASON_MODEL_DECLINED = "model_declined"  # draft says the clauses do not cover it -> abstain
REASON_EMPTY_DRAFT = "empty_draft"  # LLM produced nothing (degraded) -> needs_review
REASON_CRITIC_REJECTED = "critic_rejected"  # critic UNSUPPORTED/CONTRADICTED/degraded
REASON_NO_SUPPORTED_SENTENCE = "no_supported_sentence"  # every cited sentence failed support
REASON_DROPPED_CAVEAT = "dropped_caveat"  # a failing sentence qualifies the rest -> withhold
REASON_PARTIAL = "partial_provisional"  # some sentences shown, others dropped
REASON_BELOW_FLOOR = "below_confidence_floor"  # all sentences verified; confidence < floor
# All sentences verified, but below 0.70 the cited clauses do not cover the question (C7R).
REASON_QUESTION_NOT_COVERED = "question_not_covered"


@dataclass(frozen=True)
class _NeedsReviewDecision:
    """How a non-grounded draft is presented (decided once; recorded on the audit write)."""

    reason: str
    abstain: bool = False  # classify as silent (out-of-corpus) instead of needs_review
    shown: CitedAnswer | None = None  # the verified subset to show provisionally, if any
    shown_indices: tuple[int, ...] = ()


def _classify_needs_review(
    result: ReflectionResult, cited: CitedAnswer | None, nli: EntailmentResult | None
) -> _NeedsReviewDecision:
    """Decide abstain vs withheld vs provisional (sentence-level) for a non-grounded draft.

    Order matters and every branch is deterministic given the draft + critic verdict:

    1. Empty draft (provider degraded / nothing generated) -> needs_review, withheld.  NOT an
       abstention: "the documents don't cover that" would be false when the LLM is down.
    2. The draft DECLINES ("the provided policy does not cover ...") and no sentence is
       supported -> ABSTAIN (silent).  The model itself says the retrieved clauses do not
       answer the question: an out-of-corpus question that slipped past the retrieval gate,
       so it gets the same "not covered" shape as a retrieval abstention instead of "needs
       review".  Nothing from the draft (or an injected instruction) is shown.
    2b. No sentence carries a verified citation (and it is not a refusal) -> withheld,
       needs_review.  An uncited draft is NOT evidence the topic is out of corpus (the model
       may simply have ignored the citation rule on an in-corpus question), so it is not
       labelled "not covered"; nothing is shown either way.
    3. Critic did not say SUPPORTED -> withheld.  The lexical support check is necessary but
       not sufficient (it cannot judge e.g. "Chicago is Tier 1"); the critic is the only
       semantic verifier and its verdict is on the WHOLE draft, so we cannot tell which
       sentence it objected to — showing a "passing" subset of a draft the critic rejected
       would override the one semantic gate we have.  Kept all-or-nothing on purpose.
    4. Sentence-level filtering: keep cited sentences that pass the support check.  If a
       DROPPED sentence is a qualifier/caveat ("... does not apply to X", "only if ...") or
       states a requirement / condition ("you must get VP sign-off", "> 15% needs manager
       approval" — ``support.has_qualifier``) the whole draft is withheld — showing the rest
       without the caveat could invert its meaning.  Otherwise, show the verified subset if at
       least one SUBSTANTIVE sentence survives (a bare "Yes [§X]." is never shown alone).
    """
    if cited is None or not cited.sentences:
        return _NeedsReviewDecision(reason=REASON_EMPTY_DRAFT)
    verdicts = nli.sentences if nli is not None else ()
    if not any(v.supported and v.substantive for v in verdicts) and is_refusal(cited.full_text):
        return _NeedsReviewDecision(reason=REASON_MODEL_DECLINED, abstain=True)
    if not any(s.citations for s in cited.sentences):
        return _NeedsReviewDecision(reason=REASON_NO_CITATION)
    if not result.critic_supported:
        return _NeedsReviewDecision(reason=REASON_CRITIC_REJECTED)
    if len(verdicts) != len(cited.sentences):  # defensive: never show unverified text
        return _NeedsReviewDecision(reason=REASON_NO_SUPPORTED_SENTENCE)
    dropped = [v for v in verdicts if not v.supported]
    if any(v.qualifier for v in dropped):
        return _NeedsReviewDecision(reason=REASON_DROPPED_CAVEAT)
    kept = tuple(i for i, v in enumerate(verdicts) if v.supported)
    if not any(verdicts[i].substantive for i in kept):
        return _NeedsReviewDecision(reason=REASON_NO_SUPPORTED_SENTENCE)
    shown = CitedAnswer(sentences=tuple(cited.sentences[i] for i in kept))
    if not shown.full_text.strip():
        return _NeedsReviewDecision(reason=REASON_NO_SUPPORTED_SENTENCE)
    return _NeedsReviewDecision(
        reason=REASON_PARTIAL if dropped else REASON_BELOW_FLOOR,
        shown=shown,
        shown_indices=kept,
    )


class PolicyAgent:
    """Agent handling Cap 2 (grounded Q&A) and Cap 3 (rule authoring).

    Implements the Agent protocol (P4-T7 contracts.py).
    """

    agent_type: AiAgent = AiAgent.policy

    def __init__(self, provider: LlmProvider | None = None) -> None:
        self._provider: LlmProvider = provider or get_llm_provider()

    def handled_task_types(self) -> frozenset[str]:
        return frozenset()  # handles all task_types routed to it

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        if decision.task_type == _TASK_AUTHORING:
            return self._handle_authoring(task, decision, context=context, session=session)
        return self._handle_qa(task, decision, context=context, session=session)

    # ------------------------------------------------------------------ Cap 2

    def _handle_qa(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = dict(task.payload)
        question: str = str(payload.get("question", ""))

        # Thread the agent's provider so GroundedRetriever can run the R5-T1 HyDE branch when
        # enabled (default-off; never called on the local/CI path).
        retriever = GroundedRetriever(provider=self._provider)
        retrieval = retriever.retrieve(question, context=context, session=session)

        if retrieval.status == GroundingStatus.silent:
            latency_ms = int((time.monotonic() - t0) * 1000)
            record = DecisionRecord(
                agent=AiAgent.policy,
                task_type=decision.task_type,
                route=AiRoute.llm,
                model_provider=None,  # no LLM called on silent path
                input_summary={"question_len": len(question), "query": question},
                tools_used={
                    # R6-T3: the abstain write records the candidate set + gate signal that
                    # explain the abstention, using the same audit shape.  This is a separate,
                    # mutually-exclusive branch, so the single grounded write stays untouched.
                    "GroundedRetriever": {
                        "status": "silent",
                        **_retrieval_audit_payload(retrieval),
                    },
                },
                output_summary={
                    "answer_type": "silent",
                    "decision": "abstain",
                    "reason": retrieval.reason,
                    "citations": [],
                },
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            )
            write_decision_log(session, context=context, record=record)
            return AgentResult(
                status=AgentResultStatus.ok,
                output=_silent_output(retrieval.reason, retrieval.near_topic_suggestion),
                citations=(),
                confidence=None,
                reflection_outcome=None,
            )

        # Grounded path — LLM synthesis
        hyp = PolicyQaHypothesizer(
            provider=self._provider,
            clauses=retrieval.clauses,
            question=question,
            # The draft may cite a retrieved clause OR a section sibling it was shown in the
            # parent context (verified against that unit's own text by the support gate).
            citations=section_citations(retrieval.clauses, retrieval.citations),
            confidence=retrieval.confidence or 0.5,
        )
        critic = PolicyQaCritic(provider=self._provider, clauses=retrieval.clauses)
        # R6 gate-calibration: the citation-contract confidence floor must gate on the SAME
        # operating point the answerability gate used -- tau* on the calibrated path.  A single
        # 0.70 floor is unsatisfiable on the calibrated scale (Platt prob, tau*~0.039) -> 100%
        # false-block.  C7R: on the LOCAL reranker's cosine path the floor is the calibrated
        # LOCAL_COSINE_CITATION_THRESHOLD, and an answer below the historical 0.70 must ALSO
        # cover the question (below); any other cosine path (incl. a calibrated provider that
        # fell back to cosine) keeps 0.70.  Scale-aware via confidence_source (mirrors the
        # R6-T2 trust-surface split).  Local variable only: never written to output (that reads
        # retrieval.*), so R6-T2/R6-T3 are untouched.
        settings = get_settings()
        citation_threshold = resolve_citation_threshold(
            retrieval.confidence_source,
            retrieval.tau_star,
            rerank_provider=settings.RERANK_PROVIDER,
        )
        result = reflect(hypothesizer=hyp, critic=critic, threshold=citation_threshold)

        # R5-T2 STRICT (per-sentence): the reflected value is a CitedAnswer.  A partially-cited
        # answer — ANY sentence without a verified [Label §ref] citation — is NOT presented as a
        # clean grounded answer even if the citation contract (>=1 citation) passed.
        cited = result.value if isinstance(result.value, CitedAnswer) else None
        strict_grounded = result.asserted and (cited is None or not cited.needs_review)
        # R5-T3 support gate (strengthened C7-06/C7-10 residual, ai_agents.rag.support): each
        # cited sentence is checked against the clause text it cites — content-concept recall +
        # hard numeric / polarity / contrast guards.  Deterministic, zero LLM call.  The SAME
        # check decides "grounded" (every sentence must pass) and which sentences a provisional
        # answer may show, so the two paths can never disagree about a sentence.
        nli = (
            check_entailment(
                cited,
                threshold=settings.NLI_ENTAILMENT_THRESHOLD,
                question=question,
                retrieved=retrieval.citations,
                # Calibrated path: no relevance gate runs there, so a cited yes/no verdict
                # ("Yes [§AIR-01]. Economy is the standard ...") can never be grounded.
                strict_verdicts=retrieval.confidence_source == "calibrated",
            )
            if cited is not None
            else None
        )
        fully_grounded = strict_grounded and (nli is None or nli.entailed)
        # C7R question-relevance gate: the support check verifies each sentence against the
        # clause it CITES, never that the answer is about what was ASKED.  Below the historical
        # 0.70 floor (only reachable on the local cosine path) a verified answer is grounded only
        # when the answer AND its cited clauses cover every subject concept of the question,
        # the answer repeats every figure the question names, and no sentence opens with a
        # yes/no verdict (nothing verifies a "Yes," against the question's premise: "business
        # class on a 2-hour flight?" -> "Yes, ... for flights over 10 hours").  Otherwise it
        # stays a provisional needs_review answer ("What life insurance ...?" answered with
        # business TRAVEL insurance [§INTL-04] is true, cited and entailed — and not an
        # answer).  The calibrated path never reaches this (confidence_source check).
        relevance: QuestionRelevance | None = None
        if (
            fully_grounded
            and cited is not None
            and retrieval.confidence_source == "cosine"
            and result.grounded.confidence < DEFAULT_CITATION_THRESHOLD
        ):
            relevance = question_relevance(
                question,
                cited.full_text,
                (c.detail for c in cited.distinct_citations() if c.detail),
            )
            fully_grounded = relevance.relevant
        decision_info: _NeedsReviewDecision | None
        if not fully_grounded and relevance is not None and cited is not None:
            # Every sentence passed support and the critic: shown provisionally, never grounded.
            decision_info = _NeedsReviewDecision(
                reason=REASON_QUESTION_NOT_COVERED,
                shown=cited,
                shown_indices=tuple(range(len(cited.sentences))),
            )
        else:
            decision_info = (
                _classify_needs_review(result, cited, nli) if not fully_grounded else None
            )

        model_provider = getattr(self._provider, "model", None) or type(self._provider).__name__
        latency_ms = int((time.monotonic() - t0) * 1000)
        abstain = decision_info is not None and decision_info.abstain
        provisional = decision_info is not None and decision_info.shown is not None
        answer_type = "grounded" if fully_grounded else ("silent" if abstain else "needs_review")
        output_summary: dict[str, Any] = {
            "answer_type": answer_type,
            "asserted": fully_grounded,
            "decision": "abstain" if abstain else "answer",
            # C7-06: whether a critic-supported, cited draft was surfaced provisionally.
            "provisional_answer": provisional,
            # R5-T3: the entailment verdict rides the SINGLE existing write (Fence 6 / Rule 11).
            "nli_result": nli.to_dict() if nli is not None else None,
            # C7R: the answer-vs-question relevance verdict (only evaluated below the 0.70
            # floor on the local cosine path; None when it did not apply).  Internal only.
            "question_relevance": relevance.to_dict() if relevance is not None else None,
            # C7 observability: per-sentence support verdicts (score, failed gates, missing
            # numbers, unsupported terms) — INTERNAL record only, never in the user payload.
            "sentence_support": ([v.to_dict() for v in nli.sentences] if nli is not None else None),
            # Why a non-grounded answer was withheld / shown partially (gate that decided it).
            "withheld_reason": decision_info.reason if decision_info is not None else None,
            "shown_sentences": (
                list(decision_info.shown_indices)
                if decision_info is not None and decision_info.shown is not None
                else None
            ),
            "critic_supported": result.critic_supported,
            "reflection_rationale": result.critique,
            # R6-T3: the document-qualified citations backing the answer (snippet detail for
            # the CITED clauses ONLY — kind/ref/detail/source/document_type).
            "citations": [c.to_dict() for c in result.citations],
        }
        record = DecisionRecord(
            agent=AiAgent.policy,
            task_type=decision.task_type,
            route=AiRoute.llm,
            model_provider=model_provider,
            input_summary={"question_len": len(question), "query": question},
            tools_used={
                # R6-T3: enrich the SINGLE grounded write with the retrieval audit trail — the
                # reranked candidate set (document_id + unit_id + fused_rank + rerank_score) and
                # the answerability gate signal + its source.  Still exactly ONE grounded write.
                "GroundedRetriever": {
                    "clause_count": len(retrieval.clauses),
                    **_retrieval_audit_payload(retrieval),
                },
                "LlmProvider": {"model": model_provider},
            },
            output_summary=output_summary,
            confidence=result.grounded.confidence,
            reflection_outcome=result.outcome,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        if abstain:
            # Out-of-corpus classification (C7R): the model could not ground ANY statement in a
            # retrieved clause (no verified citation, or it declined) — the documents do not
            # answer this question.  Same user-facing shape as the retrieval abstention; the
            # draft (and any injected text) is never shown.
            assert decision_info is not None
            return AgentResult(
                status=AgentResultStatus.ok,
                output=_silent_output(decision_info.reason, None),
                citations=(),
                confidence=None,
                reflection_outcome=None,
            )

        # ``output`` is JSON-serialized on the streaming path (json.dumps), so emit the plain
        # answer STRING for "answer" and the plain-dict breakdown for "answer_structured" —
        # never a CitedAnswer dataclass.
        answer_text: str | None = None
        answer_structured: dict[str, Any] | None = cited.to_dict() if cited is not None else None
        citations_out = result.citations
        if fully_grounded:
            answer_text = cited.full_text if cited is not None else str(result.value)
        elif decision_info is not None and decision_info.shown is not None:
            # Provisional: ONLY the verified sentences, and ONLY the citations backing them, so
            # no source chip is shown for a dropped claim and answer_structured cannot leak it.
            answer_text = decision_info.shown.full_text
            answer_structured = decision_info.shown.to_dict()
            citations_out = decision_info.shown.distinct_citations()
        status = AgentResultStatus.ok if fully_grounded else AgentResultStatus.needs_review
        output: dict[str, Any] = {
            "answer_type": answer_type,
            "answer": answer_text,
            "answer_structured": answer_structured,
            "asserted": fully_grounded,
            "critique": result.critique,
            "confidence": result.grounded.confidence,
            "confidence_source": retrieval.confidence_source,
            # R6 (τ* display anchor): the calibrated τ* the abstention gate used (None on
            # the cosine/local path) — surfaced on the trust seam next to confidence_source.
            "threshold": retrieval.tau_star,
        }
        if not fully_grounded:
            # C7-06 contract: needs_review answers carry an explicit flag (the grounded shape
            # is unchanged).
            output["needs_review"] = True
        return AgentResult(
            status=status,
            output=output,
            citations=citations_out,
            confidence=result.grounded.confidence,
            reflection_outcome=result.outcome,
        )

    # ------------------------------------------------------------------ Cap 3

    def _handle_authoring(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = dict(task.payload)
        intent: str = str(payload.get("intent", ""))
        policy_id_str: str | None = payload.get("policy_id")
        policy_id = uuid.UUID(policy_id_str) if policy_id_str else uuid.UUID(int=0)

        # RBAC gate — finance/admin only
        try:
            check_action(context, AgentAction.POLICY_MANAGE)
        except GuardrailBlocked as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            record = DecisionRecord(
                agent=AiAgent.policy,
                task_type=decision.task_type,
                route=AiRoute.llm,
                model_provider=None,
                input_summary={"intent_len": len(intent)},
                tools_used={},
                output_summary={"denied": True, "reason": str(exc)},
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            )
            write_decision_log(session, context=context, record=record)
            return AgentResult(
                status=AgentResultStatus.needs_review,
                output={
                    "denied": True,
                    "draft_created": False,
                    "failure_kind": "denied",
                    "reason": str(exc),
                },
            )

        # C7R-04: the organization's corpus clause ids, loaded ONCE (fail closed:
        # empty on any lookup error). The critic cites a clause only when the
        # model's ref is one of them, and the persisted rule keeps the same check.
        known_clause_refs = load_corpus_clause_refs(session, policy_id=policy_id)

        def _attempt(
            repair_hint: str | None,
        ) -> tuple[ReflectionResult, AuthoringFailure | None]:
            hyp = RuleAuthoringHypothesizer(
                provider=self._provider,
                intent=intent,
                policy_id=policy_id,
                repair_hint=repair_hint,
            )
            critic = RuleAuthoringCritic(
                provider=self._provider, intent=intent, known_clause_refs=known_clause_refs
            )
            attempt = reflect(hypothesizer=hyp, critic=critic)
            return attempt, _authoring_failure(attempt, hyp, critic)

        # Issue 5: ONE repair round for a deterministic (validation / scope) failure,
        # re-prompted with the reason.  A judge's UNFAITHFUL verdict, the model's own
        # "unsupported" reply and an unreachable provider are never retried.
        result, failure = _attempt(None)
        repair_attempted = False
        if failure is not None and failure.repairable:
            repair_attempted = True
            result, failure = _attempt(failure.reason)

        model_provider = getattr(self._provider, "model", None) or type(self._provider).__name__
        latency_ms = int((time.monotonic() - t0) * 1000)

        if failure is not None:
            failed_output: dict[str, Any] = {
                "draft_created": False,
                "failure_kind": failure.kind,
                "reason": sanitize_reason(failure.reason),
                "repair_attempted": repair_attempted,
            }
            record = DecisionRecord(
                agent=AiAgent.policy,
                task_type=decision.task_type,
                route=AiRoute.llm,
                model_provider=model_provider,
                input_summary={"intent_len": len(intent)},
                tools_used={
                    "LlmProvider": {"model": model_provider},
                    "PolicyEvalTool": {"dry_run": True},
                },
                output_summary=dict(failed_output),
                confidence=result.grounded.confidence,
                reflection_outcome=result.outcome,
                latency_ms=latency_ms,
            )
            write_decision_log(session, context=context, record=record)
            return AgentResult(
                status=AgentResultStatus.needs_review,
                output=failed_output,
                citations=result.citations,
                confidence=result.grounded.confidence,
                reflection_outcome=result.outcome,
            )

        # Persist the validated draft rule
        candidate: dict[str, Any] = result.value  # type: ignore[assignment]
        draft_rule = persist_draft_rule(
            candidate=candidate,
            policy_id=policy_id,
            created_by=context.user_id,
            session=session,
            known_clause_refs=known_clause_refs,
        )
        title = draft_rule_title(draft_rule.rule_type, draft_rule.applies_to, draft_rule.definition)
        summary = list(
            describe_rule(draft_rule.rule_type, draft_rule.applies_to, draft_rule.definition)
        )

        record = DecisionRecord(
            agent=AiAgent.policy,
            task_type=decision.task_type,
            route=AiRoute.llm,
            model_provider=model_provider,
            input_summary={"intent_len": len(intent)},
            tools_used={
                "LlmProvider": {"model": model_provider},
                "PolicyEvalTool": {"dry_run": True},
            },
            output_summary={
                "draft_created": True,
                "rule_id": str(draft_rule.id),
                "rule_type": candidate.get("rule_type"),
                "status": "draft",
                "repair_attempted": repair_attempted,
            },
            confidence=result.grounded.confidence,
            reflection_outcome=result.outcome,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        return AgentResult(
            status=AgentResultStatus.ok,
            output={
                "draft_created": True,
                "rule_id": str(draft_rule.id),
                "rule_type": candidate.get("rule_type"),
                "applies_to": candidate.get("applies_to"),
                "title": title,
                "summary": summary,
                "source_clause_ref": draft_rule.source_clause_ref,
                "status": "draft",
                "repair_attempted": repair_attempted,
            },
            citations=result.citations,
            confidence=result.grounded.confidence,
            reflection_outcome=result.outcome,
        )


def _authoring_failure(
    result: ReflectionResult,
    hyp: RuleAuthoringHypothesizer,
    critic: RuleAuthoringCritic,
) -> AuthoringFailure | None:
    """Classify a finished reflection (Issue 5), AFTER reflect() returns.

    The critic runs inside reflect() and cannot see why the hypothesizer produced no
    candidate, so a provider outage during GENERATION is classified here from the
    hypothesizer's ``last_error`` — ahead of the critic's (then "invalid") verdict —
    and is never repaired.
    """
    if result.outcome != ReflectionOutcome.rejected and result.asserted:
        return None
    if hyp.last_error == "provider":
        return AuthoringFailure(
            kind="unavailable",
            reason="The language model provider could not be reached.",
            repairable=False,
        )
    if critic.last_failure is not None:
        return critic.last_failure
    return AuthoringFailure(
        kind="invalid",
        reason=result.critique or "The draft could not be validated.",
        repairable=False,
    )


def presentable_policy_output(
    output: Mapping[str, Any], citations: tuple[Citation, ...] | None
) -> tuple[dict[str, Any], tuple[Citation, ...]]:
    """The user-facing view of a PolicyAgent result (C7-06 / C7-09 presentation boundary).

    A ``needs_review`` result whose answer was WITHHELD (``answer`` is None: critic rejection,
    uncited / unentailed draft) must not leak the withheld draft through a side channel: its
    ``answer_structured`` (which carries ``full_text``) and the critic's ``critique`` (its
    rationale about the draft's claims) are nulled and its citations are emptied,
    so the API response, the SSE result frame and the persisted assistant message never show
    sources next to "no confident answer".  The agent's own result and the decision log keep
    the full record for audit.  Any other output (grounded, provisional, silent, non-policy
    agents) is returned unchanged.
    """
    out = dict(output)
    cits = tuple(citations or ())
    answer = out.get("answer")
    withheld = out.get("answer_type") == "needs_review" and not (
        isinstance(answer, str) and answer.strip()
    )
    if withheld:
        if "answer_structured" in out:
            out["answer_structured"] = None
        # The critique is the critic LLM's rationale ABOUT the withheld draft's
        # claims ("Last critique: ...") — another side channel for it.
        if "critique" in out:
            out["critique"] = None
        cits = ()
    return out, cits
