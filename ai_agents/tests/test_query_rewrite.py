"""Hermetic tests for R5-T1 query rewrite / HyDE (ai_agents.rag.query_rewrite).

No DB, no network, no real LLM.  Covers: the fail-safe (ProviderError / empty → original
query), the deterministic no-op rewriter, and the factory's enabled/provider gating.
"""

from __future__ import annotations

from collections.abc import Iterator

from ai_agents.rag.query_rewrite import (
    DeterministicQueryRewriter,
    HydeQueryRewriter,
    generate_hyde_doc,
    hyde_rewriter,
)
from services.exceptions import ProviderError, ProviderUnavailableError
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage


class _ScriptedProvider(LlmProvider):
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:  # noqa: E501
        self.calls += 1
        return LlmResponse(
            text=self._text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1)
        )

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:  # noqa: E501
        yield from []


class _RaisingProvider(LlmProvider):
    def __init__(self, exc: ProviderError | None = None) -> None:
        self._exc = exc or ProviderUnavailableError("boom")

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:  # noqa: E501
        raise self._exc

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:  # noqa: E501
        raise self._exc


class TestGenerateHydeDoc:
    def test_returns_hypothesis_on_success(self) -> None:
        provider = _ScriptedProvider("Economy class is required for flights under six hours.")
        out = generate_hyde_doc("can I fly business to NYC?", provider)
        assert out == "Economy class is required for flights under six hours."
        assert provider.calls == 1

    def test_provider_error_falls_back_to_original_query(self) -> None:
        query = "can I fly business to NYC?"
        out = generate_hyde_doc(query, _RaisingProvider())
        assert out == query  # fail-safe: never raises, returns the query unchanged

    def test_empty_completion_falls_back_to_original_query(self) -> None:
        query = "some query"
        out = generate_hyde_doc(query, _ScriptedProvider("   "))
        assert out == query

    def test_document_type_hint_is_accepted(self) -> None:
        out = generate_hyde_doc("q", _ScriptedProvider("excerpt"), document_type_hint="faq")
        assert out == "excerpt"


class TestRewriters:
    def test_deterministic_rewriter_is_identity(self) -> None:
        assert DeterministicQueryRewriter()("trip to NY") == "trip to NY"

    def test_hyde_rewriter_calls_provider(self) -> None:
        provider = _ScriptedProvider("policy excerpt")
        rewriter = HydeQueryRewriter(provider)
        assert rewriter("q") == "policy excerpt"
        assert provider.calls == 1


class TestHydeRewriterFactory:
    def test_disabled_returns_deterministic_noop(self) -> None:
        provider = _RaisingProvider()  # would raise if ever called
        rewriter = hyde_rewriter(provider, enabled=False)
        assert isinstance(rewriter, DeterministicQueryRewriter)
        assert rewriter("q") == "q"  # provider never touched

    def test_no_provider_returns_deterministic_noop(self) -> None:
        rewriter = hyde_rewriter(None, enabled=True)
        assert isinstance(rewriter, DeterministicQueryRewriter)
        assert rewriter("q") == "q"

    def test_enabled_with_provider_returns_hyde(self) -> None:
        rewriter = hyde_rewriter(_ScriptedProvider("x"), enabled=True)
        assert isinstance(rewriter, HydeQueryRewriter)
