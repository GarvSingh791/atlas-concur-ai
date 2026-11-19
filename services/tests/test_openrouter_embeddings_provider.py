"""Tests for services/embeddings_provider/openrouter.py (OpenRouter embeddings adapter).

All tests run HERMETICALLY with httpx.post monkeypatched — no network. They lock the
R4-T4 contract for the new adapter: provider/model prefixing, the dimensions=1536 request
(Fence 1), the loud failure when OpenRouter does NOT forward `dimensions` (FORK-K guard),
L2-renormalization, batching, and the key/config error paths. The live dimensions smoke
(real OpenRouter call) is FORK-K, run out-of-band via make eval-live-matrix's prelude — not
a committed network test.
"""

import math
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """get_settings() is lru-cached; clear it around each test so env monkeypatches apply."""
    import config.settings as cs

    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _select(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-key",
    model: str = "text-embedding-3-large",
    dimensions: str = "1536",
    base_url: str | None = None,
    batch_size: str | None = None,
) -> None:
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("EMBEDDINGS_MODEL", model)
    monkeypatch.setenv("EMBEDDINGS_DIMENSIONS", dimensions)
    if base_url is not None:
        monkeypatch.setenv("LLM_BASE_URL", base_url)
    if batch_size is not None:
        monkeypatch.setenv("EMBEDDINGS_BATCH_SIZE", batch_size)
    import config.settings as cs

    cs.get_settings.cache_clear()


class _Resp:
    """Minimal httpx-like response: .status_code + .json()."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _embeddings_payload(vectors: list[list[float]]) -> dict:
    """An OpenAI-compatible embeddings response body (indices deliberately scrambled)."""
    items = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    return {"object": "list", "data": list(reversed(items)), "model": "openai/x"}


# ---- Tier 1: interface + factory --------------------------------------------


def test_openrouter_embeddings_satisfies_interface() -> None:
    from services.embeddings_provider import EmbeddingsProvider, OpenRouterEmbeddingsProvider

    assert issubclass(OpenRouterEmbeddingsProvider, EmbeddingsProvider)
    OpenRouterEmbeddingsProvider()  # instantiation proves embed_texts is implemented


def test_factory_returns_openrouter_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    from services.embeddings_provider import OpenRouterEmbeddingsProvider, get_embeddings_provider

    assert isinstance(get_embeddings_provider(), OpenRouterEmbeddingsProvider)


def test_default_provider_is_still_local() -> None:
    """Adding openrouter must NOT change the Settings default — local stays default (CI safety)."""
    from config.settings.base import Settings

    assert Settings.model_fields["EMBEDDINGS_PROVIDER"].default == "local"


# ---- Tier 2: request shaping + renormalization (no network) ------------------


def test_embed_prefixes_model_sends_dimensions_and_renormalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai/ prefix + dimensions=1536 reach the wire; a non-unit return is L2-renormalized."""
    from config.constants.embeddings import EMBEDDING_DIMENSION

    _select(monkeypatch, model="text-embedding-3-large")
    import httpx

    # A deliberately NON-unit vector (L2 norm 2.0) to prove renormalization happens.
    raw = [2.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
    captured: dict = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp(200, _embeddings_payload([raw]))

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.embeddings_provider import OpenRouterEmbeddingsProvider

    out = OpenRouterEmbeddingsProvider().embed_texts(["hotel nightly cap"])

    assert str(captured["url"]).endswith("/embeddings")
    sent = captured["json"]
    assert isinstance(sent, dict)
    assert sent["model"] == "openai/text-embedding-3-large", "must use the openai/ route prefix"
    assert sent["dimensions"] == EMBEDDING_DIMENSION, "must request 1536-d (Fence 1)"
    assert sent["input"] == ["hotel nightly cap"]
    headers = captured["headers"]
    assert isinstance(headers, dict) and headers["Authorization"] == "Bearer test-key"

    assert len(out) == 1 and len(out[0]) == EMBEDDING_DIMENSION
    norm = math.sqrt(sum(x * x for x in out[0]))
    assert abs(norm - 1.0) < 1e-9, f"adapter did not L2-renormalize (norm={norm})"
    assert abs(out[0][0] - 1.0) < 1e-9, "renormalized leading component should be 2.0/2.0 = 1.0"


def test_already_prefixed_model_is_not_double_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.constants.embeddings import EMBEDDING_DIMENSION

    _select(monkeypatch, model="openai/text-embedding-3-small")
    import httpx

    captured: dict = {}

    def fake_post(url: str, **kwargs: object) -> _Resp:
        captured["json"] = kwargs.get("json")
        return _Resp(200, _embeddings_payload([[1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)]))

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.embeddings_provider import OpenRouterEmbeddingsProvider

    OpenRouterEmbeddingsProvider().embed_texts(["x"])
    assert captured["json"]["model"] == "openai/text-embedding-3-small"


def test_results_are_reordered_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out-of-order `data` items are restored to input order via their `index`."""
    from config.constants.embeddings import EMBEDDING_DIMENSION

    _select(monkeypatch)
    import httpx

    v0 = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
    v1 = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 2)
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(200, _embeddings_payload([v0, v1])))

    from services.embeddings_provider import OpenRouterEmbeddingsProvider

    out = OpenRouterEmbeddingsProvider().embed_texts(["a", "b"])
    assert out[0][0] == 1.0 and out[1][1] == 1.0, "index reordering broke"


def test_batches_by_embeddings_batch_size_preserving_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """>batch_size inputs split into multiple POSTs; overall order is preserved."""
    from config.constants.embeddings import EMBEDDING_DIMENSION

    _select(monkeypatch, batch_size="2")
    import httpx

    calls: list[int] = []
    unit = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)

    def fake_post(url: str, **kwargs: object) -> _Resp:
        inputs = kwargs["json"]["input"]  # type: ignore[index]
        calls.append(len(inputs))
        return _Resp(200, _embeddings_payload([unit for _ in inputs]))

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.embeddings_provider import OpenRouterEmbeddingsProvider

    out = OpenRouterEmbeddingsProvider().embed_texts(["a", "b", "c"])
    assert calls == [2, 1], f"expected batches of [2,1], got {calls}"
    assert len(out) == 3 and all(len(v) == EMBEDDING_DIMENSION for v in out)


# ---- Tier 2: failure contract (no network) ----------------------------------


def test_dimension_mismatch_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """FORK-K guard: if OpenRouter returns native 3072 (dimensions dropped), fail LOUD."""
    _select(monkeypatch, model="text-embedding-3-large")
    import httpx

    native_3072 = [0.5] * 3072
    monkeypatch.setattr(
        httpx, "post", lambda *_a, **_k: _Resp(200, _embeddings_payload([native_3072]))
    )

    from services.embeddings_provider import OpenRouterEmbeddingsProvider
    from services.exceptions import ProviderError

    with pytest.raises(ProviderError):
        OpenRouterEmbeddingsProvider().embed_texts(["x"])


def test_non_1536_dimensions_request_rejected_hermetically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fence 1: a non-1536 EMBEDDINGS_DIMENSIONS fails BEFORE any key/network round-trip."""
    _select(monkeypatch, dimensions="3072")

    from services.embeddings_provider import OpenRouterEmbeddingsProvider
    from services.exceptions import ProviderConfigurationError

    with pytest.raises(ProviderConfigurationError):
        OpenRouterEmbeddingsProvider().embed_texts(["x"])


def test_missing_key_raises_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting openrouter without LLM_API_KEY still builds, but raises on call (no network)."""
    _select(monkeypatch, key="")

    from services.embeddings_provider import get_embeddings_provider
    from services.exceptions import ProviderConfigurationError

    p = get_embeddings_provider()  # must NOT raise at construction
    with pytest.raises(ProviderConfigurationError):
        p.embed_texts(["x"])


def test_http_500_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _Resp(500, {}))

    from services.embeddings_provider import OpenRouterEmbeddingsProvider
    from services.exceptions import ProviderError

    with pytest.raises(ProviderError):
        OpenRouterEmbeddingsProvider().embed_texts(["x"])


def test_transport_error_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    def boom(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "post", boom)

    from services.embeddings_provider import OpenRouterEmbeddingsProvider
    from services.exceptions import ProviderError

    with pytest.raises(ProviderError):
        OpenRouterEmbeddingsProvider().embed_texts(["x"])


def test_empty_input_returns_empty_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch)
    import httpx

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not POST for empty input")

    monkeypatch.setattr(httpx, "post", boom)

    from services.embeddings_provider import OpenRouterEmbeddingsProvider

    assert OpenRouterEmbeddingsProvider().embed_texts([]) == []
