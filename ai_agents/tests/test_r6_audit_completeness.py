"""R6-T3 hermetic tests: decision-log AUDIT COMPLETENESS + single-write fence (no DB, no LLM).

Proves that the SINGLE decision-log write on each QA branch is enriched with every RAG stage —
query, candidate set ({document_id, unit_id, fused_rank, rerank_score}), the answerability gate
signal + its source, the answer/abstain decision, the document-qualified citations, and a
populated latency_ms — and that the fence holds on BOTH mutually-exclusive branches:

  * grounded invocation -> EXACTLY ONE write, decision == "answer"
  * abstain  invocation -> EXACTLY ONE write, decision == "abstain"
  * NO invocation produces both writes (the branches are mutually exclusive)

Also proves minimize() preserves the audit IDs/scores while still redacting secrets.

Harness mirrors test_r5_nli_grounding (scripted provider + patched GroundedRetriever.retrieve +
captured write_decision_log record).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.decision_log.minimizer import minimize
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from ai_agents.tools.policy_retrieval import CandidateAudit
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

_LEAF = "Business class is only allowed on flights over 10 hours."
_EXTRACTIVE_ANSWER = f"{_LEAF[:-1]} [T&E Policy §AIR-CABIN]."
_DOC_ID_A = "550e8400-e29b-41d4-a716-446655440000"
_DOC_ID_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="t",
)


# --------------------------------------------------------------------------- fake providers


class _ScriptedProvider(LlmProvider):
    """Returns scripted responses in order (hypothesizer answer, then critic verdict)."""

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


class _RaisingProvider(LlmProvider):
    """Raises if ever called — proves the LLM is not touched on the abstain path."""

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on the abstain path")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called on the abstain path")


# --------------------------------------------------------------------------- fixtures

_AUDIT = (
    CandidateAudit(document_id=_DOC_ID_A, unit_id="AIR-CABIN", fused_rank=1, rerank_score=0.8123),
    CandidateAudit(document_id=_DOC_ID_B, unit_id="AIR-02", fused_rank=3, rerank_score=0.4106),
)


def _cite(ref: str = "AIR-CABIN", detail: str = _LEAF) -> Citation:
    return Citation(
        kind=CitationKind.clause_ref,
        ref=ref,
        detail=detail,
        source="Atlas T&E Policy",
        document_type="policy",
    )


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
    confidence_source="cosine",
    tau_star=None,
    retrieval_audit=_AUDIT,
)

_ABSTAIN = GroundedRetrieval(
    query="What is the policy on pet travel?",
    status=GroundingStatus.silent,
    clauses=(),
    citations=(),
    confidence=None,
    reason="below_threshold: top similarity 0.12 < threshold 0.20",
    threshold=0.20,
    confidence_source=None,
    tau_star=None,
    retrieval_audit=_AUDIT,  # candidates existed but the gate abstained — explains WHY
)

_ABSTAIN_NO_CHUNKS = GroundedRetrieval(
    query="totally unrelated question",
    status=GroundingStatus.silent,
    clauses=(),
    citations=(),
    confidence=None,
    reason="no_chunks: org has no indexed policy embeddings",
    threshold=0.20,
    retrieval_audit=(),
)


def _run(retrieval: GroundedRetrieval, provider: LlmProvider) -> list[DecisionRecord]:
    agent = PolicyAgent(provider=provider)
    task = Task(context_type="policy_qa", payload={"question": "business class?"})
    ctx = AgentContext(
        organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=uuid.uuid4()
    )
    records: list[DecisionRecord] = []

    def _capture(session: object, *, context: object, record: DecisionRecord) -> None:
        records.append(record)

    with (
        patch("ai_agents.agents.policy_agent.GroundedRetriever.retrieve", return_value=retrieval),
        patch("ai_agents.agents.policy_agent.write_decision_log", side_effect=_capture),
    ):
        agent.handle(task, _DECISION, context=ctx, session=MagicMock())
    return records


def _run_grounded() -> list[DecisionRecord]:
    return _run(_GROUNDED, _ScriptedProvider(_EXTRACTIVE_ANSWER, "SUPPORTED\nok."))


def _run_abstain(retrieval: GroundedRetrieval = _ABSTAIN) -> list[DecisionRecord]:
    return _run(retrieval, _RaisingProvider())


# --------------------------------------------------------------------- grounded write completeness


class TestGroundedWriteCompleteness:
    def test_single_grounded_write_decision_answer(self) -> None:
        recs = _run_grounded()
        assert len(recs) == 1  # Fence 6 / Rule 11: exactly one write on the grounded path
        assert recs[0].output_summary["decision"] == "answer"

    def test_row_covers_every_stage(self) -> None:
        """One row -> every RAG stage: query, candidate set, gate, decision, citations, latency."""
        rec = _run_grounded()[0]
        # query
        assert rec.input_summary["query"] == "business class?"
        assert rec.input_summary["question_len"] == len("business class?")
        gr = rec.tools_used["GroundedRetriever"]
        # candidate set — each with document_id + unit_id + fused_rank + rerank_score
        assert gr["candidate_count"] == 2
        cand = gr["candidates"][0]
        assert cand["document_id"] == _DOC_ID_A
        assert cand["unit_id"] == "AIR-CABIN"
        assert cand["fused_rank"] == 1
        assert cand["rerank_score"] == 0.8123
        assert set(cand) == {"document_id", "unit_id", "fused_rank", "rerank_score"}
        # gate signal + its source (cosine on the local path; calibrated on the keyed path)
        assert gr["gate"] == {
            "source": "cosine",
            "signal": 0.92,
            "threshold": 0.20,
            "tau_star": None,
        }
        # decision + document-qualified citations (snippet detail for the CITED clauses only)
        assert rec.output_summary["decision"] == "answer"
        cits = rec.output_summary["citations"]
        assert isinstance(cits, list) and len(cits) >= 1
        assert cits[0]["ref"] == "AIR-CABIN"
        assert cits[0]["source"] == "Atlas T&E Policy"
        assert cits[0]["document_type"] == "policy"
        assert cits[0]["detail"]  # the cited-clause snippet
        # latency populated
        assert isinstance(rec.latency_ms, int) and rec.latency_ms >= 0

    def test_which_documents_the_answer_drew_from(self) -> None:
        # The audit makes the source document(s) explicit: candidate document_ids + cited doc type.
        rec = _run_grounded()[0]
        doc_ids = {c["document_id"] for c in rec.tools_used["GroundedRetriever"]["candidates"]}
        assert doc_ids == {_DOC_ID_A, _DOC_ID_B}
        assert rec.output_summary["citations"][0]["document_type"] == "policy"


# ---------------------------------------------------------------------- abstain write completeness


class TestAbstainWriteCompleteness:
    def test_single_abstain_write_decision_abstain(self) -> None:
        recs = _run_abstain()
        assert len(recs) == 1
        assert recs[0].output_summary["decision"] == "abstain"

    def test_abstain_records_candidate_set_and_gate(self) -> None:
        # The candidate set + gate signal EXPLAIN the abstention.
        rec = _run_abstain()[0]
        assert rec.input_summary["query"] == "business class?"
        gr = rec.tools_used["GroundedRetriever"]
        assert gr["status"] == "silent"
        assert gr["candidate_count"] == 2
        assert gr["candidates"][0]["document_id"] == _DOC_ID_A
        assert gr["gate"]["threshold"] == 0.20
        assert rec.output_summary["answer_type"] == "silent"
        assert rec.output_summary["citations"] == []
        assert isinstance(rec.latency_ms, int) and rec.latency_ms >= 0

    def test_abstain_no_chunks_candidate_count_zero(self) -> None:
        rec = _run_abstain(_ABSTAIN_NO_CHUNKS)[0]
        assert rec.output_summary["decision"] == "abstain"
        assert rec.tools_used["GroundedRetriever"]["candidate_count"] == 0
        assert rec.tools_used["GroundedRetriever"]["candidates"] == []


# ------------------------------------------------------------- single-write fence: mutual exclusion


class TestSingleWriteFenceMutualExclusion:
    def test_grounded_invocation_writes_exactly_one_answer_never_abstain(self) -> None:
        recs = _run_grounded()
        assert len(recs) == 1
        assert [r.output_summary.get("decision") for r in recs] == ["answer"]

    def test_abstain_invocation_writes_exactly_one_abstain_never_answer(self) -> None:
        recs = _run_abstain()
        assert len(recs) == 1
        assert [r.output_summary.get("decision") for r in recs] == ["abstain"]

    def test_decisions_are_disjoint_across_branches(self) -> None:
        # No single invocation produces both writes; the two branches are mutually exclusive.
        grounded = {r.output_summary["decision"] for r in _run_grounded()}
        abstain = {r.output_summary["decision"] for r in _run_abstain()}
        assert grounded == {"answer"}
        assert abstain == {"abstain"}
        assert grounded.isdisjoint(abstain)


# --------------------------------------------------------------- minimize() preserves audit payload


class TestMinimizePreservesAuditPayload:
    def test_minimize_preserves_ids_and_scores(self) -> None:
        rec = _run_grounded()[0]
        gr = rec.tools_used["GroundedRetriever"]
        out = minimize(rec.tools_used)["GroundedRetriever"]
        assert out["candidates"] == gr["candidates"]  # document_id/unit_id/fused_rank/rerank_score
        assert out["gate"] == gr["gate"]  # numeric signal/threshold + source
        assert out["candidate_count"] == gr["candidate_count"]
        # The query text is preserved (it carries no PII in this case).
        assert minimize(rec.input_summary)["query"] == "business class?"

    def test_minimize_redacts_secrets_but_keeps_audit_fields(self) -> None:
        payload = {
            "api_key": "sk-secret-123",
            "note": "email me at ap@helios.com or Bearer abc.def.ghijk",
            "card": "4111 1111 1111 1111",
            "document_id": _DOC_ID_A,
            "rerank_score": 0.8123,
            "fused_rank": 1,
        }
        out = minimize(payload)
        assert out["api_key"] == "[REDACTED]"  # sensitive KEY redacted
        assert "ap@helios.com" not in out["note"]  # email value masked
        assert "Bearer abc.def.ghijk" not in out["note"]  # bearer token masked
        assert out["card"] == "[REDACTED]"  # 16-digit card value masked
        # Legitimate audit identity + numerics pass through untouched.
        assert out["document_id"] == _DOC_ID_A
        assert out["rerank_score"] == 0.8123
        assert out["fused_rank"] == 1
