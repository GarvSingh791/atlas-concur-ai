"""Behavioural tests for the two-stage rerank pipeline (R2-T2, ai_agents/rag/rerank.py).

Hermetic — no DB, no network.  Uses the deterministic LocalRerankProvider plus tiny fake
providers to prove: the rerank improves a buried gold chunk (V4), exactly one batched call
capped at candidate_cap (V7), and the fail-SAFE fallback to RRF order on every failure mode
(V6 — ProviderError, timeout, malformed response).  Also: subset reranks drop no candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from ai_agents.rag.rerank import RerankContractError, apply_rerank
from services.exceptions import ProviderError
from services.rerank_provider.interface import RerankProvider
from services.rerank_provider.local import LocalRerankProvider


@dataclass(frozen=True)
class _Cand:
    """Minimal stand-in for a fused RetrievalCandidate (only chunk_text is read here)."""

    embedding_id: str
    chunk_text: str


def _text_of(c: _Cand) -> str:
    return c.chunk_text


def _rank_of(items: list[_Cand], embedding_id: str) -> int:
    return next(i for i, c in enumerate(items) if c.embedding_id == embedding_id)


# ---- V4: rerank improves a buried gold chunk, deterministically ------------


def test_rerank_lifts_buried_gold_and_is_deterministic() -> None:
    """Gold chunk is NOT first in RRF order but surfaces higher after the local reranker."""
    query = "What is the nightly hotel cap in New York? 250 USD"
    candidates = [
        _Cand("c0", "Employees may book economy class flights for domestic travel."),  # RRF #1
        _Cand("gold", "The nightly hotel cap in New York is 250 USD per night for lodging."),
        _Cand("c2", "Meal per diem allowances vary by destination city tier."),
        _Cand("c3", "Receipts are required for any expense above 25 USD."),
    ]
    rrf_gold_rank = _rank_of(candidates, "gold")
    assert rrf_gold_rank > 0  # buried below the RRF #1 chunk

    out = apply_rerank(
        query,
        candidates,
        provider=LocalRerankProvider(),
        top_k=4,
        candidate_cap=50,
        text_of=_text_of,
    )
    assert out.applied is True
    assert [c.embedding_id for c in out.items] != [c.embedding_id for c in candidates]  # reordered
    assert _rank_of(out.items, "gold") < rrf_gold_rank  # gold rank improved
    assert out.items[0].embedding_id == "gold"
    # rerank scores recorded (observability) for the reranked head
    assert out.scores[0] is not None

    # determinism: identical inputs -> identical ordering
    out2 = apply_rerank(
        query,
        candidates,
        provider=LocalRerankProvider(),
        top_k=4,
        candidate_cap=50,
        text_of=_text_of,
    )
    assert [c.embedding_id for c in out.items] == [c.embedding_id for c in out2.items]


# ---- V7: exactly one batched call, capped at candidate_cap -----------------


class _SpyProvider(RerankProvider):
    """Wraps a real provider, recording each call's document count."""

    def __init__(self, inner: RerankProvider) -> None:
        self.inner = inner
        self.calls: list[int] = []

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        self.calls.append(len(documents))
        return self.inner.rerank(query, documents)


def test_single_call_capped_at_candidate_cap() -> None:
    """60 candidates, cap 50 -> ONE provider call over exactly 50 docs; tail kept in order."""
    candidates = [_Cand(f"c{i}", f"chunk number {i}") for i in range(60)]
    spy = _SpyProvider(LocalRerankProvider())

    out = apply_rerank(
        "chunk number 7",
        candidates,
        provider=spy,
        top_k=300,
        candidate_cap=50,
        text_of=_text_of,
    )
    assert spy.calls == [50]  # exactly one call, over the capped window
    assert len(out.items) == 60  # head (50, reranked) + tail (10, RRF order) — none dropped
    # the 10 tail candidates keep their original relative order at the end
    assert [c.embedding_id for c in out.items[-10:]] == [f"c{i}" for i in range(50, 60)]
    assert all(s is None for s in out.scores[-10:])  # tail carries no rerank score


# ---- V6: fail-SAFE fallback to RRF order on every failure mode --------------


class _RaisingProvider(RerankProvider):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        raise self.exc


class _MalformedProvider(RerankProvider):
    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        return [(999, 1.0)]  # index out of range -> RerankContractError


@pytest.mark.parametrize(
    "provider",
    [
        _RaisingProvider(ProviderError("boom", code="PROVIDER_TIMEOUT")),
        _RaisingProvider(TimeoutError("slow")),  # unexpected error -> absolute fail-safe
        _MalformedProvider(),
    ],
    ids=["provider_error", "unexpected_timeout", "malformed_response"],
)
def test_failsafe_returns_rrf_order(provider: RerankProvider) -> None:
    """Any reranker failure returns the input RRF order, applied=False, never raises."""
    candidates = [_Cand(f"c{i}", f"chunk {i}") for i in range(5)]

    out = apply_rerank(
        "q", candidates, provider=provider, top_k=5, candidate_cap=50, text_of=_text_of
    )
    assert out.applied is False
    assert [c.embedding_id for c in out.items] == [c.embedding_id for c in candidates]  # RRF order
    assert all(s is None for s in out.scores)


def test_malformed_is_a_contract_error() -> None:
    """Sanity: the validator flags an out-of-range index (proves the fail-safe trigger)."""
    from ai_agents.rag.rerank import _validated_order

    with pytest.raises(RerankContractError):
        _validated_order([(999, 1.0)], n=3)


# ---- subset reranks drop no candidate --------------------------------------


class _SubsetProvider(RerankProvider):
    """Returns only the top-2 indices (like an API top_n), in a new order."""

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        return [(2, 0.9), (0, 0.8)]


def test_subset_rerank_preserves_all_candidates() -> None:
    candidates = [_Cand(f"c{i}", f"chunk {i}") for i in range(4)]
    out = apply_rerank(
        "q", candidates, provider=_SubsetProvider(), top_k=4, candidate_cap=50, text_of=_text_of
    )
    assert out.applied is True
    # ranked first (c2, c0), then the omitted candidates in original order (c1, c3)
    assert [c.embedding_id for c in out.items] == ["c2", "c0", "c1", "c3"]
    assert out.scores[0] == 0.9 and out.scores[1] == 0.8
    assert out.scores[2] is None and out.scores[3] is None  # omitted -> no score


def test_empty_candidates_is_noop() -> None:
    out = apply_rerank(
        "q", [], provider=LocalRerankProvider(), top_k=5, candidate_cap=50, text_of=_text_of
    )
    assert out.items == [] and out.scores == [] and out.applied is True


# ---- retrieve()-level wiring (_apply_rerank_stage), hermetic ----------------
# These cover the integration point added to PolicyRetrievalTool.retrieve() — provider
# acquisition, the apply_rerank call, and the outer fail-safe — WITHOUT a DB (the only other
# callers of retrieve() are @pytest.mark.db and self-skip in the hermetic CI lane).


def _policy_candidate(eid: str, text: str, rank: int) -> object:
    from ai_agents.tools.policy_retrieval import RetrievalCandidate

    return RetrievalCandidate(
        embedding_id=eid,
        document_id="d",
        clause_ref=eid.upper(),
        chunk_text=text,
        section=None,
        parent_section_ref=None,
        policy_name="p",
        score=1.0 / rank,
        rank=rank,
    )


def test_apply_rerank_stage_reorders_and_reports_applied() -> None:
    """The default (local) provider reorders the fused list and reports applied=True."""
    from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
    from config.settings import get_settings

    tool = PolicyRetrievalTool.__new__(PolicyRetrievalTool)  # bypass embedder __init__
    fused = [
        _policy_candidate("c0", "Economy class flights are allowed for domestic travel.", 1),
        _policy_candidate("gold", "The nightly hotel cap in New York is 250 USD per night.", 2),
        _policy_candidate("c2", "Meal per diem allowances vary by city tier.", 3),
    ]
    intent = RetrievalIntent(query="nightly hotel cap in New York 250 USD", top_k=3)
    items, applied, scores = tool._apply_rerank_stage(intent, fused, get_settings())  # noqa: SLF001

    assert applied is True
    assert items[0].embedding_id == "gold"  # buried gold lifted to the top
    assert len(scores) == len(items)
    assert scores[0] is not None  # reranked head carries a score


def test_apply_rerank_stage_fails_safe_to_rrf_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """If provider acquisition explodes, the stage degrades to RRF order, applied=False."""
    import services.rerank_provider.factory as factory
    from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
    from config.settings import get_settings

    def _boom() -> object:
        raise RuntimeError("provider acquisition exploded")

    monkeypatch.setattr(factory, "get_rerank_provider", _boom)

    tool = PolicyRetrievalTool.__new__(PolicyRetrievalTool)
    fused = [_policy_candidate(f"c{i}", f"chunk {i}", i + 1) for i in range(3)]
    items, applied, scores = tool._apply_rerank_stage(  # noqa: SLF001
        RetrievalIntent(query="q", top_k=3), fused, get_settings()
    )

    assert applied is False
    assert [c.embedding_id for c in items] == ["c0", "c1", "c2"]  # untouched RRF order
    assert scores == [None, None, None]
