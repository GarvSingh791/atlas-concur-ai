"""Production rerank adapter — Novita-hosted bge-reranker-v2-m3 over httpx (R4-T4 swap).

Selected when ``RERANK_PROVIDER=novita`` (default stays ``local``).  Drop-in replacement for the
HuggingFace serverless reranker after HF's FREE Inference endpoint proved unreliable under the
live matrix's sustained load (HTTP 502 + persistent 60s timeouts).  Novita serves the SAME
cross-encoder (``baai/bge-reranker-v2-m3``) behind a Cohere/Jina-style rerank API, so this mirrors
``cohere.py``: ONE batched POST to ``{base}/rerank`` over httpx (an EXISTING dependency — no
vendor SDK), Bearer auth, ``{model, query, documents, top_n}`` -> ``{results: [{index,
relevance_score}, ...]}``.

Reads the generic ``RERANK_*`` settings (FR-A6 swappability): ``RERANK_API_KEY`` (the Novita
token), ``RERANK_MODEL`` (``baai/bge-reranker-v2-m3``), ``RERANK_BASE_URL``
(``https://api.novita.ai/openai`` when blank), ``RERANK_TIMEOUT_SECONDS``.  Failure contract
mirrors cohere.py / huggingface.py: on persistent failure RAISE a ``ProviderError`` subclass
(timeout / transport / HTTP / bad-response) — never a partial/sentinel; the two-stage pipeline
(``ai_agents/rag/rerank.py``) catches it and degrades to the R1 RRF order (fail-SAFE, never
fail-abstain).  ONE retry on a transient (timeout / 5xx / 429).  All deps are imported lazily so
the default / CI path never loads this module.  Messages are SECRET-FREE (the token is never
embedded in any message/log).
"""

from __future__ import annotations

import logging
import time
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

_DEFAULT_BASE_URL = "https://api.novita.ai/openai"
# Statuses worth a retry on the same call (rate / transient / server-side).
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# Rate-limit handling: Novita enforces an RPM cap and returns HTTP 429 on a burst (the eval
# matrix fires ~155 sequential reranks/cell).  Per Novita's guidance we EXPONENTIALLY BACK OFF
# on 429/transient rather than retry instantly — this self-throttles the burst to the account's
# sustainable rate.  Bounded so a genuine outage still fails fast to the RRF fallback.
_MAX_ATTEMPTS = 7  # initial + 6 backed-off retries
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 16.0


class NovitaRerankProvider(RerankProvider):
    """Novita bge-reranker-v2-m3 (hosted cross-encoder) over httpx; raises ProviderError."""

    def _api_key(self, settings: Any) -> str:
        key: str = settings.RERANK_API_KEY
        if not key:
            raise ProviderConfigurationError(
                "RERANK_API_KEY is required when RERANK_PROVIDER=novita"
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
        """One POST. Raises ProviderError (tagged with .code) on any failure. No secrets logged."""
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"Novita rerank timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"Novita unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Novita rerank error: HTTP {resp.status_code}",
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
        url = f"{self._base_url(settings)}/rerank"
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
        for attempt in range(_MAX_ATTEMPTS):
            try:
                data = self._post_once(
                    httpx, url=url, headers=headers, payload=payload, timeout=timeout
                )
                return self._parse_results(data, len(docs))
            except ProviderError as exc:
                last_exc = exc
                # Non-transient (auth / model / bad-response) or out of attempts -> give up now;
                # the two-stage pipeline degrades to the R1 RRF order (fail-SAFE).
                if not self._is_transient(exc) or attempt == _MAX_ATTEMPTS - 1:
                    logger.warning(
                        "Novita rerank failed (%s) try %d/%d", exc.code, attempt + 1, _MAX_ATTEMPTS
                    )
                    break
                backoff = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**attempt))
                logger.warning(
                    "Novita rerank failed (%s) try %d/%d; backing off %.1fs",
                    exc.code,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    backoff,
                )
                time.sleep(backoff)

        if last_exc is None:  # pragma: no cover - a failure path always sets last_exc
            last_exc = ProviderError("Novita rerank failed")
        raise last_exc

    @staticmethod
    def _parse_results(data: dict[str, Any], n: int) -> list[tuple[int, float]]:
        """Map Novita ``results`` -> ``[(index, score)]`` desc; validate bounds.

        Defensive on the score field name (``relevance_score`` is the Cohere/Jina convention;
        ``score`` is accepted as a fallback) so an endpoint-shape variant degrades to a clean
        ProviderError, never a silent mis-rank.
        """
        results = data.get("results")
        if not isinstance(results, list):
            raise ProviderError(
                "Novita rerank: malformed response (no results array)",
                code="PROVIDER_BAD_RESPONSE",
            )
        out: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in results:
            idx = row.get("index") if isinstance(row, dict) else None
            score = row.get("relevance_score", row.get("score")) if isinstance(row, dict) else None
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or not (0 <= idx < n)
                or idx in seen  # contract: indices are unique (no duplicate permutation rows)
                or not isinstance(score, int | float)
                or isinstance(score, bool)
            ):
                raise ProviderError(
                    f"Novita rerank: bad result row {row!r}", code="PROVIDER_BAD_RESPONSE"
                )
            seen.add(idx)
            out.append((idx, float(score)))
        # Enforce deterministic order (score desc; ties -> idx asc) regardless of server order.
        out.sort(key=lambda pair: (-pair[1], pair[0]))
        return out
