"""Tests for services/llm_provider/openrouter.py (OpenRouter / Gemini adapter).

Tier 1 (contract + factory wiring) and Tier 2 degradation/parse tests run
hermetically with httpx monkeypatched — no network. The live smoke test
(@pytest.mark.live) makes one real call and is skipped unless openrouter is
explicitly selected with a key in the environment.

Failure contract note: the adapter RAISES ProviderError on persistent failure
(mirrors RealLlmProvider). The "needs_review" degradation is asserted end-to-end
against the existing caller wiring in ai_agents/tests/test_openrouter_degradation.py.
"""

import inspect

import pytest


def _select_openrouter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-key",
    model: str = "google/gemini-2.5-flash",
) -> None:
    import config.settings as cs

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_MODEL", model)
    cs.get_settings.cache_clear()


def _restore_settings() -> None:
    import config.settings as cs

    cs.get_settings.cache_clear()


# ---- Tier 1: contract + factory ---------------------------------------------


def test_openrouter_satisfies_interface() -> None:
    """OpenRouterLlmProvider is an LlmProvider with method shapes matching the interface."""
    from services.llm_provider import LlmProvider, OpenRouterLlmProvider

    assert issubclass(OpenRouterLlmProvider, LlmProvider)
    OpenRouterLlmProvider()  # instantiation proves all abstractmethods are implemented

    for name in ("complete", "stream"):
        base = inspect.signature(getattr(LlmProvider, name))
        impl = inspect.signature(getattr(OpenRouterLlmProvider, name))
        assert list(impl.parameters) == list(base.parameters), f"{name} param drift"
        assert [p.kind for p in impl.parameters.values()] == [
            p.kind for p in base.parameters.values()
        ], f"{name} kind drift"
        assert impl.return_annotation == base.return_annotation, f"{name} return drift"


def test_factory_returns_openrouter_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_llm_provider() returns OpenRouterLlmProvider when LLM_PROVIDER=openrouter."""
    _select_openrouter(monkeypatch)
    from services.llm_provider import OpenRouterLlmProvider, get_llm_provider

    assert isinstance(get_llm_provider(), OpenRouterLlmProvider)
    _restore_settings()


def test_default_provider_is_still_local() -> None:
    """Adding openrouter must not change the Settings default — local stays default."""
    from config.settings.base import Settings

    assert Settings.model_fields["LLM_PROVIDER"].default == "local"


# ---- Tier 2: parsing (200 response, no network) -----------------------------


def test_complete_parses_text_usage_and_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response is parsed into LlmResponse; request is shaped correctly."""
    _select_openrouter(monkeypatch)
    import httpx

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "model": "google/gemini-2.5-flash",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "Paris it is.",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "echo_city",
                                        "arguments": '{"city": "Paris"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.llm_provider import LlmMessage, ToolSpec, get_llm_provider

    provider = get_llm_provider()
    tool = ToolSpec(
        name="echo_city",
        description="echo a city",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    )
    resp = provider.complete(
        [LlmMessage(role="user", content="Call echo_city with Paris")],
        tools=[tool],
        tool_choice="echo_city",
    )

    assert resp.text == "Paris it is."
    assert resp.model == "google/gemini-2.5-flash"
    assert resp.usage is not None
    assert resp.usage.input_tokens == 11
    assert resp.usage.output_tokens == 7
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "echo_city"
    assert resp.tool_calls[0].arguments == {"city": "Paris"}
    assert resp.tool_calls[0].id == "call_1"

    sent = captured["json"]
    assert isinstance(sent, dict)
    assert str(captured["url"]).endswith("/chat/completions")
    assert sent["model"] == "google/gemini-2.5-flash"
    assert sent["reasoning"] == {"enabled": False}  # thinking disabled (budget 0)
    assert sent["tool_choice"] == {"type": "function", "function": {"name": "echo_city"}}
    assert sent["tools"][0]["function"]["name"] == "echo_city"
    _restore_settings()


# ---- Tier 2: degradation (adapter raises ProviderError; no network) ---------


def test_complete_raises_provider_error_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport failure surfaces as ProviderError (callers turn it into needs_review)."""
    _select_openrouter(monkeypatch)
    import httpx

    def boom(*_args: object, **_kwargs: object) -> object:
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "post", boom)

    from services.exceptions import ProviderError
    from services.llm_provider import LlmMessage, get_llm_provider

    provider = get_llm_provider()
    with pytest.raises(ProviderError):
        provider.complete([LlmMessage(role="user", content="hi")])
    _restore_settings()


def test_complete_raises_provider_error_on_http_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 5xx (after retry + fallback) surfaces as ProviderError, not a bad response."""
    _select_openrouter(monkeypatch)
    import httpx

    class _Resp500:
        status_code = 500

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp500())

    from services.exceptions import ProviderError
    from services.llm_provider import LlmMessage, get_llm_provider

    provider = get_llm_provider()
    with pytest.raises(ProviderError):
        provider.complete([LlmMessage(role="user", content="hi")])
    _restore_settings()


def test_missing_key_raises_at_call_time_not_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting openrouter without a key still builds the provider, but raises on call."""
    _select_openrouter(monkeypatch, key="")

    from services.exceptions import ProviderError
    from services.llm_provider import LlmMessage, get_llm_provider

    provider = get_llm_provider()  # must NOT raise at construction
    with pytest.raises(ProviderError):
        provider.complete([LlmMessage(role="user", content="hi")])
    _restore_settings()


# ---- Tier 2: live smoke (real network; opt-in) ------------------------------


@pytest.mark.live
def test_openrouter_live_smoke() -> None:
    """One real call to google/gemini-2.5-flash. Skipped unless openrouter is selected."""
    import os

    if os.environ.get("LLM_PROVIDER") != "openrouter" or not os.environ.get("LLM_API_KEY"):
        pytest.skip("live smoke needs LLM_PROVIDER=openrouter + LLM_API_KEY in the environment")

    import config.settings as cs

    cs.get_settings.cache_clear()
    from services.llm_provider import LlmMessage, ToolSpec, get_llm_provider

    provider = get_llm_provider()
    tool = ToolSpec(
        name="echo_city",
        description="Return the city the user names.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    resp = provider.complete(
        [LlmMessage(role="user", content="Use echo_city for the capital of France.")],
        tools=[tool],
        tool_choice="echo_city",
    )
    print(
        f"\n[live] model={resp.model!r} text={resp.text!r} "
        f"tool_calls={resp.tool_calls} usage={resp.usage}"
    )
    assert resp.usage is not None
    assert resp.usage.input_tokens + resp.usage.output_tokens > 0
    assert resp.text or resp.tool_calls
    cs.get_settings.cache_clear()
