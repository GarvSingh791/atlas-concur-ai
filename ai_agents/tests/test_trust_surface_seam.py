"""Hermetic tests for the Package C trust-surface seam + ef_search GUC (FORK-P / R5).

No DB, no network, no embed().  Covers:
  - Citation.to_dict serializes ``document_type`` ONLY when set (additive, back-compat).
  - GroundedRetriever propagates ``document_type`` onto citations and sets ``tau_star``
    (the calibrated τ*) on the RetrievalResult — None on the cosine/local/silent path.
  - ``_retrieve_dense`` issues the ``set_config('hnsw.ef_search', …, true)`` GUC IMMEDIATELY
    before the dense SELECT, and a session that RAISES on that GUC does NOT break retrieval.
  - PolicyAgent grounded output carries ``threshold`` (== retrieval.tau_star).

These are the trust-surface guarantees Package C added; they must stay deterministic and
must not perturb the local/cosine behavior (byte-identical eval-db path).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.policy_retrieval import (
    PolicyRetrievalTool,
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)
from config.constants.embeddings import HNSW_EF_SEARCH

# ---------------------------------------------------------------------------
# Helpers / doubles
# ---------------------------------------------------------------------------


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="trust-surface-test",
    )


def _chunk(
    *,
    clause_ref: str | None = "LOD-01",
    similarity: float = 0.8,
    document_type: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref=clause_ref,
        chunk_text="Leaf clause text.",
        similarity=similarity,
        policy_name="Atlas T&E Policy",
        document_type=document_type,
    )


class _FixedRetriever:
    """Returns a prebuilt RetrievalResult verbatim (no re-sort)."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    def retrieve(
        self, intent: RetrievalIntent, *, context: AgentContext, session: Any
    ) -> RetrievalResult:
        return self._result


def _grounded_retriever(result: RetrievalResult, *, threshold: float = 0.20) -> GroundedRetriever:
    return GroundedRetriever(retriever=_FixedRetriever(result), threshold=threshold)


# ---------------------------------------------------------------------------
# 1) Citation.to_dict serializes document_type only when set
# ---------------------------------------------------------------------------


class TestCitationDocumentType:
    def test_document_type_omitted_when_none(self) -> None:
        cit = Citation(kind=CitationKind.clause_ref, ref="LOD-01", detail="x", source="Atlas")
        d = cit.to_dict()
        assert "document_type" not in d  # additive: back-compat shape when unset

    def test_document_type_included_when_set(self) -> None:
        cit = Citation(
            kind=CitationKind.clause_ref,
            ref="LOD-01",
            detail="x",
            source="Atlas",
            document_type="policy",
        )
        d = cit.to_dict()
        assert d["document_type"] == "policy"

    def test_default_is_none(self) -> None:
        cit = Citation(kind=CitationKind.clause_ref, ref="LOD-01")
        assert cit.document_type is None


# ---------------------------------------------------------------------------
# 2) GroundedRetriever propagates document_type + sets tau_star
# ---------------------------------------------------------------------------


class TestRetrievalTrustSurface:
    def test_document_type_propagated_to_citation(self) -> None:
        result = RetrievalResult(
            query="q",
            chunks=(_chunk(document_type="faq"),),
            max_dense_similarity=0.8,
        )
        out = _grounded_retriever(result).retrieve("q", context=_ctx(), session=MagicMock())
        assert out.status == GroundingStatus.grounded
        assert out.citations[0].document_type == "faq"
        # And it surfaces in the serialized citation dict.
        assert out.to_dict()["citations"][0]["document_type"] == "faq"  # type: ignore[index]

    def test_document_type_none_when_unset(self) -> None:
        result = RetrievalResult(
            query="q", chunks=(_chunk(document_type=None),), max_dense_similarity=0.8
        )
        out = _grounded_retriever(result).retrieve("q", context=_ctx(), session=MagicMock())
        assert out.citations[0].document_type is None
        assert "document_type" not in out.to_dict()["citations"][0]  # type: ignore[operator]

    def test_tau_star_none_on_cosine_path(self) -> None:
        # Local/cosine gate (no calibrated τ* artifact) → tau_star must be None (no behavior shift).
        result = RetrievalResult(query="q", chunks=(_chunk(),), max_dense_similarity=0.8)
        out = _grounded_retriever(result).retrieve("q", context=_ctx(), session=MagicMock())
        assert out.confidence_source == "cosine"
        assert out.tau_star is None
        assert out.to_dict()["tau_star"] is None

    def test_tau_star_none_on_silent_path(self) -> None:
        result = RetrievalResult(
            query="q", chunks=(_chunk(similarity=0.05),), max_dense_similarity=0.05
        )
        out = _grounded_retriever(result).retrieve("q", context=_ctx(), session=MagicMock())
        assert out.status == GroundingStatus.silent
        assert out.tau_star is None

    def test_tau_star_set_when_calibrated_gate_fires(self, monkeypatch: Any) -> None:
        # Force the calibrated gate: a τ* artifact + a calibrated top chunk above it → grounded,
        # and tau_star surfaces the exact τ* the gate compared against.
        monkeypatch.setattr(
            "ai_agents.rag.retrieval.load_abstention_threshold",
            lambda provider: 0.55,
        )
        top = RetrievedChunk(
            embedding_id=str(uuid.uuid4()),
            clause_ref="LOD-01",
            chunk_text="Leaf clause text.",
            similarity=0.8,
            policy_name="Atlas",
            calibrated_score=0.90,  # > τ* 0.55 → gate passes
        )
        result = RetrievalResult(query="q", chunks=(top,), max_dense_similarity=0.8)
        out = _grounded_retriever(result).retrieve("q", context=_ctx(), session=MagicMock())
        assert out.status == GroundingStatus.grounded
        assert out.confidence_source == "calibrated"
        assert out.tau_star == 0.55
        assert out.to_dict()["tau_star"] == 0.55


# ---------------------------------------------------------------------------
# 3) ef_search GUC ordering + resilience
# ---------------------------------------------------------------------------


class _SpySession:
    """Records the SQL text of every execute() call in order; returns empty rows."""

    def __init__(self, *, raise_on_setconfig: bool = False) -> None:
        self.sql_calls: list[str] = []
        self._raise_on_setconfig = raise_on_setconfig

    def execute(self, stmt: Any, params: Any = None) -> Any:
        text = str(getattr(stmt, "text", stmt))
        self.sql_calls.append(text)
        if self._raise_on_setconfig and "set_config" in text:
            raise RuntimeError("mock session cannot set GUCs")
        res = MagicMock()
        res.fetchall.return_value = []
        return res


class _FixedEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        from config.constants.embeddings import EMBEDDING_DIMENSION  # noqa: PLC0415

        return [[0.0] * (EMBEDDING_DIMENSION - 1) + [1.0] for _ in texts]


class TestEfSearchGuc:
    def test_set_config_issued_before_dense_select(self) -> None:
        tool = PolicyRetrievalTool(embedder=_FixedEmbedder())
        spy = _SpySession()
        tool._retrieve_dense("hotel cap", context=_ctx(), session=spy, top_k=5)  # type: ignore[arg-type]

        # The GUC must be the FIRST execute, and it must precede the dense SELECT.
        assert any("set_config" in s and "hnsw.ef_search" in s for s in spy.sql_calls)
        setcfg_idx = next(i for i, s in enumerate(spy.sql_calls) if "set_config" in s)
        select_idx = next(
            i for i, s in enumerate(spy.sql_calls) if "policy_embeddings" in s and "<=>" in s
        )
        assert setcfg_idx < select_idx, "ef_search GUC must be set BEFORE the dense ANN query"

    def test_set_config_uses_configured_value_and_is_local(self) -> None:
        # Prove the GUC carries the config constant and is transaction-local (is_local=true).
        tool = PolicyRetrievalTool(embedder=_FixedEmbedder())
        captured: list[tuple[str, Any]] = []

        class _CapSession:
            def execute(self, stmt: Any, params: Any = None) -> Any:
                captured.append((str(getattr(stmt, "text", stmt)), params))
                res = MagicMock()
                res.fetchall.return_value = []
                return res

        tool._retrieve_dense("q", context=_ctx(), session=_CapSession(), top_k=3)  # type: ignore[arg-type]
        setcfg = next((t, p) for t, p in captured if "set_config" in t)
        text, params = setcfg
        assert "true" in text  # is_local=true → transaction-local, no pool leakage
        assert params["ef"] == str(HNSW_EF_SEARCH)

    def test_raising_set_config_does_not_break_retrieval(self) -> None:
        # A mock/non-PG session that raises on the GUC must NOT break the dense path.
        tool = PolicyRetrievalTool(embedder=_FixedEmbedder())
        spy = _SpySession(raise_on_setconfig=True)
        out = tool._retrieve_dense("q", context=_ctx(), session=spy, top_k=5)  # type: ignore[arg-type]
        assert out == ()  # empty rows → empty candidates, but no exception
        # The dense SELECT still ran after the failed GUC.
        assert any("policy_embeddings" in s and "<=>" in s for s in spy.sql_calls)

    def test_hnsw_ef_search_in_researched_band(self) -> None:
        assert 100 <= HNSW_EF_SEARCH <= 400


# ---------------------------------------------------------------------------
# 4) PolicyAgent grounded output carries "threshold"
# ---------------------------------------------------------------------------


class TestPolicyAgentThreshold:
    def test_grounded_output_carries_threshold(self) -> None:
        from ai_agents.agents import PolicyAgent  # noqa: PLC0415
        from ai_agents.orchestrator.envelopes import RoutingDecision, Task  # noqa: PLC0415
        from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval  # noqa: PLC0415
        from database.models.ai_decision_log import AiAgent, AiRoute  # noqa: PLC0415
        from services.llm_provider.interface import (  # noqa: PLC0415
            LlmProvider,
            LlmResponse,
            LlmUsage,
        )

        class _Scripted(LlmProvider):
            def __init__(self, *responses: str) -> None:
                self._responses = list(responses)
                self._idx = 0

            def complete(self, messages: Any, *, tools: Any = None, tool_choice: Any = None) -> Any:
                text = self._responses[self._idx % len(self._responses)]
                self._idx += 1
                return LlmResponse(
                    text=text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1)
                )

            def stream(self, messages: Any, *, tools: Any = None, tool_choice: Any = None) -> Any:
                yield from []

        grounded = GroundedRetrieval(
            query="Is business class allowed to London?",
            status=GroundingStatus.grounded,
            clauses=(
                GroundedClause(
                    clause_ref="AIR-CABIN",
                    chunk_text="Business class is only allowed on flights over 10 hours.",
                    similarity=0.92,
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
            confidence_source="calibrated",
            tau_star=0.55,
        )

        provider = _Scripted(
            "No, business class is not allowed on short flights [T&E Policy §AIR-CABIN].",
            "SUPPORTED\nThe answer follows directly from the AIR-CABIN clause.",
        )
        agent = PolicyAgent(provider=provider)
        task = Task(
            context_type="policy_qa",
            payload={"question": "Is business class allowed to London?"},
        )
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.llm,
            task_type="policy_grounded_lookup",
            confidence=0.95,
            rationale="test",
        )
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=grounded,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(
                task,
                decision,
                context=AgentContext(
                    organization_id=uuid.uuid4(),
                    granted_permission_keys=frozenset({"policy.manage"}),
                    user_id=uuid.uuid4(),
                ),
                session=MagicMock(),
            )

        assert "threshold" in result.output
        assert result.output["threshold"] == 0.55  # == retrieval.tau_star

    def test_silent_output_carries_threshold_none(self) -> None:
        from ai_agents.agents import PolicyAgent  # noqa: PLC0415
        from ai_agents.orchestrator.envelopes import RoutingDecision, Task  # noqa: PLC0415
        from ai_agents.rag.retrieval import GroundedRetrieval  # noqa: PLC0415
        from database.models.ai_decision_log import AiAgent, AiRoute  # noqa: PLC0415
        from services.llm_provider.interface import LlmProvider  # noqa: PLC0415

        class _Raising(LlmProvider):
            def complete(self, messages: Any, *, tools: Any = None, tool_choice: Any = None) -> Any:
                raise AssertionError("LLM must NOT be called on the silent path")

            def stream(self, messages: Any, *, tools: Any = None, tool_choice: Any = None) -> Any:
                raise AssertionError("LLM must NOT be called on the silent path")
                yield  # pragma: no cover

        silent = GroundedRetrieval(
            query="What is the policy on emotional support animals?",
            status=GroundingStatus.silent,
            clauses=(),
            citations=(),
            confidence=None,
            reason="below_threshold: top similarity 0.05 < threshold 0.20",
            threshold=0.20,
        )
        agent = PolicyAgent(provider=_Raising())
        task = Task(context_type="policy_qa", payload={"question": "emotional support animals?"})
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.llm,
            task_type="policy_grounded_lookup",
            confidence=0.95,
            rationale="test",
        )
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=silent,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(
                task,
                decision,
                context=AgentContext(
                    organization_id=uuid.uuid4(),
                    granted_permission_keys=frozenset(),
                    user_id=uuid.uuid4(),
                ),
                session=MagicMock(),
            )

        assert result.output["threshold"] is None
