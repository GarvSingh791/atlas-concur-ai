"""Hermetic tests for the R3 near-topic suggestion on a SILENT (abstaining) result.

No DB, no network, no embed().  These tests force the SILENT below-threshold branch of
GroundedRetriever.retrieve() and assert on the additive NearTopicSuggestion:

  - Tau* is forced absent (load_abstention_threshold -> None) so the COSINE gate applies;
    a chunk below RETRIEVAL_SIMILARITY_THRESHOLD therefore abstains.
  - The near-miss suggestion is built ONLY from a retrieved chunk (never fabricated) and
    ONLY when max_dense_similarity > 0.10 (the soft relevance floor).

Mirrors the _FakeRetriever fake-retriever pattern from test_rag_retrieval.py /
test_rag_retrieval_calibrated_gate.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai_agents.context import AgentContext
from ai_agents.rag import retrieval as retrieval_mod
from ai_agents.rag.retrieval import (
    GroundedRetriever,
    GroundingStatus,
    NearTopicSuggestion,
)
from ai_agents.tools.policy_retrieval import (
    RetrievalIntent,
    RetrievalResult,
    RetrievedChunk,
)


class _FakeRetriever:
    """Returns a prebuilt RetrievalResult verbatim with a pinned max_dense_similarity."""

    def __init__(self, chunks: tuple[RetrievedChunk, ...], max_dense: float | None) -> None:
        self._chunks = chunks
        self._max_dense = max_dense

    def retrieve(self, intent: RetrievalIntent, *, context: Any, session: Any) -> RetrievalResult:
        return RetrievalResult(
            query=intent.query, chunks=self._chunks, max_dense_similarity=self._max_dense
        )


def _chunk(
    *,
    similarity: float,
    chunk_text: str = "Some near-miss policy text about cabin class.",
    policy_name: str = "TEP-FY26",
    clause_ref: str | None = "AIR-01",
) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref=clause_ref,
        chunk_text=chunk_text,
        similarity=similarity,
        policy_name=policy_name,
        calibrated_score=None,  # force cosine fallback alongside tau_star=None
    )


_CTX = AgentContext(
    organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), correlation_id="hermetic"
)


def _force_cosine_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """tau* absent -> the cosine gate (>= RETRIEVAL_SIMILARITY_THRESHOLD, default 0.20) applies."""
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: None)


def test_near_topic_populated_above_soft_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # max_dense 0.15 (> 0.10 soft floor) but below the 0.20 cosine gate -> SILENT with a
    # near-miss suggestion mirrored from the top retrieved chunk.
    _force_cosine_gate(monkeypatch)
    chunk = _chunk(
        similarity=0.15,
        chunk_text="Economy class is required for short-haul flights under three hours.",
        policy_name="TEP-FY26",
        clause_ref="AIR-01",
    )
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.15)).retrieve(
        "q", context=_CTX, session=None
    )

    assert out.status is GroundingStatus.silent
    assert isinstance(out.near_topic_suggestion, NearTopicSuggestion)
    sug = out.near_topic_suggestion
    assert sug.chunk_text == chunk.chunk_text[:200]
    assert len(sug.chunk_text) <= 200
    assert sug.policy_name == "TEP-FY26"
    assert sug.clause_ref == "AIR-01"


def test_near_topic_chunk_text_sliced_to_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_cosine_gate(monkeypatch)
    long_text = "X" * 500
    chunk = _chunk(similarity=0.15, chunk_text=long_text)
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.15)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.silent
    assert out.near_topic_suggestion is not None
    assert len(out.near_topic_suggestion.chunk_text) == 200
    assert out.near_topic_suggestion.chunk_text == long_text[:200]


def test_near_topic_none_at_or_below_soft_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # max_dense 0.05 (<= 0.10 soft floor) -> SILENT but NO near-miss suggestion.
    _force_cosine_gate(monkeypatch)
    chunk = _chunk(similarity=0.05)
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.05)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.silent
    assert out.near_topic_suggestion is None


def test_near_topic_none_when_no_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty RetrievalResult -> the no_chunks silent branch never builds a suggestion.
    _force_cosine_gate(monkeypatch)
    out = GroundedRetriever(_FakeRetriever((), max_dense=None)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.silent
    assert out.near_topic_suggestion is None


def test_near_topic_policy_name_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty policy_name on the chunk falls back to the generic "Policy Document" title.
    _force_cosine_gate(monkeypatch)
    chunk = _chunk(similarity=0.15, policy_name="")
    out = GroundedRetriever(_FakeRetriever((chunk,), max_dense=0.15)).retrieve(
        "q", context=_CTX, session=None
    )
    assert out.status is GroundingStatus.silent
    assert out.near_topic_suggestion is not None
    assert out.near_topic_suggestion.policy_name == "Policy Document"
