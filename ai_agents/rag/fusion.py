"""Reciprocal Rank Fusion (R1-T3) — pure, deterministic combiner for ranked lists.

Fuses N>=1 independently-ranked candidate lists (e.g. the dense cosine path and the sparse
``ts_rank_cd`` path from ``PolicyRetrievalTool.retrieve_parallel``) into ONE ranking by

    RRF(d) = sum over lists of  1 / (k + rank_d)        # rank is 1-based; absent from a list => 0

with the classic ``k`` damping (passed in by the caller; this module imports no settings).
An item present in several lists accumulates one term per list; an item absent from a list
contributes nothing from that list.  The result is ordered by fused score DESC, ties broken
by a caller-supplied, REPRODUCIBLE key (e.g. ``clause_ref``) ASC — never by a per-seed UUID.

PURE: no DB, no settings, no backend import.  ``k`` is a parameter.  The floats are identical
run-to-run (the ranks are identical), so ``(-score, tie_breaker, first_seen)`` sorts
deterministically with NO epsilon rounding.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison

T = TypeVar("T")


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[T]],
    *,
    key: Callable[[T], Hashable],
    k: int,
    tie_breaker: Callable[[T], SupportsRichComparison] | None = None,
) -> list[T]:
    """Fuse ranked lists by Reciprocal Rank Fusion; return one deterministically-ordered list.

    Args:
        ranked_lists: N>=1 lists, each already ordered best-first (position 0 == rank 1).
        key: maps an item to its stable fusion identity — items sharing a key are the SAME
            entity across lists and their RRF terms sum (e.g. ``lambda c: c.embedding_id``).
        k: the RRF damping constant (caller passes ``settings.RRF_K``; canonical 60).
        tie_breaker: maps an item to a REPRODUCIBLE sort key; items with equal fused score
            are ordered by this ASC.  Must be stable across re-seeds (use ``clause_ref``,
            NEVER a per-seed UUID).  When ``None``, equal-score items keep first-seen order.

    Returns:
        Items ordered by fused score DESC, then ``tie_breaker`` ASC, then first-seen order.
        The representative item for each key is the FIRST occurrence encountered scanning the
        lists left-to-right, top-to-bottom.
    """
    scores: dict[Hashable, float] = {}
    representative: dict[Hashable, T] = {}
    first_seen: dict[Hashable, int] = {}
    counter = 0

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):  # 1-based: position 1 == top
            kid = key(item)
            if kid not in scores:
                scores[kid] = 0.0
                representative[kid] = item
                first_seen[kid] = counter
                counter += 1
            scores[kid] += 1.0 / (k + rank)

    if tie_breaker is None:
        keys_sorted = sorted(scores, key=lambda kid: (-scores[kid], first_seen[kid]))
    else:
        tb = tie_breaker
        keys_sorted = sorted(
            scores,
            key=lambda kid: (-scores[kid], tb(representative[kid]), first_seen[kid]),
        )

    return [representative[kid] for kid in keys_sorted]
