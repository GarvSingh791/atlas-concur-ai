"""Manual (non-CI) LIVE cross-encoder rerank eval — HF bge-reranker-v2-m3 (R2-T4).

``@pytest.mark.live`` + ``@pytest.mark.db`` -> SKIPPED by default; runs only with a HOSTED
bge-reranker-v2-m3 selected:
  - ``RERANK_PROVIDER=huggingface`` + a valid ``HF_API_TOKEN`` (``make eval-rerank-live``), or
  - ``RERANK_PROVIDER=novita`` + ``RERANK_API_KEY`` (``make eval-rerank-live-novita``) — Novita
    serves the IDENTICAL model, so it is a drop-in host when HF Inference credits are exhausted.
    Novita batches are throttled to <=30/min OUTSIDE the timed region (latency stays honest) and
    the report is written to ``rag_corpus_novita_rerank_live.json`` (the HF report is untouched).

The PHASE-CLOSING precision evidence.  Retrieval is held CONSTANT (the deterministic LOCAL
embedder, the SAME fused candidate set as R1), and ONLY the reranker varies — so the nDCG@5
delta is purely the cross-encoder's effect (honest, attributable).  For every gold query it
computes BOTH orders in-process:
  - RRF (pre-rerank) order  == the R1 baseline, AND
  - HF cross-encoder order  == apply_rerank() over the SAME fused candidates,
asserts HF nDCG@5 > RRF nDCG@5 (+ precision@5), prints per-doctype / per-query-type movement,
and measures the rerank-stage p50/p95 latency EXCLUDING the warmup ping (Section 6 budget).

Also runs the near-miss CHECKPOINT (evals/datasets/checkpoint_queries.jsonl, OUTSIDE the 197
denominator): the relevant unit must outrank every distractor under HF rerank; pre/post ranks
printed.
"""

from __future__ import annotations

import json
import time
from time import perf_counter
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.fusion import reciprocal_rank_fusion
from ai_agents.rag.rerank import apply_rerank
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from backend.tests.eval.test_rag_corpus_eval import _doc_type_of, _percentile, _seed_corpus_for_org
from config.settings import get_settings
from database.models.identity import Organization
from evals.harness import metrics
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.local import LocalEmbeddingsProvider
from services.rerank_provider.factory import get_rerank_provider
from services.rerank_provider.interface import RerankProvider

pytestmark = pytest.mark.live

_CAND_TOP_K = 50  # parallel candidates per path (== production _PARALLEL_TOP_K)
_K5 = 5
_K10 = 10
_CHECKPOINT = metrics.REPORT_PATH.resolve().parents[1] / "datasets" / "checkpoint_queries.jsonl"


_HOSTED_BGE = ("huggingface", "novita")  # both host the SAME bge-reranker-v2-m3 cross-encoder
_NOVITA_MIN_GAP_S = 2.1  # <=30 Novita rerank batches/min (60/30 = 2.0s + margin)


class _Throttle:
    """Spaces rerank batches (Novita: <=30/min).  Eval-side only; the adapter is never touched.
    ``wait()`` is called BEFORE the timed region so the sleep never pollutes rerank latency."""

    def __init__(self, min_gap_s: float) -> None:
        self._min_gap = min_gap_s
        self._last = 0.0

    def wait(self) -> None:
        if self._min_gap <= 0:
            return
        sleep_s = self._min_gap - (time.monotonic() - self._last)
        if sleep_s > 0:
            time.sleep(sleep_s)
        self._last = time.monotonic()


def _require_hf() -> tuple[RerankProvider, str, _Throttle]:
    """Skip cleanly unless a hosted bge-reranker-v2-m3 (huggingface | novita) is selected + keyed.

    Returns ``(provider, provider_name, throttle)``.
    """
    get_settings.cache_clear()
    s = get_settings()
    name = s.RERANK_PROVIDER
    if name not in _HOSTED_BGE:
        pytest.skip("RERANK_PROVIDER not in (huggingface, novita) — live rerank eval skipped.")
    if name == "huggingface" and not s.HF_API_TOKEN:
        pytest.skip("HF_API_TOKEN unset — live HF rerank eval skipped.")
    if name == "novita" and not s.RERANK_API_KEY:
        pytest.skip("RERANK_API_KEY unset — live Novita rerank eval skipped.")
    throttle = _Throttle(_NOVITA_MIN_GAP_S if name == "novita" else 0.0)
    provider = get_rerank_provider()
    try:
        throttle.wait()
        probe = provider.rerank("warmup probe", ["a relevant warmup document", "an irrelevant one"])
    except Exception as exc:  # noqa: BLE001 — any provider/auth error -> skip, never fail
        pytest.skip(f"{name} rerank unavailable ({type(exc).__name__}) — skipped.")
    assert len(probe) == 2, f"{name} probe did not rank both documents"
    return provider, name, throttle


def _rrf_order(
    tool: PolicyRetrievalTool, ctx: AgentContext, session: Session, query: str, rrf_k: int
) -> list[Any]:
    """The R1 RRF-fused candidate order for *query* (dense+sparse), no rerank."""
    par = tool.retrieve_parallel(
        RetrievalIntent(query=query), context=ctx, session=session, top_k=_CAND_TOP_K
    )
    return reciprocal_rank_fusion(
        [par.dense, par.sparse],
        key=lambda c: c.embedding_id,
        k=rrf_k,
        tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
    )


def _refs(cands: list[Any]) -> list[str]:
    return [c.clause_ref for c in cands if c.clause_ref is not None]


@pytest.mark.db
def test_hf_rerank_precision_gain_live(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """HF cross-encoder must improve nDCG@5 over the RRF order on the SAME candidate set."""
    provider, pname, throttle = _require_hf()
    if hasattr(provider, "warmup"):
        provider.warmup()  # cold-start ping — EXCLUDED from the latency sample below
    rrf_k = get_settings().RRF_K

    _seed_corpus_for_org(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())

    rrf_pairs: list[tuple[list[str], list[str]]] = []
    hf_pairs: list[tuple[list[str], list[str]]] = []
    by_type_rrf: dict[str, list[tuple[list[str], list[str]]]] = {}
    by_type_hf: dict[str, list[tuple[list[str], list[str]]]] = {}
    dt_rrf: dict[str, list[float]] = {}
    dt_hf: dict[str, list[float]] = {}
    rerank_ms: list[float] = []
    n_failed = 0

    for case in rag_corpus_cases:
        fused = _rrf_order(tool, ctx, db_session, case.query, rrf_k)
        if not fused:
            continue
        rrf_refs = _refs(fused)
        throttle.wait()  # rate limit OUTSIDE the timed region
        t0 = perf_counter()
        outcome = apply_rerank(
            case.query,
            fused,
            provider=provider,
            top_k=max(1, len(fused)),
            candidate_cap=_CAND_TOP_K,
            text_of=lambda c: c.chunk_text,
        )
        rerank_ms.append((perf_counter() - t0) * 1000.0)
        if not outcome.applied:
            n_failed += 1
        hf_refs = _refs(outcome.items)

        if case.answerable:
            gold = case.expected_unit_ids
            rrf_pairs.append((rrf_refs, gold))
            hf_pairs.append((hf_refs, gold))
            by_type_rrf.setdefault(case.type, []).append((rrf_refs, gold))
            by_type_hf.setdefault(case.type, []).append((hf_refs, gold))
            rrf_top5, hf_top5 = set(rrf_refs[:_K5]), set(hf_refs[:_K5])
            for u in gold:
                dt = _doc_type_of(u)
                dt_rrf.setdefault(dt, []).append(1.0 if u in rrf_top5 else 0.0)
                dt_hf.setdefault(dt, []).append(1.0 if u in hf_top5 else 0.0)

    rrf5 = metrics.aggregate_retrieval_extended(rrf_pairs, _K5)
    hf5 = metrics.aggregate_retrieval_extended(hf_pairs, _K5)
    rrf10 = metrics.aggregate_retrieval_extended(rrf_pairs, _K10)
    hf10 = metrics.aggregate_retrieval_extended(hf_pairs, _K10)
    lat = {
        "p50": round(_percentile(rerank_ms, 50), 1),
        "p95": round(_percentile(rerank_ms, 95), 1),
        "n": len(rerank_ms),
        "warmup_excluded": True,
    }

    print("\n" + "=" * 72)
    print(f"R2-T4 LIVE — {pname} bge-reranker-v2-m3 vs RRF (R1), same fused candidate set")
    print("=" * 72)
    print(f"answerable queries scored: {len(hf_pairs)}   rerank fail-safe fallbacks: {n_failed}")

    def _delta(hf: float, base: float) -> str:
        return f"{base:.4f} -> {hf:.4f} ({hf - base:+.4f})"

    print(f"OVERALL  (RRF -> {pname}, delta):")
    print(f"  nDCG@5      = {_delta(hf5['ndcg_at_k'], rrf5['ndcg_at_k'])}")
    print(f"  precision@5 = {_delta(hf5['precision_at_k'], rrf5['precision_at_k'])}")
    print(f"  recall@5    = {_delta(hf5['recall_at_k'], rrf5['recall_at_k'])}")
    print(f"  nDCG@10     = {_delta(hf10['ndcg_at_k'], rrf10['ndcg_at_k'])}")
    print(f"  recall@10   = {_delta(hf10['recall_at_k'], rrf10['recall_at_k'])}")
    print(f"by query-type nDCG@5 (RRF -> {pname}):")
    for t in sorted(by_type_hf):
        b = metrics.aggregate_retrieval_extended(by_type_rrf[t], _K5)["ndcg_at_k"]
        h = metrics.aggregate_retrieval_extended(by_type_hf[t], _K5)["ndcg_at_k"]
        print(f"  {t:15} n={len(by_type_hf[t]):3}  {_delta(h, b)}")
    print(f"per-doctype recall@5 (RRF -> {pname}):")
    for dt in sorted(dt_hf):
        b = sum(dt_rrf[dt]) / len(dt_rrf[dt])
        h = sum(dt_hf[dt]) / len(dt_hf[dt])
        print(f"  {dt:10} {_delta(h, b)}")
    print(f"RERANK STAGE LATENCY ({pname}, warm, <= {_CAND_TOP_K} docs; warmup EXCLUDED):")
    print(f"  p50={lat['p50']}ms  p95={lat['p95']}ms  (n={lat['n']})")
    print("  Section 6 budget = +80-200ms p50 (Cohere ref); HF free tier may exceed — honest.")
    print("=" * 72)

    snapshot = {
        "note": f"LIVE {pname} bge-reranker-v2-m3 rerank vs RRF (R1); same local-emb candidates.",
        "provider": pname,
        "n_answerable": len(hf_pairs),
        "rerank_failsafe_fallbacks": n_failed,
        "rrf_overall_at_5": rrf5,
        "hf_overall_at_5": hf5,
        "rrf_overall_at_10": rrf10,
        "hf_overall_at_10": hf10,
        "rerank_latency_ms": lat,
    }
    report = (
        "rag_corpus_hf_rerank_live.json"
        if pname == "huggingface"
        else (f"rag_corpus_{pname}_rerank_live.json")
    )
    out = metrics.REPORT_PATH.parent / report
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {pname} rerank report: {out}")

    # PHASE-CLOSING ASSERTIONS — the precision gain (V14).  Loud failure if not met (do not bless).
    assert len(hf_pairs) > 100, "too few answerable queries scored — check the run"
    assert hf5["ndcg_at_k"] > rrf5["ndcg_at_k"], (
        f"HF nDCG@5 {hf5['ndcg_at_k']:.4f} did NOT improve over RRF {rrf5['ndcg_at_k']:.4f} "
        f"— the cross-encoder must improve ordering (FLAG; do not bless)."
    )
    assert (
        hf5["precision_at_k"] >= rrf5["precision_at_k"]
    ), f"HF precision@5 {hf5['precision_at_k']:.4f} < RRF {rrf5['precision_at_k']:.4f}"
    # Recall@10 is the SET in the top-10 — reranking reorders within it, so it must not drop.
    assert hf10["recall_at_k"] >= rrf10["recall_at_k"] - 1e-9, "HF rerank dropped recall@10"


@pytest.mark.db
def test_hf_near_miss_checkpoint_live(
    db_session: Session,
    org_a: Organization,
) -> None:
    """Near-miss CHECKPOINT: under HF rerank a relevant unit outranks every distractor."""
    provider, pname, throttle = _require_hf()
    if hasattr(provider, "warmup"):
        provider.warmup()
    rrf_k = get_settings().RRF_K

    _seed_corpus_for_org(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    tool = PolicyRetrievalTool(embedder=LocalEmbeddingsProvider())

    cases = [
        json.loads(line)
        for line in _CHECKPOINT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert cases, "no checkpoint queries loaded"

    def _rank(refs: list[str], unit: str) -> int | None:
        return refs.index(unit) + 1 if unit in refs else None

    print("\n" + "=" * 72)
    print(f"R2 NEAR-MISS CHECKPOINT — {pname} rerank (pre-RRF rank -> post-rerank rank)")
    print("=" * 72)
    failures: list[str] = []
    for c in cases:
        fused = _rrf_order(tool, ctx, db_session, c["query"], rrf_k)
        rrf_refs = _refs(fused)
        throttle.wait()
        outcome = apply_rerank(
            c["query"],
            fused,
            provider=provider,
            top_k=max(1, len(fused)),
            candidate_cap=_CAND_TOP_K,
            text_of=lambda x: x.chunk_text,
        )
        hf_refs = _refs(outcome.items)
        rel = c["relevant_unit_ids"]
        dis = c["distractor_unit_ids"]
        rel_rrf = [_rank(rrf_refs, u) for u in rel]
        rel_hf = [_rank(hf_refs, u) for u in rel]
        dis_rrf = [_rank(rrf_refs, u) for u in dis]
        dis_hf = [_rank(hf_refs, u) for u in dis]
        best_rel = min([r for r in rel_hf if r is not None], default=None)
        worst_dis = max([r for r in dis_hf if r is not None], default=None)
        ok = best_rel is not None and (worst_dis is None or best_rel < worst_dis)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {c['id']}: {c['query']!r}")
        print(f"   relevant {rel}: RRF rank {rel_rrf} -> HF rank {rel_hf}")
        print(f"   distractor {dis}: RRF rank {dis_rrf} -> HF rank {dis_hf}")
        print(f"   note: {c['note']}")
        if not ok:
            failures.append(c["id"])
    print("=" * 72)
    assert not failures, f"near-miss checkpoint FAILED for {failures} (relevant below distractor)"
