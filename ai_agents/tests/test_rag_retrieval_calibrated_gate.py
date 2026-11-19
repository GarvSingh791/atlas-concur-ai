"""Hermetic tests for the R3-T2 calibrated abstention gate in GroundedRetriever.

No DB, no network.  Proves the gate uses the TOP chunk's calibrated_score vs τ* as the
PRIMARY answer/abstain decision, and that it FAILS SAFE to the cosine gate when the
calibrated score is unavailable (I_R3_3, V4) or the τ* artifact is absent (V5).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from ai_agents.context import AgentContext
from ai_agents.rag import retrieval as retrieval_mod
from ai_agents.rag import threshold
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.policy_retrieval import (
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)


def _chunk(
    *, similarity: float, calibrated_score: float | None, from_sparse: bool = False
) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref="AIR-01",
        chunk_text="Economy class for short flights.",
        similarity=similarity,
        policy_name="TEP-FY26",
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


# ---- Primary calibrated gate (artifact present, calibrated_score present) -------------
def test_calibrated_gate_grounds_above_tau_despite_low_cosine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # τ*=0.5; top calibrated 0.6 > 0.5 -> GROUNDED even though cosine 0.05 < 0.20.
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.5)
    chunk = _chunk(similarity=0.05, calibrated_score=0.6, from_sparse=True)
    gr = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.05))
    out = gr.retrieve("q", context=_CTX, session=None)
    assert out.status is GroundingStatus.grounded


def test_calibrated_gate_abstains_below_tau_despite_high_cosine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # τ*=0.5; top calibrated 0.4 <= 0.5 -> SILENT even though cosine 0.90 >= 0.20.
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.5)
    chunk = _chunk(similarity=0.90, calibrated_score=0.4)
    gr = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.90))
    out = gr.retrieve("q", context=_CTX, session=None)
    assert out.status is GroundingStatus.silent
    assert "calibrated" in (out.reason or "")


# ---- V4: calibrated_score None -> cosine fallback (no crash) --------------------------
def test_fallback_when_calibrated_none_uses_cosine_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: 0.5)
    # calibrated None -> fall back to cosine: 0.30 >= 0.20 grounds; 0.10 < 0.20 abstains.
    grounded = GroundedRetriever(
        _FakeRetriever((_chunk(similarity=0.30, calibrated_score=None),), max_dense=0.30)
    ).retrieve("q", context=_CTX, session=None)
    silent = GroundedRetriever(
        _FakeRetriever((_chunk(similarity=0.10, calibrated_score=None),), max_dense=0.10)
    ).retrieve("q", context=_CTX, session=None)
    assert grounded.status is GroundingStatus.grounded
    assert silent.status is GroundingStatus.silent
    assert "similarity" in (silent.reason or "")


# ---- V5: τ* artifact absent -> WARNING + cosine fallback (never raises) ---------------
def test_fallback_when_artifact_absent_warns_and_uses_cosine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Point the loader at a nonexistent artifact; high calibrated_score must NOT save a
    # low-cosine chunk because the gate has fallen back to cosine.
    monkeypatch.setattr(threshold, "ABSTENTION_PATH", tmp_path / "missing.json")
    threshold.load_abstention_threshold.cache_clear()
    chunk = _chunk(similarity=0.05, calibrated_score=0.99)
    gr = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.05))
    with caplog.at_level(logging.WARNING):
        out = gr.retrieve("q", context=_CTX, session=None)
    assert out.status is GroundingStatus.silent  # cosine 0.05 < 0.20
    assert any("abstention" in r.message.lower() for r in caplog.records)
    threshold.load_abstention_threshold.cache_clear()
