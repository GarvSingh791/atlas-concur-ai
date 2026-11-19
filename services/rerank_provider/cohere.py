"""Swap-target rerank adapter — Cohere Rerank API over httpx (R2-T1; superseded by R2-T1b).

SWAP TARGET / SKELETON: the production reranker is now HuggingFace ``bge-reranker-v2-m3``
(``services/rerank_provider/huggingface.py``, ``RERANK_PROVIDER=huggingface``).  This Cohere
adapter is kept INTACT as the documented one-file swap: set ``RERANK_PROVIDER=cohere``,
``RERANK_API_KEY=...`` and ``RERANK_MODEL=rerank-v3.5`` (and ``RERANK_BASE_URL=`` to use the
Cohere default) to route to a dedicated Cohere endpoint if sub-200 ms p50 is ever required.
The ``RerankProvider`` interface makes this a config-only change — no pipeline edits.

Selected when ``RERANK_PROVIDER=cohere`` (default stays ``local``).  Cohere exposes a hosted
cross-encoder reranker, so we POST to ``/v2/rerank`` with httpx (an EXISTING dependency)
rather than adding the vendor SDK or any torch / model-weight dependency — keeping the
production image minimal (fence F7).  Mirrors the OpenRouter adapter's failure contract: on
persistent failure it RAISES a ``ProviderError`` subclass (timeout / transport / HTTP), never
a partial or sentinel result.  The two-stage pipeline (``ai_agents/rag/rerank.py``) catches
that and degrades to the R1 RRF order — fail-SAFE, never fail-abstain.

Reads ``RERANK_*``: ``RERANK_API_KEY``, ``RERANK_MODEL``, ``RERANK_BASE_URL``,
``RERANK_TIMEOUT_SECONDS``.  ONE batched call over all (already-capped) candidates, ONE retry
on a transient failure.  All deps are imported lazily so the default / CI path never loads
this module.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from config.settings import get_settings
from services.exceptions import (
    ProviderConfigurationError,
    ProviderError,
    ProviderUnavailableError,
)
from services.rerank_provider.interface import RerankProvider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.cohere.com"
# Statuses worth one retry on the same call (rate / transient / server-side).
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class CohereRerankProvider(RerankProvider):
    """Cohere Rerank (hosted cross-encoder) over httpx; raises ProviderError on failure."""

    def _api_key(self, settings: Any) -> str:
        key: str = settings.RERANK_API_KEY
        if not key:
            raise ProviderConfigurationError(
                "RERANK_API_KEY is required when RERANK_PROVIDER=cohere"
            )
        return key

    def _base_url(self, settings: Any) -> str:
        return str(settings.RERANK_BASE_URL or _DEFAULT_BASE_URL).rstrip("/")

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps import-time hermetic
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderUnavailableError("httpx not installed; run: pip install httpx") from exc
        return httpx

    @staticmethod
    def _is_transient(exc: ProviderError) -> bool:
        code = exc.code
        if code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"}:
            return True
        if not code.startswith("PROVIDER_HTTP_"):
            return False
        return int(code.rsplit("_", 1)[-1]) in _TRANSIENT_STATUS

    def _post_once(
        self,
        httpx: Any,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """One POST. Raises ProviderError (tagged with .code) on any failure."""
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"Cohere rerank timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"Cohere unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Cohere rerank error: HTTP {resp.status_code}",
                code=f"PROVIDER_HTTP_{resp.status_code}",
            )
        data: dict[str, Any] = resp.json()
        return data

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        docs = list(documents)
        if not docs:
            return []
        settings = get_settings()
        api_key = self._api_key(settings)
        url = f"{self._base_url(settings)}/v2/rerank"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": str(settings.RERANK_MODEL),
            "query": query,
            "documents": docs,
            "top_n": len(docs),  # full ranking — the pipeline owns the final top-k cut
        }
        timeout = float(settings.RERANK_TIMEOUT_SECONDS)
        httpx = self._httpx()

        last_exc: ProviderError | None = None
        for attempt in range(2):  # initial + one retry on transient
            try:
                data = self._post_once(
                    httpx, url=url, headers=headers, payload=payload, timeout=timeout
                )
                return self._parse_results(data, len(docs))
            except ProviderError as exc:
                last_exc = exc
                logger.warning("Cohere rerank failed (%s) try %d/2", exc.code, attempt + 1)
                if not self._is_transient(exc):
                    break  # auth / model error — retrying won't help

        if last_exc is None:  # pragma: no cover - a failure path always sets last_exc
            last_exc = ProviderError("Cohere rerank failed")
        raise last_exc

    @staticmethod
    def _parse_results(data: dict[str, Any], n: int) -> list[tuple[int, float]]:
        """Map Cohere ``results`` -> ``[(index, relevance_score)]`` desc; validate bounds."""
        results = data.get("results")
        if not isinstance(results, list):
            raise ProviderError(
                "Cohere rerank: malformed response (no results array)",
                code="PROVIDER_BAD_RESPONSE",
            )
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in results:
            idx = row.get("index") if isinstance(row, dict) else None
            score = row.get("relevance_score") if isinstance(row, dict) else None
            if (
                not isinstance(idx, int)
                or not (0 <= idx < n)
                or idx in seen  # contract: indices are unique (no duplicate permutation rows)
                or not isinstance(score, int | float)
            ):
                raise ProviderError(
                    f"Cohere rerank: bad result row {row!r}", code="PROVIDER_BAD_RESPONSE"
                )
            seen.add(idx)
            out.append((idx, float(score)))
        # Cohere returns results relevance-desc already; enforce deterministically (ties->idx asc).
        out.sort(key=lambda pair: (-pair[1], pair[0]))
        return out
