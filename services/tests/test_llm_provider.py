"""Smoke tests for services/llm_provider."""

import pytest


def test_factory_returns_local_by_default() -> None:
    """get_llm_provider() defaults to LocalLlmProvider with no env override."""
    from services.llm_provider import LocalLlmProvider, get_llm_provider

    p = get_llm_provider()
    assert isinstance(p, LocalLlmProvider)


def test_local_complete_returns_deterministic_output() -> None:
    """LocalLlmProvider.complete() returns non-empty text, no key needed."""
    from services.llm_provider import LlmMessage, get_llm_provider

    p = get_llm_provider()
    msgs = [LlmMessage(role="user", content="hello")]
    r = p.complete(msgs)
    assert isinstance(r.text, str)
    assert len(r.text) > 0
    assert r.model == "local-stub"
    assert r.tool_calls == []


def test_local_stream_yields_chunks() -> None:
    """LocalLlmProvider.stream() yields LlmChunk objects covering full response."""
    from services.llm_provider import LlmChunk, LlmMessage, get_llm_provider

    p = get_llm_provider()
    msgs = [LlmMessage(role="user", content="hello")]
    chunks = list(p.stream(msgs))
    assert len(chunks) > 0
    assert all(isinstance(c, LlmChunk) for c in chunks)
    full = "".join(c.delta for c in chunks)
    assert len(full) > 0


def test_real_mode_raises_provider_error_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switching to anthropic without key raises ProviderError at call time, not import time."""
    import config.settings as cs

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "")
    cs.get_settings.cache_clear()

    from services.exceptions import ProviderError
    from services.llm_provider import LlmMessage, get_llm_provider

    p = get_llm_provider()  # must NOT raise
    with pytest.raises(ProviderError):
        p.complete([LlmMessage(role="user", content="hi")])

    cs.get_settings.cache_clear()  # restore
