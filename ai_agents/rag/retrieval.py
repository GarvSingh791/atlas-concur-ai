"""Grounded RAG retrieval flow (P4-T6) — deterministic, LLM-free.

Sits on top of PolicyRetrievalTool (P4-T4) and adds:
  - A configurable similarity threshold that gates the "policy is silent"
    decision: when no retrieved chunk exceeds the threshold the flow returns
    a SILENT result rather than a low-confidence guess.
  - Citation assembly (reusing the P4-T2 Citation shape).
  - A confidence indicator derived from the top similarity score.

Design decisions
----------------
- Composes PolicyRetrievalTool — does NOT re-implement the ANN search and
  does NOT embed the query separately, so embedder/seed parity is inherited
  from the wrapped tool by construction.
- policy_id scoping is deferred; org-granularity (via AgentContext) is
  sufficient while the seed has one policy per org.  When multi-policy orgs
  land, add policy_id additively to RetrievalIntent + the tool's WHERE.
- Uses a _Retriever Protocol seam so hermetic tests can inject a fake
  retriever without subclassing PolicyRetrievalTool (mypy-clean).

Threshold calibration (RETRIEVAL_SIMILARITY_THRESHOLD)
-------------------------------------------------------
Default 0.20, calibrated against LocalEmbeddingsProvider's cosine distribution
over the 8 seeded clauses (dev embedder = signed feature-hashing, 1536-dim,
unit-normalized).  Token-rich on-policy queries produced similarities in
[0.25, 0.60]; off-topic queries produced [0.00, 0.17].  The formula
  threshold = off_max + 0.4 * (on_min - off_max) = 0.1677 + 0.4 * 0.0823 = 0.20
sits cleanly in the gap.

IMPORTANT: LocalEmbeddingsProvider uses Python's salted hash(), so absolute
similarity values are stable only within one process (seed + query run in the
same interpreter — as the live tests do).  PYTHONHASHSEED is not pinned.
For production/OpenAI embeddings, recalibrate by setting RETRIEVAL_SIMILARITY_THRESHOLD
in the environment — the calibrated default is a dev-embedder artifact.

Confidence mapping
------------------
  confidence = clamp(0.5 + 0.5 * (top - threshold) / (1 - threshold), 0.5, 1.0)

Floor 0.5 when top == threshold (grounded but at the minimum), 1.0 at perfect
match.  Monotonic and deterministic.  Silent results carry None (type-distinct
from "matched at the floor").  Does not overclaim semantic confidence for a
lexical embedder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.rag.lexical import is_lexically_relevant
from ai_agents.rag.parent_context import resolve_parent_context_for_embedding
from ai_agents.rag.query_rewrite import hyde_rewriter
from ai_agents.rag.threshold import load_abstention_threshold
from ai_agents.tools.policy_retrieval import CandidateAudit, RetrievalIntent, RetrievalResult
from config.settings import get_settings

if TYPE_CHECKING:
    from services.llm_provider.interface import LlmProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class GroundingStatus(str, Enum):
    """Whether the flow found sufficiently similar clauses or declined to assert."""

    grounded = "grounded"
    silent = "silent"


@dataclass(frozen=True)
class GroundedClause:
    """One threshold-passing policy chunk, ready to cite."""

    clause_ref: str | None  # None for unlabelled chunks (rare in practice)
    chunk_text: str
    similarity: float  # cosine ∈ [0, 1]
    # R5-T2 (FORK-P): parent document discriminator (policy|faq|howto|reference | None),
    # propagated from RetrievedChunk.  Drives the document-qualified citation label in
    # ai_agents.rag.grounding.citation_label; None → generic "Policy" fallback.  Additive.
    document_type: str | None = None


@dataclass(frozen=True)
class NearTopicSuggestion:
    """A near-miss corpus unit surfaced on a silent (abstaining) result.

    Built ONLY from chunks actually retrieved (never fabricated). Populated when the
    silent result still has some corpus relevance (max dense cosine above a soft floor).
    """

    chunk_text: str  # first 200 chars of the nearest retrieved unit
    policy_name: str  # source document title (falls back to "Policy Document")
    clause_ref: str  # clause id, or "" when unlabelled


@dataclass(frozen=True)
class GroundedRetrieval:
    """Result of the grounded retrieval flow.

    status="grounded" → clauses, citations, and confidence are populated.
    status="silent"   → clauses=(), citations=(), confidence=None, reason is set.

    Serialize with to_dict() for JSONB / API use.
    """

    query: str
    status: GroundingStatus
    clauses: tuple[GroundedClause, ...]  # () when silent
    citations: tuple[Citation, ...]  # () when silent
    confidence: float | None  # None when silent
    reason: str | None  # set when silent
    threshold: float  # threshold actually applied (observability)
    confidence_source: str | None = None  # "calibrated" | "cosine"; None on silent
    # R5 (FORK-P): the CALIBRATED τ* the gate compared against, surfaced for the trust seam.
    # Set ONLY when the calibrated gate decided grounding (confidence_source=="calibrated");
    # None on the cosine/local fallback and on every silent path.  Additive + nullable so the
    # local/cosine behavior is byte-identical (no gate change).  Distinct from ``threshold``
    # above, which is the *applied* gate threshold (always a float, cosine value on the local
    # path); this field isolates the τ* specifically so a consumer can tell the two apart.
    tau_star: float | None = None
    near_topic_suggestion: NearTopicSuggestion | None = None  # near-miss on silent
    # R6-T3 (audit completeness): the per-candidate retrieval audit trail
    # ({document_id, unit_id, fused_rank, rerank_score} for the reranked top-N), threaded up
    # from RetrievalResult so the SINGLE decision-log write can record the candidate set that
    # was answered/abstained on.  Additive + nullable (hermetic fakes omit it),
    # observability-ONLY; deliberately NOT serialized in to_dict() — the API/conversation
    # payload is unchanged; this field rides the audit path only.
    retrieval_audit: tuple[CandidateAudit, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for conversation_messages.citations / API responses."""
        return {
            "status": self.status.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "threshold": self.threshold,
            "clauses": [
                {
                    "clause_ref": c.clause_ref,
                    "chunk_text": c.chunk_text,
                    "similarity": c.similarity,
                    "document_type": c.document_type,
                }
                for c in self.clauses
            ],
            "citations": [c.to_dict() for c in self.citations],
            "confidence_source": self.confidence_source,
            "tau_star": self.tau_star,
            "near_topic_suggestion": (
                {
                    "chunk_text": self.near_topic_suggestion.chunk_text,
                    "policy_name": self.near_topic_suggestion.policy_name,
                    "clause_ref": self.near_topic_suggestion.clause_ref,
                }
                if self.near_topic_suggestion is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Retriever Protocol seam (keeps fakes mypy-clean without subclassing the tool)
# ---------------------------------------------------------------------------


class _Retriever(Protocol):
    def retrieve(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Session,
    ) -> RetrievalResult: ...


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


class GroundedRetriever:
    """Grounded RAG flow: compose PolicyRetrievalTool → threshold → citation assembly.

    Constructor params override the Settings defaults when supplied explicitly
    (useful in tests and for per-call calibration).

    The retriever seam (_Retriever Protocol) accepts any object that exposes a
    `retrieve(intent, *, context, session) -> RetrievalResult` method.
    PolicyRetrievalTool satisfies it structurally.
    """

    def __init__(
        self,
        retriever: _Retriever | None = None,
        *,
        threshold: float | None = None,
        top_k: int | None = None,
        provider: LlmProvider | None = None,
        hyde_enabled: bool | None = None,
        hyde_weak_band_upper: float | None = None,
    ) -> None:
        if retriever is None:
            # Lazy import keeps the import graph clean for the layering-law test.
            from ai_agents.tools.policy_retrieval import PolicyRetrievalTool  # noqa: PLC0415

            retriever = PolicyRetrievalTool()
        self._retriever = retriever

        settings = get_settings()
        self._threshold: float = (
            threshold if threshold is not None else settings.RETRIEVAL_SIMILARITY_THRESHOLD
        )
        self._top_k: int = top_k if top_k is not None else settings.RETRIEVAL_TOP_K
        # R3-T2: the active rerank provider keys the calibrated τ* lookup (per-provider scale).
        self._rerank_provider: str = settings.RERANK_PROVIDER
        # R5-T1: conditional HyDE (default-off, latency-gated).  ``provider`` is only used on the
        # HyDE branch; it is NEVER called when disabled or under the local stub.  Fold the
        # "local provider is a no-op" rule into the rewriter's enabled flag so the CI/eval path
        # (LLM_PROVIDER=local) makes zero LLM calls even if HYDE_ENABLED is flipped on.
        self._hyde_enabled: bool = settings.HYDE_ENABLED if hyde_enabled is None else hyde_enabled
        self._hyde_weak_band_upper: float = (
            settings.HYDE_WEAK_BAND_UPPER if hyde_weak_band_upper is None else hyde_weak_band_upper
        )
        self._hyde_rewriter = hyde_rewriter(
            provider, enabled=self._hyde_enabled and settings.LLM_PROVIDER != "local"
        )

    def retrieve(
        self,
        query: str,
        *,
        context: AgentContext,
        session: Session,
    ) -> GroundedRetrieval:
        """Embed → search → (optional HyDE re-retrieve) → threshold → grounded or silent.

        Raises ToolError (from the wrapped tool) if query is empty — propagates
        unchanged; the caller is responsible for not passing blank queries.

        R5-T1: when HyDE is enabled AND the first pass lands in the weak band, a second
        retrieval with a hypothetical answer-document runs and the better pass is kept
        (``_maybe_hyde``).  When HyDE is disabled the entire branch is skipped, so the flow is
        a single pass and ``_ground`` is byte-identical to the pre-R5 behavior — no extra LLM
        call, no added latency.

        Returns:
            GroundedRetrieval with status="grounded" when at least one chunk
            exceeds the threshold, or status="silent" otherwise.
        """
        intent = RetrievalIntent(query=query, top_k=self._top_k)
        result = self._retriever.retrieve(intent, context=context, session=session)

        if self._hyde_enabled:
            result = self._maybe_hyde(query, result, context=context, session=session)

        return self._ground(query, result, context=context, session=session)

    def _maybe_hyde(
        self,
        query: str,
        result: RetrievalResult,
        *,
        context: AgentContext,
        session: Session,
    ) -> RetrievalResult:
        """Latency-gated HyDE second pass (R5-T1); return the better of the two passes.

        Fires ONLY when the top max-dense cosine is in the weak band
        ``[RETRIEVAL_SIMILARITY_THRESHOLD, HYDE_WEAK_BAND_UPPER)``: above the abstention floor
        (below it we would just abstain — HyDE cannot rescue a clearly off-topic query) yet
        below a strong match (above it no second pass is warranted).  The rewriter is a no-op
        under a disabled/local provider (returns the query unchanged) — the ``hypothesis ==
        query`` guard then skips the second retrieval entirely, so no LLM call occurs on the
        CI/eval path.  Keeps whichever pass has the stronger answerability signal, so HyDE can
        only help, never regress.
        """
        md = result.max_dense_similarity
        if md is None or not (self._threshold <= md < self._hyde_weak_band_upper):
            return result
        hypothesis = self._hyde_rewriter(query)
        if hypothesis == query:
            return result
        second = self._retriever.retrieve(
            RetrievalIntent(query=hypothesis, top_k=self._top_k),
            context=context,
            session=session,
        )
        if (second.max_dense_similarity or 0.0) > md:
            logger.debug(
                "GroundedRetriever: HyDE second pass used (max_dense %.4f → %.4f)",
                md,
                second.max_dense_similarity or 0.0,
            )
            return second
        logger.debug(
            "GroundedRetriever: HyDE second pass not better (%.4f vs %.4f); keeping first pass",
            second.max_dense_similarity or 0.0,
            md,
        )
        return result

    def _ground(
        self,
        query: str,
        result: RetrievalResult,
        *,
        context: AgentContext,
        session: Session,
    ) -> GroundedRetrieval:
        """Threshold-gate a RetrievalResult into a grounded or silent GroundedRetrieval.

        The gate / confidence / citation logic is UNCHANGED from the pre-R5 single-pass flow;
        it is factored into its own method only so the HyDE two-pass orchestration in
        ``retrieve`` can choose which RetrievalResult to ground on.  tau*/top_similarity are
        (re)computed HERE (not hoisted into ``retrieve``) so the module-level monkeypatch seams
        the tests rely on keep applying to the gate.
        """
        logger.debug(
            "GroundedRetriever: query=%r org=%s returned %d raw chunks; threshold=%.4f",
            query,
            context.organization_id,
            len(result.chunks),
            self._threshold,
        )

        # --- Silent branch: no chunks or best score below threshold ----------
        if not result.chunks:
            reason = f"no_chunks: org {context.organization_id} has no indexed policy embeddings"
            return GroundedRetrieval(
                query=query,
                status=GroundingStatus.silent,
                clauses=(),
                citations=(),
                confidence=None,
                reason=reason,
                threshold=self._threshold,
                retrieval_audit=result.audit_trail,
            )

        # Cosine signal = MAX DENSE COSINE (CRUX A): structural, never an RRF score.  Fall back
        # to chunks[0].similarity only for a fake retriever that omits the field (hermetic
        # tests).  Computed unconditionally — the grounded branch's chunk cull + confidence
        # still use it even when the calibrated gate decides answer/abstain.
        top_similarity = (
            result.max_dense_similarity
            if result.max_dense_similarity is not None
            else result.chunks[0].similarity
        )
        # R3-T2 GATE: PRIMARY = the TOP chunk's calibrated reranker score vs the data-derived
        # τ* (answer iff calibrated > τ*).  FAIL-SAFE (I_R3_3): when the calibrated score is
        # unavailable (rerank not applied / no provider params) OR no τ* artifact exists for
        # this provider, fall back to the proven R2 cosine gate (>= RETRIEVAL_SIMILARITY_THRESHOLD).
        # load_abstention_threshold never raises (logs a WARNING + returns None on a missing
        # artifact), so a reranker/artifact outage degrades to cosine — it never crashes.
        tau_star = load_abstention_threshold(self._rerank_provider)
        top_calibrated = result.chunks[0].calibrated_score
        # Build the reason detail INSIDE each branch (where the None-narrowing holds) so the
        # message always matches the gate actually applied — and mypy proves it type-safe.
        # ``gate_tau_star`` isolates the CALIBRATED τ* for the trust seam: set ONLY when the
        # calibrated gate fired, None on the cosine/local fallback (so no local behavior change).
        gate_tau_star: float | None
        if tau_star is not None and top_calibrated is not None:
            gate_passes = top_calibrated > tau_star
            gate_threshold: float = tau_star
            gate_tau_star = tau_star
            reason_detail = f"calibrated score {top_calibrated:.4f} <= tau* {tau_star:.4f}"
        else:
            gate_passes = top_similarity >= self._threshold
            gate_threshold = self._threshold
            gate_tau_star = None
            reason_detail = f"top similarity {top_similarity:.4f} < threshold {self._threshold:.4f}"
        if not gate_passes:
            near = None
            if result.chunks and (result.max_dense_similarity or 0.0) > 0.10:
                top = result.chunks[0]
                near = NearTopicSuggestion(
                    chunk_text=top.chunk_text[:200],
                    policy_name=top.policy_name or "Policy Document",
                    clause_ref=top.clause_ref or "",
                )
            return GroundedRetrieval(
                query=query,
                status=GroundingStatus.silent,
                clauses=(),
                citations=(),
                confidence=None,
                reason=f"below_threshold: {reason_detail}",
                threshold=gate_threshold,
                confidence_source=None,
                near_topic_suggestion=near,
                retrieval_audit=result.audit_trail,
            )

        # --- Grounded branch: keep dense-strong chunks AND every sparse (lexical) hit ------
        # The tool returns RRF-fused order (NOT cosine-DESC), so iterate fully (no break).
        # Keep a chunk when it clears the cosine gate OR the sparse path surfaced it: sparse
        # hits have no meaningful cosine (similarity 0.0), so a per-candidate cosine cull
        # would silently drop them and defeat hybrid retrieval (CRUX A).
        kept_chunks: list[GroundedClause] = []
        citations: list[Citation] = []

        # C7-10: a third keep-reason — query-time LEXICAL relevance over the chunk TEXT
        # (stop-word-free, stemmed, symmetric; ai_agents.rag.lexical).  The raw-token local
        # embedder can score the very clause a question is about below the cosine floor
        # ("nightly hotel cap" vs "Hotel nightly rate caps": 0.142), and the sparse path cannot
        # rescue rows whose chunk_tsv was never built.  This only decides which ALREADY-RETRIEVED
        # candidates reach the LLM context AFTER the answerability gate passed — it never
        # changes the gate, so it cannot turn an abstention into an answer.
        for chunk in result.chunks:
            if (
                chunk.similarity < self._threshold
                and not chunk.from_sparse
                and not is_lexically_relevant(query, chunk.chunk_text, chunk.clause_ref)
            ):
                continue
            # Parent-context live switch (R1): ground on the enclosing SECTION (small-to-big)
            # while the CITATION stays the LEAF unit.  Guarded — any resolver failure (e.g. a
            # hermetic mock session, or an unknown embedding_id) degrades to the leaf text and
            # never breaks the answer.
            try:
                parent_text = resolve_parent_context_for_embedding(session, chunk.embedding_id)
            except Exception:  # noqa: BLE001 — parent context is enrichment; degrade to leaf
                logger.debug(
                    "parent-context resolve failed for embedding_id=%s; using leaf text",
                    chunk.embedding_id,
                )
                parent_text = ""
            kept_chunks.append(
                GroundedClause(
                    clause_ref=chunk.clause_ref,
                    chunk_text=parent_text or chunk.chunk_text,
                    similarity=chunk.similarity,
                    document_type=chunk.document_type,
                )
            )
            citations.append(
                Citation(
                    kind=CitationKind.clause_ref,
                    ref=chunk.clause_ref or "",
                    detail=chunk.chunk_text,  # citation detail stays the LEAF unit text
                    source=chunk.policy_name or "Policy Document",
                    # R5 (FORK-P): parent document discriminator on the citation surface —
                    # same chunk field already consumed by GroundedClause above; no JOIN.
                    document_type=chunk.document_type,
                )
            )

        # R3-T3: source confidence from the SAME basis the gate used.  When the calibrated
        # τ* gate decided grounding, report the calibrated Platt probability; otherwise the
        # cosine rescale.  NOTE: local ALSO yields a calibrated_score (Platt params exist),
        # so we key off the GATE predicate (tau_star present), not bare calibrated presence —
        # this makes local report "cosine" and fixes the deferred R3-T2 QA finding.
        if tau_star is not None and top_calibrated is not None:
            confidence = top_calibrated
            confidence_source = "calibrated"
        else:
            # Fallback: cosine rescale (local provider or rerank/τ* not applied).
            confidence = _confidence(top_similarity, self._threshold)
            confidence_source = "cosine"

        logger.debug(
            "GroundedRetriever: grounded — %d clauses above threshold, confidence=%.4f",
            len(kept_chunks),
            confidence,
        )

        return GroundedRetrieval(
            query=query,
            status=GroundingStatus.grounded,
            clauses=tuple(kept_chunks),
            citations=tuple(citations),
            confidence=confidence,
            reason=None,
            threshold=self._threshold,
            confidence_source=confidence_source,
            tau_star=gate_tau_star,
            retrieval_audit=result.audit_trail,
        )


# ---------------------------------------------------------------------------
# Confidence helper
# ---------------------------------------------------------------------------


def _confidence(top_similarity: float, threshold: float) -> float:
    """Map top chunk similarity to a [0.5, 1.0] confidence score.

    Linear rescale of [threshold, 1.0] → [0.5, 1.0].
    Floor 0.5 reflects "we passed grounding but just barely."
    Monotonic and deterministic.
    """
    if threshold >= 1.0:
        # Degenerate: only a perfect match can ground; report full confidence.
        return 1.0
    raw = 0.5 + 0.5 * (top_similarity - threshold) / (1.0 - threshold)
    return float(max(0.5, min(1.0, raw)))
