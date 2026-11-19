"""Two-stage retrieval — cross-encoder rerank over the RRF top-N (R2-T2).

A NEW pure module alongside ``fusion.py`` (RRF) and ``parent_context.py``.  After R1 fuses
the dense + sparse paths, ``apply_rerank`` rescores the fused TOP-N candidates with a
cross-encoder (via a ``services`` ``RerankProvider``) and reorders them down to a
high-precision top-k, in ONE batched call capped at ``candidate_cap`` (research §5.3).

Generic over the candidate type (like ``fusion.py``): it reads each candidate's text through
``text_of`` and maps the provider's returned indices back onto the candidates, so it never
imports ``RetrievalCandidate`` (no import cycle).

FAIL-SAFE (I4): a reranker failure — ``ProviderError``, timeout, malformed response, or any
unexpected error — is caught HERE and the call returns the INPUT (R1 RRF) order with
``applied=False`` and a logged warning.  It NEVER raises and NEVER abstains; the RRF order is
the proven R1 baseline.  The answerability gate is untouched: this module only reorders the
context list — ``max_dense_similarity`` (the gate's signal) is computed elsewhere from the
DENSE path, independent of rerank order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from services.exceptions import ProviderError
from services.rerank_provider.interface import RerankProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RerankContractError(Exception):
    """The provider returned a structurally invalid permutation (caught -> fail-safe)."""


@dataclass(frozen=True)
class RerankOutcome(Generic[T]):
    """Reordered candidates + per-item rerank scores + whether the reranker actually ran.

    ``items``   — the reordered candidate list (or the input order on fallback), cut to top_k.
    ``scores``  — parallel to ``items``: the provider's score for a reranked head item, or
                  ``None`` for a tail item (beyond the cap) or when ``applied`` is False.
                  Observability only — never read by the answerability gate.
    ``applied`` — False when the reranker failed and the R1 RRF order was kept.
    """

    items: list[T]
    scores: list[float | None]
    applied: bool


def _validated_order(scored: Sequence[tuple[int, float]], n: int) -> list[tuple[int, float | None]]:
    """Validate provider output is a unique-index subset of ``range(n)``; return full order.

    Raises ``RerankContractError`` on any structural violation so the caller can fail safe.
    Appends any indices the provider OMITTED (a legitimate ``top_n`` subset) in their original
    order with a ``None`` score, so no candidate is ever silently dropped by reranking.
    """
    seen: set[int] = set()
    order: list[tuple[int, float | None]] = []
    for pair in scored:
        idx = pair[0] if isinstance(pair, tuple) and len(pair) == 2 else None
        if not isinstance(idx, int) or not (0 <= idx < n) or idx in seen:
            raise RerankContractError(f"invalid rerank row: {pair!r} (n={n})")
        seen.add(idx)
        order.append((idx, float(pair[1])))
    for i in range(n):  # preserve omitted candidates (subset reranks) after the ranked ones
        if i not in seen:
            order.append((i, None))
    return order


def apply_rerank(
    query: str,
    candidates: Sequence[T],
    *,
    provider: RerankProvider,
    top_k: int,
    candidate_cap: int,
    text_of: Callable[[T], str],
) -> RerankOutcome[T]:
    """Rerank the RRF top ``candidate_cap`` candidates; return the top ``top_k`` reordered.

    Exactly ONE batched provider call over <= ``candidate_cap`` documents.  Candidates beyond
    the cap keep their R1 RRF order and are appended unchanged (never reranked).  On ANY
    provider failure the input (RRF) order is returned with ``applied=False`` (fail-safe).
    """
    n_total = len(candidates)
    if n_total == 0:
        return RerankOutcome(items=[], scores=[], applied=True)

    cap = max(1, candidate_cap)
    head = list(candidates[:cap])
    tail = list(candidates[cap:])

    try:
        texts = [text_of(c) for c in head]
        scored = provider.rerank(query, texts)  # ONE batched call
        order = _validated_order(scored, len(head))
        reordered_head = [head[i] for i, _ in order]
        head_scores: list[float | None] = [s for _, s in order]
        applied = True
    except (ProviderError, RerankContractError) as exc:
        logger.warning("rerank failed (%s); falling back to R1 RRF order", exc)
        reordered_head, head_scores, applied = head, [None] * len(head), False
    except Exception:  # absolute fail-safe — a reranker bug must never break retrieval (I4)
        logger.warning("rerank raised unexpectedly; falling back to R1 RRF order", exc_info=True)
        reordered_head, head_scores, applied = head, [None] * len(head), False

    items = reordered_head + tail
    scores = head_scores + [None] * len(tail)
    cut = max(1, top_k)
    return RerankOutcome(items=items[:cut], scores=scores[:cut], applied=applied)
