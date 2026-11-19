"""Real (OpenRouter) embeddings adapter — OpenAI-compatible /embeddings via httpx.

Selected when EMBEDDINGS_PROVIDER=openrouter (default stays local). Mirrors the LLM-layer
precedent (services/llm_provider/openrouter.py): a SEPARATE adapter class over httpx (an
existing dependency — no vendor SDK), POSTing to ``{base}/embeddings``.

Routes the OpenAI embedding models through the SAME OpenRouter account the LLM layer already
uses — OpenRouter is one account spanning chat + embeddings, so the adapter reads the generic
``LLM_API_KEY`` + ``LLM_BASE_URL`` (FR-A6 swappability; no second key var) for the connection,
and the ``EMBEDDINGS_*`` knobs for the embedding request itself (``EMBEDDINGS_MODEL``,
``EMBEDDINGS_DIMENSIONS``, ``EMBEDDINGS_BATCH_SIZE``).

OpenRouter requires the provider/model format, so the bare ``EMBEDDINGS_MODEL``
(text-embedding-3-small / -large) is mapped to the ``openai/`` route on the wire. Honours the
EmbeddingsProvider contract exactly like real.py: requests ``dimensions=1536`` (Fence 1),
asserts the returned width (a dropped ``dimensions`` param surfaces as a loud ProviderError —
never a silent client-side truncation), and L2-renormalizes the (possibly Matryoshka-truncated)
vector. Per-input embeddings are independent of batch composition, so batching by
``EMBEDDINGS_BATCH_SIZE`` is result-identical and only bounds request size.
"""

from __future__ import annotations

import logging
from typing import Any

from config.constants.embeddings import EMBEDDING_DIMENSION
from config.settings import get_settings
from services.embeddings_provider._normalize import l2_normalize
from services.embeddings_provider.interface import EmbeddingsProvider
from services.exceptions import ProviderConfigurationError, ProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterEmbeddingsProvider(EmbeddingsProvider):
    """OpenAI embedding models via OpenRouter (/embeddings over httpx)."""

    # ---- config helpers -----------------------------------------------------

    def _api_key(self, settings: Any) -> str:
        # OpenRouter is ONE account spanning chat + embeddings; reuse the SAME token the LLM
        # layer already owns (LLM_API_KEY) rather than introducing a second key var.
        key: str = settings.LLM_API_KEY
        if not key:
            raise ProviderConfigurationError(
                "LLM_API_KEY is required when EMBEDDINGS_PROVIDER=openrouter"
            )
        return key

    def _base_url(self, settings: Any) -> str:
        return str(settings.LLM_BASE_URL or _DEFAULT_BASE_URL).rstrip("/")

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Atlas",  # optional OpenRouter app attribution
        }

    @staticmethod
    def _model_name(settings: Any) -> str:
        # OpenRouter needs the provider/model format; the bare EMBEDDINGS_MODEL
        # (text-embedding-3-small / -large) maps to the openai/ route. Idempotent if already
        # prefixed, so a future direct-openai run with a bare name and an openrouter run agree.
        model = str(settings.EMBEDDINGS_MODEL)
        return model if "/" in model else f"openai/{model}"

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps import-time hermetic
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderUnavailableError("httpx not installed; run: pip install httpx") from exc
        return httpx

    # ---- transport (raises ProviderError on failure) ------------------------

    def _post_batch(
        self,
        httpx: Any,
        *,
        base_url: str,
        headers: dict[str, str],
        model: str,
        inputs: list[str],
        dimensions: int,
        timeout: float,
    ) -> list[list[float]]:
        """One POST for a batch; returns index-ordered raw vectors. Raises ProviderError."""
        payload: dict[str, Any] = {
            "model": model,
            "input": inputs,
            "dimensions": dimensions,
            "encoding_format": "float",
        }
        try:
            resp = httpx.post(
                f"{base_url}/embeddings",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"OpenRouter embeddings timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"OpenRouter unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter embeddings error: HTTP {resp.status_code}",
                code=f"PROVIDER_HTTP_{resp.status_code}",
            )

        data: dict[str, Any] = resp.json()
        items = data.get("data") or []
        if len(items) != len(inputs):
            raise ProviderError(
                f"OpenRouter returned {len(items)} embeddings for {len(inputs)} inputs"
            )
        # OpenAI-compatible: each item carries its input `index`; reorder to match `inputs`.
        ordered = sorted(items, key=lambda it: int(it.get("index", 0)))
        return [list(it["embedding"]) for it in ordered]

    # ---- interface ----------------------------------------------------------

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        # Fence 1 (1536-d only): identical guard to real.py, checked FIRST so a config error
        # surfaces without a key/network round-trip (keeps the guard hermetic).
        if settings.EMBEDDINGS_DIMENSIONS != EMBEDDING_DIMENSION:
            raise ProviderConfigurationError(
                f"EMBEDDINGS_DIMENSIONS={settings.EMBEDDINGS_DIMENSIONS} must equal the schema "
                f"dimension {EMBEDDING_DIMENSION} (1536-d only — Fence 1)."
            )
        if not texts:
            return []

        api_key = self._api_key(settings)
        base_url = self._base_url(settings)
        headers = self._headers(api_key)
        model = self._model_name(settings)
        dimensions = int(settings.EMBEDDINGS_DIMENSIONS)
        batch_size = max(1, int(settings.EMBEDDINGS_BATCH_SIZE))
        timeout = float(settings.LLM_TIMEOUT_SECONDS)
        httpx = self._httpx()

        out: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            raw = self._post_batch(
                httpx,
                base_url=base_url,
                headers=headers,
                model=model,
                inputs=texts[start : start + batch_size],
                dimensions=dimensions,
                timeout=timeout,
            )
            for vec in raw:
                if len(vec) != EMBEDDING_DIMENSION:
                    raise ProviderError(
                        f"Embeddings dimension mismatch: expected {EMBEDDING_DIMENSION}, "
                        f"got {len(vec)} (OpenRouter did not forward dimensions={dimensions})"
                    )
                # Renormalize after a (possible) Matryoshka truncation so cosine distance is valid.
                out.append(l2_normalize(vec))
        return out
