"""Platt calibration fitter for reranker scores (R2-T3).

``@pytest.mark.db`` — needs Postgres + pgvector.  Run MANUALLY via ``make calibrate-local``
(deterministic, no key) or ``make calibrate-hf`` (``RERANK_PROVIDER=huggingface`` +
``HF_API_TOKEN``).  NOT a CI gate: the broad ``pytest`` job collects it but db-self-skips
without Postgres, and the ``eval-db`` job only collects ``backend/tests/eval``.

What it does (leakage-free, deterministic):
  - Seeds the Helios corpus under ``org_a`` with the LOCAL embedder (retrieval held CONSTANT
    so the candidate set is identical across providers — only the reranker varies).
  - FIXED SEEDED split of the 197 gold queries (``seed=42``, first 40 of the shuffle):
        FIT  (28) = [109, 42, 2, 155, 113, 12, 79, 13, 90, 69, 3, 184, 30, 164, 168, 15,
                     158, 36, 133, 44, 5, 53, 84, 80, 64, 73, 37, 125]
        HELD (12) = [118, 78, 159, 66, 126, 171, 32, 52, 191, 142, 10, 60]
    ``FIT ∩ HELD = ∅`` is asserted (bidirectional).  The RETRIEVAL eval stays on all 197
    queries — calibration is NON-reordering (I5), so it cannot change recall/nDCG, which is
    why the calibration-slice / eval-set overlap is harmless.  (R3's abstention calibration
    will need a clean held-out split not reused for anything else — flagged forward.)
  - Fits Platt over the FIT slice's (rerank_score, is_relevant) pairs for the ACTIVE provider
    and writes ``ai_agents/rag/rerank_calibration.json`` keyed by provider name (params
    rounded to 6 dp for reproducible byte-match).
  - Validates on the disjoint HELD-OUT slice: Spearman(calibrated, relevance) and a
    cross-doctype mean-calibrated consistency table.  ρ>0 is HARD-asserted for huggingface
    (the production calibration); for the local stand-in it is reported (soft).
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.calibration import (
    CALIBRATION_PATH,
    PlattParams,
    apply_platt,
    calibration_key,
    fit_platt,
    live_artifact_path,
    load_calibration_params,
    spearman,
)
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from backend.tests.eval.test_rag_corpus_eval import _doc_type_of, _seed_corpus_for_org
from config.settings import get_settings
from database.models.identity import Organization
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.factory import get_embeddings_provider
from services.rerank_provider.factory import get_rerank_provider

pytestmark = pytest.mark.db

_FIT_SEED = 42
_SLICE_N = 40  # first 40 of the seeded shuffle of range(197)
_FIT_N = 28  # 70% FIT; the remaining 12 are HELD-OUT (30%)
_CAND_TOP_K = 50  # candidates per query (matches the rerank candidate cap)


def _seeded_split(n_cases: int) -> tuple[list[int], list[int]]:
    """Deterministic FIT / HELD-OUT index split with a leakage assert (bidirectional)."""
    idx = list(range(n_cases))
    random.Random(_FIT_SEED).shuffle(idx)
    fit_idx = idx[:_FIT_N]
    held_idx = idx[_FIT_N:_SLICE_N]
    assert set(fit_idx).isdisjoint(held_idx), "FIT leaked into HELD-OUT"
    assert set(held_idx).isdisjoint(fit_idx), "HELD-OUT leaked into FIT"
    return fit_idx, held_idx


def _collect_pairs(
    tool: PolicyRetrievalTool,
    ctx: AgentContext,
    session: Session,
    cases: list[CorpusRagCase],
    indices: list[int],
) -> list[tuple[float, int, str]]:
    """(rerank_score, is_relevant, doctype) per reranked candidate over the given cases."""
    pairs: list[tuple[float, int, str]] = []
    for i in indices:
        case = cases[i]
        gold = set(case.expected_unit_ids)
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=_CAND_TOP_K), context=ctx, session=session
        )
        for c in r.chunks:
            if c.rerank_score is None or c.clause_ref is None:
                continue
            is_rel = 1 if c.clause_ref in gold else 0
            pairs.append((c.rerank_score, is_rel, _doc_type_of(c.clause_ref)))
    return pairs


def test_fit_and_validate_rerank_calibration(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """Fit + persist Platt params for the active provider; validate monotonicity on held-out."""
    get_settings.cache_clear()
    settings = get_settings()
    provider_name = settings.RERANK_PROVIDER
    provider = get_rerank_provider()
    if hasattr(provider, "warmup"):
        provider.warmup()  # HF cold-start ping BEFORE the fit loop (excluded from any timing)

    _seed_corpus_for_org(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    # FORK-G: embedder follows EMBEDDINGS_PROVIDER — local (byte-identical to R3) for
    # calibrate-local; the real model for `make calibrate-live` (re-fit Platt on the
    # live-embedded candidate set, query + doc matched).
    tool = PolicyRetrievalTool(embedder=get_embeddings_provider())

    fit_idx, held_idx = _seeded_split(len(rag_corpus_cases))
    fit_pairs = _collect_pairs(tool, ctx, db_session, rag_corpus_cases, fit_idx)
    held_pairs = _collect_pairs(tool, ctx, db_session, rag_corpus_cases, held_idx)
    assert fit_pairs and held_pairs, "no candidate pairs collected"

    fit_scores = [s for s, _, _ in fit_pairs]
    fit_labels = [y for _, y, _ in fit_pairs]
    n_pos = sum(fit_labels)
    n_neg = len(fit_labels) - n_pos
    assert n_pos > 0 and n_neg > 0, f"FIT slice is single-class (pos={n_pos} neg={n_neg})"

    raw = fit_platt(fit_scores, fit_labels)
    params = PlattParams(round(raw.a, 6), round(raw.b, 6))  # rounded -> reproducible byte-match

    # Write/update the artifact keyed by pipeline (preserve other keys' params).
    # R4-T3 write-guard: under a REAL embedder write to a scratch ``.live.json`` sibling under an
    # embedder-qualified key, so a live Platt re-fit never clobbers the committed local-embedder
    # params the CI/prod gate reads.  Under local → committed path + bare key → BYTE-IDENTICAL.
    out_path = live_artifact_path(
        CALIBRATION_PATH, embeddings_provider=settings.EMBEDDINGS_PROVIDER
    )
    key = calibration_key(
        provider_name,
        embeddings_provider=settings.EMBEDDINGS_PROVIDER,
        embeddings_model=settings.EMBEDDINGS_MODEL,
        embeddings_dimensions=settings.EMBEDDINGS_DIMENSIONS,
        contextual_prefixes=settings.RAG_CONTEXTUAL_PREFIXES,
    )
    existing: dict[str, Any] = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    before = json.loads(json.dumps(existing))
    existing[key] = {"a": params.a, "b": params.b}
    # Rewrite the tracked file only when a value changed: re-running with the same
    # result must not churn it (json.dumps re-escaping a note is a no-op diff).
    if existing != before:
        out_path.write_text(
            json.dumps(existing, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    load_calibration_params.cache_clear()  # so a later in-process read sees the fresh params

    # Held-out validation: monotonicity of calibrated score in true relevance.
    held_scores = [s for s, _, _ in held_pairs]
    held_labels = [float(y) for _, y, _ in held_pairs]
    calibrated = [apply_platt(s, params) for s in held_scores]
    rho = spearman(calibrated, held_labels)

    # Cross-doctype consistency: mean calibrated score for relevant vs irrelevant per doctype.
    by_dt: dict[str, dict[str, list[float]]] = {}
    for cal, (_, y, dt) in zip(calibrated, held_pairs, strict=True):
        bucket = by_dt.setdefault(dt, {"rel": [], "irr": []})
        bucket["rel" if y == 1 else "irr"].append(cal)

    print("\n" + "=" * 72)
    print(f"RERANK CALIBRATION (Platt) — provider={provider_name}")
    print("=" * 72)
    print(f"FIT pairs={len(fit_pairs)} (pos={n_pos} neg={n_neg})  HELD-OUT pairs={len(held_pairs)}")
    print(f"params: a={params.a} b={params.b}  (raw a={raw.a:.6f} b={raw.b:.6f})")
    print(f"held-out Spearman(calibrated, relevance) rho = {rho:.4f}")
    print("cross-doctype mean calibrated score (held-out):")
    for dt in sorted(by_dt):
        rel, irr = by_dt[dt]["rel"], by_dt[dt]["irr"]
        mrel = f"{sum(rel) / len(rel):.4f}" if rel else "n/a"
        mirr = f"{sum(irr) / len(irr):.4f}" if irr else "n/a"
        print(f"  {dt:10} relevant={mrel} (n={len(rel)})  irrelevant={mirr} (n={len(irr)})")
    print(f"wrote {out_path} (keys: {sorted(existing)})")
    print("=" * 72)

    # Determinism + sign convention always hold.
    assert params.a <= 0.0, "Platt slope must be <= 0 (higher score -> higher probability)"
    assert fit_platt(fit_scores, fit_labels) == raw, "fit is not deterministic"
    # Monotonicity in relevance: HARD for the production (HF) calibration; soft for the
    # local CI stand-in (its score is RRF-rank-anchored, so the signal can be weak).
    if provider_name == "huggingface":
        assert rho > 0.0, f"HF held-out Spearman rho={rho:.4f} not > 0 — calibration not meaningful"
    elif rho <= 0.0:
        print(f"[NOTE] local stand-in held-out rho={rho:.4f} <= 0 (acceptable; HF is the signal)")
