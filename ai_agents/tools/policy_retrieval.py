"""PolicyRetrievalTool — grounded policy RAG via pgvector (P4-T4).

Design:
  - Embeds the query via an injected EmbeddingsProvider (must be the SAME
    embedder that seeded policy_embeddings — dev=LocalEmbeddingsProvider).
  - Runs a cosine ANN query over org-scoped policy_embeddings via pgvector.
  - Returns top-k RetrievedChunks with clause_ref + similarity; NO threshold,
    NO "policy is silent" decision (P4-T6), NO answer assembly, NO re-index (P4-T5).
  - Org-scoping: policy_embeddings has no organization_id — JOINed to policies.

Downstream seam: chunks feed Policy Agent (P4-T8) retrieval path.
clause_ref → Citation(kind=clause_ref, ref=clause_ref, detail=chunk_text, source=policy_name).

R1-T2 (hybrid retrieval, part 1): ``retrieve_parallel`` runs TWO parallel paths — top-50
dense (cosine, widened) AND top-50 sparse (Postgres full-text ``ts_rank_cd`` over the
``chunk_tsv`` GIN index) — and returns BOTH ranked lists un-fused.

R1-T4 (hybrid retrieval, part 2): ``retrieve()`` now DELEGATES to ``retrieve_parallel`` and
returns the Reciprocal-Rank-Fused (``ai_agents.rag.fusion``) ranking — same public signature
and ``RetrievalResult`` shape, so the Policy Agent tool path and the eval harness pick up the
fused ranking without an interface change.  The answerability signal stays on the dense-cosine
scale (``max_dense_similarity`` + per-chunk ``similarity``); RRF scores never reach the gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools.base import ToolError
from config.constants.embeddings import HNSW_EF_SEARCH
from config.settings import get_settings
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.interface import EmbeddingsProvider

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 5


@dataclass(frozen=True)
class RetrievalIntent:
    """Structured retrieval query intent."""

    query: str
    top_k: int = _DEFAULT_TOP_K


@dataclass(frozen=True)
class RetrievedChunk:
    """One policy chunk retrieved by ANN similarity search.

    clause_ref → Citation(kind=clause_ref, ref=clause_ref,
                          detail=chunk_text, source=policy_name).
    similarity ∈ [0, 1]: 1 = identical, 0 = orthogonal (cosine similarity).
    """

    embedding_id: str  # PolicyEmbedding.id (UUID as str)
    clause_ref: str | None  # citation pointer; None for unlabelled chunks
    chunk_text: str  # policy clause text → Citation.detail
    similarity: float  # DENSE cosine (1 - cosine_distance); 0.0 for a sparse-only fused chunk
    policy_name: str  # → Citation.source
    # R1-T4: True when the sparse (lexical) path surfaced this chunk.  The grounding step
    # keeps sparse hits even when their dense cosine is below the 0.20 gate — they have no
    # meaningful cosine, so culling them by cosine would defeat hybrid retrieval (CRUX A).
    from_sparse: bool = False
    # R2-T2: the cross-encoder rerank score for this chunk (None when the reranker did not
    # run for it — a fail-safe fallback, or a tail candidate beyond the rerank cap).
    # OBSERVABILITY ONLY — never read by the answerability gate (which reads dense cosine).
    rerank_score: float | None = None
    # R2-T3: calibrated probability of relevance — Platt scaling of ``rerank_score`` with the
    # active provider's params.  EXPOSED for observability + R3's future threshold; it NEVER
    # reorders results (I5) and is NEVER read by the answerability gate (I2).  None when the
    # reranker did not run for this chunk or no calibration params exist for the provider.
    calibrated_score: float | None = None
    # R5-T2 (FORK-P): the parent document's discriminator (``policies.document_type``:
    # policy|faq|howto|reference; None on the legacy single-policy stub).  Propagated to
    # GroundedClause so the citation label can qualify the document ("[T&E Policy §AIR-01]").
    # Additive + nullable; NEVER read by retrieval / fusion / gate logic.
    document_type: str | None = None


@dataclass(frozen=True)
class CandidateAudit:
    """R6-T3 (audit completeness): the IDENTITY + NUMERICS of ONE reranked candidate, captured
    for the ``ai_decision_logs`` audit trail.  Structured identity + numerics ONLY — NEVER
    ``chunk_text`` — so the audit row stays small and queryable.  Additive + observability-only:
    NEVER read by retrieval / fusion / gate logic (same discipline as R1's ``max_dense_similarity``
    / ``from_sparse``).

      - ``document_id`` — parent policy/document id (= ``policy_id`` UUID), recovered from the
        fused ``RetrievalCandidate`` (it is intentionally dropped when building ``RetrievedChunk``).
      - ``unit_id``     — within-document unit id (``clause_ref``; None for unlabelled chunks).
      - ``fused_rank``  — 1-based rank in the RRF-fused order (BEFORE the rerank reorder).
      - ``rerank_score``— the cross-encoder rerank score (None when the reranker did not run).
    """

    document_id: str
    unit_id: str | None
    fused_rank: int
    rerank_score: float | None


@dataclass(frozen=True)
class RetrievalResult:
    """Ranked retrieval results (RRF-fused order, R1-T4), no threshold applied.

    ``max_dense_similarity`` is the answerability signal the downstream 0.20 gate reads:
    the MAX dense cosine over the candidates (== ``dense[0].score``), on the SAME scale as
    before fusion.  The gate is fenced from fusion STRUCTURALLY — it reads this cosine scalar,
    and an RRF score is never compared to the threshold nor stored on a chunk's ``similarity``
    — so even a mis-tuned ``RRF_K`` could not leak into the answer/abstain decision (the tiny
    ~0.03 RRF magnitude is only secondary corroboration).  ``None`` (the default) means
    "unset" — a hermetic fake retriever that omits it; the gate then falls back to the top
    chunk's cosine.
    """

    query: str
    chunks: tuple[RetrievedChunk, ...]  # RRF-fused then reranked order (R1-T4, R2-T2)
    max_dense_similarity: float | None = None  # max dense cosine; the answerability signal
    # R2-T2: False when the rerank stage failed and the proven R1 RRF order was kept
    # (fail-SAFE).  Additive + defaulted True so nothing the gate reads is disturbed.
    rerank_applied: bool = True
    # R6-T3 (audit completeness): the per-candidate audit trail for the reranked top-N that
    # became ``chunks`` — a ``CandidateAudit`` each ({document_id, unit_id, fused_rank,
    # rerank_score}).  Additive + nullable (hermetic fakes omit it), observability-ONLY: NEVER
    # read by the gate.  Carries the candidate identity + numerics up to the SINGLE decision-log
    # write (mirrors ``max_dense_similarity``); capped at ``top_k`` by construction.
    audit_trail: tuple[CandidateAudit, ...] | None = None


# R1-T2: each path returns up to this many candidates for downstream fusion (R1-T3 RRF).
_PARALLEL_TOP_K = 50


@dataclass(frozen=True)
class RetrievalCandidate:
    """One candidate from a SINGLE retrieval path (dense OR sparse), R1-T2.

    Built UNIFORMLY by both paths so fusion (R1-T3) can key on a single shape:
      - ``embedding_id`` — the stable per-chunk key RRF fuses on (a dense candidate and a
        sparse candidate for the same chunk share it).  Never None.
      - ``document_id``  — the parent policy/document id (= ``policy_id``).
      - ``clause_ref``   — the within-document unit id (citation pointer; None if unset).
      - ``section`` / ``parent_section_ref`` — feed the R1-T4 parent-context resolver.
      - ``policy_name``  — the document title (JOIN-derived, NOT denormalized on the row).
      - ``score``        — path-native: cosine similarity (dense) | ts_rank_cd (sparse).
      - ``rank``         — 1-based rank WITHIN this path (dense and sparse rank separately).
      - ``document_type``— parent document discriminator (JOIN-derived; R5-T2/FORK-P).
    """

    embedding_id: str
    document_id: str
    clause_ref: str | None
    chunk_text: str
    section: str | None
    parent_section_ref: str | None
    policy_name: str
    score: float
    rank: int
    # R5-T2 (FORK-P): ``policies.document_type::text`` (policy|faq|howto|reference | None).
    # Additive + nullable so both paths key on one shape and fusion carries it through.
    document_type: str | None = None


@dataclass(frozen=True)
class ParallelRetrievalResult:
    """The two UN-FUSED ranked candidate lists from the dense and sparse paths (R1-T2).

    Fusion (RRF) is R1-T3 — this carrier deliberately keeps the lists separate.  An empty
    ``sparse`` tuple is valid and expected for a query with no lexical match (e.g. an
    all-stopword utterance, or terms absent from the corpus).
    """

    query: str
    dense: tuple[RetrievalCandidate, ...]  # cosine similarity desc, <= top_k
    sparse: tuple[RetrievalCandidate, ...]  # ts_rank_cd desc, <= top_k (() if no match)


class PolicyRetrievalTool:
    """Grounded policy RAG tool: embed → pgvector ANN → top-k chunks.

    Constructor: inject embedder for tests; defaults to the factory (LocalEmbeddingsProvider
    in dev — the SAME embedder used to seed policy_embeddings; if mismatched, retrieval
    degrades gracefully but results will be meaningless).
    """

    def __init__(self, embedder: EmbeddingsProvider | None = None) -> None:
        self._embedder = embedder or get_embeddings_provider()

    def retrieve(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Session,
    ) -> RetrievalResult:
        """Return the top-k chunks as the RRF-fused dense+sparse ranking (R1-T4).

        Delegates to ``retrieve_parallel`` (one dense SQL + one sparse SQL) and fuses the two
        ranked lists with Reciprocal Rank Fusion (``settings.RRF_K``), keying on
        ``embedding_id`` and breaking ties on the reproducible ``clause_ref`` (NEVER the
        per-seed UUID).  Same public signature + ``RetrievalResult`` shape; no threshold
        filtering, no answer assembly.

        Answerability is preserved for the downstream 0.20 gate (CRUX A):
          - each chunk's ``similarity`` stays the DENSE cosine (sparse-only chunks → 0.0), so
            the gate never sees an RRF score;
          - ``max_dense_similarity`` carries ``dense[0].score`` — the unchanged-scale signal.
        ``from_sparse`` marks lexical hits so grounding keeps them without a cosine cull.
        """
        if not intent.query.strip():
            raise ToolError(
                "Retrieval query must not be empty.",
                code="POLICY_RETRIEVAL_TOOL",
            )

        # Lazy import: avoids the ai_agents.rag package __init__ <-> policy_retrieval import
        # cycle (rag/__init__ -> retrieval -> policy_retrieval).  fusion.py is backend-free.
        from ai_agents.rag.fusion import reciprocal_rank_fusion  # noqa: PLC0415

        parallel = self.retrieve_parallel(
            intent, context=context, session=session, top_k=_PARALLEL_TOP_K
        )
        settings = get_settings()
        fused = reciprocal_rank_fusion(
            [parallel.dense, parallel.sparse],
            key=lambda c: c.embedding_id,
            k=settings.RRF_K,
            tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
        )

        # R2-T2: cross-encoder rerank stage — rescores the RRF top-N for precision and
        # reorders, with a fail-SAFE fallback to the R1 RRF order (NEVER raises, NEVER
        # abstains).  Purely a reordering of the context list: ``max_dense`` (the
        # answerability signal) is computed below from the DENSE path and is UNTOUCHED, so
        # the 0.20 gate's coverage/abstention is byte-identical to R1 regardless of order.
        reranked, rerank_applied, rerank_scores = self._apply_rerank_stage(intent, fused, settings)

        # R2-T3: load the ACTIVE provider's Platt calibration params ONCE (cached) and annotate
        # each reranked chunk with a calibrated probability.  NON-REORDERING (I5): the order is
        # the reranker's; this only sets ``calibrated_score``.  It does NOT touch ``max_dense``
        # (the answerability signal, I2).  Lazy import keeps ai_agents import-time minimal.
        from ai_agents.rag.calibration import apply_platt, load_calibration_params  # noqa: PLC0415

        calib_params = load_calibration_params(settings.RERANK_PROVIDER) if rerank_applied else None

        dense_cosine = {c.embedding_id: c.score for c in parallel.dense}
        sparse_ids = {c.embedding_id for c in parallel.sparse}
        max_dense = parallel.dense[0].score if parallel.dense else 0.0

        top_n = max(1, intent.top_k)  # the SOLE final truncation (unchanged from R1)
        chunks = tuple(
            RetrievedChunk(
                embedding_id=c.embedding_id,
                clause_ref=c.clause_ref,
                chunk_text=c.chunk_text,
                similarity=dense_cosine.get(c.embedding_id, 0.0),  # DENSE cosine, never RRF
                policy_name=c.policy_name,
                from_sparse=c.embedding_id in sparse_ids,
                rerank_score=rscore,
                calibrated_score=(
                    apply_platt(rscore, calib_params)
                    if rscore is not None and calib_params is not None
                    else None
                ),
                document_type=c.document_type,
            )
            for c, rscore in zip(reranked[:top_n], rerank_scores[:top_n], strict=False)
        )
        # R6-T3 (audit completeness): capture the per-candidate identity + numerics for the
        # SINGLE decision-log write — the reranked top-N that became ``chunks``.  ``document_id``
        # is recovered HERE from the fused RetrievalCandidate (intentionally NOT carried onto
        # RetrievedChunk); ``fused_rank`` is the 1-based RRF position BEFORE the rerank reorder.
        # Pure observability — nothing the answerability gate reads is touched.
        fused_rank_by_id = {c.embedding_id: i + 1 for i, c in enumerate(fused)}
        audit_trail = tuple(
            CandidateAudit(
                document_id=c.document_id,
                unit_id=c.clause_ref,
                fused_rank=fused_rank_by_id.get(c.embedding_id, 0),
                rerank_score=rscore,
            )
            for c, rscore in zip(reranked[:top_n], rerank_scores[:top_n], strict=False)
        )
        logger.debug(
            "PolicyRetrievalTool.retrieve: query=%r org=%s top_k=%d dense=%d sparse=%d "
            "fused=%d rerank_applied=%s max_dense=%.4f returned=%d",
            intent.query,
            context.organization_id,
            intent.top_k,
            len(parallel.dense),
            len(parallel.sparse),
            len(fused),
            rerank_applied,
            max_dense,
            len(chunks),
        )
        return RetrievalResult(
            query=intent.query,
            chunks=chunks,
            max_dense_similarity=max_dense,
            rerank_applied=rerank_applied,
            audit_trail=audit_trail,
        )

    def _apply_rerank_stage(
        self,
        intent: RetrievalIntent,
        fused: list[RetrievalCandidate],
        settings: Any,
    ) -> tuple[list[RetrievalCandidate], bool, list[float | None]]:
        """Run the R2 rerank stage over the fused candidates; fail-SAFE to the RRF order.

        Returns ``(reordered_candidates, rerank_applied, rerank_scores)`` with ``scores``
        parallel to the candidates.  BOTH provider acquisition and the call are guarded: any
        failure degrades to the input RRF order (``applied=False``) — the answerability gate
        and the answer/abstain decision are never affected (I4).

        ``top_k=max(intent.top_k, RERANK_TOP_K)`` so the reranker never truncates BELOW the
        caller's requested top_k — the caller's ``[: intent.top_k]`` slice stays the SOLE final
        truncation, so the recall@k measurement WINDOW is not narrowed (order WITHIN it is
        reordered by the reranker).  Imports are lazy to mirror the fusion import and keep
        ``ai_agents`` import-time free of the services rerank package.
        """
        try:
            from ai_agents.rag.rerank import apply_rerank  # noqa: PLC0415 — avoid import cycle
            from services.rerank_provider.factory import (  # noqa: PLC0415 — lazy, layering
                get_rerank_provider,
            )

            outcome = apply_rerank(
                intent.query,
                fused,
                provider=get_rerank_provider(),
                top_k=max(intent.top_k, settings.RERANK_TOP_K),
                candidate_cap=settings.RERANK_CANDIDATE_CAP,
                text_of=lambda c: c.chunk_text,
            )
            return outcome.items, outcome.applied, outcome.scores
        except Exception:  # noqa: BLE001 — rerank is best-effort; never break retrieval (I4)
            logger.warning("rerank stage unavailable; using R1 RRF order", exc_info=True)
            return list(fused), False, [None] * len(fused)

    # ------------------------------------------------------------------
    # R1-T2 — parallel dense + sparse retrieval (org-scoped, multi-document)
    # ------------------------------------------------------------------

    def retrieve_parallel(
        self,
        intent: RetrievalIntent,
        *,
        context: AgentContext,
        session: Session,
        top_k: int = _PARALLEL_TOP_K,
    ) -> ParallelRetrievalResult:
        """Retrieve top-k from the DENSE and SPARSE paths in parallel; return both (R1-T2).

        Both paths are org-scoped via the SAME JOIN to ``policies`` (``policy_embeddings``
        has no ``organization_id`` — a missing JOIN would be a cross-tenant leak) and span
        all of the org's documents.  The two ranked lists are returned WITHOUT fusion
        (RRF is R1-T3).

        Invariants:
          - the dense path uses the IDENTICAL query embedding, ``<=>`` cosine operator and
            ``ORDER BY`` as the live ``retrieve()``, so its top-N matches ``retrieve()``
            top-N for the same query (the duplicated SQL cannot silently diverge);
          - the sparse path is pure Postgres full-text — NO embedder, NO external calls —
            so it is deterministic and CI-safe;
          - an empty sparse list is valid (no lexical match) and returned as ``()``.
        """
        if not intent.query.strip():
            raise ToolError(
                "Retrieval query must not be empty.",
                code="POLICY_RETRIEVAL_TOOL",
            )
        k = max(1, top_k)
        dense = self._retrieve_dense(intent.query, context=context, session=session, top_k=k)
        sparse = self._retrieve_sparse(intent.query, context=context, session=session, top_k=k)
        logger.debug(
            "PolicyRetrievalTool.retrieve_parallel: query=%r org=%s top_k=%d dense=%d sparse=%d",
            intent.query,
            context.organization_id,
            k,
            len(dense),
            len(sparse),
        )
        return ParallelRetrievalResult(query=intent.query, dense=dense, sparse=sparse)

    def _retrieve_dense(
        self,
        query: str,
        *,
        context: AgentContext,
        session: Session,
        top_k: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """Dense cosine ANN path, widened, carrying full candidate metadata (R1-T2).

        Same embedding + ``<=>`` operator + org-scoping JOIN + ``ORDER BY`` as the live
        ``retrieve()``; only the projected columns and LIMIT widen.
        """
        vec = self._embed(query)
        vec_literal = "[" + ",".join(str(x) for x in vec) + "]"
        stmt = sa.text(
            """
            SELECT
                pe.id::text            AS embedding_id,
                pe.policy_id::text     AS document_id,
                pe.clause_ref          AS clause_ref,
                pe.chunk_text          AS chunk_text,
                pe.section             AS section,
                pe.parent_section_ref  AS parent_section_ref,
                p.name                 AS policy_name,
                p.document_type::text  AS document_type,
                1 - (pe.embedding <=> CAST(:q AS vector)) AS score
            FROM policy_embeddings pe
            JOIN policies p ON p.id = pe.policy_id
            WHERE p.organization_id = :org
            ORDER BY pe.embedding <=> CAST(:q AS vector), pe.clause_ref ASC, pe.ordinal ASC
            LIMIT :top_k
            """
        )
        # HNSW query-time search breadth — set TRANSACTION-LOCALLY (is_local=true) immediately
        # before the dense ANN query so it applies to THIS SELECT and never leaks across pooled
        # connections (no pool leakage; provable via SHOW/EXPLAIN).  Resilient: a mock/non-PG
        # session (hermetic tests) must never break retrieval — mirror the parent-context
        # try/except degrade style.  DENSE-ONLY: HNSW is irrelevant to the sparse (GIN) path.
        # INERT if the planner seq-scans at ~208 rows (forward-looking as the corpus grows).
        try:
            session.execute(
                sa.text("SELECT set_config('hnsw.ef_search', :ef, true)"),
                {"ef": str(HNSW_EF_SEARCH)},
            )
        except Exception:  # noqa: BLE001 — GUC tuning is best-effort; never break retrieval
            logger.debug("could not set hnsw.ef_search (non-PG/mock session?); continuing")
        rows = session.execute(
            stmt,
            {"q": vec_literal, "org": str(context.organization_id), "top_k": top_k},
        ).fetchall()
        return tuple(
            RetrievalCandidate(
                embedding_id=row.embedding_id,
                document_id=row.document_id,
                clause_ref=row.clause_ref,
                chunk_text=row.chunk_text,
                section=row.section,
                parent_section_ref=row.parent_section_ref,
                policy_name=row.policy_name,
                score=float(row.score),
                rank=i,
                document_type=row.document_type,
            )
            for i, row in enumerate(rows, start=1)
        )

    def _retrieve_sparse(
        self,
        query: str,
        *,
        context: AgentContext,
        session: Session,
        top_k: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """Sparse lexical (BM25-flavored) path over the ``chunk_tsv`` GIN index (R1-T2/R1-T4).

        Pure Postgres full-text — NO embedder, NO external calls — so it is deterministic
        and CI-safe.  Org-scoped via the SAME JOIN to ``policies`` as the dense path.  The
        regconfig 'english' MUST match ``database.seeds.chunk_tsv.TSV_REGCONFIG``.

        R1-T4 (CRUX B): ``plainto_tsquery`` joins lexemes with ``&`` (AND), which empties on
        intent-mismatch utterances (e.g. "I want to make a trip to New York" →
        ``'want' & 'make' & 'trip' & 'new' & 'york'`` matches no single chunk).  A genuinely
        BM25-flavored sparse path is OR-ish (it sums over MATCHING terms, never requires all),
        so we convert the AND query to OR by rewriting ``' & '`` → ``' | '`` on the
        ``plainto_tsquery`` text and re-parsing with ``to_tsquery`` — preserving english
        stemming/normalization while changing only the operator.  OR is a superset of AND
        (the exact-term probes still pass); ``ts_rank_cd`` stays cover-density (units matching
        more/denser query terms rank above units matching only a common term — the accepted
        native-Postgres tradeoff).  An empty/all-stopword query yields an empty tsquery →
        ``()`` (the rewrite is empty-safe).  A deterministic secondary/tertiary sort
        (clause_ref, ordinal) — both STABLE keys, never the per-seed ``id`` UUID — makes
        ts_rank_cd ties reproducible across re-seeds.
        """
        stmt = sa.text(
            """
            SELECT
                pe.id::text            AS embedding_id,
                pe.policy_id::text     AS document_id,
                pe.clause_ref          AS clause_ref,
                pe.chunk_text          AS chunk_text,
                pe.section             AS section,
                pe.parent_section_ref  AS parent_section_ref,
                p.name                 AS policy_name,
                p.document_type::text  AS document_type,
                ts_rank_cd(
                    pe.chunk_tsv,
                    to_tsquery(
                        'english',
                        regexp_replace(plainto_tsquery('english', :q)::text, ' & ', ' | ', 'g')
                    )
                ) AS score
            FROM policy_embeddings pe
            JOIN policies p ON p.id = pe.policy_id
            WHERE p.organization_id = :org
              AND pe.chunk_tsv @@ to_tsquery(
                    'english',
                    regexp_replace(plainto_tsquery('english', :q)::text, ' & ', ' | ', 'g')
                  )
            ORDER BY score DESC, pe.clause_ref ASC, pe.ordinal ASC
            LIMIT :top_k
            """
        )
        rows = session.execute(
            stmt,
            {"q": query, "org": str(context.organization_id), "top_k": top_k},
        ).fetchall()
        return tuple(
            RetrievalCandidate(
                embedding_id=row.embedding_id,
                document_id=row.document_id,
                clause_ref=row.clause_ref,
                chunk_text=row.chunk_text,
                section=row.section,
                parent_section_ref=row.parent_section_ref,
                policy_name=row.policy_name,
                score=float(row.score),
                rank=i,
                document_type=row.document_type,
            )
            for i, row in enumerate(rows, start=1)
        )

    # ------------------------------------------------------------------

    def _embed(self, query: str) -> list[float]:
        try:
            return self._embedder.embed_texts([query])[0]
        except Exception as exc:
            raise ToolError(
                f"Embedding failed for retrieval query: {exc}",
                code="POLICY_RETRIEVAL_TOOL",
            ) from exc
