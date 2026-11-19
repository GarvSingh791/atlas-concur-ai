"""Conditional query rewrite / HyDE for lexically-weak retrieval queries (R5-T1).

HyDE (Hypothetical Document Embeddings) bridges the *query* vocabulary and the
*document* vocabulary: instead of embedding a terse user question, we ask an LLM to
write a short hypothetical excerpt the target corpus *would* contain if it answered the
question, then retrieve with THAT text.  For lexically-weak queries (a paraphrase or a
terse intent that shares few tokens with the policy wording) the hypothesis pulls in
better dense matches than the raw query.

Design (Fence 2 — layering, Fence 3 — deterministic-first):
  - Pure module: imports ONLY ``services.*`` (the LLM provider seam) + stdlib.  No DB,
    no ``backend.*``, no ``config`` read (the caller decides *whether* to rewrite).
  - ``generate_hyde_doc`` is FAIL-SAFE: any ``ProviderError`` returns the ORIGINAL query
    unchanged, so a provider outage degrades HyDE to a plain re-retrieval (a no-op second
    pass the caller can detect via ``hypothesis == query``) and NEVER breaks retrieval.
  - ``DeterministicQueryRewriter`` returns the query unchanged — used when HyDE is disabled
    or the provider is the deterministic local stub (whose canned text is not a useful
    hypothesis).  ``hyde_rewriter`` is the factory the caller uses so it never branches on
    ``LLM_PROVIDER`` itself.

The caller (``GroundedRetriever``) only invokes a rewriter inside the weak-band branch and
only when ``HYDE_ENABLED`` is true AND the provider is non-local, so the common CI/eval
path makes ZERO LLM calls.
"""

from __future__ import annotations

import logging
from typing import Protocol

from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

# Instructs the model to emit a short, corpus-shaped excerpt — not a chatty answer — so the
# embedding lands in policy-document space.  Kept deliberately terse to bound latency/tokens.
_HYDE_PROMPT = (
    "Write a 1-2 sentence excerpt from a corporate Travel & Expense policy document that "
    "directly answers the question below. Write it as policy prose (the kind of text the "
    "document itself would contain), not as a reply to the user. Be concise and factual.\n\n"
    "Question: {query}\n\n"
    "Policy excerpt:"
)


def generate_hyde_doc(
    query: str,
    provider: LlmProvider,
    *,
    document_type_hint: str | None = None,
) -> str:
    """Return a hypothetical policy excerpt answering *query*, or *query* on failure.

    FAIL-SAFE: on ``ProviderError`` (or an empty completion) the ORIGINAL query is returned
    unchanged, so the caller's ``hypothesis == query`` check makes the second retrieval pass
    a no-op rather than a crash.  ``document_type_hint`` is accepted for future targeting
    (e.g. "faq" vs "policy") and currently only annotates the log line.
    """
    prompt = _HYDE_PROMPT.format(query=query)
    try:
        response = provider.complete([LlmMessage(role="user", content=prompt)])
    except ProviderError as exc:
        logger.warning(
            "generate_hyde_doc: provider failed (%s); falling back to original query", exc.code
        )
        return query
    hypothesis = (response.text or "").strip()
    if not hypothesis:
        logger.debug("generate_hyde_doc: empty hypothesis; falling back to original query")
        return query
    logger.debug(
        "generate_hyde_doc: rewrote query (hint=%s) %d→%d chars",
        document_type_hint,
        len(query),
        len(hypothesis),
    )
    return hypothesis


class QueryRewriter(Protocol):
    """A callable that maps a query to a (possibly rewritten) retrieval text."""

    def __call__(self, query: str) -> str: ...


class DeterministicQueryRewriter:
    """No-op rewriter: returns the query unchanged (HyDE disabled / local provider).

    Structurally inert — holds no provider and makes NO LLM call — so installing it is the
    CI-safety guarantee: a second retrieval pass with this rewriter re-embeds the SAME query
    text, and the caller's ``hypothesis == query`` guard skips the pass entirely.
    """

    def __call__(self, query: str) -> str:
        return query


class HydeQueryRewriter:
    """LLM-backed rewriter: returns a hypothetical answer-document for the query (R5-T1)."""

    def __init__(self, provider: LlmProvider) -> None:
        self._provider = provider

    def __call__(self, query: str) -> str:
        return generate_hyde_doc(query, self._provider)


def hyde_rewriter(provider: LlmProvider | None, *, enabled: bool) -> QueryRewriter:
    """Return the appropriate rewriter so the caller never branches on ``LLM_PROVIDER``.

    Returns the deterministic no-op when HyDE is disabled OR no provider was supplied;
    otherwise the live ``HydeQueryRewriter``.  The caller is responsible for folding the
    "local provider is a no-op" rule into ``enabled`` (e.g.
    ``enabled=HYDE_ENABLED and LLM_PROVIDER != "local"``), keeping this factory provider-agnostic.
    """
    if not enabled or provider is None:
        return DeterministicQueryRewriter()
    return HydeQueryRewriter(provider)
