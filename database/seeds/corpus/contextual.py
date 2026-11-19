"""R4-T1 — corpus-side contextual prefixes (canonical implementation).

Anthropic's *Contextual Retrieval* prepends a short, document-situating blurb to
each chunk *before* it is embedded / indexed, so that an otherwise-ambiguous
clause carries enough surrounding context to be retrieved on its own.  This module
is the single source of truth for generating that prefix.

Two generation paths, selected by :func:`context_prefix` (the dispatcher):

* **deterministic** (``LLM_PROVIDER == "local"``) — a pure, stdlib-only template
  keyed on ``document_type``.  ZERO external calls, byte-stable for identical
  inputs, so CI / eval reproduce exactly.  This is what the committed tree uses.
* **LLM** (production) — :func:`llm_context_prefix` asks the configured provider
  to write the situating sentence, passing the FULL document as a (cacheable)
  system block and the target chunk as the user turn.  Fails safe to the
  deterministic template on any :class:`ProviderError`.

LAYERING NOTE: this canonical impl lives in the *seed* layer (not ai_agents)
because the seed's ``_embed_corpus`` must call the dispatcher, and a
``seed -> ai_agents`` import would be an illegal upward dependency (P4-T5
precedent).  ``database -> services`` is already legal (see
``database/seeds/_local_embedder.py``).  ``ai_agents/rag/contextual.py`` is a
thin downward re-export that gives the masterplan-literal / ai_agents-layer path.

The prefix is fused into the embedding vector and the lexical tsvector ONLY — it
must never reach the ``chunk_text`` column, the reranker input, or the citation
snippet (TAU*-INVARIANCE hinge).  That fusion happens in the seed/index layer,
not here; this module only *produces* the prefix string.
"""

from __future__ import annotations

from config.settings import get_settings

# Canonical human-readable document names, keyed by ``document_type``.  Every
# template MUST name its document so the embedded chunk is self-situating.
_DOCUMENT_NAMES: dict[str, str] = {
    "policy": "Helios Global Travel & Expense Policy",
    "faq": "Helios T&E FAQ",
    "howto": "Helios T&E How-To Guide",
    "reference": "Helios T&E Reference",
}

# Max characters of document text to feed the LLM as cacheable context.  Bounds
# the implicit-cache block; the Helios corpus docs sit comfortably under this.
_MAX_DOC_CHARS = 24_000


def deterministic_context_prefix(
    *,
    document_title: str,
    document_type: str,
    section: str,
    clause_ref: str,
) -> str:
    """Return a deterministic, document-type-aware situating prefix.

    PURE: stdlib only, no DB, no network, no settings read.  Identical inputs
    always yield the identical string.  Four ``document_type`` templates
    (policy / faq / howto / reference); each names the document, the ``section``,
    and the unit ``clause_ref`` across two sentences and is tuned to land in the
    50-100 token band (token := ``re.findall(r"\\w+|[^\\w\\s]", text)``).

    ``document_title`` is accepted for signature symmetry but the canonical
    document name (keyed on ``document_type``) is used so the prefix is stable
    regardless of per-doc title drift; unknown types fall back to the reference
    template with the raw title.
    """
    name = _DOCUMENT_NAMES.get(document_type)

    if document_type == "policy":
        return (
            f"This excerpt is drawn from the {name}, within the section titled "
            f'"{section}" and is formally identified as clause {clause_ref}. '
            f"It states a binding rule that Helios employees must follow when they "
            f"book travel, incur expenses, or submit reports for reimbursement and "
            f"approval under company policy."
        )
    if document_type == "faq":
        return (
            f"This passage comes from the {name}, under the topic section "
            f'"{section}" and is catalogued as entry {clause_ref}. '
            f"It answers a common question that Helios employees ask about travel, "
            f"expenses, reimbursement timing, or approval, and points them toward the "
            f"governing policy rule that applies to their situation."
        )
    if document_type == "howto":
        return (
            f"This passage is taken from the {name}, within the procedure section "
            f'"{section}" and is recorded as step reference {clause_ref}. '
            f"It walks a Helios employee through the concrete actions needed to "
            f"complete a travel or expense task in the system, such as booking, "
            f"uploading a receipt, or submitting a report for approval."
        )
    # reference (and unknown-type fallback)
    ref_name = name or document_title
    return (
        f"This entry is from the {ref_name}, listed under the section "
        f'"{section}" and indexed as reference {clause_ref}. '
        f"It supplies a specific fact, limit, rate, or definition that Helios "
        f"employees and approvers rely on when they interpret travel and expense "
        f"rules or check an amount against the governing policy."
    )


def llm_context_prefix(
    *,
    document_text: str,
    chunk_text: str,
    provider,  # noqa: ANN001 — services.llm_provider.interface.LlmProvider (lazy-typed)
    document_title: str,
    document_type: str,
    section: str,
    clause_ref: str,
) -> str:
    """Generate the situating prefix via the LLM, failing safe to the template.

    Builds a system message carrying the FULL ``document_text`` as the cacheable
    context block and a user message carrying the target ``chunk_text`` plus a
    bounded-output instruction.  Honest note: Gemini-via-OpenRouter gives
    *implicit* context caching (repeated identical prefix tokens), NOT Anthropic
    ``cache_control``; passing the whole document as the leading system block is
    what lets the provider amortise it across the doc's chunks.

    Any :class:`ProviderError`, or an empty/whitespace completion, falls back to
    :func:`deterministic_context_prefix` so indexing can never be blocked by a
    transient provider failure.
    """
    # Lazy imports: keep module import light and layering-clean (no services
    # provider machinery pulled in just to import the deterministic path).
    from services.exceptions import ProviderError
    from services.llm_provider.interface import LlmMessage

    doc = document_text[:_MAX_DOC_CHARS]
    messages = [
        LlmMessage(
            role="system",
            content=(
                "You are indexing a corporate travel-and-expense knowledge base. "
                "Below is the full source document. Use it only as background "
                "context for situating a single chunk that follows.\n\n"
                f"<document>\n{doc}\n</document>"
            ),
        ),
        LlmMessage(
            role="user",
            content=(
                "Write a short, self-contained context blurb (2 sentences, 50-100 "
                "words) that situates the following chunk within the document above. "
                "Name the document, its section, and the clause/entry identifier, so "
                "the chunk can be retrieved on its own. Output ONLY the blurb, with no "
                "preamble, quotes, or markdown.\n\n"
                f"Document: {_DOCUMENT_NAMES.get(document_type, document_title)}\n"
                f"Section: {section}\n"
                f"Identifier: {clause_ref}\n\n"
                f"<chunk>\n{chunk_text}\n</chunk>"
            ),
        ),
    ]

    try:
        response = provider.complete(messages)
    except ProviderError:
        return deterministic_context_prefix(
            document_title=document_title,
            document_type=document_type,
            section=section,
            clause_ref=clause_ref,
        )

    text = (response.text or "").strip()
    if not text:
        return deterministic_context_prefix(
            document_title=document_title,
            document_type=document_type,
            section=section,
            clause_ref=clause_ref,
        )
    return text


def context_prefix(
    *,
    document_title: str,
    document_type: str,
    section: str,
    clause_ref: str,
    document_text: str,
    chunk_text: str,
    cached: str | None = None,
) -> str:
    """Dispatch to the correct prefix path based on settings (FORK-C / FORK-D / FORK-H).

    * ``EMBEDDINGS_PROVIDER == "local"`` -> ``""`` UNCONDITIONALLY (FORK-H, R4-T3): the
      local feature-hash embedder treats prefix tokens as noise (R4-T1 measured an AURC /
      coverage regression on the local cosine gate), so the deterministic CI eval-db lane
      MUST stay prefix-free even if ``RAG_CONTEXTUAL_PREFIXES`` is flipped True for
      production.  This lets the adopt decision flip the prefix default to True WITHOUT
      breaking ``make eval-db`` (it stays BYTE-IDENTICAL to R3) and makes ZERO LLM calls on
      the local path regardless of LLM_PROVIDER.
    * ``RAG_CONTEXTUAL_PREFIXES`` OFF -> ``""`` (embed the bare chunk; the seed
      stores a NULL ``context_prefix`` and the committed tree reproduces R3).
    * ``LLM_PROVIDER == "local"`` -> deterministic template (CI/eval, zero calls).
    * production + ``cached`` present -> reuse the persisted prefix (FORK-D
      persist-and-reuse makes a reindex a no-op; no fresh LLM call).
    * production, no cache -> generate via the LLM (fail-safe to deterministic).
    """
    settings = get_settings()
    # FORK-H (R4-T3): the local embedder never gets a prefix, even with the flag ON — the
    # CI-safety enabler that keeps the local eval-db lane R3-byte-identical under any default.
    if settings.EMBEDDINGS_PROVIDER == "local":
        return ""
    if not settings.RAG_CONTEXTUAL_PREFIXES:
        return ""
    if settings.LLM_PROVIDER == "local":
        return deterministic_context_prefix(
            document_title=document_title,
            document_type=document_type,
            section=section,
            clause_ref=clause_ref,
        )
    if cached:
        return cached
    # Lazy import: factory pulls in provider adapters; only needed on this path.
    from services.llm_provider.factory import get_llm_provider

    provider = get_llm_provider()
    return llm_context_prefix(
        document_text=document_text,
        chunk_text=chunk_text,
        provider=provider,
        document_title=document_title,
        document_type=document_type,
        section=section,
        clause_ref=clause_ref,
    )
