"""Smoke tests for services/embeddings_provider."""

import math
import subprocess
import sys

import pytest


def test_factory_returns_local_by_default() -> None:
    from services.embeddings_provider import LocalEmbeddingsProvider, get_embeddings_provider

    p = get_embeddings_provider()
    assert isinstance(p, LocalEmbeddingsProvider)


def test_embed_dimension_and_normalized() -> None:
    """embed() returns a 1536-dim unit-normalized vector."""
    from config.constants.embeddings import EMBEDDING_DIMENSION
    from services.embeddings_provider import embed

    v = embed("hotel nightly cap")
    assert len(v) == EMBEDDING_DIMENSION
    norm = math.sqrt(sum(x**2 for x in v))
    assert abs(norm - 1.0) < 1e-6, f"Not normalized: norm={norm}"


def test_embed_is_deterministic() -> None:
    """Repeated calls and equal inputs produce identical vectors."""
    from services.embeddings_provider import embed

    text = "hotel nightly cap"
    v1 = embed(text)
    v2 = embed(text)
    v3 = embed("hotel nightly cap")
    assert v1 == v2, "embed() is not deterministic across repeated calls"
    assert v1 == v3, "embed() differs for equal inputs"


def test_embed_is_stable_across_processes() -> None:
    """embed() must be stable across separate Python processes.

    Python's builtin hash() is salted per-process (PYTHONHASHSEED), so the
    previous implementation produced different vectors in each interpreter.
    The blake2b-based hashing must yield byte-identical vectors regardless of
    PYTHONHASHSEED. Run two subprocesses with different hash seeds and compare.
    """
    import os

    # Import from .local directly to avoid the package __init__ pulling in the
    # provider factory / settings, keeping the subprocess fully hermetic.
    snippet = (
        "from services.embeddings_provider.local import embed;"
        "print(embed('hotel nightly cap policy'))"
    )
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

    def _run(seed: str) -> str:
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            check=True,
            env=env,
            cwd=repo_root,
        )
        return out.stdout.strip()

    assert _run("0") == _run("1"), "embed() vector changed across PYTHONHASHSEED values"


def test_embed_texts_batch() -> None:
    """embed_texts returns one vector per text."""
    from config.constants.embeddings import EMBEDDING_DIMENSION
    from services.embeddings_provider import get_embeddings_provider

    p = get_embeddings_provider()
    vecs = p.embed_texts(["hotel nightly cap", "air cabin class"])
    assert len(vecs) == 2
    assert all(len(v) == EMBEDDING_DIMENSION for v in vecs)


def test_seed_shim_parity() -> None:
    """Local adapter vectors match database seed shim (same function, clean swap for P4-T5)."""
    from database.seeds._local_embedder import embed as seed_embed
    from services.embeddings_provider import embed

    text = "hotel nightly cap"
    v_provider = embed(text)
    v_seed = seed_embed(text)
    assert v_provider == v_seed, "Parity failure: seed shim and provider vector differ"


def test_real_mode_raises_provider_error_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import config.settings as cs

    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "")
    cs.get_settings.cache_clear()

    from services.embeddings_provider import get_embeddings_provider
    from services.exceptions import ProviderError

    p = get_embeddings_provider()  # must NOT raise
    with pytest.raises(ProviderError):
        p.embed_texts(["test"])

    cs.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# R4-T3 FORK-G: routing the corpus through the factory under EMBEDDINGS_PROVIDER=local
# is BYTE-IDENTICAL to the legacy ``embed`` — the hinge that keeps `make eval-db`
# R3-identical after FORK-G swaps ``embed(x)`` for ``provider.embed_texts([x])[0]``.
# --------------------------------------------------------------------------- #
def test_local_provider_embed_texts_byte_identical_to_legacy_embed() -> None:
    """FORK-G hinge: get_embeddings_provider() (local) embed_texts([x])[0] == embed(x)."""
    from services.embeddings_provider import embed, get_embeddings_provider

    provider = get_embeddings_provider()
    samples = [
        "Lodging nightly cap for a New York tier-one city",
        "is alcohol reimbursable at team dinners",
        "how do I file an expense report with a missing receipt",
        "",
    ]
    for s in samples:
        assert provider.embed_texts([s])[0] == embed(s), f"FORK-G byte-identity broke for {s!r}"


# --------------------------------------------------------------------------- #
# R4-T3 FORK-F: the real (OpenAI) provider passes dimensions=1536 (Matryoshka) and
# L2-renormalizes the (possibly truncated) vector, and rejects a non-1536 request (Fence 1).
# --------------------------------------------------------------------------- #
class _FakeEmbItem:
    def __init__(self, embedding: list[float], index: int) -> None:
        self.embedding = embedding
        self.index = index


class _FakeEmbResponse:
    def __init__(self, data: list["_FakeEmbItem"]) -> None:
        self.data = data


def _install_fake_openai(
    monkeypatch: pytest.MonkeyPatch, recorder: dict, raw_vectors: list[list[float]]
) -> None:
    """Inject a fake ``openai`` module into ``sys.modules`` so the test is HERMETIC.

    The OpenAI SDK is intentionally NOT a declared dependency (it is imported lazily inside
    real.py:_get_client), and bare ``pytest`` in CI must pass without it — so we must NOT
    ``import openai`` here.  ``setitem(sys.modules, "openai", fake)`` makes the lazy import
    inside the provider resolve to our fake regardless of whether the real SDK is installed.
    """
    import sys
    import types

    class _FakeEmbeddings:
        def create(self, *, model: str, input: list[str], dimensions: int) -> "_FakeEmbResponse":
            recorder["model"] = model
            recorder["dimensions"] = dimensions
            recorder["n_inputs"] = len(input)
            return _FakeEmbResponse(
                [_FakeEmbItem(list(raw_vectors[i]), i) for i in range(len(input))]
            )

    class _FakeClient:
        def __init__(self, api_key: str | None = None, **_kw: object) -> None:
            self.embeddings = _FakeEmbeddings()

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)


def test_real_provider_passes_dimensions_and_l2_renormalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORK-F: dimensions=1536 reaches the API and a non-unit returned vector is renormalized."""
    import config.settings as cs
    from config.constants.embeddings import EMBEDDING_DIMENSION

    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "text-embedding-3-large")
    cs.get_settings.cache_clear()

    # A deliberately NON-unit vector (L2 norm 2.0) to prove renormalization happens.
    raw = [2.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
    recorder: dict = {}
    _install_fake_openai(monkeypatch, recorder, [raw])

    from services.embeddings_provider.real import RealEmbeddingsProvider

    out = RealEmbeddingsProvider().embed_texts(["hotel nightly cap"])
    assert recorder["model"] == "text-embedding-3-large"
    assert recorder["dimensions"] == EMBEDDING_DIMENSION, "must request 1536-d (Fence 1)"
    assert len(out) == 1 and len(out[0]) == EMBEDDING_DIMENSION
    norm = math.sqrt(sum(x * x for x in out[0]))
    assert abs(norm - 1.0) < 1e-9, f"real provider did not L2-renormalize (norm={norm})"
    assert abs(out[0][0] - 1.0) < 1e-9, "renormalized leading component should be 2.0/2.0 = 1.0"
    cs.get_settings.cache_clear()


def test_real_provider_rejects_non_1536_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """FORK-F / Fence 1: a non-1536 dimensions request fails loud (no wrong-width insert).

    The guard runs BEFORE _get_client(), so this is hermetic — it raises a config error
    without needing the openai SDK or a key.
    """
    import config.settings as cs

    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EMBEDDINGS_DIMENSIONS", "3072")  # would break the 1536-d schema
    cs.get_settings.cache_clear()

    from services.embeddings_provider.real import RealEmbeddingsProvider
    from services.exceptions import ProviderConfigurationError

    with pytest.raises(ProviderConfigurationError):
        RealEmbeddingsProvider().embed_texts(["x"])
    cs.get_settings.cache_clear()


def test_real_provider_rejects_zero_norm_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """FORK-F: a zero-norm vector from the API fails loud (undefined cosine; not HNSW-indexable)."""
    import config.settings as cs
    from config.constants.embeddings import EMBEDDING_DIMENSION

    monkeypatch.setenv("EMBEDDINGS_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "test-key-not-real")
    cs.get_settings.cache_clear()

    recorder: dict = {}
    _install_fake_openai(monkeypatch, recorder, [[0.0] * EMBEDDING_DIMENSION])

    from services.embeddings_provider.real import RealEmbeddingsProvider
    from services.exceptions import ProviderError

    with pytest.raises(ProviderError):
        RealEmbeddingsProvider().embed_texts(["x"])
    cs.get_settings.cache_clear()
