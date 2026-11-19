"""Contract + smoke tests for services/rerank_provider (R2-T1).

Covers the interface contract (both adapters), the factory default + lazy prod import, the
local provider's determinism + no-network guarantee, and the Cohere adapter's call-time
config error / response parsing / timeout-and-retry behaviour over a monkeypatched httpx.
A @pytest.mark.live smoke (SKIPPED in CI) exercises the real API once.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    """Isolate each test from settings cached under another test's monkeypatched env."""
    import config.settings as cs

    cs.get_settings.cache_clear()
    yield
    cs.get_settings.cache_clear()


def _select_cohere(monkeypatch: pytest.MonkeyPatch, *, key: str = "test-rerank-key") -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "cohere")
    monkeypatch.setenv("RERANK_API_KEY", key)
    # The default model/base URL are now HuggingFace's (R2-T1b); cohere is the explicit-config
    # swap-target skeleton, so its tests pin the Cohere model + default endpoint.
    monkeypatch.setenv("RERANK_MODEL", "rerank-v3.5")
    monkeypatch.setenv("RERANK_BASE_URL", "")
    import config.settings as cs

    cs.get_settings.cache_clear()


def _select_hf(monkeypatch: pytest.MonkeyPatch, *, token: str = "test-hf-token") -> None:
    monkeypatch.setenv("RERANK_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_API_TOKEN", token)
    monkeypatch.setenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("RERANK_BASE_URL", "https://router.huggingface.co/hf-inference/models")
    import config.settings as cs

    cs.get_settings.cache_clear()


# ---- interface contract ----------------------------------------------------


def test_both_adapters_satisfy_interface() -> None:
    """Local, Cohere AND HuggingFace adapters are RerankProvider subclasses with the method."""
    from services.rerank_provider.cohere import CohereRerankProvider
    from services.rerank_provider.huggingface import HFRerankProvider
    from services.rerank_provider.interface import RerankProvider
    from services.rerank_provider.local import LocalRerankProvider

    for cls in (LocalRerankProvider, CohereRerankProvider, HFRerankProvider):
        assert issubclass(cls, RerankProvider)
        # rerank(self, query: str, documents) -> list[tuple[int, float]]
        assert callable(cls.rerank)


# ---- factory + lazy prod import (V2) ---------------------------------------


def test_factory_returns_local_by_default() -> None:
    """get_rerank_provider() defaults to LocalRerankProvider with no env override."""
    from services.rerank_provider import LocalRerankProvider, get_rerank_provider

    assert isinstance(get_rerank_provider(), LocalRerankProvider)


def test_factory_returns_cohere_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod adapter is returned ONLY when RERANK_PROVIDER=cohere."""
    _select_cohere(monkeypatch)
    from services.rerank_provider import get_rerank_provider
    from services.rerank_provider.cohere import CohereRerankProvider

    assert isinstance(get_rerank_provider(), CohereRerankProvider)


def test_default_path_imports_no_prod_deps() -> None:
    """In a FRESH interpreter, the default factory path imports neither cohere nor httpx.

    Runs in a subprocess so the assertion is not polluted by other tests that DO import the
    cohere adapter (sys.modules is process-global).  This is the I3 deterministic-first +
    lazy-import proof: the production reranker's deps never load on the default / CI path.
    """
    script = (
        "import sys\n"
        "import services.rerank_provider as rp\n"
        "p = rp.get_rerank_provider()\n"
        "assert type(p).__name__ == 'LocalRerankProvider', type(p).__name__\n"
        "assert 'services.rerank_provider.cohere' not in sys.modules, 'cohere imported'\n"
        "assert 'services.rerank_provider.huggingface' not in sys.modules, 'huggingface imported'\n"
        "assert 'httpx' not in sys.modules, 'httpx imported on default path'\n"
        "print('OK')\n"
    )
    env = {
        **os.environ,
        "RERANK_PROVIDER": "local",
        "JWT_SECRET": "test-secret-for-services-ci",
        "DATABASE_URL": "postgresql://atlas_user:test@localhost:5432/atlas_db",
    }
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


def test_rerank_provider_imports_no_ai_agents() -> None:
    """F9 layering: the whole rerank_provider package (incl. cohere) imports ZERO ai_agents.

    Subprocess-isolated so a sibling test that already imported ai_agents cannot mask a leak.
    Guards the headline interface invariant (plain str/int only; services free of ai_agents).
    """
    script = (
        "import sys\n"
        "import services.rerank_provider\n"
        "import services.rerank_provider.cohere\n"
        "import services.rerank_provider.huggingface\n"
        "import services.rerank_provider.factory\n"
        "import services.rerank_provider.local\n"
        "leaks = [m for m in sys.modules if m.startswith('ai_agents') or m.startswith('backend')]\n"
        "assert not leaks, f'services.rerank_provider leaked {leaks}'\n"
        "print('OK')\n"
    )
    env = {
        **os.environ,
        "RERANK_PROVIDER": "local",
        "JWT_SECRET": "test-secret-for-services-ci",
        "DATABASE_URL": "postgresql://atlas_user:test@localhost:5432/atlas_db",
    }
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "OK" in proc.stdout


# ---- local provider: deterministic, no network -----------------------------


def test_local_is_deterministic_and_well_ordered() -> None:
    """Local rerank: pure function of (query, documents); desc by score; ties -> idx asc."""
    from services.rerank_provider.local import LocalRerankProvider

    p = LocalRerankProvider()
    query = "nightly hotel cap in new york 250 usd"
    docs = [
        "Economy class flights are allowed for domestic travel.",  # 0 — off-topic
        "The nightly hotel cap in New York is 250 USD per night.",  # 1 — strong overlap
        "Meal per diem allowances vary by city tier.",  # 2 — off-topic
    ]
    out_a = p.rerank(query, docs)
    out_b = p.rerank(query, docs)
    assert out_a == out_b  # deterministic
    # ordered by score DESC
    scores = [s for _, s in out_a]
    assert scores == sorted(scores, reverse=True)
    # strong-overlap doc surfaces first
    assert out_a[0][0] == 1

    # tie-break: identical docs keep ascending index order
    tie = p.rerank("alpha", ["zzz", "zzz", "zzz"])
    assert [i for i, _ in tie] == [0, 1, 2]


def test_local_empty_documents() -> None:
    from services.rerank_provider.local import LocalRerankProvider

    assert LocalRerankProvider().rerank("q", []) == []


# ---- cohere adapter (monkeypatched httpx) ----------------------------------


def test_cohere_missing_key_raises_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting cohere without a key does NOT raise at factory time, but does at call time."""
    monkeypatch.setenv("RERANK_PROVIDER", "cohere")
    monkeypatch.setenv("RERANK_API_KEY", "")
    import config.settings as cs

    cs.get_settings.cache_clear()

    from services.exceptions import ProviderConfigurationError
    from services.rerank_provider import get_rerank_provider

    provider = get_rerank_provider()  # must NOT raise
    with pytest.raises(ProviderConfigurationError):
        provider.rerank("q", ["a", "b"])


def test_cohere_parses_results_and_shapes_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 maps results -> (index, score) desc and the request payload is well-formed."""
    _select_cohere(monkeypatch)
    import httpx

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.42},
                    {"index": 1, "relevance_score": 0.07},
                ]
            }

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.rerank_provider import get_rerank_provider

    out = get_rerank_provider().rerank("find the cap", ["a", "b", "c"])
    assert out == [(2, 0.91), (0, 0.42), (1, 0.07)]  # desc by score
    assert captured["url"].endswith("/v2/rerank")
    body = captured["json"]
    assert body["model"] == "rerank-v3.5"
    assert body["query"] == "find the cap"
    assert body["documents"] == ["a", "b", "c"]
    assert body["top_n"] == 3
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_cohere_transient_500_retries_once_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent transient (500) is retried exactly once, then raises ProviderError."""
    _select_cohere(monkeypatch)
    import httpx

    calls: dict[str, int] = {"n": 0}

    class _Resp:
        status_code = 500

        @staticmethod
        def json() -> dict[str, Any]:
            return {}

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError):
        get_rerank_provider().rerank("q", ["a", "b"])
    assert calls["n"] == 2  # initial + one retry


def test_cohere_timeout_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An httpx timeout is wrapped as ProviderError (code PROVIDER_TIMEOUT)."""
    _select_cohere(monkeypatch)
    import httpx

    def fake_post(url: str, **kwargs: Any) -> Any:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError) as exc:
        get_rerank_provider().rerank("q", ["a"])
    assert exc.value.code == "PROVIDER_TIMEOUT"


def test_cohere_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-range index in the response is a ProviderError (pipeline then fails safe)."""
    _select_cohere(monkeypatch)
    import httpx

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"results": [{"index": 99, "relevance_score": 1.0}]}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp())

    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError):
        get_rerank_provider().rerank("q", ["a", "b"])


def test_cohere_duplicate_index_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated index violates the unique-index contract -> ProviderError (then fail-safe)."""
    _select_cohere(monkeypatch)
    import httpx

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.5},  # duplicate index
                ]
            }

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp())

    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError):
        get_rerank_provider().rerank("q", ["a", "b"])


# ---- huggingface adapter (monkeypatched httpx) -----------------------------

# Confirmed live shape: single-wrapped list of per-doc {label, score} dicts, in input order.
_HF_OK: list[list[dict[str, Any]]] = [
    [
        {"label": "LABEL_0", "score": 0.02},
        {"label": "LABEL_0", "score": 0.97},
        {"label": "LABEL_0", "score": 0.10},
    ]
]


class _HFResp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


def test_factory_returns_hf_when_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HF adapter is returned ONLY when RERANK_PROVIDER=huggingface."""
    _select_hf(monkeypatch)
    from services.rerank_provider import get_rerank_provider
    from services.rerank_provider.huggingface import HFRerankProvider

    assert isinstance(get_rerank_provider(), HFRerankProvider)


def test_hf_missing_token_raises_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting huggingface without a token does NOT raise at factory time, but does at call."""
    monkeypatch.setenv("RERANK_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_API_TOKEN", "")
    import config.settings as cs

    cs.get_settings.cache_clear()

    from services.exceptions import ProviderConfigurationError
    from services.rerank_provider import get_rerank_provider

    provider = get_rerank_provider()  # must NOT raise
    with pytest.raises(ProviderConfigurationError):
        provider.rerank("q", ["a", "b"])


def test_hf_parses_confirmed_shape_and_shapes_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 maps the [[{label,score}]] body -> (index, score) desc; request is a pair batch."""
    _select_hf(monkeypatch)
    import httpx

    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _HFResp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _HFResp(200, _HF_OK)

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.rerank_provider import get_rerank_provider

    out = get_rerank_provider().rerank("expense alcohol", ["a", "b", "c"])
    assert out == [(1, 0.97), (2, 0.10), (0, 0.02)]  # desc by score; ties -> idx asc
    assert captured["url"].endswith("/BAAI/bge-reranker-v2-m3")  # {base}/{model}
    body = captured["json"]
    assert body["inputs"] == [
        {"text": "expense alcohol", "text_pair": "a"},
        {"text": "expense alcohol", "text_pair": "b"},
        {"text": "expense alcohol", "text_pair": "c"},
    ]
    assert body["parameters"]["function_to_apply"] == "sigmoid"
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_hf_parses_flat_float_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: a flat list[float] (TEI text-ranking shape) is accepted, zipped by index."""
    _select_hf(monkeypatch)
    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _HFResp(200, [0.2, 0.9, 0.5]))
    from services.rerank_provider import get_rerank_provider

    assert get_rerank_provider().rerank("q", ["a", "b", "c"]) == [(1, 0.9), (2, 0.5), (0, 0.2)]


def test_hf_parses_index_dict_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: a list of {index, score} dicts (TEI rerank shape) is accepted."""
    _select_hf(monkeypatch)
    import httpx

    body = [{"index": 2, "score": 0.8}, {"index": 0, "score": 0.1}, {"index": 1, "score": 0.5}]
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _HFResp(200, body))
    from services.rerank_provider import get_rerank_provider

    assert get_rerank_provider().rerank("q", ["a", "b", "c"]) == [(2, 0.8), (1, 0.5), (0, 0.1)]


def test_hf_429_raises_immediately_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 429 (rate limit) raises immediately — NO retry (pipeline owns the RRF fallback)."""
    _select_hf(monkeypatch)
    import httpx

    calls = {"n": 0}

    def fake_post(url: str, **kwargs: Any) -> _HFResp:
        calls["n"] += 1
        return _HFResp(429, {})

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError) as exc:
        get_rerank_provider().rerank("q", ["a", "b"])
    assert exc.value.code == "PROVIDER_HTTP_429"
    assert calls["n"] == 1  # no retry on a rate limit


def test_hf_503_on_query_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 503 (cold model) on a LIVE query raises immediately — never blocks ~30s on load."""
    _select_hf(monkeypatch)
    import httpx

    calls = {"n": 0}

    def fake_post(url: str, **kwargs: Any) -> _HFResp:
        calls["n"] += 1
        return _HFResp(503, {})

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError) as exc:
        get_rerank_provider().rerank("q", ["a"])
    assert exc.value.code == "PROVIDER_HTTP_503"
    assert calls["n"] == 1  # a live query never waits on a cold model


def test_hf_timeout_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network timeout is retried exactly once, then raises ProviderError(PROVIDER_TIMEOUT)."""
    _select_hf(monkeypatch)
    import httpx

    calls = {"n": 0}

    def fake_post(url: str, **kwargs: Any) -> Any:
        calls["n"] += 1
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError) as exc:
        get_rerank_provider().rerank("q", ["a"])
    assert exc.value.code == "PROVIDER_TIMEOUT"
    assert calls["n"] == 2  # initial + one retry on a transient network error


def test_hf_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inner length that disagrees with n is a ProviderError (pipeline then fails safe)."""
    _select_hf(monkeypatch)
    import httpx

    bad = [[{"label": "LABEL_0", "score": 0.5}, {"label": "LABEL_0", "score": 0.6}]]  # len 2 != n 3
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _HFResp(200, bad))
    from services.exceptions import ProviderError
    from services.rerank_provider import get_rerank_provider

    with pytest.raises(ProviderError):
        get_rerank_provider().rerank("q", ["a", "b", "c"])


def test_hf_warmup_cold_then_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup(): a 503 on the first ping sleeps then retries once and succeeds; never raises."""
    _select_hf(monkeypatch)
    import httpx

    import services.rerank_provider.huggingface as hf

    monkeypatch.setattr(hf, "_WARMUP_BACKOFF_SECONDS", 0.0)  # no real sleep in the test
    seq = [503, 200]
    calls = {"n": 0}

    def fake_post(url: str, **kwargs: Any) -> _HFResp:
        status = seq[calls["n"]]
        calls["n"] += 1
        return _HFResp(status, _HF_OK if status == 200 else {})

    monkeypatch.setattr(httpx, "post", fake_post)
    from services.rerank_provider import get_rerank_provider

    get_rerank_provider().warmup()  # must NOT raise
    assert calls["n"] == 2  # 503 then retry -> 200


def test_hf_warmup_persistent_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup() swallows a persistent 503 — logs 'will retry on first query', never raises."""
    _select_hf(monkeypatch)
    import httpx

    import services.rerank_provider.huggingface as hf

    monkeypatch.setattr(hf, "_WARMUP_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _HFResp(503, {}))
    from services.rerank_provider import get_rerank_provider

    get_rerank_provider().warmup()  # no raise


def test_hf_warmup_missing_token_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup() with no token logs + returns (no network, no raise)."""
    monkeypatch.setenv("RERANK_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_API_TOKEN", "")
    import config.settings as cs

    cs.get_settings.cache_clear()
    from services.rerank_provider.huggingface import HFRerankProvider

    HFRerankProvider().warmup()  # no raise


@pytest.mark.live
def test_hf_live_smoke() -> None:
    """One real HF rerank over ~5 candidates. Skipped unless huggingface is selected + keyed."""
    if os.environ.get("RERANK_PROVIDER") != "huggingface" or not os.environ.get("HF_API_TOKEN"):
        pytest.skip("live smoke needs RERANK_PROVIDER=huggingface + HF_API_TOKEN set")

    import config.settings as cs

    cs.get_settings.cache_clear()
    from services.rerank_provider import get_rerank_provider

    provider = get_rerank_provider()
    if hasattr(provider, "warmup"):
        provider.warmup()  # excluded from any timing
    query = "What is the nightly hotel cap in New York?"
    docs = [
        "Economy class is required for domestic flights under four hours.",
        "The nightly lodging cap in New York City is 350 USD.",
        "Meal per diem is 75 USD per day in tier-1 cities.",
        "Receipts are required for any expense above 25 USD.",
        "Ride-share is reimbursable for airport transfers.",
    ]
    out = provider.rerank(query, docs)
    print(f"\n[live] hf rerank -> {out}")
    assert len(out) == len(docs)
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True)
    assert out[0][0] == 1  # the NYC lodging doc is the most relevant
    cs.get_settings.cache_clear()


@pytest.mark.live
def test_cohere_live_smoke() -> None:
    """One real Cohere rerank over ~5 candidates. Skipped unless cohere is selected + keyed."""
    if os.environ.get("RERANK_PROVIDER") != "cohere" or not os.environ.get("RERANK_API_KEY"):
        pytest.skip("live smoke needs RERANK_PROVIDER=cohere + RERANK_API_KEY in the environment")

    import config.settings as cs

    cs.get_settings.cache_clear()
    from services.rerank_provider import get_rerank_provider

    query = "What is the nightly hotel cap in New York?"
    docs = [
        "Economy class is required for domestic flights under four hours.",
        "The nightly lodging cap in New York City is 350 USD.",
        "Meal per diem is 75 USD per day in tier-1 cities.",
        "Receipts are required for any expense above 25 USD.",
        "Ride-share is reimbursable for airport transfers.",
    ]
    out = get_rerank_provider().rerank(query, docs)
    print(f"\n[live] cohere rerank -> {out}")
    assert len(out) == len(docs)
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True)
    cs.get_settings.cache_clear()


@pytest.mark.live
def test_novita_live_smoke() -> None:
    """One real Novita rerank over ~5 candidates. Skipped unless novita is selected + keyed."""
    if os.environ.get("RERANK_PROVIDER") != "novita" or not os.environ.get("RERANK_API_KEY"):
        pytest.skip("live smoke needs RERANK_PROVIDER=novita + RERANK_API_KEY in the environment")

    import config.settings as cs

    cs.get_settings.cache_clear()
    from services.rerank_provider import get_rerank_provider

    query = "What is the nightly hotel cap in New York?"
    docs = [
        "Economy class is required for domestic flights under four hours.",
        "The nightly lodging cap in New York City is 350 USD.",
        "Meal per diem is 75 USD per day in tier-1 cities.",
        "Receipts are required for any expense above 25 USD.",
        "Ride-share is reimbursable for airport transfers.",
    ]
    out = get_rerank_provider().rerank(query, docs)
    print(f"\n[live] novita rerank -> {out}")
    assert len(out) == len(docs)
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True)
    assert out[0][0] == 1  # the NYC lodging doc is the most relevant
    cs.get_settings.cache_clear()
