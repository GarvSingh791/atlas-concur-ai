"""Tests for services/rerank_provider/novita.py (Novita bge-reranker-v2-m3 adapter, R4-T4 swap).

All hermetic with httpx.post monkeypatched — no network. Locks the contract: Cohere-style
request shape (POST {base}/rerank, Bearer, {model,query,documents,top_n}), defensive score-field
parsing (relevance_score | score), deterministic (index, score) desc ordering, and the
fail-loud-then-RRF error contract. The live smoke (real Novita call) is run out-of-band.
"""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    import config.settings as cs

    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the adapter's 429/transient exponential backoff so retry tests run instantly."""
    import services.rerank_provider.novita as nv

    monkeypatch.setattr(nv.time, "sleep", lambda *_a, **_k: None)


def _select(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-key",
    model: str = "baai/bge-reranker-v2-m3",
    base: str | None = None,
) -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "novita")
    monkeypatch.setenv("RERANK_API_KEY", key)
    monkeypatch.setenv("RERANK_MODEL", model)
    if base is not None:
        monkeypatch.setenv("RERANK_BASE_URL", base)
    import config.settings as cs

    cs.get_settings.cache_clear()


class _Resp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _results(scored: list[tuple[int, float]], *, field: str = "relevance_score") -> dict:
    return {"results": [{"index": i, field: s} for i, s in scored]}


# ---- Tier 1: interface + factory ---------------------------------------------


def test_novita_satisfies_interface() -> None:
    from services.rerank_provider.interface import RerankProvider
    from services.rerank_provider.novita import NovitaRerankProvider

    assert issubclass(NovitaRerankProvider, RerankProvider)
    NovitaRerankProvider()


def test_factory_returns_novita_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    from services.rerank_provider.factory import get_rerank_provider
    from services.rerank_provider.novita import NovitaRerankProvider

    assert isinstance(get_rerank_provider(), NovitaRerankProvider)


def test_default_rerank_provider_is_still_local() -> None:
    from config.settings.base import Settings

    assert Settings.model_fields["RERANK_PROVIDER"].default == "local"


# ---- Tier 2: request shaping + parsing (no network) --------------------------


def test_rerank_request_shape_and_desc_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch, base="https://api.novita.ai/openai")
    import httpx

    captured: dict = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        # deliberately out-of-order + scrambled to prove we sort + index-map
        return _Resp(200, _results([(0, 0.10), (1, 0.99), (2, 0.50)]))

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.rerank_provider.novita import NovitaRerankProvider

    out = NovitaRerankProvider().rerank("q", ["d0", "d1", "d2"])

    assert str(captured["url"]) == "https://api.novita.ai/openai/rerank"
    sent = captured["json"]
    assert isinstance(sent, dict)
    assert sent["model"] == "baai/bge-reranker-v2-m3"
    assert sent["query"] == "q"
    assert sent["documents"] == ["d0", "d1", "d2"]
    assert sent["top_n"] == 3
    headers = captured["headers"]
    assert isinstance(headers, dict) and headers["Authorization"] == "Bearer test-key"
    # desc by score; ties -> idx asc
    assert out == [(1, 0.99), (2, 0.50), (0, 0.10)]


def test_parser_accepts_score_field_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Novita may emit `score` instead of `relevance_score` — accept either."""
    _select(monkeypatch)
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *_a, **_k: _Resp(200, _results([(0, 0.2), (1, 0.8)], field="score"))
    )
    from services.rerank_provider.novita import NovitaRerankProvider

    assert NovitaRerankProvider().rerank("q", ["a", "b"]) == [(1, 0.8), (0, 0.2)]


def test_default_base_url_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch, base="")
    import httpx

    captured: dict = {}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: captured.update(url=url) or _Resp(200, _results([(0, 1.0)])),
    )
    from services.rerank_provider.novita import NovitaRerankProvider

    NovitaRerankProvider().rerank("q", ["only"])
    assert captured["url"] == "https://api.novita.ai/openai/rerank"


# ---- Tier 2: failure contract (no network) -----------------------------------


def test_missing_key_raises_config_error_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch, key="")
    from services.exceptions import ProviderConfigurationError
    from services.rerank_provider.factory import get_rerank_provider

    p = get_rerank_provider()  # must NOT raise at construction
    with pytest.raises(ProviderConfigurationError):
        p.rerank("q", ["a"])


def test_429_backoff_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rate-limit handling: two HTTP 429s are backed off + retried, then the call succeeds."""
    _select(monkeypatch)
    import httpx

    calls = {"n": 0}

    def fake_post(*_a: object, **_k: object) -> _Resp:
        calls["n"] += 1
        if calls["n"] < 3:  # first two attempts rate-limited, third succeeds
            return _Resp(429, {})
        return _Resp(200, _results([(0, 0.9)]))

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.rerank_provider.novita import NovitaRerankProvider

    assert NovitaRerankProvider().rerank("q", ["a"]) == [(0, 0.9)]
    assert calls["n"] == 3, "should retry past the 429s, not fall back early"


def test_persistent_429_eventually_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sustained 429 (rate limit never clears) is bounded — raises after _MAX_ATTEMPTS."""
    _select(monkeypatch)
    import httpx

    calls = {"n": 0}

    def fake_post(*_a: object, **_k: object) -> _Resp:
        calls["n"] += 1
        return _Resp(429, {})

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.exceptions import ProviderError
    from services.rerank_provider.novita import _MAX_ATTEMPTS, NovitaRerankProvider

    with pytest.raises(ProviderError):
        NovitaRerankProvider().rerank("q", ["a"])
    assert calls["n"] == _MAX_ATTEMPTS, "must stop after the bounded attempt budget"


def test_http_500_raises_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(500, {}))
    from services.exceptions import ProviderError
    from services.rerank_provider.novita import NovitaRerankProvider

    with pytest.raises(ProviderError):
        NovitaRerankProvider().rerank("q", ["a"])


def test_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    def boom(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    from services.exceptions import ProviderError
    from services.rerank_provider.novita import NovitaRerankProvider

    with pytest.raises(ProviderError):
        NovitaRerankProvider().rerank("q", ["a"])


def test_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(200, {"no_results": []}))
    from services.exceptions import ProviderError
    from services.rerank_provider.novita import NovitaRerankProvider

    with pytest.raises(ProviderError):
        NovitaRerankProvider().rerank("q", ["a"])


def test_out_of_range_index_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(200, _results([(5, 0.9)])))
    from services.exceptions import ProviderError
    from services.rerank_provider.novita import NovitaRerankProvider

    with pytest.raises(ProviderError):
        NovitaRerankProvider().rerank("q", ["a", "b"])  # index 5 out of range for n=2


def test_empty_docs_returns_empty_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not POST for empty docs")

    monkeypatch.setattr(httpx, "post", boom)
    from services.rerank_provider.novita import NovitaRerankProvider

    assert NovitaRerankProvider().rerank("q", []) == []
