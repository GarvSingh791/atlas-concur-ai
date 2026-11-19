"""Hermetic tests for the Package B prompt-cache split in PolicyQaHypothesizer.seed().

No network, no DB. Proves:
  * LOCAL path (LLM_PROVIDER==local) builds exactly ONE user message whose content is
    _SEED_PROMPT_V2.format(...) — BYTE-IDENTICAL to the pre-split prompt (eval-db stability).
  * LIVE path (LLM_PROVIDER!=local, monkeypatched) builds [system(cache=True), user], with the
    static persona/rules in the cached system block and the per-request clauses/question in the
    user block.

The cache mechanism itself is wired but INERT at the current ~150-token static block
(below Gemini's ~1024-token cache floor) and is not live-verified — these tests only assert the
message SHAPE handed to the provider, which is what changes on the live path.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import config.settings as cs
from ai_agents.agents.policy_qa import (
    _QUERY_TEMPLATE,
    _SEED_PROMPT_V2,
    _STATIC_INSTRUCTIONS,
    PolicyQaHypothesizer,
)
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.rag.retrieval import GroundedClause
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage


class _CapturingProvider(LlmProvider):
    """Records the messages handed to complete() so the test can assert their shape."""

    def __init__(self) -> None:
        self.captured: list[LlmMessage] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.captured = list(messages)
        return LlmResponse(
            text="Economy is required [T&E Policy §AIR-01].",
            model="stub",
            usage=LlmUsage(input_tokens=1, output_tokens=1),
        )

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


def _clause() -> GroundedClause:
    return GroundedClause(
        clause_ref="AIR-01",
        chunk_text="Economy for short flights.",
        similarity=0.9,
        document_type="policy",
    )


def _cite() -> Citation:
    return Citation(
        kind=CitationKind.clause_ref,
        ref="AIR-01",
        detail="Economy for short flights.",
        source="Atlas T&E Policy",
    )


def _hypothesizer(provider: LlmProvider) -> PolicyQaHypothesizer:
    return PolicyQaHypothesizer(
        provider=provider,
        clauses=(_clause(),),
        question="Is economy required?",
        citations=(_cite(),),
        confidence=0.9,
    )


def _set_provider(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LLM_PROVIDER", value)
    if value == "openrouter":
        # openrouter selection needs a key/model for get_settings() to validate; not called here.
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "google/gemini-2.5-flash")
    cs.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_settings_cache() -> Iterator[None]:
    yield
    cs.get_settings.cache_clear()


def test_local_path_builds_single_byte_identical_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_provider(monkeypatch, "local")
    provider = _CapturingProvider()
    hyp = _hypothesizer(provider)

    hyp.seed()

    assert len(provider.captured) == 1
    msg = provider.captured[0]
    assert msg.role == "user"
    assert msg.cache is False
    expected = _SEED_PROMPT_V2.format(clauses=hyp._clause_text(), question="Is economy required?")
    assert msg.content == expected


def test_live_path_builds_cached_system_plus_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_provider(monkeypatch, "openrouter")
    provider = _CapturingProvider()
    hyp = _hypothesizer(provider)

    hyp.seed()

    assert len(provider.captured) == 2
    system, user = provider.captured

    assert system.role == "system"
    assert system.cache is True
    assert system.content == _STATIC_INSTRUCTIONS

    assert user.role == "user"
    assert user.cache is False
    assert user.content == _QUERY_TEMPLATE.format(
        clauses=hyp._clause_text(), question="Is economy required?"
    )


def test_static_plus_query_reconstructs_fused_prompt() -> None:
    # Guards eval-db byte-identity: the split constants must recombine to the exact fused prompt.
    assert _SEED_PROMPT_V2 == _STATIC_INSTRUCTIONS + "\n\n" + _QUERY_TEMPLATE
