"""Production rerank adapter — HuggingFace bge-reranker-v2-m3 over httpx (R2-T1b).

Selected when ``RERANK_PROVIDER=huggingface`` (default stays ``local``).  Replaces Cohere as
the production reranker: ``BAAI/bge-reranker-v2-m3`` on the FREE HuggingFace serverless
Inference API (``router.huggingface.co/hf-inference``), called over httpx — NO
``huggingface_hub`` SDK, transformers or torch (fence F7b), so the image stays minimal.

Wire format (EMPIRICALLY CONFIRMED against the live endpoint — the model is served as a
sentence-pair TEXT-CLASSIFICATION pipeline, not TEI text-ranking):

    POST {base}/{model}
    Authorization: Bearer {HF_API_TOKEN}
    body: {"inputs": [{"text": query, "text_pair": doc0}, ...],
           "parameters": {"function_to_apply": "sigmoid"}}
    -> 200: [[{"label": "LABEL_0", "score": <prob in [0,1]>}, ...]]   # input order, single-wrapped

We send one ``[query, doc]`` pair per candidate in ONE batched call (<= ``candidate_cap``),
read each doc's sigmoid relevance probability, and return ``(index, score)`` sorted DESC
(ties -> idx asc).  ``_extract_scores`` is defensive: the single-wrapped list-of-label-dicts
is the primary shape, with a flat ``list[float]`` and a list of ``{index, score}`` dicts as
fallbacks should the endpoint shape change.

Failure contract (mirrors ``cohere.py``; the two-stage pipeline owns the fail-SAFE fallback to
the RRF order — NEVER fail-abstain):
  - timeout / transport error  -> ``ProviderError``, ONE retry.
  - HTTP 429 (rate limit)      -> ``ProviderError`` IMMEDIATELY (no retry; pipeline -> RRF).
  - HTTP 503 (cold model) on a LIVE query -> ``ProviderError`` IMMEDIATELY (never block a real
    query ~30s waiting for the model to load; the fail-safe handles it).
  - HTTP 503 on ``warmup()``   -> sleep + ONE retry (the ONLY place we wait on a cold model).
  - malformed / out-of-range   -> ``ProviderError``.
Messages are SECRET-FREE (the token is never embedded in any message/log).  All deps are
imported lazily so the default / CI path never loads this module.
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

_DEFAULT_BASE_URL = "https://router.huggingface.co/hf-inference/models"
# Cold-start wait, WARMUP ONLY — never on a live query (a live 503 raises immediately).
_WARMUP_BACKOFF_SECONDS = 8.0


class HFRerankProvider(RerankProvider):
    """HuggingFace bge-reranker-v2-m3 (hosted cross-encoder) over httpx; raises ProviderError."""

    def _token(self, settings: Any) -> str:
        token: str = settings.HF_API_TOKEN
        if not token:
            raise ProviderConfigurationError(
                "HF_API_TOKEN is required when RERANK_PROVIDER=huggingface"
            )
        return token

    def _base_url(self, settings: Any) -> str:
        return str(settings.RERANK_BASE_URL or _DEFAULT_BASE_URL).rstrip("/")

    def _url(self, settings: Any) -> str:
        return f"{self._base_url(settings)}/{settings.RERANK_MODEL}"

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps import-time hermetic
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderUnavailableError("httpx not installed; run: pip install httpx") from exc
        return httpx

    @staticmethod
    def _payload(query: str, docs: list[str]) -> dict[str, Any]:
        return {
            "inputs": [{"text": query, "text_pair": d} for d in docs],
            "parameters": {"function_to_apply": "sigmoid"},
        }

    def _post_once(
        self,
        httpx: Any,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> Any:
        """One POST. Raises ProviderError (tagged with .code) on any failure. No secrets logged."""
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"HF rerank timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(f"HF unreachable: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"HF rerank error: HTTP {resp.status_code}",
                code=f"PROVIDER_HTTP_{resp.status_code}",
            )
        return resp.json()

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        docs = list(documents)
        if not docs:
            return []
        settings = get_settings()
        token = self._token(settings)
        url = self._url(settings)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = self._payload(query, docs)
        timeout = float(settings.RERANK_TIMEOUT_SECONDS)
        httpx = self._httpx()

        # Retry ONLY on a network timeout/transport error.  429 and 503 raise IMMEDIATELY —
        # the pipeline degrades to RRF; we never block a live query on a rate limit / cold model.
        last_exc: ProviderError | None = None
        for attempt in range(2):
            try:
                data = self._post_once(
                    httpx, url=url, headers=headers, payload=payload, timeout=timeout
                )
                return self._parse_results(data, len(docs))
            except ProviderError as exc:
                last_exc = exc
                logger.warning("HF rerank failed (%s) try %d/2", exc.code, attempt + 1)
                if exc.code not in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"}:
                    break  # 429 / 503 / HTTP / bad-response — retrying won't help a live query

        if last_exc is None:  # pragma: no cover - a failure path always sets last_exc
            last_exc = ProviderError("HF rerank failed")
        raise last_exc

    def warmup(self) -> None:
        """Best-effort cold-start ping (ONE minimal call; on 503 sleep + ONE retry).

        Called by the eval/fit harness BEFORE the timed loop so the cold-start cost is excluded
        from the per-query latency sample.  NEVER raises — logs 'HF rerank model warmed' or
        'warmup failed (will retry on first query)'.  A missing token / httpx just logs + returns.
        """
        try:
            settings = get_settings()
            token = self._token(settings)
            httpx = self._httpx()
        except (ProviderConfigurationError, ProviderUnavailableError) as exc:
            logger.warning("HF warmup skipped (%s; will retry on first query)", type(exc).__name__)
            return
        url = self._url(settings)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = self._payload("warmup", ["warmup document"])
        timeout = float(settings.RERANK_TIMEOUT_SECONDS)

        for attempt in range(2):
            try:
                self._post_once(httpx, url=url, headers=headers, payload=payload, timeout=timeout)
                logger.info("HF rerank model warmed")
                return
            except ProviderError as exc:
                if exc.code == "PROVIDER_HTTP_503" and attempt == 0:
                    logger.info(
                        "HF warmup: model cold (503); sleeping %.0fs then retrying once",
                        _WARMUP_BACKOFF_SECONDS,
                    )
                    time.sleep(_WARMUP_BACKOFF_SECONDS)
                    continue
                logger.warning("HF warmup failed (%s; will retry on first query)", exc.code)
                return

    # ---- response parsing ---------------------------------------------------

    @staticmethod
    def _parse_results(data: Any, n: int) -> list[tuple[int, float]]:
        """Map the HF response -> ``[(index, score)]`` desc; validate; defensive across shapes."""
        scores = HFRerankProvider._extract_scores(data, n)
        out = [(i, float(s)) for i, s in enumerate(scores)]
        out.sort(key=lambda pair: (-pair[1], pair[0]))  # desc by score; ties -> idx asc
        return out

    @staticmethod
    def _extract_scores(data: Any, n: int) -> list[float]:
        """Return ``n`` per-doc scores in INPUT order across the known/observed response shapes."""
        # Fallback A: flat list[float] of length n (TEI text-ranking shape).
        if (
            isinstance(data, list)
            and len(data) == n
            and all(isinstance(x, int | float) and not isinstance(x, bool) for x in data)
        ):
            return [float(x) for x in data]
        # Fallback B: list of {index, score} dicts (TEI rerank shape).
        if (
            isinstance(data, list)
            and data
            and all(isinstance(x, dict) and "index" in x for x in data)
        ):
            slots: list[float | None] = [None] * n
            for row in data:
                idx = row.get("index")
                sc = row.get("score", row.get("relevance_score"))
                if (
                    not isinstance(idx, int)
                    or isinstance(idx, bool)
                    or not (0 <= idx < n)
                    or slots[idx] is not None
                    or not isinstance(sc, int | float)
                ):
                    raise ProviderError(f"HF rerank: bad row {row!r}", code="PROVIDER_BAD_RESPONSE")
                slots[idx] = float(sc)
            if any(v is None for v in slots):
                raise ProviderError("HF rerank: missing indices", code="PROVIDER_BAD_RESPONSE")
            return [v for v in slots if v is not None]
        # PRIMARY (confirmed): single-wrapped list of per-doc {label, score} dicts.
        seq = data
        if (
            isinstance(seq, list)
            and len(seq) == 1
            and isinstance(seq[0], list)
            and len(seq[0]) == n
        ):
            seq = seq[0]  # unwrap the single batch dimension
        if isinstance(seq, list) and len(seq) == n:
            return [HFRerankProvider._score_of(item) for item in seq]
        raise ProviderError(
            f"HF rerank: malformed response (shape {type(data).__name__}, n={n})",
            code="PROVIDER_BAD_RESPONSE",
        )

    @staticmethod
    def _score_of(item: Any) -> float:
        """Pull a relevance score from one per-doc result element."""
        if isinstance(item, int | float) and not isinstance(item, bool):
            return float(item)
        if isinstance(item, dict) and isinstance(item.get("score"), int | float):
            return float(item["score"])
        if isinstance(item, list):  # per-doc list of label dicts (multi-label); prefer LABEL_0
            cands = [
                d for d in item if isinstance(d, dict) and isinstance(d.get("score"), int | float)
            ]
            if cands:
                label0 = [d for d in cands if d.get("label") == "LABEL_0"]
                chosen = label0[0] if label0 else max(cands, key=lambda d: d["score"])
                return float(chosen["score"])
        raise ProviderError(f"HF rerank: bad result element {item!r}", code="PROVIDER_BAD_RESPONSE")
