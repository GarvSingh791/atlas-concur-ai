"""Rerank provider interface and contract (R2-T1).

A cross-encoder reranker rescores retrieval candidates with joint query<->document
attention and returns them reordered by relevance — the precision stage layered on top of
R1's dense+sparse+RRF recall stage.  This module defines the swappable interface ONLY;
the adapters live alongside it (``local.py`` deterministic stand-in, ``cohere.py``
production) and ``factory.get_rerank_provider()`` selects one via ``RERANK_PROVIDER``.

LAYERING (critical): this interface takes and returns PLAIN ``str``/``int`` only — it never
imports an ``ai_agents`` type.  The caller (``ai_agents/rag/rerank.py``) owns mapping the
returned indices back onto its own candidate objects.  This keeps ``services/`` free of any
``ai_agents`` import, exactly like the LLM / embeddings / OCR providers (FR-A6).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence

logger = logging.getLogger(__name__)


class RerankProvider(ABC):
    """Model-agnostic reranking interface (one forward pass per candidate).

    Contract for ``rerank(query, documents)``:
      - Returns a list of ``(index, score)`` pairs where ``index`` is a position into the
        INPUT ``documents`` sequence and ``score`` is that document's relevance to ``query``
        (higher = more relevant in this provider's own scale).
      - The list is ordered by ``score`` DESCENDING.
      - It is a permutation OR a subset of ``range(len(documents))`` — an adapter MAY return
        fewer rows than given (e.g. an API ``top_n``).  Indices are unique.
      - Ties are broken by ASCENDING index, so the ordering is reproducible.
      - On failure a production adapter RAISES ``ProviderError`` (never a sentinel); the
        two-stage pipeline owns the fail-SAFE fallback to the pre-rerank (RRF) order.
    """

    @abstractmethod
    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        """Rescore ``documents`` against ``query``; return ``(index, score)`` desc."""
