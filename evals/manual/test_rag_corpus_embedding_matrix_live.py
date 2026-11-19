"""R4-T3/T4 — LIVE embedding-upgrade MATRIX over the Helios corpus (the adopt-decision evidence).

``@pytest.mark.live`` + ``@pytest.mark.db`` -> SKIPPED by default; runs only with a REAL
embeddings provider (``EMBEDDINGS_PROVIDER=openrouter`` reusing ``LLM_API_KEY`` + ``LLM_BASE_URL``,
or ``EMBEDDINGS_PROVIDER=openai`` + a valid ``EMBEDDINGS_API_KEY``) and, for the REAL reranked
numbers, ``RERANK_PROVIDER=huggingface`` + ``HF_API_TOKEN``.  Non-deterministic snapshot, NOT a
CI gate (Fence 3).  Run via ``make eval-live-matrix``.

WHAT IT MEASURES — the 4-cell matrix, reference = C0 (the FIRST real production-retrieval
measurement, established fresh here — NOT the R3-LOCAL feature-hash baseline):

    C0  text-embedding-3-small @1536, prefixes OFF   <- LIVE REFERENCE
    C1  text-embedding-3-small @1536, prefixes ON     (does the prefix help a SEMANTIC embedder?)
    C2  text-embedding-3-large  @1536, prefixes OFF    (does the model upgrade help?)
    C3  text-embedding-3-large  @1536, prefixes ON     (both)

Each cell re-embeds the 208 corpus units + the gold queries with the SAME real embedder
(query + doc matched), runs the FULL retrieve -> RRF -> rerank -> gate pipeline, and prints:
recall@10, nDCG@10, reranked nDCG@5, reranked recall@5, AURC, RERANKED cross_document
recall@5 (the number R4-T1 demanded), policy recall@10 (>=0.55 check), coverage, abstention,
plus the recalibrated risk-coverage tau* (0.039299 -> new) on the R3 calibration slice.

THREE PAIRED INVARIANTS (per the R4 Part-2 brief):
  1. C0 CONTROL COLUMN — C0 also prints PRE-rerank FUSED recall@5 / nDCG@5 beside its reranked
     numbers, so the reranker's marginal contribution on the REAL embedder is visible as its
     own delta (the HF reranker was selected on the LOCAL pipeline; a flat reranked
     cross_document recall@5 is otherwise ambiguous between "prefix didn't help retrieval" and
     "prefix helped but the reranker reshuffled it" — opposite R5 implications).
  2. PER-CELL DETERMINISM + PREFIX-FREE QUERY — every cell asserts the query embeddings are
     prefix-free (the corpus-side-only invariant; a recording embedder proves no prefix leaks
     into the query path), and C0 re-runs its scoring once to confirm the headline numbers
     reproduce (the live analogue of PYTHONHASHSEED=0) — reported, not trusted.
  3. tau* — REPORTED here on the current (local-fit) Platt scale as evidence; AURC is invariant
     to the monotone Platt transform, so the AURC delta is honest regardless.  The full,
     PERSISTED re-fit (Platt + tau*) for an adopted pipeline is ``make recalibrate-live`` (it
     writes a scratch ``*.live.json``, never the committed CI artifact — R4-T4 adopt step).

Self-skips cleanly when the embedder is local / the key is a placeholder (a single probe embed
is attempted before any seeding).  Writes a scratch snapshot to evals/reports/ (gitignored).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.threshold import (
    CalibrationError,
    compare_signals,
    compute_risk_coverage,
    select_threshold,
)
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from backend.tests.eval.test_rag_corpus_eval import _doc_type_of
from config.settings import get_settings
from database.models.identity import Organization
from database.models.policy import Policy, PolicyDocumentType
from database.models.policy_embedding import PolicyEmbedding
from database.seeds.chunk_tsv import populate_chunk_tsv
from database.seeds.corpus import ALL_DOCUMENTS
from database.seeds.corpus.chunking import chunk_document
from database.seeds.corpus.contextual import context_prefix
from evals.harness import metrics
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.interface import EmbeddingsProvider
from services.rerank_provider.factory import get_rerank_provider

pytestmark = pytest.mark.live

_RETRIEVE_K = 25  # caller-side truncation window (the reranker reorders WITHIN it)
_FIT_SEED = 42  # the R3 calibration shuffle seed (must match the τ* fitter)
_PLATT_SLICE_N = 40  # indices 0–39 consumed by R2; the R3 calibration slice is 40–196
_NY_CASE_ID = "ans-142"  # the cross-document "trip to New York" canary
_COMMITTED_TAU = 0.039299  # the active committed huggingface τ* (R3) — the recalibration anchor
# The real embedder the make target selected (openrouter | openai). _apply_cell_env honours it
# (rather than hardcoding "openai") so each cell routes through the factory-selected real adapter —
# R4-T4 added EMBEDDINGS_PROVIDER=openrouter (OpenAI models via OpenRouter, reusing LLM_API_KEY).
_LIVE_EMBEDDINGS_PROVIDER = os.environ.get("EMBEDDINGS_PROVIDER", "openai")
# Determinism band for the C0 re-run: OpenAI query embeds are near- but not bit-deterministic, so
# a few borderline rank flips (≈ 1/n each on recall) are expected; this catches only gross drift.
_DETERMINISM_TOL = 0.03

# Canonical document names — present in EVERY deterministic prefix template (_DOCUMENT_NAMES in
# database/seeds/corpus/contextual.py).  A query-time embed input containing any of these means a
# corpus-side prefix leaked into the QUERY path (the corpus-side-only invariant is broken).
_PREFIX_MARKERS = (
    "Helios Global Travel & Expense Policy",
    "Helios T&E FAQ",
    "Helios T&E How-To Guide",
    "Helios T&E Reference",
)


@dataclass(frozen=True)
class _Cell:
    label: str
    model: str
    prefixes: bool


# C0 first (the reference); C3 optional-both last.
_CELLS: tuple[_Cell, ...] = (
    _Cell("C0", "text-embedding-3-small", False),
    _Cell("C1", "text-embedding-3-small", True),
    _Cell("C2", "text-embedding-3-large", False),
    _Cell("C3", "text-embedding-3-large", True),
)


class _RecordingEmbedder(EmbeddingsProvider):
    """Wraps a real provider and records every text passed to ``embed_texts``.

    Used ONLY for the query-time tool so we can assert the corpus-side-only invariant: the
    query is embedded RAW (no situating prefix ever reaches the query vector).
    """

    def __init__(self, inner: EmbeddingsProvider) -> None:
        self.inner = inner
        self.inputs: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        return self.inner.embed_texts(texts)


def _require_real_embedder() -> None:
    """Skip cleanly unless a REAL embeddings provider is configured AND reachable."""
    get_settings.cache_clear()
    s = get_settings()
    if s.EMBEDDINGS_PROVIDER not in ("openai", "openrouter"):
        pytest.skip("EMBEDDINGS_PROVIDER not openai/openrouter — live embedding matrix skipped.")
    try:
        probe = get_embeddings_provider().embed_texts(["hotel nightly rate cap tier one new york"])
    except Exception as exc:  # noqa: BLE001 — any provider/auth error → skip, never fail
        pytest.skip(f"real embeddings provider unavailable ({type(exc).__name__}) — skipped.")
    assert probe and len(probe[0]) == 1536, "real embedder did not return a 1536-d vector"


def _r3_indices(n_cases: int) -> list[int]:
    """Indices 40–196 of the seed=42 shuffle — the SAME R3 calibration slice the τ* fitter uses."""
    import random  # local import: keep module import-time RNG-free

    idx = list(range(n_cases))
    random.Random(_FIT_SEED).shuffle(idx)
    return idx[_PLATT_SLICE_N:]


def _apply_cell_env(monkeypatch: pytest.MonkeyPatch, cell: _Cell) -> EmbeddingsProvider:
    """Set the per-cell env (model + prefixes), clear the settings cache, return the provider.

    EMBEDDINGS_PROVIDER (openrouter | openai) is supplied by the make target; LLM_PROVIDER=local
    pins the DETERMINISTIC template prefix (the controlled prefix, no LLM cost) for prefix cells.
    """
    monkeypatch.setenv("EMBEDDINGS_PROVIDER", _LIVE_EMBEDDINGS_PROVIDER)
    monkeypatch.setenv("EMBEDDINGS_MODEL", cell.model)
    monkeypatch.setenv("EMBEDDINGS_DIMENSIONS", "1536")
    monkeypatch.setenv("RAG_CONTEXTUAL_PREFIXES", "true" if cell.prefixes else "false")
    monkeypatch.setenv("LLM_PROVIDER", "local")
    get_settings.cache_clear()
    return get_embeddings_provider()


def _wipe_corpus(session: Session, org: Organization) -> None:
    """Drop this org's corpus rows so the next cell re-seeds from clean state."""
    pol_ids = [p.id for p in session.query(Policy).filter(Policy.organization_id == org.id).all()]
    if pol_ids:
        session.execute(delete(PolicyEmbedding).where(PolicyEmbedding.policy_id.in_(pol_ids)))
        session.execute(delete(Policy).where(Policy.id.in_(pol_ids)))
    session.flush()


def _seed_cell(session: Session, org: Organization, provider: EmbeddingsProvider) -> int:
    """Re-embed the 208 corpus units under *org* with *provider*, prefixes per current settings.

    Mirrors ``seed_all._embed_corpus``: the prefix (when ON) fuses into the embedding input +
    the tsvector; ``chunk_text`` stays PRISTINE (reranker + citations read it).  Batched one
    embed call per document (4 calls/cell).
    """
    total = 0
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
        chunks = chunk_document(doc)
        prefixes = [
            context_prefix(
                document_title=doc.title,
                document_type=doc.document_type,
                section=ch.section,
                clause_ref=ch.clause_ref,
                document_text=doc.document_text,
                chunk_text=ch.chunk_text,
            )
            for ch in chunks
        ]
        embed_inputs = [
            f"{pfx}\n\n{ch.chunk_text}" if pfx else ch.chunk_text
            for pfx, ch in zip(prefixes, chunks, strict=True)
        ]
        vectors = provider.embed_texts(embed_inputs)  # one batch call per document
        for ch, pfx, vec in zip(chunks, prefixes, vectors, strict=True):
            session.add(
                PolicyEmbedding(
                    policy_id=policy.id,
                    chunk_text=ch.chunk_text,
                    context_prefix=(pfx or None),
                    clause_ref=ch.clause_ref,
                    section=ch.section,
                    parent_section_ref=ch.parent_section_ref,
                    ordinal=ch.ordinal,
                    embedding=vec,
                )
            )
        total += len(chunks)
    session.flush()
    populate_chunk_tsv(session, org.id)
    return total


def _score_cell(
    tool: PolicyRetrievalTool,
    ctx: AgentContext,
    session: Session,
    cases: list[CorpusRagCase],
) -> dict:
    """Run the full pipeline over the gold set; return retrieval + answerability metrics.

    Two distinct aggregations (mirroring backend/tests/eval/test_rag_corpus_eval.py):
      - ``by_type`` keys off ``case.type`` (the QUERY type — cross_document, trip_planning…);
      - ``by_doctype`` keys off ``_doc_type_of(gold_unit)`` (the DOCUMENT type — policy/faq/…),
        a per-gold-UNIT hit rate.  ``policy recall@10`` is a DOCTYPE metric (the R4-T1 floor),
        NOT a query type — keying it off ``case.type`` would always be empty (the original bug).
    """
    ans_pairs: list[tuple[list[str], list[str]]] = []
    by_type: dict[str, list[tuple[list[str], list[str]]]] = {}
    doctype_hits10: dict[str, list[float]] = {}  # per-gold-unit recall@10 by DOCUMENT type
    # R3-slice gate signals for the risk-coverage / AURC / τ* recalibration.
    r3 = set(_r3_indices(len(cases)))
    cal_scores: list[float] = []
    cos_scores: list[float] = []
    r3_labels: list[int] = []
    n_calibrated_none = 0
    rerank_applied_n = 0

    for i, case in enumerate(cases):
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=_RETRIEVE_K), context=ctx, session=session
        )
        if r.rerank_applied:
            rerank_applied_n += 1
        refs = [c.clause_ref for c in r.chunks if c.clause_ref is not None]
        if case.answerable:
            gold = case.expected_unit_ids
            ans_pairs.append((refs, gold))
            by_type.setdefault(case.type, []).append((refs, gold))
            top10 = set(refs[:10])
            for u in gold:
                doctype_hits10.setdefault(_doc_type_of(u), []).append(1.0 if u in top10 else 0.0)
        if i in r3:
            label = 1 if case.answerable else 0
            if r.chunks and r.chunks[0].calibrated_score is not None:
                cal_scores.append(r.chunks[0].calibrated_score)
                cos = r.max_dense_similarity if r.max_dense_similarity is not None else 0.0
                cos_scores.append(cos)
                r3_labels.append(label)
            else:
                n_calibrated_none += 1

    overall10 = metrics.aggregate_retrieval_extended(ans_pairs, 10)
    overall5 = metrics.aggregate_retrieval_extended(ans_pairs, 5)
    per_type5 = {t: metrics.aggregate_retrieval_extended(p, 5) for t, p in sorted(by_type.items())}
    by_doctype10 = {dt: sum(v) / len(v) for dt, v in doctype_hits10.items() if v}

    return {
        "recall_at_10": overall10["recall_at_k"],
        "ndcg_at_10": overall10["ndcg_at_k"],
        "reranked_recall_at_5": overall5["recall_at_k"],
        "reranked_ndcg_at_5": overall5["ndcg_at_k"],
        "reranked_cross_document_recall_at_5": (
            per_type5.get("cross_document", {}).get("recall_at_k")
        ),
        "policy_recall_at_10": by_doctype10.get("policy"),  # DOCTYPE unit-recall (the R4-T1 floor)
        "by_doctype_unit_recall_at_10": by_doctype10,
        "rerank_applied_n": rerank_applied_n,
        "n_cases": len(cases),
        "_cal_scores": cal_scores,
        "_cos_scores": cos_scores,
        "_r3_labels": r3_labels,
        "_n_calibrated_none": n_calibrated_none,
    }


def _fused_at_5(
    tool: PolicyRetrievalTool,
    ctx: AgentContext,
    session: Session,
    cases: list[CorpusRagCase],
) -> dict:
    """C0 CONTROL: PRE-rerank FUSED recall@5 / nDCG@5 (the reranker's marginal contribution)."""
    from ai_agents.rag.fusion import reciprocal_rank_fusion  # noqa: PLC0415 — mirror the tool

    settings = get_settings()
    pairs: list[tuple[list[str], list[str]]] = []
    cross_pairs: list[tuple[list[str], list[str]]] = []
    for case in cases:
        if not case.answerable:
            continue
        parallel = tool.retrieve_parallel(
            RetrievalIntent(query=case.query, top_k=_RETRIEVE_K), context=ctx, session=session
        )
        fused = reciprocal_rank_fusion(
            [parallel.dense, parallel.sparse],
            key=lambda c: c.embedding_id,
            k=settings.RRF_K,
            tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
        )
        refs = [c.clause_ref for c in fused if c.clause_ref is not None]
        pairs.append((refs, case.expected_unit_ids))
        if case.type == "cross_document":
            cross_pairs.append((refs, case.expected_unit_ids))
    agg = metrics.aggregate_retrieval_extended(pairs, 5)
    cross = metrics.aggregate_retrieval_extended(cross_pairs, 5) if cross_pairs else {}
    return {
        "fused_recall_at_5": agg["recall_at_k"],
        "fused_ndcg_at_5": agg["ndcg_at_k"],
        "fused_cross_document_recall_at_5": cross.get("recall_at_k"),
    }


def _recalibrate_tau(scored: dict, target_risk: float) -> dict | None:
    """Recompute the risk-coverage τ* + AURC on this cell's R3 calibration slice.

    AURC is INVARIANT to the monotone Platt transform, so the AURC delta is honest even though
    the Platt params are the committed local-fit ones (full re-fit = ``make recalibrate-live``).
    """
    cal, cos, labels = scored["_cal_scores"], scored["_cos_scores"], scored["_r3_labels"]
    n = len(cal)
    if n == 0 or sum(labels) in (0, n):
        return None  # no calibrated signal (reranker inactive) or single-class — cannot fit
    curve = compute_risk_coverage(cal, labels)
    cmp = compare_signals(cos, cal, labels, target_risk=target_risk)
    try:
        tau_star = select_threshold(curve, target_risk=target_risk)
        unachievable = False
    except CalibrationError:
        tau_star = min(cal)
        unachievable = True
    answered = [(s, lab) for s, lab in zip(cal, labels, strict=True) if s > tau_star]
    n_ans = len(answered)
    n_unans = sum(1 for lab in labels if lab == 0)
    leakers = sum(1 for _s, lab in answered if lab == 0)
    return {
        "tau_star": round(tau_star, 6),
        "unachievable": unachievable,
        "coverage_at_tau": round(n_ans / n, 6),
        "risk_at_tau": round(leakers / max(n_ans, 1), 6),
        "abstention_recall": round((n_unans - leakers) / max(n_unans, 1), 6),
        "aurc_reranker": cmp["reranker_aurc"],
        "aurc_cosine": cmp["cosine_aurc"],
        "n": n,
    }


def _verdict(candidate: float | None, reference: float | None, *, higher_is_better: bool) -> str:
    """helped / flat / regressed vs the C0 reference (flat band = |Δ| < 0.005)."""
    if candidate is None or reference is None:
        return "n/a"
    delta = candidate - reference
    if abs(delta) < 0.005:
        return f"flat ({delta:+.4f})"
    good = delta > 0 if higher_is_better else delta < 0
    return f"{'helped' if good else 'regressed'} ({delta:+.4f})"


@pytest.mark.db
def test_embedding_upgrade_matrix_live(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run C0..C3 on the REAL embedder; print the consolidated adopt/gate/revert evidence."""
    _require_real_embedder()
    target_risk = get_settings().ABSTENTION_TARGET_RISK
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)

    results: dict[str, dict] = {}
    control: dict = {}
    determinism: dict = {}

    for cell in _CELLS:
        provider = _apply_cell_env(monkeypatch, cell)
        _wipe_corpus(db_session, org_a)
        n_chunks = _seed_cell(db_session, org_a, provider)
        assert n_chunks == 208, f"{cell.label}: expected 208 chunks, seeded {n_chunks}"

        # HF cold-start: warm the reranker BEFORE the per-cell scoring burst so the opening queries
        # don't hang on a loading model (mirrors the calibration fitters / latency eval; the R4
        # Part-2 matrix omitted this, so a cold model timed every query out -> RRF fallback -> the
        # reranked columns silently collapsed to fused).  Per-cell because each cell re-embeds 208
        # units first, long enough for HF serverless to unload.  Idempotent + cheap (one ping).
        rerank_provider = get_rerank_provider()
        if hasattr(rerank_provider, "warmup"):
            rerank_provider.warmup()

        recorder = _RecordingEmbedder(provider)
        tool = PolicyRetrievalTool(embedder=recorder)
        scored = _score_cell(tool, ctx, db_session, rag_corpus_cases)
        scored["tau"] = _recalibrate_tau(scored, target_risk)
        results[cell.label] = scored

        # INVARIANT 2a — the query path is PREFIX-FREE (corpus-side-only).  Every recorded
        # query-time input must be a RAW gold query, never a situating prefix.
        leaked = [t for t in recorder.inputs if any(m in t for m in _PREFIX_MARKERS)]
        assert (
            not leaked
        ), f"{cell.label}: a context prefix leaked into the QUERY embedder: {leaked[:2]}"

        # INVARIANT 1 — C0 control column: the PRE-rerank fused @5 numbers.
        if cell.label == "C0":
            control = _fused_at_5(tool, ctx, db_session, rag_corpus_cases)
            # INVARIANT 2b — determinism: re-score C0 over the SAME seeded corpus; headline
            # metrics must reproduce (the live analogue of PYTHONHASHSEED=0).  Tolerance is
            # generous (_DETERMINISM_TOL): OpenAI query embeddings are near-deterministic but
            # not bit-exact, so a few borderline rank flips (each ≈ 1/n on recall) are expected
            # jitter — the assert only catches GROSS non-determinism (a seeding/wiring bug).
            rescored = _score_cell(tool, ctx, db_session, rag_corpus_cases)
            determinism = {
                "recall_at_10_delta": abs(rescored["recall_at_10"] - scored["recall_at_10"]),
                "ndcg_at_10_delta": abs(rescored["ndcg_at_10"] - scored["ndcg_at_10"]),
                "tolerance": _DETERMINISM_TOL,
            }
            assert determinism["recall_at_10_delta"] < _DETERMINISM_TOL, (
                f"C0 re-run not reproducible (recall@10 Δ={determinism['recall_at_10_delta']:.4f} "
                f">= {_DETERMINISM_TOL}) — gross non-determinism, not API jitter"
            )
            assert (
                determinism["ndcg_at_10_delta"] < _DETERMINISM_TOL
            ), f"C0 re-run not reproducible (nDCG@10 Δ={determinism['ndcg_at_10_delta']:.4f})"

    _print_matrix(results, control, determinism, target_risk)
    _write_snapshot(results, control, determinism, target_risk)

    # Loosest sanity (NOT a quality gate): the live reference must clear the local R0 floor.
    assert results["C0"]["recall_at_10"] >= 0.5, "C0 recall@10 implausibly low — check the run"


def _print_matrix(results: dict, control: dict, determinism: dict, target_risk: float) -> None:
    ref = results["C0"]
    print("\n" + "=" * 92)
    print("R4-T3/T4 LIVE EMBEDDING MATRIX — reference = C0 (text-embedding-3-small, prefixes OFF)")
    print("=" * 92)
    hdr = f"{'cell':>4} {'recall@10':>10} {'nDCG@10':>9} {'rr_nDCG@5':>10} {'rr_recall@5':>12} "
    hdr += f"{'rr_xdoc@5':>10} {'policy@10':>10}"
    print(hdr)
    for cell in _CELLS:
        m = results[cell.label]
        print(
            f"{cell.label:>4} {m['recall_at_10']:>10.4f} {m['ndcg_at_10']:>9.4f} "
            f"{m['reranked_ndcg_at_5']:>10.4f} {m['reranked_recall_at_5']:>12.4f} "
            f"{(m['reranked_cross_document_recall_at_5'] or 0.0):>10.4f} "
            f"{(m['policy_recall_at_10'] or 0.0):>10.4f}"
        )
    print("-" * 92)
    # INVARIANT 6 — surface whether the reranker actually ran.  If rerank_applied_n is 0 the
    # "rr_*" columns are FUSED, not reranked (HF unavailable / fail-safe), and τ* is unfit-able.
    print("RERANKER STATUS (rerank_applied / n_cases; calibrated_none over R3 slice):")
    for cell in _CELLS:
        m = results[cell.label]
        applied, n, cnone = m["rerank_applied_n"], m["n_cases"], m["_n_calibrated_none"]
        flag = "" if applied else "  <-- WARNING: reranker NEVER applied; rr_* columns are FUSED"
        print(f"  {cell.label}: rerank_applied={applied}/{n}  calibrated_none={cnone}{flag}")
    print("-" * 92)
    print("PER-CELL VERDICT vs C0 (flat band |Δ| < 0.005):")
    for cell in _CELLS:
        if cell.label == "C0":
            continue
        m = results[cell.label]
        v_recall = _verdict(m["recall_at_10"], ref["recall_at_10"], higher_is_better=True)
        v_xdoc = _verdict(
            m["reranked_cross_document_recall_at_5"],
            ref["reranked_cross_document_recall_at_5"],
            higher_is_better=True,
        )
        v_policy = _verdict(
            m["policy_recall_at_10"], ref["policy_recall_at_10"], higher_is_better=True
        )
        print(f"  {cell.label}: recall@10 {v_recall} | rr_xdoc@5 {v_xdoc} | policy@10 {v_policy}")
    print("-" * 92)
    # INVARIANT 1 — C0 control: reranker marginal contribution on the REAL embedder.
    print("C0 CONTROL (reranker confound) — PRE-rerank FUSED vs RERANKED @5:")
    rr_delta = ref["reranked_recall_at_5"] - control.get("fused_recall_at_5", 0.0)
    print(
        f"  fused recall@5={control.get('fused_recall_at_5', 0.0):.4f}  "
        f"reranked recall@5={ref['reranked_recall_at_5']:.4f}  (reranker Δrecall@5={rr_delta:+.4f})"
    )
    print(
        f"  fused xdoc@5={(control.get('fused_cross_document_recall_at_5') or 0.0):.4f}  "
        f"reranked xdoc@5={(ref['reranked_cross_document_recall_at_5'] or 0.0):.4f}"
    )
    print("-" * 92)
    print(f"DETERMINISM (C0 re-run): {determinism}")
    print("-" * 92)
    print(f"τ* RECALIBRATION (R3 slice; r*={target_risk}; anchor={_COMMITTED_TAU}):")
    for cell in _CELLS:
        tau = results[cell.label]["tau"]
        if tau is None:
            print(
                f"  {cell.label}: no calibrated signal (HF reranker inactive?) — reranked == fused"
            )
            continue
        print(
            f"  {cell.label}: τ* {_COMMITTED_TAU} -> {tau['tau_star']}  "
            f"coverage={tau['coverage_at_tau']:.4f} risk={tau['risk_at_tau']:.4f}  "
            f"AURC reranker={tau['aurc_reranker']:.4f} cosine={tau['aurc_cosine']:.4f}"
        )
    print("=" * 92)


def _write_snapshot(results: dict, control: dict, determinism: dict, target_risk: float) -> None:
    snapshot = {
        "note": "R4-T3/T4 LIVE embedding matrix — reference C0; not a CI gate; scratch.",
        "target_risk": target_risk,
        "committed_tau_anchor": _COMMITTED_TAU,
        "control_c0_fused_at_5": control,
        "determinism_c0_rerun": determinism,
        "cells": {
            label: {k: v for k, v in m.items() if not k.startswith("_")}
            for label, m in results.items()
        },
    }
    out = metrics.REPORT_PATH.parent / "rag_corpus_embedding_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote live matrix snapshot: {out}")
