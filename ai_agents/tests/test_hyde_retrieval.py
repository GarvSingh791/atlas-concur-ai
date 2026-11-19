"""Hermetic tests for R5-T1 HyDE two-pass in GroundedRetriever.

No DB, no LLM.  Proves:
  (1) CI-safety — with HYDE_ENABLED=False the HyDE branch is structurally skipped and the
      injected provider is NEVER called (the eval-db invariant);
  (2) the local provider is a no-op even with HyDE enabled (deterministic rewriter);
  (3) the weak-band gate fires HyDE only when the top max-dense cosine is in
      [threshold, HYDE_WEAK_BAND_UPPER), and keeps the better of the two passes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from ai_agents.context import AgentContext
from ai_agents.rag import retrieval as retrieval_mod
from ai_agents.rag.retrieval import GroundedRetriever, GroundingStatus
from ai_agents.tools.policy_retrieval import RetrievalIntent, RetrievalResult, RetrievedChunk
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse

_CTX = AgentContext(organization_id=uuid.uuid4(), granted_permission_keys=frozenset(), user_id=None)


class _RaisingProvider(LlmProvider):
    """complete() raises — proves the HyDE branch never calls the LLM."""

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:  # noqa: E501
        raise AssertionError("LLM must NOT be called on the CI/eval path")

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:  # noqa: E501
        raise AssertionError("LLM must NOT be called on the CI/eval path")


class _SpyRewriter:
    """A live rewriter stand-in: records calls, returns a fixed hypothesis."""

    def __init__(self, hypothesis: str = "hypothetical policy excerpt") -> None:
        self._hypothesis = hypothesis
        self.calls = 0

    def __call__(self, query: str) -> str:
        self.calls += 1
        return self._hypothesis


def _chunk(similarity: float) -> RetrievedChunk:
    return RetrievedChunk(
        embedding_id=str(uuid.uuid4()),
        clause_ref="AIR-01",
        chunk_text="Economy class for short flights.",
        similarity=similarity,
        policy_name="Atlas T&E Policy",  # type: ignore[arg-type]
        document_type="policy",
    )


def _result(md: float) -> RetrievalResult:
    return RetrievalResult(query="q", chunks=(_chunk(md),), max_dense_similarity=md)


class _QueueRetriever:
    """Returns queued RetrievalResults in order (clamps to last); records intent queries."""

    def __init__(self, *results: RetrievalResult) -> None:
        self._results = list(results)
        self.calls = 0
        self.queries: list[str] = []

    def retrieve(self, intent: RetrievalIntent, *, context: Any, session: Any) -> RetrievalResult:
        idx = min(self.calls, len(self._results) - 1)
        self.calls += 1
        self.queries.append(intent.query)
        return self._results[idx]


@pytest.fixture(autouse=True)
def _cosine_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the cosine gate (τ* absent) so max_dense drives grounding — the committed local path.
    monkeypatch.setattr(retrieval_mod, "load_abstention_threshold", lambda _p: None)


class TestCiSafety:
    def test_disabled_never_calls_provider(self) -> None:
        # HYDE_ENABLED=False → branch skipped even with a weak-band first pass + raising provider.
        retriever = _QueueRetriever(_result(0.25))
        gr = GroundedRetriever(
            retriever, threshold=0.20, provider=_RaisingProvider(), hyde_enabled=False
        )
        out = gr.retrieve("q", context=_CTX, session=None)  # must NOT raise
        assert out.status is GroundingStatus.grounded
        assert retriever.calls == 1  # single pass, no HyDE re-retrieval

    def test_local_provider_is_noop_even_when_enabled(self) -> None:
        # hyde_enabled=True but LLM_PROVIDER=local (conftest default) → deterministic rewriter →
        # hypothesis == query → second pass skipped → the raising provider is never called.
        retriever = _QueueRetriever(_result(0.25))
        gr = GroundedRetriever(
            retriever, threshold=0.20, provider=_RaisingProvider(), hyde_enabled=True
        )
        out = gr.retrieve("q", context=_CTX, session=None)  # must NOT raise
        assert out.status is GroundingStatus.grounded
        assert retriever.calls == 1


def _gr_with_spy(retriever: _QueueRetriever, spy: _SpyRewriter) -> GroundedRetriever:
    gr = GroundedRetriever(retriever, threshold=0.20, hyde_enabled=True, hyde_weak_band_upper=0.30)
    gr._hyde_rewriter = spy  # inject a live rewriter to exercise the weak-band gate directly
    return gr


class TestWeakBandGate:
    def test_weak_band_fires_hyde_and_keeps_better_pass(self) -> None:
        retriever = _QueueRetriever(_result(0.25), _result(0.55))
        spy = _SpyRewriter()
        out = _gr_with_spy(retriever, spy).retrieve("q", context=_CTX, session=None)
        assert spy.calls == 1  # HyDE fired
        assert retriever.calls == 2  # two retrieval passes
        assert retriever.queries == ["q", "hypothetical policy excerpt"]  # 2nd used the hypothesis
        assert out.status is GroundingStatus.grounded

    def test_strong_match_skips_hyde(self) -> None:
        retriever = _QueueRetriever(_result(0.40))  # >= upper 0.30 → strong
        spy = _SpyRewriter()
        _gr_with_spy(retriever, spy).retrieve("q", context=_CTX, session=None)
        assert spy.calls == 0
        assert retriever.calls == 1

    def test_below_floor_skips_hyde(self) -> None:
        retriever = _QueueRetriever(_result(0.15))  # < threshold 0.20 → abstain territory
        spy = _SpyRewriter()
        _gr_with_spy(retriever, spy).retrieve("q", context=_CTX, session=None)
        assert spy.calls == 0
        assert retriever.calls == 1

    def test_second_pass_worse_keeps_first(self) -> None:
        retriever = _QueueRetriever(_result(0.25), _result(0.10))
        spy = _SpyRewriter()
        out = _gr_with_spy(retriever, spy).retrieve("q", context=_CTX, session=None)
        assert spy.calls == 1
        assert retriever.calls == 2
        assert out.status is GroundingStatus.grounded  # kept the stronger first pass (0.25)
