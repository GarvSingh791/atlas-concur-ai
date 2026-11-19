"""Tier-2 DB BASELINE for multi-document RAG retrieval over the Helios corpus (R0-T6).

``@pytest.mark.db`` — needs Postgres + pgvector (the ``eval-db`` CI job; runs under
``make eval`` / ``make eval-db``).  Seeds the corpus into two orgs (org_a measured,
org_b for the cross-tenant probe) in a ROLLED-BACK session — harness-owned seeding
that mirrors ``seed_all._embed_corpus`` (same chunker + deterministic embedder), so
the measured vectors are byte-identical to production but isolated from other tests.

Computes + PRINTS the BASELINE the later phases improve on:
  - RETRIEVAL on the raw unit ranking (independent of the 0.20 abstention gate):
    recall@k / precision@k / hit@k / MRR / nDCG@k / MAP, overall + per query-type +
    per document-type;
  - ABSTENTION on the unanswerable set + coverage on the answerable set;
  - a RISK-COVERAGE curve + AURC over the cosine top-1 signal (seeds R3);
  - a deterministic parent-context faithfulness proxy (real number: ``make eval-live``).

R0 has NO hard regression floors (those arrive at R6-T4): the gates here are only that
it runs green, the stub is retired, and org-scoping holds.  Merges a ``rag_corpus``
section into evals/reports/metrics.json.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from time import perf_counter
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.parent_context import resolve_parent_context_for_embedding
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from database.models.identity import Organization
from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds.chunk_tsv import populate_chunk_tsv
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus.chunking import chunk_document
from database.seeds.corpus.contextual import context_prefix
from evals import thresholds
from evals.harness import metrics
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.factory import get_embeddings_provider

pytestmark = pytest.mark.db

_RETRIEVE_K = 25  # wide raw ranking (for MRR/MAP + gold-rank reporting)
_METRIC_K = 10  # the @k for recall/precision/hit/nDCG
_METRIC_K_5 = 5  # R2-T4: nDCG@5 / precision@5 — the cross-encoder's primary ordering metric
_SILENT_GATE = 0.20  # mirrors ai_agents/rag/retrieval.py RETRIEVAL_SIMILARITY_THRESHOLD
_STUB_REFS = frozenset(
    {
        "AIR-CABIN",
        "AIR-PREF",
        "HOTEL-CAP",
        "HOTEL-PREF",
        "MEALS-PD",
        "GROUND-CLASS",
        "RECEIPT-REQ",
        "SUSTAIN",
    }
)

_CORPUS_CACHE: dict[str, Any] | None = None

# R0 dense-only baseline (recorded in evals/reports/metrics.json `rag_corpus`, pre-hybrid).
# The labeled pre-hybrid reference this R1 hybrid (RRF) cycle must BEAT on recall while
# holding the answerability signal (coverage + abstention) byte-for-byte identical — the
# CRUX A oracle: recall/nDCG UP while coverage+abstention IDENTICAL == wiring correct; any
# drift in coverage/abstention == an RRF score leaked into the 0.20 gate.
_R0_BASELINE: dict[str, Any] = {
    "overall": {
        "recall_at_k": 0.726667,
        "precision_at_k": 0.121379,
        "hit_at_k": 0.917241,
        "mrr": 0.679274,
        "ndcg_at_k": 0.59647,
        "map": 0.506033,
    },
    "by_doctype_unit_recall": {
        "faq": 1.0,
        "howto": 0.884615,
        "policy": 0.394958,
        "reference": 0.545455,
    },
    "answerable_coverage": 1.0,
    "unanswerable_abstention": 0.019231,
    "aurc": 0.101279,
}


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation) — deterministic, dependency-free."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def _gold_in_top(ranks: dict[str, int | None], gold: list[str], k: int) -> list[str]:
    """Gold units whose rank in ``ranks`` (1-based; None = absent) is within the top ``k``."""
    return [u for u in gold if ranks.get(u) is not None and ranks[u] <= k]  # type: ignore[operator]


def _doc_type_of(unit_id: str) -> str:
    if unit_id.startswith("FAQ-Q"):
        return "faq"
    if unit_id.startswith("HOW-"):
        return "howto"
    if unit_id.startswith("REF-"):
        return "reference"
    return "policy"


def _seed_corpus_for_org(session: Session, org: Organization) -> None:
    """Seed the four corpus documents + their embeddings under *org* (harness-owned).

    Mirrors ``seed_all._embed_corpus`` exactly (chunk_document + the config-selected
    embedder), so the vectors match production while staying inside the rolled-back test
    session.  R4-T3 FORK-G: the embedder is config-selected via ``get_embeddings_provider()``
    — ``EMBEDDINGS_PROVIDER=local`` (CI/eval default) is byte-identical to the legacy local
    ``embed``; ``EMBEDDINGS_PROVIDER=openai`` (``make threshold-live``) re-embeds with the
    real model, query + doc matched.
    """
    provider = get_embeddings_provider()
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
            # R4-T1: situate each chunk (FORK-C dispatch on settings).  Fresh harness
            # rows -> cached=None; the prefix fuses into the embedding input + (via
            # populate_chunk_tsv) the lexical vector, while chunk_text stays pristine.
            prefix = context_prefix(
                document_title=doc.title,
                document_type=doc.document_type,
                section=ch.section,
                clause_ref=ch.clause_ref,
                document_text=doc.document_text,
                chunk_text=ch.chunk_text,
            )
            embed_input = f"{prefix}\n\n{ch.chunk_text}" if prefix else ch.chunk_text
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=ch.chunk_text,
                    context_prefix=(prefix or None),
                    clause_ref=ch.clause_ref,
                    section=ch.section,
                    parent_section_ref=ch.parent_section_ref,
                    ordinal=ch.ordinal,
                    embedding=provider.embed_texts([embed_input])[0],
                )
            )
    session.flush()
    # R1-T1: populate the lexical index (chunk_tsv) for the sparse retrieval path, via
    # the SAME helper production uses — keeps harness seeding faithful to seed_all and is
    # inert for the dense-only baseline (the cosine path never reads chunk_tsv).
    populate_chunk_tsv(session, org.id)


def _score_corpus(
    session: Session,
    org_a: Organization,
    org_b: Organization,
    cases: list[CorpusRagCase],
) -> dict[str, Any]:
    _seed_corpus_for_org(session, org_a)
    _seed_corpus_for_org(session, org_b)
    org_b_ids = {
        str(r[0])
        for r in session.execute(
            sa.text(
                "SELECT pe.id::text FROM policy_embeddings pe "
                "JOIN policies p ON p.id = pe.policy_id WHERE p.organization_id = :o"
            ),
            {"o": str(org_b.id)},
        )
    }

    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    # FORK-G: query embedder is config-selected (factory) so query + doc embedders match per
    # EMBEDDINGS_PROVIDER — local in CI/eval (byte-identical to R3), real under threshold-live.
    tool = PolicyRetrievalTool(embedder=get_embeddings_provider())

    ans_pairs: list[tuple[list[str], list[str]]] = []
    pairs_by_type: dict[str, list[tuple[list[str], list[str]]]] = {}
    doctype_hits: dict[str, list[float]] = {}
    doctype_hits_5: dict[str, list[float]] = {}  # R2-T4: per-doctype recall at k=5
    # R6-T4: per-QUERY per-doc-type metrics (recall@10/nDCG@5/precision-on-answered/coverage +
    # answered-count n).  A query is attributed to a doc type only when ALL its gold units share
    # that type (single-type); multi-type queries go to "cross_document" (a per-query ranking
    # metric cannot be cleanly attributed to one doc type).
    pairs_by_doctype: dict[str, list[tuple[list[str], list[str]]]] = {}
    coverage_by_doctype: dict[str, list[int]] = {}
    rerank_applied_n = 0  # R2-T2: queries where the rerank stage actually ran
    calibrated_pop = 0  # R2-T3: chunks carrying a calibrated_score (rerank applied + params)
    chunks_total = 0
    risk_cov: list[tuple[float, bool]] = []
    faithfulness: list[float] = []
    fused_ms: list[float] = []
    dense_ms: list[float] = []
    ny_report: dict[str, Any] = {}
    cross_tenant = 0
    stub_leaks: set[str] = set()
    answered_answerable = 0
    abstained_unanswerable = 0
    n_answerable = 0
    n_unanswerable = 0

    for case in cases:
        _t0 = perf_counter()
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=_RETRIEVE_K), context=ctx, session=session
        )
        fused_ms.append((perf_counter() - _t0) * 1000.0)
        # Dense-only timing on the SAME query (the pre-hybrid path cost) for the latency delta.
        _t1 = perf_counter()
        tool._retrieve_dense(case.query, context=ctx, session=session, top_k=_RETRIEVE_K)  # noqa: SLF001
        dense_ms.append((perf_counter() - _t1) * 1000.0)

        refs = [c.clause_ref for c in r.chunks if c.clause_ref is not None]
        # R2-T2/T3 observability (additive; does NOT touch the answerability signal/gate).
        rerank_applied_n += 1 if r.rerank_applied else 0
        chunks_total += len(r.chunks)
        calibrated_pop += sum(1 for c in r.chunks if c.calibrated_score is not None)
        # Answerability signal = max DENSE cosine (CRUX A) — identical to the R0 dense-only
        # top-1, so coverage/abstention reproduce R0 byte-for-byte even as the fused RANKING
        # (hence recall) improves.  An RRF score must NEVER reach this gate.
        top1 = (
            r.max_dense_similarity
            if r.max_dense_similarity is not None
            else (r.chunks[0].similarity if r.chunks else 0.0)
        )
        grounded = top1 >= _SILENT_GATE
        stub_leaks |= {x for x in refs if x in _STUB_REFS}
        for c in r.chunks:
            if c.embedding_id in org_b_ids:
                cross_tenant += 1

        if case.answerable:
            n_answerable += 1
            gold = case.expected_unit_ids
            ans_pairs.append((refs, gold))
            pairs_by_type.setdefault(case.type, []).append((refs, gold))
            hit = metrics.hit_at_k(refs, gold, _METRIC_K) >= 1.0
            if grounded:
                answered_answerable += 1
            risk_cov.append((top1, hit))
            # per-doc-type unit recall (each gold unit counted under its doc type)
            top_set = set(refs[:_METRIC_K])
            for u in gold:
                doctype_hits.setdefault(_doc_type_of(u), []).append(1.0 if u in top_set else 0.0)
            top_set_5 = set(refs[:_METRIC_K_5])  # R2-T4: parallel per-doctype recall at k=5
            for u in gold:
                dt5 = _doc_type_of(u)
                doctype_hits_5.setdefault(dt5, []).append(1.0 if u in top_set_5 else 0.0)
            # R6-T4: attribute this answerable query to a doc type (single-type) or cross_document.
            dts = {_doc_type_of(u) for u in gold}
            dt_key = next(iter(dts)) if len(dts) == 1 else "cross_document"
            pairs_by_doctype.setdefault(dt_key, []).append((refs, gold))
            coverage_by_doctype.setdefault(dt_key, []).append(1 if grounded else 0)
            # deterministic parent-context faithfulness proxy: a retrieved gold
            # chunk's text must be supported by its assembled parent context.
            for c in r.chunks:
                if c.clause_ref in set(gold):
                    parent = resolve_parent_context_for_embedding(session, c.embedding_id)
                    faithfulness.append(metrics.token_support(c.chunk_text, parent))
                    break
            if case.type == "trip_planning" and "new york" in case.query.lower() and not ny_report:
                # Wide probe of BOTH paths so we can report the gold units' true dense-only
                # ranks (R0) vs the fused ranks (R1) — the principled before/after the NY
                # regression asserts.  (Dense-only buries them at r33..r176; fusion lifts the
                # lexically-matchable ones.)
                wide_fused = tool.retrieve(
                    RetrievalIntent(query=case.query, top_k=300), context=ctx, session=session
                )
                wide_dense = tool.retrieve_parallel(
                    RetrievalIntent(query=case.query), context=ctx, session=session, top_k=300
                ).dense
                fused_rank: dict[str, int | None] = {
                    c.clause_ref: i + 1 for i, c in enumerate(wide_fused.chunks) if c.clause_ref
                }
                dense_rank: dict[str, int | None] = {
                    c.clause_ref: i + 1 for i, c in enumerate(wide_dense) if c.clause_ref
                }
                ny_report = {
                    "query": case.query,
                    "expected": gold,
                    "fused_ranks": {u: fused_rank.get(u) for u in gold},
                    "dense_ranks": {u: dense_rank.get(u) for u in gold},
                    "fused_in_top_k": len(_gold_in_top(fused_rank, gold, _METRIC_K)),
                    "dense_in_top_k": len(_gold_in_top(dense_rank, gold, _METRIC_K)),
                    "fused_in_top_25": len(_gold_in_top(fused_rank, gold, 25)),
                    "dense_in_top_25": len(_gold_in_top(dense_rank, gold, 25)),
                    "units_in_top_k": _gold_in_top(fused_rank, gold, _METRIC_K),
                    "n_in_top_k": len(_gold_in_top(fused_rank, gold, _METRIC_K)),
                    "top5": refs[:5],
                    "top1_sim": round(top1, 4),
                    "grounded": grounded,
                    "metric_k": _METRIC_K,
                }
        else:
            n_unanswerable += 1
            if not grounded:
                abstained_unanswerable += 1
            risk_cov.append((top1, False))  # answering an unanswerable is always wrong

    overall = metrics.aggregate_retrieval_extended(ans_pairs, _METRIC_K)
    by_type = {
        t: metrics.aggregate_retrieval_extended(p, _METRIC_K)
        for t, p in sorted(pairs_by_type.items())
    }
    by_doctype = {dt: round(sum(v) / len(v), 6) for dt, v in sorted(doctype_hits.items()) if v}
    # R2-T4: the same metrics at k=5 (the cross-encoder's primary ordering metric, nDCG@5).
    # Local rerank is RRF-anchored / identity-on-gold, so these are the R2-local snapshot; the
    # LIVE HF eval (evals/manual) owns the honest pre/post (RRF vs HF) nDCG@5 comparison.
    overall_5 = metrics.aggregate_retrieval_extended(ans_pairs, _METRIC_K_5)
    by_type_5 = {
        t: metrics.aggregate_retrieval_extended(p, _METRIC_K_5)
        for t, p in sorted(pairs_by_type.items())
    }
    by_doctype_5 = {dt: round(sum(v) / len(v), 6) for dt, v in sorted(doctype_hits_5.items()) if v}
    # R6-T4: per-doc-type table row per single-type doc group — recall@10 + nDCG@5 +
    # precision-on-answered + coverage + answered-count n (a small n makes a 1.000 precision cell
    # legible as low-support, not signal).  abstention + AURC stay corpus-level (the unanswerable
    # set is out-of-corpus / untyped) — reported N/A per doc type in the artifact.
    by_doctype_metrics: dict[str, dict[str, float]] = {}
    for dt, p in sorted(pairs_by_doctype.items()):
        agg10 = metrics.aggregate_retrieval_extended(p, _METRIC_K)
        agg5 = metrics.aggregate_retrieval_extended(p, _METRIC_K_5)
        cov = coverage_by_doctype[dt]
        by_doctype_metrics[dt] = {
            "n_answered": agg10["n"],
            "recall_at_10": agg10["recall_at_k"],
            "ndcg_at_5": agg5["ndcg_at_k"],
            "precision_on_answered": agg5["precision_at_k"],
            "coverage": round(sum(cov) / len(cov), 6) if cov else 0.0,
        }
    return {
        "k": _METRIC_K,
        "k5": _METRIC_K_5,
        "n_answerable": n_answerable,
        "n_unanswerable": n_unanswerable,
        "overall": overall,
        "overall_at_5": overall_5,
        "by_type": by_type,
        "by_type_at_5": by_type_5,
        "by_doctype_unit_recall": by_doctype,
        "by_doctype_unit_recall_at_5": by_doctype_5,
        "by_doctype_metrics": by_doctype_metrics,
        "risk_coverage_full": metrics.risk_coverage_curve_with_tau(risk_cov),
        "rerank_applied_count": rerank_applied_n,
        "calibrated_populated": calibrated_pop,
        "calibrated_total": chunks_total,
        "answerable_coverage": round(answered_answerable / n_answerable, 6)
        if n_answerable
        else 0.0,
        "unanswerable_abstention": round(abstained_unanswerable / n_unanswerable, 6)
        if n_unanswerable
        else 0.0,
        "aurc": metrics.aurc(risk_cov),
        "risk_coverage_points": [
            (round(c, 4), round(r, 4))
            for c, r in metrics.risk_coverage_curve(risk_cov)[:: max(1, len(risk_cov) // 10)]
        ],
        "faithfulness_proxy": round(sum(faithfulness) / len(faithfulness), 6)
        if faithfulness
        else 0.0,
        "stub_leaks": sorted(stub_leaks),
        "cross_tenant_hits": cross_tenant,
        "ny_report": ny_report,
        "latency_ms": {
            "fused_p50": round(_percentile(fused_ms, 50), 3),
            "fused_p95": round(_percentile(fused_ms, 95), 3),
            "dense_p50": round(_percentile(dense_ms, 50), 3),
            "dense_p95": round(_percentile(dense_ms, 95), 3),
            "n": len(fused_ms),
        },
    }


@pytest.fixture()
def corpus_scored(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> dict[str, Any]:
    """Seed + score the whole gold set ONCE; memoize plain dicts at module scope."""
    global _CORPUS_CACHE
    if _CORPUS_CACHE is None:
        _CORPUS_CACHE = _score_corpus(db_session, org_a, org_b, rag_corpus_cases)
    return _CORPUS_CACHE


def test_corpus_baseline_runs_and_prints(corpus_scored: dict[str, Any]) -> None:
    """PRINT the R1 hybrid (RRF) numbers and the R0->R1 delta — overall, per query-type,
    per document-type — plus the latency delta.  Soft sanity flags only; the hard gates live
    in test_r1_hybrid_beats_r0_baseline and test_trip_to_ny_regression."""
    s = corpus_scored
    o = s["overall"]
    r0 = _R0_BASELINE["overall"]
    k = s["k"]

    def _d(cur: float, base: float) -> str:
        return f"{cur:.3f} ({cur - base:+.3f} vs R0)"

    print("\n" + "=" * 72)
    print(f"RAG CORPUS — R1 HYBRID (dense+sparse, RRF)  vs  R0 dense-only   k={k}")
    print("=" * 72)
    print(f"answerable={s['n_answerable']}  unanswerable={s['n_unanswerable']}")
    print("OVERALL (R1 value, delta vs R0):")
    print(f"  recall@{k} = {_d(o['recall_at_k'], r0['recall_at_k'])}")
    print(f"  nDCG@{k}   = {_d(o['ndcg_at_k'], r0['ndcg_at_k'])}")
    print(f"  MAP      = {_d(o['map'], r0['map'])}")
    print(f"  MRR      = {_d(o['mrr'], r0['mrr'])}")
    print(f"  hit@{k}    = {_d(o['hit_at_k'], r0['hit_at_k'])}")
    print(f"  prec@{k}   = {_d(o['precision_at_k'], r0['precision_at_k'])}")
    o5 = s["overall_at_5"]
    print("\nR2 @k=5 (cross-encoder ordering metric; local rerank = RRF-anchored identity):")
    print(f"  nDCG@5      = {o5['ndcg_at_k']:.4f}")
    print(f"  precision@5 = {o5['precision_at_k']:.4f}")
    print(f"  recall@5    = {o5['recall_at_k']:.4f}   MRR={o5['mrr']:.4f}")
    print(f"  per-doctype recall@5: {s['by_doctype_unit_recall_at_5']}")
    n_q = s["n_answerable"] + s["n_unanswerable"]
    print(
        f"R2 rerank applied on {s['rerank_applied_count']}/{n_q} queries; "
        f"calibrated_score on {s['calibrated_populated']}/{s['calibrated_total']} returned chunks"
    )
    print("\nby query-type (R1):")
    for t, m in s["by_type"].items():
        print(
            f"  {t:15} n={m['n']:3}  recall@{k}={m['recall_at_k']:.3f}  "
            f"hit@{k}={m['hit_at_k']:.3f}  nDCG={m['ndcg_at_k']:.3f}"
        )
    print(f"\nper-document-type unit recall@{k} (R1 value, delta vs R0):")
    r0_dt = _R0_BASELINE["by_doctype_unit_recall"]
    for dt, v in s["by_doctype_unit_recall"].items():
        base = r0_dt.get(dt)
        delta = f"({v - base:+.3f})" if base is not None else "(n/a)"
        print(f"  {dt:10} {v:.3f} {delta}")
    print("\nABSTENTION (answerability signal — expect IDENTICAL to R0):")
    print(
        f"  answerable_coverage     = {s['answerable_coverage']:.6f} "
        f"(R0 {_R0_BASELINE['answerable_coverage']:.6f})"
    )
    print(
        f"  unanswerable_abstention = {s['unanswerable_abstention']:.6f} "
        f"(R0 {_R0_BASELINE['unanswerable_abstention']:.6f})"
    )
    print(f"  AURC = {s['aurc']:.4f} (R0 {_R0_BASELINE['aurc']:.4f}; lower=better)")
    lat = s["latency_ms"]
    print(
        f"\nLATENCY ms (n={lat['n']}, dense+sparse run sequentially):  "
        f"fused p50={lat['fused_p50']} p95={lat['fused_p95']}  |  "
        f"dense-only p50={lat['dense_p50']} p95={lat['dense_p95']}"
    )
    print(f"faithfulness_proxy (deterministic; real=eval-live)={s['faithfulness_proxy']:.3f}")
    print("=" * 72)

    # --- soft sanity flags (printed for the FSR, never a hard fail here) ---
    rk = o["recall_at_k"]
    if rk < _R0_BASELINE["overall"]["recall_at_k"]:
        print(f"[SANITY FLAG] recall@{k}={rk:.3f} BELOW R0 — hybrid regressed; root-cause it.")
    if rk < 0.9:
        print(
            f"[NOTE] recall@{k}={rk:.3f} < 0.90 — the documented R2 (cross-encoder reranking) "
            f"trigger (research-report change-course threshold)."
        )
    if o["ndcg_at_k"] < r0["ndcg_at_k"]:
        print(f"[SANITY FLAG] nDCG@{k}={o['ndcg_at_k']:.3f} below R0 {r0['ndcg_at_k']:.3f}.")
    r0_dt_flags = _R0_BASELINE["by_doctype_unit_recall"]
    for dt, v in s["by_doctype_unit_recall"].items():
        base = r0_dt_flags.get(dt)
        if base is not None and v < base:
            print(
                f"[SANITY FLAG] per-doctype {dt} recall {v:.3f} BELOW R0 {base:.3f} "
                f"(broad OR-sparse dilution; candidate for R2 reranking)."
            )

    # Soft run gate retained: it RAN over the whole gold set.
    assert o["n"] == s["n_answerable"] > 0
    assert s["n_unanswerable"] > 0


def test_r1_hybrid_beats_r0_baseline(corpus_scored: dict[str, Any]) -> None:
    """PHASE GATE: hybrid RRF BEATS dense-only on recall while the answerability signal
    (coverage + abstention) reproduces R0 BYTE-FOR-BYTE — the CRUX A oracle (recall UP +
    coverage/abstention IDENTICAL == RRF correctly fenced out of the 0.20 gate)."""
    s = corpus_scored
    o = s["overall"]
    r0 = _R0_BASELINE
    k = s["k"]

    # (1) Recall up — a FIXED, principled gate that beats dense-only (not the measured run).
    assert o["recall_at_k"] > thresholds.RAG_CORPUS_R0_RECALL_AT_K, (
        f"recall@{k}={o['recall_at_k']:.4f} did NOT beat the R0 dense-only baseline "
        f"{thresholds.RAG_CORPUS_R0_RECALL_AT_K:.4f} — hybrid must improve recall."
    )
    assert o["recall_at_k"] >= thresholds.RAG_CORPUS_RECALL_AT_K_FLOOR, (
        f"recall@{k}={o['recall_at_k']:.4f} < durable floor "
        f"{thresholds.RAG_CORPUS_RECALL_AT_K_FLOOR} (R2-regression guard)."
    )

    # (2) CRUX A oracle: the answer/abstain signal is unchanged BYTE-FOR-BYTE.
    assert s["answerable_coverage"] == r0["answerable_coverage"], (
        f"answerable_coverage {s['answerable_coverage']} != R0 {r0['answerable_coverage']} "
        f"— RRF must not change the answer/abstain signal."
    )
    assert s["unanswerable_abstention"] == r0["unanswerable_abstention"], (
        f"unanswerable_abstention {s['unanswerable_abstention']} != R0 "
        f"{r0['unanswerable_abstention']} — an RRF score leaked into the 0.20 gate."
    )
    # AURC: flat-or-better only (better recall can only lower risk at a given coverage).
    assert (
        s["aurc"] <= r0["aurc"] + 1e-6
    ), f"AURC {s['aurc']:.4f} worsened vs R0 {r0['aurc']:.4f} — risk-coverage regressed."

    # (3) Per-doc-type durable floors: a single doctype must not silently rot while the
    # overall mean stays green.  faq REGRESSED -0.031 at R1 (broad OR-sparse dilution); these
    # floors sit below the measured R1 values and catch any FURTHER per-type decline.
    for dt, floor in thresholds.RAG_CORPUS_BY_DOCTYPE_FLOOR.items():
        got = s["by_doctype_unit_recall"].get(dt)
        assert (
            got is not None and got >= floor
        ), f"per-doctype unit recall {dt}={got} < floor {floor} (R2-regression guard)."


def test_stub_is_retired(corpus_scored: dict[str, Any]) -> None:
    """No legacy stub clause (AIR-CABIN / HOTEL-CAP / SUSTAIN ...) may be retrieved."""
    assert not corpus_scored["stub_leaks"], f"stub clauses leaked: {corpus_scored['stub_leaks']}"


def test_org_scoping_zero_cross_tenant(corpus_scored: dict[str, Any]) -> None:
    """HARD: a retrieval scoped to org_a returns ZERO org_b corpus chunks."""
    cross = corpus_scored["cross_tenant_hits"]
    assert (
        cross <= thresholds.RAG_MAX_CROSS_TENANT_CHUNKS
    ), f"cross-tenant leak: {cross} org_b chunk(s)"


def test_trip_to_ny_regression(corpus_scored: dict[str, Any]) -> None:
    """PERMANENT NY regression (RETRIEVAL, deterministic): hybrid fusion strictly lifts the gold
    "trip to New York" units (ans-142) that dense-only buries (r33..r176; 0 in top-10, 0 in
    top-25).  Principled before/after, not a circular magic number:
      - fused top-10 gold count  >  dense-only top-10 gold count  (0 -> 1: HOW-03), AND
      - fused top-25 gold count  >= RAG_CORPUS_NY_MIN_GOLD_IN_TOP25 (>= 3; dense-only = 0).

    RECALL, NOT answer: this is purely about which units are retrieved.  (NB: contrary to the
    masterplan's stale note, the max DENSE cosine for this utterance is ~0.41, so the live
    agent already GROUNDS on NY at both R0 and R1 — on NYC FAQ units; the answerability signal
    is unchanged by fusion.  Whether the agent grounds on the RIGHT units is R2/R3 territory.)
    """
    ny = corpus_scored["ny_report"]
    assert ny, "trip-to-NY gold case did not run"
    k = ny["metric_k"]
    print("\nTRIP-TO-NY (R1 hybrid vs R0 dense-only, local embedder):")
    print(f"  query: {ny['query']!r}  max_dense_cosine={ny['top1_sim']}  grounded={ny['grounded']}")
    print(f"  dense-only ranks: {ny['dense_ranks']}")
    print(f"  fused      ranks: {ny['fused_ranks']}")
    print(
        f"  gold in top-{k}:  dense={ny['dense_in_top_k']} -> fused={ny['fused_in_top_k']}  "
        f"({ny['units_in_top_k']})"
    )
    print(f"  gold in top-25: dense={ny['dense_in_top_25']} -> fused={ny['fused_in_top_25']}")
    print(f"  fused top5: {ny['top5']}")

    # (1) fusion strictly improves the top-k gold count vs dense-only (the recall fix).
    assert ny["fused_in_top_k"] > ny["dense_in_top_k"], (
        f"fusion did not improve NY top-{k} gold recall: dense={ny['dense_in_top_k']} "
        f"fused={ny['fused_in_top_k']} — RRF/sparse failed to lift any gold unit."
    )
    # (2) fixed absolute floor at top-25, beating dense-only's 0.
    floor = thresholds.RAG_CORPUS_NY_MIN_GOLD_IN_TOP25
    assert ny["fused_in_top_25"] >= floor, (
        f"only {ny['fused_in_top_25']} gold NY unit(s) in the fused top-25 "
        f"(dense-only {ny['dense_in_top_25']}); need >= {floor}."
    )
    assert ny["fused_in_top_25"] > ny["dense_in_top_25"], "fusion did not beat dense-only at top-25"


def test_r6t4_regression_floors(corpus_scored: dict[str, Any]) -> None:
    """R6-T4 durable regression gate: committed floors/ceilings on the DETERMINISTIC local-path
    metrics FAIL CI (eval-db) on a recall / nDCG@5 / precision / AURC / coverage / abstention drop.

    These lock the local-path plumbing (byte-reproducible), NOT the production calibrated numbers
    (tau*=0.039299 — proven on the keyed eval-live-r5 run).  Faithfulness is NOT gated here: it
    needs an LLM judge and is enforced only on the keyed run (thresholds.RAG_FAITHFULNESS_FLOOR +
    evals/manual/test_rag_corpus_ragas_live.py).
    """
    s = corpus_scored
    o = s["overall"]
    o5 = s["overall_at_5"]
    assert o["recall_at_k"] >= thresholds.RAG_CORPUS_RECALL_AT_K_FLOOR, (
        f"recall@{s['k']}={o['recall_at_k']:.6f} < floor "
        f"{thresholds.RAG_CORPUS_RECALL_AT_K_FLOOR} (R6-T4 regression guard)."
    )
    assert o5["ndcg_at_k"] >= thresholds.RAG_CORPUS_NDCG_AT_5_FLOOR, (
        f"nDCG@{s['k5']}={o5['ndcg_at_k']:.6f} < floor "
        f"{thresholds.RAG_CORPUS_NDCG_AT_5_FLOOR} (R6-T4 regression guard)."
    )
    assert o5["precision_at_k"] >= thresholds.RAG_CORPUS_PRECISION_ON_ANSWERED_FLOOR, (
        f"precision-on-answered@{s['k5']}={o5['precision_at_k']:.6f} < floor "
        f"{thresholds.RAG_CORPUS_PRECISION_ON_ANSWERED_FLOOR} (R6-T4 regression guard)."
    )
    assert s["aurc"] <= thresholds.RAG_CORPUS_AURC_CEILING, (
        f"AURC={s['aurc']:.6f} > ceiling "
        f"{thresholds.RAG_CORPUS_AURC_CEILING} (R6-T4 regression guard)."
    )
    assert (
        s["answerable_coverage"] == 1.0
    ), f"coverage={s['answerable_coverage']} != 1.0 (R6-T4 regression guard)."
    assert (
        s["unanswerable_abstention"] == 0.019231
    ), f"abstention={s['unanswerable_abstention']} != 0.019231 (R6-T4 regression guard)."
    # Per-doc-type unit-recall floors (also enforced in test_r1_hybrid_beats_r0_baseline;
    # re-asserted here as part of the consolidated R6-T4 regression gate).
    for dt, floor in thresholds.RAG_CORPUS_BY_DOCTYPE_FLOOR.items():
        got = s["by_doctype_unit_recall"].get(dt)
        assert (
            got is not None and got >= floor
        ), f"by_doctype_unit_recall[{dt}]={got} < floor {floor} (R6-T4 regression guard)."


def test_emit_corpus_metrics_report(corpus_scored: dict[str, Any]) -> None:
    """Merge the rag_corpus baseline section into evals/reports/metrics.json."""
    existing: dict[str, Any] = {}
    if metrics.REPORT_PATH.exists():
        existing = json.loads(metrics.REPORT_PATH.read_text(encoding="utf-8"))
    s = corpus_scored
    existing["rag_corpus"] = {
        "tier": "db",
        "note": (
            "R1 hybrid — dense + sparse (OR-tsquery) fused via RRF (k=settings.RRF_K); "
            "answerability signal = max dense cosine, so coverage/abstention match R0. "
            "R0 dense-only kept under `baseline_r0`."
        ),
        "k": s["k"],
        "n_answerable": s["n_answerable"],
        "n_unanswerable": s["n_unanswerable"],
        "overall": s["overall"],
        "overall_at_5": s["overall_at_5"],
        "by_type": s["by_type"],
        "by_type_at_5": s["by_type_at_5"],
        "by_doctype_unit_recall": s["by_doctype_unit_recall"],
        "by_doctype_unit_recall_at_5": s["by_doctype_unit_recall_at_5"],
        "by_doctype_metrics": s["by_doctype_metrics"],
        "rerank_applied_count": s["rerank_applied_count"],
        "calibrated_populated": s["calibrated_populated"],
        "calibrated_total": s["calibrated_total"],
        "answerable_coverage": s["answerable_coverage"],
        "unanswerable_abstention": s["unanswerable_abstention"],
        "aurc": s["aurc"],
        "faithfulness_proxy": s["faithfulness_proxy"],
        "cross_tenant_hits": s["cross_tenant_hits"],
        "stub_leaks": s["stub_leaks"],
        "ny_units_in_top_k": s["ny_report"].get("n_in_top_k"),
        "latency_ms": s["latency_ms"],
        "baseline_r0": _R0_BASELINE,
    }
    written = metrics.write_report(existing)
    assert written.exists()
    reloaded = json.loads(written.read_text(encoding="utf-8"))
    assert reloaded["rag_corpus"]["n_answerable"] == s["n_answerable"]
    for sibling in ("rag", "m_a3_db"):
        if sibling in existing:
            assert sibling in reloaded, f"merge dropped the {sibling!r} section"
    # R6-T4: flat, auditor-legible artifacts alongside metrics.json — the overall + per-doc-type
    # metrics TABLE and the risk-coverage curve DATA (tau-sweep).  No PNG (no plotting dep; the
    # curve data IS the machine-readable artifact — see evals/reports/README.md).
    reports_dir = metrics.REPORT_PATH.parent
    o = s["overall"]
    o5 = s["overall_at_5"]
    table_path = reports_dir / "rag_corpus_metrics_table.csv"
    with table_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "doc_type",
                "n_answered",
                "recall_at_10",
                "ndcg_at_5",
                "precision_on_answered",
                "coverage",
                "abstention",
                "aurc",
            ]
        )
        writer.writerow(
            [
                "overall",
                s["n_answerable"],
                o["recall_at_k"],
                o5["ndcg_at_k"],
                o5["precision_at_k"],
                s["answerable_coverage"],
                s["unanswerable_abstention"],
                s["aurc"],
            ]
        )
        for dt, m in s["by_doctype_metrics"].items():
            # abstention + AURC are corpus-level (unanswerable queries are out-of-corpus / untyped).
            writer.writerow(
                [
                    dt,
                    m["n_answered"],
                    m["recall_at_10"],
                    m["ndcg_at_5"],
                    m["precision_on_answered"],
                    m["coverage"],
                    "N/A",
                    "N/A",
                ]
            )
    rc_path = reports_dir / "risk_coverage.csv"
    with rc_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["tau", "coverage", "risk"])
        for tau, cov, risk in s["risk_coverage_full"]:
            writer.writerow([tau, cov, risk])
    assert table_path.exists() and rc_path.exists()
    print(
        f"\nWrote metrics report: {written}"
        f"\nWrote metrics table: {table_path}"
        f"\nWrote risk-coverage curve: {rc_path}"
    )
