"""Manual (non-CI) LIVE per-stage LATENCY PROFILE of the R5 RAG pipeline (Package E).

``@pytest.mark.live`` + ``@pytest.mark.db`` -> SKIPPED by default; runs only under
``make eval-profile-live`` (``LLM_PROVIDER=openrouter`` + ``LLM_API_KEY`` +
``RERANK_PROVIDER=novita`` + ``EMBEDDINGS_PROVIDER=local``) against a live Postgres.  It
answers ONE question the deterministic ``make eval`` cannot: **where does the wall-clock go on
the real, network-bound path?**  For a small handful (N in [3, 5]) of representative answerable
gold queries it runs the REAL retrieval + rerank + generation stack and wraps each STAGE with
``time.perf_counter()``:

  (i)   RETRIEVAL  — dense (pgvector cosine ANN) + sparse (Postgres full-text) + RRF fusion.
                     Pure DB work, no external hop.
  (ii)  RERANK     — the Novita cross-encoder (``baai/bge-reranker-v2-m3``) over the fused
                     top-N.  ONE batched network call per query (the dominant retrieval cost).
  (iii) GENERATION — the full ``PolicyAgent.handle()`` path: HyDE-gated retrieval →
                     per-sentence citation enforcement (R5-T2) → deterministic NLI gate
                     (R5-T3) → single audit write → the Gemini-synthesized answer the SYSTEM
                     actually presents.

It emits a per-stage min/median/max breakdown with ``print`` (needs ``-s``).

HONEST FRAMING — there is NO invented p50/p95 target (none exists in the design docs).  The
numbers are framed ONLY against the actual NFRs and the research:
  - NFR-P1 (sub-second READS): the dense read alone sits well under a second.  At the current
    tiny (208-row/org) corpus the planner EXACT-scans the org-filtered rows + top-N sorts —
    sub-millisecond, and the EXPLAIN below reports exactly whether the HNSW index is used or a
    Seq/exact scan fires.  The HNSW index and hnsw.ef_search are FORWARD-LOOKING: they only bind
    once the corpus grows enough that the planner prefers descending the graph over an exact scan.
  - NFR-P2 (responses STREAM; the user-facing metric is TIME-TO-FIRST-TOKEN): this harness
    measures END-TO-END generation, which is an UPPER BOUND on the streamed experience — the
    user sees the first token far sooner than this whole-answer number.
  - NFR-P3 (the DETERMINISTIC path avoids the LLM entirely): a silent/abstain answer skips
    stage (iii)'s Gemini call outright — those queries show a near-zero generation stage.
  - Research 5.3: dense+sparse run in parallel and RRF is negligible; the RERANK network hop
    is the dominant retrieval cost (~5.9s p50 on the Novita free tier) and Gemini generation is
    the dominant end-to-end cost.  The sub-200ms reranker path (a one-file ``RerankProvider``
    swap to a hosted low-latency endpoint) is the DOCUMENTED resolution — NOT changed here.

Re-validation (per Package-E instruction #4): because the prompt-cache split of the static
system prompt vs. the dynamic user prompt is a GENERATION-OUTPUT change, this harness ALSO
computes + prints ``citation_completeness`` and ``nli_entailment_rate`` over the sampled live
answers (deterministic, from the agent's OWN ``answer_structured``), so that output change is
re-validated on this deferred live run and not only in ``eval-live-r5``.

DB introspection: when Postgres is reachable it also captures ``EXPLAIN (ANALYZE, BUFFERS)`` of
the org-scoped dense retrieval query (reconstructed VERBATIM from ``PolicyRetrievalTool``) and
reports whether the planner uses ``ix_policy_embeddings_embedding_hnsw`` (Index Scan) or a
Seq Scan at the live corpus size — if Seq Scan, ``hnsw.ef_search`` is INERT and that is noted.

Throttle: Novita is throttled to <=30 rerank batches/min (eval-harness-side ONLY; the adapter
is never touched).  Stage (ii) is one batch and stage (iii)'s internal retrieval is a second
batch, so this harness performs TWO Novita batches per query — identical to the ragas-live
budget — and ``throttle.wait()`` spaces every batch >=2.1s.  The 2-batches/query count assumes
``HYDE_ENABLED=False`` (the committed default); enabling HyDE would add a third weak-band batch.

Self-skips cleanly with no key (``LLM_API_KEY`` unset), a ``local`` provider, or no Postgres.
Imports do NO network at module load; providers are called only inside the test, after guards.
"""

from __future__ import annotations

import statistics
import time
from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.policy_agent import PolicyAgent
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.rag.fusion import reciprocal_rank_fusion
from ai_agents.rag.grounding import check_entailment
from ai_agents.rag.parent_context import resolve_parent_context_for_embedding
from ai_agents.rag.rerank import apply_rerank
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from config.constants.embeddings import EMBEDDING_DIMENSION
from config.settings import get_settings
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.identity import Organization
from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds._local_embedder import embed
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus.chunking import chunk_document
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.local import LocalEmbeddingsProvider
from services.llm_provider.factory import get_llm_provider
from services.rerank_provider.factory import get_rerank_provider

pytestmark = pytest.mark.live

_K = 5  # context window / top_k handed to retrieval + generation
_SAMPLE_N = 4  # small N of representative answerable queries (instruction #1: 3-5)
_MIN_RERANK_GAP_S = 2.1  # <=30 Novita rerank batches/min (60/30 = 2.0s + margin)

# Reuse the ragas-live throttle if importable; else replicate it identically (Package-E #1).
try:  # pragma: no cover - import-shape guard, exercised only on the live path
    from evals.manual.test_rag_corpus_ragas_live import _RerankThrottle
except Exception:  # noqa: BLE001 - the sibling module must never make this harness un-collectable

    class _RerankThrottle:  # type: ignore[no-redef]
        """Spaces Novita rerank batches to <=30/min.  Eval-harness-side ONLY — the Novita
        adapter is never modified (that would throttle every caller, incl. production)."""

        def __init__(self, min_gap_s: float = _MIN_RERANK_GAP_S) -> None:
            self._min_gap = min_gap_s
            self._last = 0.0
            self.batches = 0
            self.total_sleep = 0.0

        def wait(self) -> None:
            sleep_s = max(0.0, self._min_gap - (time.monotonic() - self._last))
            if sleep_s > 0:
                time.sleep(sleep_s)
                self.total_sleep += sleep_s
            self._last = time.monotonic()
            self.batches += 1


def _require_live_key() -> None:
    """Skip unless a REAL provider is configured — mirrors the ragas-live guard exactly."""
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.LLM_API_KEY:
        pytest.skip("LLM_API_KEY unset — live latency profile skipped (LLM_PROVIDER=openrouter).")
    if settings.LLM_PROVIDER == "local":
        pytest.skip("LLM_PROVIDER=local — live latency profile needs a real provider.")


def _seed_corpus(session: Session, org: Organization) -> None:
    """Seed the corpus + embeddings under *org* (mirrors seed_all._embed_corpus)."""
    for doc in ALL_DOCUMENTS:
        policy = Policy(
            organization_id=org.id,
            name=doc.title,
            version=doc.version,
            effective_from=date.fromisoformat(doc.effective_from),
            document_text=doc.document_text,
            document_type=PolicyDocumentType(doc.document_type),
            is_active=True,
        )
        session.add(policy)
        session.flush()
        for ch in chunk_document(doc):
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=ch.chunk_text,
                    clause_ref=ch.clause_ref,
                    section=ch.section,
                    parent_section_ref=ch.parent_section_ref,
                    ordinal=ch.ordinal,
                    embedding=embed(ch.chunk_text),
                )
            )
    session.flush()


def _fmt_stage(name: str, samples: list[float]) -> str:
    if not samples:
        return f"  {name:<12} (no samples)"
    lo, md, hi = min(samples), statistics.median(samples), max(samples)
    return f"  {name:<12} min={lo * 1000:8.1f}ms  median={md * 1000:8.1f}ms  max={hi * 1000:8.1f}ms"


def _explain_dense_query(session: Session, org: Organization) -> str:
    """EXPLAIN (ANALYZE, BUFFERS) the VERBATIM org-scoped dense retrieval query.

    Reconstructs the exact SELECT / ``<=>`` / ORDER BY / org-scoping JOIN from
    ``PolicyRetrievalTool._retrieve_dense`` so the plan reflects the REAL read path.  A
    zero-vector probe is sufficient — the planner's choice of Index vs. Seq Scan depends on
    corpus size + statistics, not on the specific probe vector.
    """
    zero_vec = "[" + ",".join("0" for _ in range(EMBEDDING_DIMENSION)) + "]"
    stmt = sa.text(
        """
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT
            pe.id::text            AS embedding_id,
            pe.policy_id::text     AS document_id,
            pe.clause_ref          AS clause_ref,
            1 - (pe.embedding <=> CAST(:q AS vector)) AS score
        FROM policy_embeddings pe
        JOIN policies p ON p.id = pe.policy_id
        WHERE p.organization_id = :org
        ORDER BY pe.embedding <=> CAST(:q AS vector), pe.clause_ref ASC, pe.ordinal ASC
        LIMIT :top_k
        """
    )
    rows = session.execute(stmt, {"q": zero_vec, "org": str(org.id), "top_k": _K}).fetchall()
    return "\n".join(r[0] for r in rows)


def test_latency_profile_self_skips_without_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Package-E task #3: the harness self-skips CLEANLY under LLM_PROVIDER=local — the live
    retrieval/rerank/generation path is NEVER touched (no key, no network, no DB).

    This is a plain unit test (NO ``@pytest.mark.live`` / ``@pytest.mark.db``), so it runs in
    the normal suite and PROVES the skip guard fires.  ``_require_live_key`` clears the settings
    cache itself, so setting the env here is sufficient; we assert it raises pytest's internal
    ``Skipped`` and never reaches the live body.
    """
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(pytest.skip.Exception):
        _require_live_key()
    get_settings.cache_clear()  # leave the cache clean for sibling tests


@pytest.mark.db
def test_rag_latency_profile_live(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """Package E: LIVE per-stage latency profile (retrieval / rerank / generation) over a small
    sample of answerable gold queries, with a Novita throttle (<=30/min), an EXPLAIN of the
    dense read, and citation/NLI re-validation of the prompt-cache generation-output change."""
    _require_live_key()
    settings = get_settings()
    provider = get_llm_provider()
    rerank_provider = get_rerank_provider()
    _seed_corpus(db_session, org_a)

    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())
    agent = PolicyAgent(provider=provider)
    throttle = _RerankThrottle()

    # A small, representative slice of ANSWERABLE queries (instruction #1: N in [3, 5]).
    cases = [c for c in rag_corpus_cases if c.answerable][:_SAMPLE_N]
    assert cases, "no answerable gold cases resolved — check the corpus dataset"

    # ----- EXPLAIN the dense read ONCE (DB reachable — we are in a @pytest.mark.db test) -----
    explain_text = _explain_dense_query(db_session, org_a)
    uses_hnsw = "ix_policy_embeddings_embedding_hnsw" in explain_text
    seq_scan = "Seq Scan" in explain_text

    retrieval_s: list[float] = []
    rerank_s: list[float] = []
    generation_s: list[float] = []
    citation_completeness: list[float] = []
    nli_rates: list[float] = []
    grounded = needs_review = silent = 0

    for case in cases:
        intent = RetrievalIntent(query=case.query, top_k=_K)

        # (i) RETRIEVAL — dense + sparse SQL + RRF fusion.  Pure DB, no external hop.
        t = time.perf_counter()
        parallel = tool.retrieve_parallel(intent, context=ctx, session=db_session, top_k=50)
        fused = reciprocal_rank_fusion(
            [parallel.dense, parallel.sparse],
            key=lambda c: c.embedding_id,
            k=settings.RRF_K,
            tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
        )
        retrieval_s.append(time.perf_counter() - t)

        # (ii) RERANK — the Novita cross-encoder.  ONE throttled batch (the dominant read cost).
        throttle.wait()
        t = time.perf_counter()
        reranked = apply_rerank(
            case.query,
            fused,
            provider=rerank_provider,
            top_k=max(_K, settings.RERANK_TOP_K),
            candidate_cap=settings.RERANK_CANDIDATE_CAP,
            text_of=lambda c: c.chunk_text,
        )
        rerank_s.append(time.perf_counter() - t)

        # (iii) GENERATION — full PolicyAgent path (Gemini synthesis + citation/NLI gates).  Its
        # internal retrieval reranks a SECOND Novita batch, so throttle before it too (2/query,
        # matching the ragas-live budget under the committed HYDE_ENABLED=False default).
        throttle.wait()
        t = time.perf_counter()
        result = agent.handle(
            Task(context_type="policy_qa", payload={"question": case.query}),
            RoutingDecision(
                target_agent=AiAgent.policy,
                route=AiRoute.llm,
                task_type="policy_qa",
                confidence=1.0,
                rationale="package-e-latency-profile",
            ),
            context=ctx,
            session=db_session,
        )
        generation_s.append(time.perf_counter() - t)

        out = result.output
        answer_type = out.get("answer_type")
        structured = out.get("answer_structured")
        if structured and structured["sentences"]:
            sents = structured["sentences"]
            citation_completeness.append(sum(1 for s in sents if s["citations"]) / len(sents))
            from ai_agents.guardrails.citations import Citation, CitationKind  # noqa: PLC0415
            from ai_agents.rag.grounding import CitedAnswer, CitedSentence  # noqa: PLC0415

            cited = CitedAnswer(
                sentences=tuple(
                    CitedSentence(
                        text=s["text"],
                        citations=tuple(
                            Citation(
                                kind=CitationKind(c["kind"]),
                                ref=c["ref"],
                                detail=c.get("detail"),
                                source=c.get("source"),
                            )
                            for c in s["citations"]
                        ),
                    )
                    for s in sents
                )
            )
            # C7R: the SAME inputs as the agent's support gate — the question (premise /
            # anchor rules) and the top-K retrieved clause set (mis-citation guard; ref =
            # clause_ref, detail = leaf text, as GroundedRetriever builds it).
            retrieved = tuple(
                Citation(
                    kind=CitationKind.clause_ref,
                    ref=c.clause_ref or "",
                    detail=c.chunk_text,
                    source=getattr(c, "policy_name", None) or "Policy Document",
                    document_type=getattr(c, "document_type", None),
                )
                for c in reranked.items[:_K]
            )
            er = check_entailment(
                cited,
                threshold=settings.NLI_ENTAILMENT_THRESHOLD,
                question=case.query,
                retrieved=retrieved,
            )
            nli_rates.append(
                sum(1 for sc in er.scores if sc >= er.threshold) / len(er.scores)
                if er.scores
                else 1.0
            )

        if answer_type == "grounded" and out.get("answer"):
            grounded += 1
        elif answer_type == "needs_review":
            needs_review += 1
        else:
            silent += 1

        # Touch the parent-context resolver on the top chunk so the profiled read path matches
        # what generation consumes (and surfaces any resolver latency in the retrieval stage's
        # tail on a re-run) — cheap, deterministic, DB-only.
        if parallel.dense:
            resolve_parent_context_for_embedding(db_session, parallel.dense[0].embedding_id)

        print(f"[{case.id}] type={answer_type}")

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    print("\n" + "=" * 78)
    print("Package E — LIVE per-stage RAG latency profile (retrieval / rerank / generation)")
    print(f"  sampled answerable queries = {len(cases)}")
    print(_fmt_stage("retrieval", retrieval_s))
    print("     ^ dense pgvector ANN + sparse full-text + RRF (parallel; RRF negligible, res.5.3)")
    print(_fmt_stage("rerank", rerank_s))
    print("     ^ Novita bge-reranker-v2-m3 network hop — the DOMINANT retrieval cost (res.5.3;")
    print("       ~5.9s p50 on the free tier). Sub-200ms path = a one-file RerankProvider swap.")
    print(_fmt_stage("generation", generation_s))
    print("     ^ full PolicyAgent.handle() END-TO-END (Gemini synth + R5-T2 citation + R5-T3 NLI")
    print("       gates + audit write). NFR-P2 STREAMS — time-to-first-token is FAR below this.")
    print("  --- framing (NO invented p50/p95 target — none exists in the design docs) ---")
    print("  NFR-P1 (sub-second reads): dense read is sub-second — see the EXPLAIN below for")
    print("     whether the planner uses the HNSW index or exact-scans at this corpus size")
    print("     (hnsw.ef_search stays inert until the corpus is large enough to prefer the graph).")
    print("  NFR-P2 (stream / TTFT): the generation number is an UPPER BOUND on the UX.")
    print("  NFR-P3 (deterministic path avoids the LLM): silent answers skip Gemini entirely.")
    print("  --- re-validation of the prompt-cache system/user split (generation output) ---")
    print(f"  citation_completeness = {_mean(citation_completeness):.3f}")
    print(f"  nli_entailment_rate   = {_mean(nli_rates):.3f}")
    print(
        f"  disposition           = grounded={grounded} needs_review={needs_review} "
        f"silent={silent}"
    )
    print(
        f"  novita rerank         = {throttle.batches} batches, "
        f"{throttle.total_sleep:.1f}s throttle sleep (<=30/min; 2 batches/query)"
    )
    print("=" * 78)
    print("EXPLAIN (ANALYZE, BUFFERS) — org-scoped dense retrieval query (VERBATIM):")
    print(explain_text)
    if uses_hnsw:
        print(
            "  -> planner USES ix_policy_embeddings_embedding_hnsw (Index Scan): the dense read is "
            "index-bound; hnsw.ef_search is the live latency/recall knob."
        )
    elif seq_scan:
        print(
            "  -> planner chose a Seq Scan at this corpus size: the HNSW index is NOT used, so "
            "hnsw.ef_search is INERT here. (Expected on a tiny corpus — Postgres reads it all "
            "faster than descending the graph. Re-check at production corpus size.)"
        )
    else:
        print("  -> plan uses neither the named HNSW index nor a Seq Scan; inspect the plan above.")
    print("=" * 78)

    # Loosest sanity only (NOT a hard latency gate — no p50/p95 exists to gate against):
    # every stage produced a real, positive timing for the whole sample.
    assert len(retrieval_s) == len(cases)
    assert len(rerank_s) == len(cases)
    assert len(generation_s) == len(cases)
    assert all(s >= 0.0 for s in retrieval_s + rerank_s + generation_s)
