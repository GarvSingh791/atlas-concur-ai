"""Hermetic tests for GroundedRetriever (P4-T6) — no DB, no network, no embed().

All threshold/silent/grounded branching, citation assembly, confidence mapping,
and boundary conditions are exercised here via a _FakeRetriever that returns
hand-built RetrievedChunks with controlled similarity scores.

These tests MUST NOT call the real PolicyRetrievalTool, LocalEmbeddingsProvider,
or any database session — all dependencies are injected fakes.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import CitationKind
from ai_agents.rag.retrieval import (
    GroundedRetriever,
    GroundingStatus,
)
from ai_agents.tools.base import ToolError
from ai_agents.tools.policy_retrieval import (
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeRetriever:
    """Returns a fixed set of chunks ordered by similarity DESC, honoring top_k."""

    def __init__(self, chunks: tuple[RetrievedChunk, ...]) -> None:
        self._chunks = chunks
        # Track the last intent passed in so tests can assert on it.
        self.last_intent: RetrievalIntent | None = None

    def retrieve(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Any,
    ) -> RetrievalResult:
        self.last_intent = intent
        ordered = tuple(sorted(self._chunks, key=lambda c: c.similarity, reverse=True))
        sliced = ordered[: max(1, intent.top_k)] if ordered else ()
        return RetrievalResult(query=intent.query, chunks=sliced)


class _EmptyRetriever:
    """Always returns zero chunks."""

    def retrieve(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Any,
    ) -> RetrievalResult:
        return RetrievalResult(query=intent.query, chunks=())


class _RaisingRetriever:
    """Raises ToolError on empty query (mimics PolicyRetrievalTool's guard)."""

    def retrieve(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Any,
    ) -> RetrievalResult:
        if not intent.query.strip():
            raise ToolError("Retrieval query must not be empty.", code="POLICY_RETRIEVAL_TOOL")
        return RetrievalResult(query=intent.query, chunks=())


def _chunk(
    clause_ref: str | None,
    similarity: float,
    chunk_text: str = "Some policy text.",
    policy_name: str = "Atlas Policy",
) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref=clause_ref,
        chunk_text=chunk_text,
        similarity=similarity,
        policy_name=policy_name,
    )


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=uuid.uuid4(),
        granted_permission_keys=frozenset(),
        correlation_id="hermetic-test",
    )


def _retriever(
    retriever: Any,
    threshold: float = 0.5,
    top_k: int = 5,
) -> GroundedRetriever:
    return GroundedRetriever(retriever=retriever, threshold=threshold, top_k=top_k)


def _session() -> Any:
    """Sentinel session for hermetic tests — the fake retriever never touches it."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Grounded branch
# ---------------------------------------------------------------------------


class TestGroundedBranch:
    def test_above_threshold_is_grounded(self) -> None:
        fake = _FakeRetriever((_chunk("AIR-CABIN", 0.8),))
        result = _retriever(fake, threshold=0.5).retrieve(
            "some query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.grounded
        assert len(result.clauses) == 1
        assert result.clauses[0].clause_ref == "AIR-CABIN"
        assert len(result.citations) == 1
        assert result.confidence is not None

    def test_multiple_on_policy_clauses_all_grounded(self) -> None:
        chunks = (
            _chunk("AIR-CABIN", 0.9),
            _chunk("HOTEL-CAP", 0.7),
            _chunk("MEALS-PD", 0.55),
        )
        result = _retriever(_FakeRetriever(chunks), threshold=0.5).retrieve(
            "query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.grounded
        assert len(result.clauses) == 3
        refs = [c.clause_ref for c in result.clauses]
        assert refs == ["AIR-CABIN", "HOTEL-CAP", "MEALS-PD"]  # DESC order preserved

    def test_subthreshold_chunks_dropped_from_grounded(self) -> None:
        chunks = (
            _chunk("AIR-CABIN", 0.8),
            _chunk("HOTEL-CAP", 0.6),
            _chunk("MEALS-PD", 0.2),  # below threshold 0.5
        )
        result = _retriever(_FakeRetriever(chunks), threshold=0.5).retrieve(
            "query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.grounded
        assert len(result.clauses) == 2
        refs = [c.clause_ref for c in result.clauses]
        assert "MEALS-PD" not in refs

    def test_grounded_confidence_from_top_similarity_exact_at_threshold(self) -> None:
        # top == threshold → confidence == 0.5
        result = _retriever(_FakeRetriever((_chunk("X", 0.5),)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.confidence is not None
        assert abs(result.confidence - 0.5) < 1e-9

    def test_grounded_confidence_from_top_similarity_at_one(self) -> None:
        # top == 1.0 → confidence == 1.0
        result = _retriever(_FakeRetriever((_chunk("X", 1.0),)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.confidence == 1.0

    def test_grounded_confidence_midpoint(self) -> None:
        # top=0.75, threshold=0.5: raw = 0.5 + 0.5*(0.75-0.5)/(1.0-0.5) = 0.75
        result = _retriever(_FakeRetriever((_chunk("X", 0.75),)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.confidence is not None
        assert abs(result.confidence - 0.75) < 1e-9

    def test_grounded_confidence_in_unit_range_and_floor_half(self) -> None:
        for sim in [0.5, 0.55, 0.7, 0.9, 1.0]:
            result = _retriever(_FakeRetriever((_chunk("X", sim),)), threshold=0.5).retrieve(
                "q", context=_ctx(), session=_session()
            )
            assert result.confidence is not None
            assert 0.5 <= result.confidence <= 1.0


# ---------------------------------------------------------------------------
# Silent branch
# ---------------------------------------------------------------------------


class TestSilentBranch:
    def test_below_threshold_is_silent(self) -> None:
        fake = _FakeRetriever((_chunk("AIR-CABIN", 0.2),))
        result = _retriever(fake, threshold=0.5).retrieve(
            "query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.silent
        assert result.clauses == ()
        assert result.citations == ()
        assert result.confidence is None
        assert result.reason is not None
        assert result.reason.startswith("below_threshold")

    def test_zero_chunks_is_silent(self) -> None:
        result = _retriever(_EmptyRetriever(), threshold=0.5).retrieve(
            "query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.silent
        assert result.clauses == ()
        assert result.citations == ()
        assert result.confidence is None
        assert result.reason is not None
        assert result.reason.startswith("no_chunks")

    def test_silent_emits_no_guessed_clause(self) -> None:
        # Multiple sub-threshold chunks exist — none should appear in the result.
        chunks = (
            _chunk("AIR-CABIN", 0.3),
            _chunk("HOTEL-CAP", 0.1),
        )
        result = _retriever(_FakeRetriever(chunks), threshold=0.5).retrieve(
            "query", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.silent
        assert len(result.clauses) == 0
        assert len(result.citations) == 0

    def test_silent_confidence_is_none_not_zero(self) -> None:
        result = _retriever(_EmptyRetriever()).retrieve("query", context=_ctx(), session=_session())
        assert result.confidence is None


# ---------------------------------------------------------------------------
# Boundary / determinism
# ---------------------------------------------------------------------------


class TestBoundaryDeterminism:
    def test_exact_threshold_passes(self) -> None:
        # sim == threshold (>=) → grounded
        result = _retriever(_FakeRetriever((_chunk("X", 0.5),)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.status == GroundingStatus.grounded

    def test_just_below_threshold_silent(self) -> None:
        # sim < threshold → silent
        result = _retriever(_FakeRetriever((_chunk("X", 0.5 - 1e-9),)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.status == GroundingStatus.silent

    def test_determinism_repeated_calls_identical(self) -> None:
        fake = _FakeRetriever((_chunk("AIR-CABIN", 0.8), _chunk("HOTEL-CAP", 0.6)))
        gr = _retriever(fake, threshold=0.5)
        ctx = _ctx()
        sess = _session()
        r1 = gr.retrieve("query", context=ctx, session=sess)
        r2 = gr.retrieve("query", context=ctx, session=sess)
        assert r1.to_dict() == r2.to_dict()


# ---------------------------------------------------------------------------
# Citation integrity
# ---------------------------------------------------------------------------


class TestCitationIntegrity:
    def test_citation_fields_mapped(self) -> None:
        chunk = _chunk("HOTEL-CAP", 0.8, chunk_text="Hotel rate cap text.", policy_name="Atlas")
        result = _retriever(_FakeRetriever((chunk,)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )

        assert len(result.citations) == 1
        cit = result.citations[0]
        assert cit.kind == CitationKind.clause_ref
        assert cit.ref == "HOTEL-CAP"
        assert cit.detail == "Hotel rate cap text."
        assert cit.source == "Atlas"

    def test_sustain_rule_less_clause_grounds(self) -> None:
        # SUSTAIN has no policy_rule; it still grounds normally via citation.
        chunk = _chunk("SUSTAIN", 0.75, chunk_text="Sustainability guidance.", policy_name="Atlas")
        result = _retriever(_FakeRetriever((chunk,)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.grounded
        refs = [c.ref for c in result.citations]
        assert "SUSTAIN" in refs

    def test_clause_ref_none_yields_empty_ref_no_crash(self) -> None:
        # Unlabelled chunk: clause_ref=None → Citation.ref=""
        chunk = _chunk(None, 0.8, chunk_text="Unlabelled text.")
        result = _retriever(_FakeRetriever((chunk,)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )

        assert result.status == GroundingStatus.grounded
        assert result.citations[0].ref == ""

    def test_to_dict_shape(self) -> None:
        chunk = _chunk("RECEIPT-REQ", 0.9, chunk_text="Receipt text.", policy_name="Atlas")
        result = _retriever(_FakeRetriever((chunk,)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        d = result.to_dict()

        # R3-T3: additive DTO fields (confidence_source, near_topic_suggestion) join the shape.
        # R6-T2: tau_star (calibrated abstention threshold; None on cosine/local) joins additively.
        expected_keys = {
            "status",
            "confidence",
            "reason",
            "threshold",
            "clauses",
            "citations",
            "confidence_source",
            "near_topic_suggestion",
            "tau_star",
        }
        assert set(d.keys()) == expected_keys
        assert d["status"] == "grounded"
        assert d["reason"] is None
        assert isinstance(d["threshold"], float)
        assert len(d["clauses"]) == 1  # type: ignore[arg-type]
        assert d["clauses"][0]["clause_ref"] == "RECEIPT-REQ"  # type: ignore[index]
        assert len(d["citations"]) == 1  # type: ignore[arg-type]
        assert d["citations"][0]["kind"] == "clause_ref"  # type: ignore[index]
        assert d["citations"][0]["ref"] == "RECEIPT-REQ"  # type: ignore[index]

    def test_to_dict_silent_shape(self) -> None:
        result = _retriever(_EmptyRetriever(), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        d = result.to_dict()
        assert d["status"] == "silent"
        assert d["clauses"] == []
        assert d["citations"] == []
        assert d["confidence"] is None
        assert d["reason"] is not None


# ---------------------------------------------------------------------------
# Configuration seam
# ---------------------------------------------------------------------------


class TestConfigSeam:
    def test_explicit_threshold_overrides_settings(self) -> None:
        chunk = _chunk("X", 0.4)
        # With default settings threshold=0.20 → 0.4 would be grounded
        # but we inject threshold=0.5 → should be silent
        result = _retriever(_FakeRetriever((chunk,)), threshold=0.5).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert result.status == GroundingStatus.silent

    def test_top_k_propagated_to_intent(self) -> None:
        fake = _FakeRetriever(())
        gr = GroundedRetriever(retriever=fake, threshold=0.5, top_k=3)
        gr.retrieve("query", context=_ctx(), session=_session())
        assert fake.last_intent is not None
        assert fake.last_intent.top_k == 3

    def test_threshold_recorded_in_result(self) -> None:
        result = _retriever(_EmptyRetriever(), threshold=0.42).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert abs(result.threshold - 0.42) < 1e-9

    def test_empty_query_raises_tool_error(self) -> None:
        gr = GroundedRetriever(retriever=_RaisingRetriever(), threshold=0.5, top_k=5)
        with pytest.raises(ToolError, match="empty"):
            gr.retrieve("   ", context=_ctx(), session=_session())


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    def test_settings_expose_retrieval_threshold(self) -> None:
        from config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        assert hasattr(settings, "RETRIEVAL_SIMILARITY_THRESHOLD")
        assert isinstance(settings.RETRIEVAL_SIMILARITY_THRESHOLD, float)
        assert 0.0 <= settings.RETRIEVAL_SIMILARITY_THRESHOLD <= 1.0

    def test_settings_expose_retrieval_top_k(self) -> None:
        from config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        assert hasattr(settings, "RETRIEVAL_TOP_K")
        assert isinstance(settings.RETRIEVAL_TOP_K, int)
        assert settings.RETRIEVAL_TOP_K > 0


# ---------------------------------------------------------------------------
# R1-T4 hybrid-fusion wiring (CRUX A): the gate reads the dense-cosine signal,
# sparse hits survive grounding, parent context grounds while citations stay leaf
# ---------------------------------------------------------------------------


class _FixedRetriever:
    """Returns a prebuilt RetrievalResult verbatim (no re-sorting) — lets a test pin the
    RRF-fused order AND ``max_dense_similarity`` independently."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    def retrieve(
        self, intent: RetrievalIntent, *, context: AgentContext, session: Any
    ) -> RetrievalResult:
        return self._result


class TestHybridFusionWiring:
    def test_gate_reads_max_dense_similarity_not_top_chunk(self) -> None:
        # Top chunk has a high cosine, but the real signal (max_dense_similarity) is below the
        # gate → SILENT.  Proves RRF order / the top fused chunk's cosine cannot force grounding.
        result = RetrievalResult(query="q", chunks=(_chunk("X", 0.9),), max_dense_similarity=0.10)
        out = GroundedRetriever(retriever=_FixedRetriever(result), threshold=0.20).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert out.status == GroundingStatus.silent
        assert out.reason is not None and out.reason.startswith("below_threshold")

    def test_high_max_dense_grounds_even_when_top_fused_chunk_low_cosine(self) -> None:
        # Top fused chunk is a sparse hit (cosine 0.0); max_dense_similarity is high → grounded,
        # and the sparse hit is KEPT (never cosine-culled).
        sparse_hit = RetrievedChunk(
            embedding_id=str(uuid.uuid4()),
            clause_ref="SPARSE-1",
            chunk_text="lexical text",
            similarity=0.0,
            policy_name="Atlas",
            from_sparse=True,
        )
        result = RetrievalResult(query="q", chunks=(sparse_hit,), max_dense_similarity=0.8)
        out = GroundedRetriever(retriever=_FixedRetriever(result), threshold=0.20).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert out.status == GroundingStatus.grounded
        assert [c.clause_ref for c in out.clauses] == ["SPARSE-1"]

    def test_dense_subthreshold_nonsparse_chunk_still_dropped(self) -> None:
        # A dense-only chunk below the gate (from_sparse=False) is still dropped from grounding.
        strong = _chunk("KEEP", 0.8)
        weak = _chunk("DROP", 0.10)  # from_sparse defaults False
        result = RetrievalResult(query="q", chunks=(strong, weak), max_dense_similarity=0.8)
        out = GroundedRetriever(retriever=_FixedRetriever(result), threshold=0.20).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert [c.clause_ref for c in out.clauses] == ["KEEP"]

    def test_parent_context_grounds_clause_citation_stays_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaf = _chunk("LOD-01", 0.8, chunk_text="LEAF clause text.", policy_name="Atlas")
        result = RetrievalResult(query="q", chunks=(leaf,), max_dense_similarity=0.8)
        monkeypatch.setattr(
            "ai_agents.rag.retrieval.resolve_parent_context_for_embedding",
            lambda session, eid: "ENCLOSING SECTION TEXT",
        )
        out = GroundedRetriever(retriever=_FixedRetriever(result), threshold=0.20).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert out.clauses[0].chunk_text == "ENCLOSING SECTION TEXT"  # grounds on the parent
        assert out.citations[0].detail == "LEAF clause text."  # citation detail = leaf unit
        assert out.citations[0].ref == "LOD-01"  # citation ref = leaf unit id

    def test_parent_context_resolver_failure_degrades_to_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaf = _chunk("LOD-01", 0.8, chunk_text="LEAF text.")
        result = RetrievalResult(query="q", chunks=(leaf,), max_dense_similarity=0.8)

        def _boom(session: Any, eid: Any) -> str:
            raise ValueError("bad id")

        monkeypatch.setattr("ai_agents.rag.retrieval.resolve_parent_context_for_embedding", _boom)
        out = GroundedRetriever(retriever=_FixedRetriever(result), threshold=0.20).retrieve(
            "q", context=_ctx(), session=_session()
        )
        assert out.status == GroundingStatus.grounded
        assert out.clauses[0].chunk_text == "LEAF text."  # degraded to leaf, no crash
