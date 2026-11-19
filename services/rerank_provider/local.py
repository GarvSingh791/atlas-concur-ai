"""Local (deterministic, no-network) rerank adapter for dev/CI (R2-T1).

The DEFAULT ``RERANK_PROVIDER``.  It scores each document against the query from LEXICAL
features only — no torch, no model weights, no network — so CI stays hermetic and the
ordering is a pure, reproducible function of ``(query, documents)``.

This is a DETERMINISM STAND-IN, not a quality lever (do NOT try to beat RRF on the gold
set).  Two design properties matter, and they are in deliberate tension:

  1. NON-DEGENERATE: when the lexical signal clearly disagrees with the input order it
     reorders — so it is never a trivial identity (proven on a crafted fixture, V4).

  2. GENTLE / RRF-ANCHORED: the input order IS the R1 RRF-fused ranking — a strong prior the
     real cross-encoder will REFINE, not discard.  The score subtracts a per-rank penalty
     (``_RANK_PENALTY``) from the [0,1] lexical score, so a candidate only overtakes the one
     directly above it when its lexical score is more than ``_RANK_PENALTY`` higher (a
     DECISIVE win).  With ``_RANK_PENALTY = 0.5`` that bound keeps the realistic top-k SET
     essentially intact — and the corpus eval's recall@k / per-doctype / NY gates are
     set-based — so they stay green while a decisive lexical case still climbs.

The PRODUCTION reranker (``cohere.py``) carries NO such prior; its real reordering gain is
measured in Part 2.  ``_RANK_PENALTY`` lives here, not in the pipeline, precisely so the
gentleness is a property of the local STAND-IN only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from services.rerank_provider.interface import RerankProvider

logger = logging.getLogger(__name__)

# Per-rank prior penalty subtracted from a 0..1 lexical score.  0.5 => a candidate only
# outranks the one directly above it when its lexical score is >0.5 higher (a decisive win),
# which keeps the realistic top-k SET stable (gentle) while a crafted strong-overlap case
# still reorders (non-degenerate).  This is the determinism-stand-in's RRF anchor.
_RANK_PENALTY = 0.5

# Lexical score component weights (sum to 1.0 => score in [0, 1]).
_W_COVERAGE = 0.5  # fraction of query tokens present in the document
_W_JACCARD = 0.3  # token-set Jaccard (penalises mismatch both ways)
_W_EXACT = 0.2  # exact figure / currency / clause-id matches (what BM25 exists for)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# "Exact terms": numbers / currency amounts (250, $1,500.00) and clause-or-unit ids (air-01).
_EXACT_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?|[a-z]{2,}-[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _exact_terms(text: str) -> set[str]:
    return set(_EXACT_RE.findall(text.lower()))


def _lexical_score(query: str, document: str) -> float:
    """Deterministic lexical relevance in [0, 1]: coverage + Jaccard + exact-term bonus."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return 0.0
    q_set = set(q_tokens)
    d_set = set(_tokens(document))
    if not d_set:
        return 0.0
    inter = q_set & d_set
    coverage = len(inter) / len(q_set)
    union = q_set | d_set
    jaccard = len(inter) / len(union) if union else 0.0
    q_exact = _exact_terms(query)
    exact = len(q_exact & _exact_terms(document)) / len(q_exact) if q_exact else 0.0
    return _W_COVERAGE * coverage + _W_JACCARD * jaccard + _W_EXACT * exact


class LocalRerankProvider(RerankProvider):
    """Deterministic, no-network reranker: gentle RRF-anchored lexical reorder."""

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        # Score = lexical relevance (0..1) minus a per-rank prior on the INPUT (RRF) position.
        scored = [
            (i, _lexical_score(query, doc) - _RANK_PENALTY * i) for i, doc in enumerate(documents)
        ]
        # Order by score DESC; ties broken by ASCENDING index (reproducible, per contract).
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        logger.debug("LocalRerankProvider.rerank: reranked %d docs (local mode).", len(scored))
        return scored
