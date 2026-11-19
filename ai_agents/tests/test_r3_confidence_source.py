"""Hermetic tests for the R3-T3 confidence-source basis in GroundedRetriever.

No DB, no network.  Proves the grounded result reports confidence from the SAME
basis the gate used: the calibrated Platt probability when the τ* calibrated gate
decided grounding (confidence_source == "calibrated"), and the cosine rescale when
the gate fell back to the cosine path (confidence_source == "cosine").  Keying off
the GATE predicate (tau_star present) — NOT bare calibrated_score presence — is
asserted explicitly (V3).  Also covers citation doc-qualification (V4): a grounded
citation's source is non-None and falls back to "Policy Document".

Mirrors ai_agents/tests/test_rag_retrieval_calibrated_gate.py for the fake
retriever / RetrievedChunk / RetrievalResult / AgentContext construction.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai_agents.context import AgentContext
from ai_agents.rag import retrieval as retrieval_mod
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.policy_retrieval import (
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)


def _chunk(
    *,
    similarity: float,
    calibrated_score: float | None,
    policy_name: str | None = "TEP-FY26",
    from_sparse: bool = False,
) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref="AIR-01",
        chunk_text="Economy class for short flights.",
        similarity=similarity,
        policy_name=policy_name,  # type: ignore[arg-type]
        from_sparse=from_sparse,
        rerank_score=0.5,
        calibrated_score=calibrated_score,
    )


class _FakeRetriever:
    def __init__(self, chunks: tuple[RetrievedChunk, ...], max_dense: float | None) -> None:
        self._chunks = chunks
        self._max_dense = max_dense

    def retrieve(self, intent: RetrievalIntent, *, context: Any, session: Any) -> RetrievalResult:
        return RetrievalResult(
            query=intent.query, chunks=self._chunks, max_dense_similarity=self._max_dense
        )


_CTX = AgentContext(organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None)


# ---- V3: HF / calibrated path -------------------------------------------------------
def test_calibrated_path_confidence_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # τ*=0.04 present AND top calibrated 0.42 > τ* AND high cosine -> GROUNDED via the
    # calibrated gate; confidence == the calibrated probability, source == "calibrated".
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.04)
    chunk = _chunk(similarity=0.90, calibrated_score=0.42)
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.90)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.grounded
    assert out.confidence == 0.42
    assert out.confidence_source == "calibrated"


# ---- V3: Local / cosine path (keys off the GATE, not calibrated presence) ------------
def test_cosine_path_confidence_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # τ* absent (None) -> calibrated gate inactive -> cosine fallback.  calibrated_score=0.42
    # IS present, proving the source keys off the GATE predicate (tau_star is None), NOT the
    # bare presence of a calibrated score.  High cosine passes the cosine gate -> GROUNDED.
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: None)
    chunk = _chunk(similarity=0.90, calibrated_score=0.42)
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.90), threshold=0.20).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.grounded
    assert out.confidence_source == "cosine"
    assert out.confidence is not None
    assert 0.5 <= out.confidence <= 1.0


# ---- V4: citation doc-qualification --------------------------------------------------
def test_citation_source_qualifies_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.04)
    chunk = _chunk(similarity=0.90, calibrated_score=0.42, policy_name="TEP-FY26")
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.90)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.grounded
    cit = out.citations[0]
    assert cit.source is not None
    assert cit.source == "TEP-FY26"
    # A "§{ref} ({source})"-style display string is constructible from non-None fields.
    display = f"§{cit.ref} ({cit.source})"
    assert display == "§AIR-01 (TEP-FY26)"


def test_citation_source_falls_back_to_policy_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.04)
    for empty in ("", None):
        chunk = _chunk(similarity=0.90, calibrated_score=0.42, policy_name=empty)
        out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.90)).retrieve(
            "q", context=_CTX, session=None
        )
        assert out.status is GroundingStatus.grounded
        assert out.citations[0].source == "Policy Document"
