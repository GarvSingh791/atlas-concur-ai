"""R4-T1 — pure unit suite for corpus contextual prefixes.

Targets ``database/seeds/corpus/contextual.py``:
``deterministic_context_prefix`` (pure template), ``llm_context_prefix``
(fail-safe LLM path), and ``context_prefix`` (the FORK-C/FORK-D dispatcher).

Hermetic: NO database, NO network, NO ``pytest.mark.db``.  ``get_settings`` is
``lru_cache``d, so every test that flips ``RAG_CONTEXTUAL_PREFIXES`` /
``LLM_PROVIDER`` via ``monkeypatch.setenv`` must clear that cache first so the
new env is read.  The LLM provider is always a spy/stub installed via
``monkeypatch`` on ``services.llm_provider.factory.get_llm_provider`` — the
local path must NEVER touch it.
"""

from __future__ import annotations

import re

import pytest

from config.settings import get_settings
from database.seeds.corpus import contextual
from database.seeds.corpus.contextual import (
    context_prefix,
    deterministic_context_prefix,
    llm_context_prefix,
)
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmResponse

_DOCUMENT_TYPES = ("policy", "faq", "howto", "reference")

_DOC_NAMES = {
    "policy": "Helios Global Travel & Expense Policy",
    "faq": "Helios T&E FAQ",
    "howto": "Helios T&E How-To Guide",
    "reference": "Helios T&E Reference",
}


def _token_count(text: str) -> int:
    """Token = a word run OR a standalone punctuation char (the R4-T1 metric)."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def _reset_settings() -> None:
    """Drop the lru_cached Settings so a freshly-set env is picked up."""
    get_settings.cache_clear()


class _SpyProvider:
    """Records whether ``complete`` was called; returns a canned LlmResponse."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[list[LlmMessage]] = []

    def complete(self, messages: list[LlmMessage], **_kwargs: object) -> LlmResponse:
        self.calls.append(messages)
        return LlmResponse(text=self.text)


class _RaisingProvider:
    """Always raises ProviderError from ``complete`` (fail-safe exercise)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[LlmMessage], **_kwargs: object) -> LlmResponse:
        self.calls += 1
        raise ProviderError("simulated provider outage")


# --------------------------------------------------------------------------- #
# (a) + (b) deterministic_context_prefix is pure, deterministic, self-naming
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("document_type", _DOCUMENT_TYPES)
def test_deterministic_is_pure_and_deterministic(document_type: str) -> None:
    """R4-T1 — identical inputs yield byte-identical output for all four types."""
    kwargs = dict(
        document_title="Helios T&E (per-doc title)",
        document_type=document_type,
        section="Lodging Limits",
        clause_ref="POL-4.2",
    )
    first = deterministic_context_prefix(**kwargs)
    second = deterministic_context_prefix(**kwargs)
    assert first == second
    assert isinstance(first, str) and first


@pytest.mark.parametrize("document_type", _DOCUMENT_TYPES)
def test_deterministic_names_document_section_and_clause(document_type: str) -> None:
    """R4-T1 — each output names its document AND the section AND the clause_ref."""
    section = "Lodging Limits"
    clause_ref = "POL-4.2"
    out = deterministic_context_prefix(
        document_title="ignored-title",
        document_type=document_type,
        section=section,
        clause_ref=clause_ref,
    )
    assert _DOC_NAMES[document_type] in out
    assert section in out
    assert clause_ref in out


# --------------------------------------------------------------------------- #
# (c) token band [50, 100]
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("document_type", _DOCUMENT_TYPES)
def test_deterministic_token_count_in_band(document_type: str) -> None:
    """R4-T1 — every template lands in the [50, 100] token band."""
    out = deterministic_context_prefix(
        document_title="Helios T&E",
        document_type=document_type,
        section="Lodging Limits",
        clause_ref="POL-4.2",
    )
    n = _token_count(out)
    assert 50 <= n <= 100, f"{document_type}: {n} tokens out of band"


# --------------------------------------------------------------------------- #
# (d) dispatcher routing: local path -> deterministic, ZERO LLM calls
# --------------------------------------------------------------------------- #
def test_dispatcher_local_returns_template_and_never_calls_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4-T1 — ON + LLM_PROVIDER=local routes to the template; no provider call."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")  # FORK-H: prefixes only on a real embedder
    _reset_settings()

    spy = _SpyProvider(text="SHOULD-NOT-BE-USED")

    def _fail_if_called() -> _SpyProvider:
        spy.calls.append([])  # would be a violation
        return spy

    monkeypatch.setattr("services.llm_provider.factory.get_llm_provider", _fail_if_called)

    expected = deterministic_context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
    )
    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="full doc text",
        chunk_text="a chunk",
    )
    assert out == expected
    assert spy.calls == [], "local path must make ZERO LLM provider calls"
    _reset_settings()


# --------------------------------------------------------------------------- #
# (e) OFF -> empty string
# --------------------------------------------------------------------------- #
def test_dispatcher_off_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """R4-T1 — flag OFF yields "" (bare-chunk embedding, NULL context_prefix)."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "false")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")  # isolate the FLAG-off path from FORK-H
    _reset_settings()

    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="full doc text",
        chunk_text="a chunk",
    )
    assert out == ""
    _reset_settings()


# --------------------------------------------------------------------------- #
# (f) fail-safe: ProviderError -> deterministic template, never raises
# --------------------------------------------------------------------------- #
def test_llm_prefix_failsafe_on_provider_error() -> None:
    """R4-T1 — a raising provider falls back to the deterministic template."""
    provider = _RaisingProvider()
    expected = deterministic_context_prefix(
        document_title="t",
        document_type="faq",
        section="Receipts",
        clause_ref="FAQ-7",
    )
    out = llm_context_prefix(
        document_text="full doc text",
        chunk_text="a chunk",
        provider=provider,
        document_title="t",
        document_type="faq",
        section="Receipts",
        clause_ref="FAQ-7",
    )
    assert provider.calls == 1
    assert out == expected


def test_llm_prefix_failsafe_on_empty_completion() -> None:
    """R4-T1 — an empty/whitespace completion also falls back to the template."""
    provider = _SpyProvider(text="   \n  ")
    expected = deterministic_context_prefix(
        document_title="t",
        document_type="reference",
        section="Per-Diem Rates",
        clause_ref="REF-12",
    )
    out = llm_context_prefix(
        document_text="full doc text",
        chunk_text="a chunk",
        provider=provider,
        document_title="t",
        document_type="reference",
        section="Per-Diem Rates",
        clause_ref="REF-12",
    )
    assert len(provider.calls) == 1
    assert out == expected


# --------------------------------------------------------------------------- #
# (g) routing-to-LLM: production path returns the LLM text
# --------------------------------------------------------------------------- #
def test_dispatcher_production_routes_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4-T1 — ON + non-local provider routes to the LLM and returns its text."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")  # FORK-H: prefixes only on a real embedder
    _reset_settings()

    canned = "This chunk sits in the Helios policy under Lodging Limits, clause POL-4.2."
    spy = _SpyProvider(text=canned)
    monkeypatch.setattr("services.llm_provider.factory.get_llm_provider", lambda: spy)

    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="the full source document body",
        chunk_text="the target chunk body",
    )
    assert out == canned
    assert len(spy.calls) == 1
    # System message carries the full document; user turn carries the chunk.
    messages = spy.calls[0]
    assert messages[0].role == "system"
    assert "the full source document body" in messages[0].content
    assert messages[-1].role == "user"
    assert "the target chunk body" in messages[-1].content
    _reset_settings()


def test_dispatcher_production_reuses_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4-T1 — FORK-D: a cached prefix is reused without any LLM call."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")  # FORK-H: prefixes only on a real embedder
    _reset_settings()

    spy = _SpyProvider(text="fresh-generation-should-not-happen")
    monkeypatch.setattr("services.llm_provider.factory.get_llm_provider", lambda: spy)

    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="doc",
        chunk_text="chunk",
        cached="persisted prefix from a prior index run",
    )
    assert out == "persisted prefix from a prior index run"
    assert spy.calls == [], "cached path must not call the LLM"
    _reset_settings()


# --------------------------------------------------------------------------- #
# (h) FORK-H (R4-T3): EMBEDDINGS_PROVIDER==local forces "" even with the flag ON,
#     and makes ZERO LLM calls regardless of LLM_PROVIDER — the CI-safety enabler
#     that keeps `make eval-db` R3-byte-identical when the prefix default is flipped.
# --------------------------------------------------------------------------- #
def test_fork_h_local_embedder_forces_empty_even_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORK-H — flag ON + LLM_PROVIDER=local + EMBEDDINGS_PROVIDER=local -> "" (no prefix)."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "local")
    _reset_settings()

    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="full doc text",
        chunk_text="a chunk",
    )
    assert out == "", "FORK-H: the local embedder must never receive a prefix"
    _reset_settings()


def test_fork_h_local_embedder_makes_zero_llm_calls_with_production_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORK-H — even flag ON + a PRODUCTION LLM provider, the local embedder path is "" and
    NEVER touches the LLM (the eval-db lane can run with the prefix default flipped True)."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "local")
    _reset_settings()

    spy = _SpyProvider(text="SHOULD-NOT-BE-USED")
    monkeypatch.setattr("services.llm_provider.factory.get_llm_provider", lambda: spy)

    out = context_prefix(
        document_title="t",
        document_type="policy",
        section="Lodging Limits",
        clause_ref="POL-4.2",
        document_text="full doc text",
        chunk_text="a chunk",
        cached="a persisted prefix that must be ignored on the local embedder",
    )
    assert out == "", "FORK-H short-circuits before the flag/cache/LLM branches"
    assert spy.calls == [], "FORK-H local path must make ZERO LLM provider calls"
    _reset_settings()


def test_fork_h_openai_embedder_emits_template_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORK-H — on a real embedder + flag ON + LLM_PROVIDER=local, the template IS emitted."""
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    _reset_settings()

    out = context_prefix(
        document_title="t",
        document_type="faq",
        section="Receipts",
        clause_ref="FAQ-7",
        document_text="full doc text",
        chunk_text="a chunk",
    )
    expected = deterministic_context_prefix(
        document_title="t", document_type="faq", section="Receipts", clause_ref="FAQ-7"
    )
    assert out == expected and out, "FORK-H: a real embedder with the flag ON gets the template"
    _reset_settings()


def test_module_exposes_public_api() -> None:
    """R4-T1 — the three public callables are present on the module."""
    assert callable(contextual.deterministic_context_prefix)
    assert callable(contextual.llm_context_prefix)
    assert callable(contextual.context_prefix)
