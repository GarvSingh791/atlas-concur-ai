"""Abstention-threshold fitter — risk-coverage sweep over the R3 calibration set (R3-T1).

``@pytest.mark.db`` — needs Postgres + pgvector.  Run MANUALLY via ``make threshold-local``
(deterministic, no key) or ``make threshold-hf`` (``RERANK_PROVIDER=huggingface`` +
``HF_API_TOKEN``).  NOT a CI gate: the broad ``pytest`` job collects it but db-self-skips
without Postgres, and the ``eval-db`` job only collects ``backend/tests/eval``.

R3 CALIBRATION SET — DO NOT REUSE FOR FURTHER CALIBRATION
--------------------------------------------------------
The 197 gold queries are shuffled ONCE with ``seed=42`` (the same shuffle the R2 Platt fitter
uses).  The slices are partitioned, leakage-free:

    indices  0–27   (28)  -> Platt FIT      slice   (R2, consumed — do NOT reuse)
    indices 28–39   (12)  -> Platt HELD-OUT slice   (R2, consumed — do NOT reuse)
    indices 40–196  (157) -> R3 abstention calibration set  <-- THIS FITTER

This set (indices 40–196) MUST NOT be reused for any further calibration.  R4 (an embedder
change) and beyond need a FRESH split or cross-validation — τ* fit here is in-sample for this
set, and reusing it downstream would leak.

What it does (leakage-free, deterministic on local):
  - Seeds the Helios corpus under ``org_a`` with the LOCAL embedder (retrieval held CONSTANT
    so the candidate set is identical across providers — only the reranker varies).
  - For each R3 query: runs ``retrieve()`` with the ACTIVE provider and records the GATE
    SIGNAL = the TOP chunk's ``calibrated_score`` (what ``retrieval.py`` thresholds), the
    cosine baseline ``max_dense_similarity``, and the gold ``answerable`` label.
  - Sweeps the risk-coverage curve, computes AURC, selects τ* for ``ABSTENTION_TARGET_RISK``,
    compares the reranker signal vs the cosine baseline, and prints a text table.
  - Writes ``ai_agents/rag/abstention_threshold.json`` keyed by provider (τ* rounded to 6 dp
    for a reproducible byte-match), preserving other providers' entries.

EPISTEMIC NOTE (replaces the masterplan's mistaken "≈3–4 unanswerable" premise): the R3 set
holds ~44 unanswerable queries (52 of 197 overall), so the curve is well-powered.  The honest
caveat is that τ* is selected IN-SAMPLE on this set; the AURC comparison is the
threshold-independent, more trustworthy signal-quality metric.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.rag.calibration import calibration_key, live_artifact_path
from ai_agents.rag.threshold import (
    ABSTENTION_PATH,
    CalibrationError,
    compare_signals,
    compute_risk_coverage,
    load_abstention_threshold,
    select_threshold,
)
from ai_agents.tools.policy_retrieval import PolicyRetrievalTool, RetrievalIntent
from backend.tests.eval.test_rag_corpus_eval import _seed_corpus_for_org
from config.settings import get_settings
from database.models.identity import Organization
from evals.harness.loaders import CorpusRagCase
from services.embeddings_provider.factory import get_embeddings_provider
from services.rerank_provider.factory import get_rerank_provider

pytestmark = pytest.mark.db

_FIT_SEED = 42  # the SAME seed the R2 Platt fitter uses (deterministic, RNG-free split)
_PLATT_SLICE_N = 40  # indices 0–39 are consumed by R2 (28 FIT + 12 HELD-OUT)
_PLATT_FIT_N = 28
_NY_CASE_ID = "ans-142"  # the cross-document "trip to New York" query — answerable, must ANSWER


def _r3_indices(n_cases: int) -> list[int]:
    """Indices 40–196 of the seed=42 shuffle, with a bidirectional leakage assert vs R2."""
    import random  # local import: keep module import-time pure (no RNG at collection)

    idx = list(range(n_cases))
    random.Random(_FIT_SEED).shuffle(idx)
    platt_fit = set(idx[:_PLATT_FIT_N])
    platt_held = set(idx[_PLATT_FIT_N:_PLATT_SLICE_N])
    r3 = idx[_PLATT_SLICE_N:]
    # Leakage-free by construction; assert both directions so a reviewer trusts the split.
    assert set(r3).isdisjoint(platt_fit), "R3 set leaked into the Platt FIT slice"
    assert set(r3).isdisjoint(platt_held), "R3 set leaked into the Platt HELD-OUT slice"
    return r3


def _percentile_rows(curve: list[dict]) -> list[dict]:
    """A 5-row τ-sweep sample: max / 75th / 50th / 25th / min τ (curve is high→low τ)."""
    n = len(curve)
    return [curve[i] for i in (0, n // 4, n // 2, (3 * n) // 4, n - 1)]


def test_fit_and_persist_abstention_threshold(
    db_session: Session,
    org_a: Organization,
    rag_corpus_cases: list[CorpusRagCase],
) -> None:
    """Sweep the risk-coverage curve over the R3 set, select τ*, and persist it per provider."""
    get_settings.cache_clear()
    settings = get_settings()
    provider_name = settings.RERANK_PROVIDER
    target_risk = settings.ABSTENTION_TARGET_RISK
    top_k = settings.RETRIEVAL_TOP_K

    provider = get_rerank_provider()
    if hasattr(provider, "warmup"):
        provider.warmup()  # HF cold-start ping BEFORE the loop (excluded from any timing)

    _seed_corpus_for_org(db_session, org_a)
    ctx = AgentContext(organization_id=org_a.id, granted_permission_keys=frozenset(), user_id=None)
    # FORK-G: embedder follows EMBEDDINGS_PROVIDER — local (byte-identical to R3) for
    # threshold-local; the real model for `make threshold-live` (recalibrate τ* on the
    # live-embedded candidate set, query + doc matched).
    tool = PolicyRetrievalTool(embedder=get_embeddings_provider())

    r3 = _r3_indices(len(rag_corpus_cases))

    # Per-query GATE signal: the TOP chunk's calibrated_score (what retrieval.py thresholds),
    # the cosine baseline, the label, and the id.  A no-chunks query is a forced abstention.
    cal_scores: list[float] = []
    cos_scores: list[float] = []
    labels: list[int] = []
    ids: list[str] = []
    n_no_chunks = 0
    n_calibrated_none = 0
    for i in r3:
        case = rag_corpus_cases[i]
        label = 1 if case.answerable else 0
        r = tool.retrieve(
            RetrievalIntent(query=case.query, top_k=top_k), context=ctx, session=db_session
        )
        if not r.chunks:
            n_no_chunks += 1
            continue  # gate abstains (no_chunks); excluded from the score-based curve
        top = r.chunks[0]
        if top.calibrated_score is None:
            n_calibrated_none += 1
            continue  # this query would hit the cosine fallback at runtime, not the τ* gate
        cos = r.max_dense_similarity if r.max_dense_similarity is not None else top.similarity
        cal_scores.append(top.calibrated_score)
        cos_scores.append(cos)
        labels.append(label)
        ids.append(case.id)

    n = len(cal_scores)
    assert n > 0, "no calibrated signals collected over the R3 set"
    n_pos = sum(labels)
    n_neg = n - n_pos
    assert n_pos > 0 and n_neg > 0, f"R3 set single-class (answerable={n_pos} unanswerable={n_neg})"

    # Risk-coverage sweep on the calibrated signal; AURC vs the cosine baseline.
    curve = compute_risk_coverage(cal_scores, labels)
    cmp = compare_signals(cos_scores, cal_scores, labels, target_risk=target_risk)
    try:
        tau_star = select_threshold(curve, target_risk=target_risk)
        unachievable = False
    except CalibrationError as exc:
        tau_star = min(cal_scores)
        unachievable = True
        print(f"\n[WARN] select_threshold raised: {exc}\n  -> τ*=min(scores) (answer-all)")

    # Recompute the operating point at τ* directly from the raw signal (no float-match on rows).
    answered = [
        (sc, lab, qid)
        for sc, lab, qid in zip(cal_scores, labels, ids, strict=True)
        if sc > tau_star
    ]
    n_answered = len(answered)
    n_abstained = n - n_answered
    coverage_at = n_answered / n
    risk_at = sum(1 for _sc, lab, _id in answered if lab == 0) / max(n_answered, 1)

    # Unanswerable abstention recall + leakers (V10/V17): unanswerable queries that ANSWER.
    unanswerable_ids = [
        qid for sc, lab, qid in zip(cal_scores, labels, ids, strict=True) if lab == 0
    ]
    leakers = [qid for sc, lab, qid in answered if lab == 0]
    abstention_recall = (len(unanswerable_ids) - len(leakers)) / max(len(unanswerable_ids), 1)

    # NY case (V10/V17): answerable, must be ANSWERED (not abstained) under τ*.
    ny_in_r3 = _NY_CASE_ID in ids
    ny_answered = _NY_CASE_ID in {qid for _sc, _lab, qid in answered}

    # ---- Text table ---------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"ABSTENTION THRESHOLD (risk-coverage) — provider={provider_name}")
    print("=" * 78)
    print(
        f"R3 set: n={n} usable (answerable={n_pos} unanswerable={n_neg}); "
        f"no_chunks={n_no_chunks} calibrated_none={n_calibrated_none}"
    )
    print(f"target_risk r*={target_risk}  (precision-on-answered floor = {1 - target_risk:.2%})")
    print("-" * 78)
    print(f"{'tau':>10} {'coverage':>10} {'risk':>10} {'n_answered':>12}")
    for row in _percentile_rows(curve):
        print(
            f"{row['tau']:>10.4f} {row['coverage']:>10.4f} {row['risk']:>10.4f} "
            f"{row['n_answered']:>12d}"
        )
    print("-" * 78)
    print(f"AURC reranker = {cmp['reranker_aurc']:.6f}   cosine = {cmp['cosine_aurc']:.6f}")
    print(f"AURC delta (reranker - cosine) = {cmp['aurc_delta']:+.6f}", end="  ")
    if cmp["aurc_delta"] < 0:
        print("[reranker signal WINS — lower risk at equal coverage]")
    else:
        print("[reranker signal does NOT beat cosine — see caveat below]")
    if "warning" in cmp:
        print(f"[CAVEAT] {cmp['warning']}")
    print(
        "[CAVEAT] τ* is selected IN-SAMPLE on the 157-query R3 set "
        f"({n_neg} unanswerable); AURC is the threshold-independent signal-quality metric."
    )
    print("-" * 78)
    print(
        f"SELECTED τ* = {tau_star:.6f}  coverage@τ*={coverage_at:.4f}  risk@τ*={risk_at:.4f}  "
        f"(answered={n_answered} abstained={n_abstained})"
        + ("  [TARGET UNACHIEVABLE — answer-all fallback]" if unachievable else "")
    )
    print(
        f"unanswerable abstention recall = {abstention_recall:.4f} "
        f"({len(unanswerable_ids) - len(leakers)}/{len(unanswerable_ids)})"
        + (f"  LEAKERS={leakers}" if leakers else "  (all unanswerable abstain)")
    )
    leaker_conf = [(qid, sc) for sc, lab, qid in answered if lab == 0]
    if leaker_conf:
        print(
            "  leaker calibrated confidences: " + ", ".join(f"{q}={s:.4f}" for q, s in leaker_conf)
        )
    print(
        f"NY case {_NY_CASE_ID}: in_R3={ny_in_r3}  answered={ny_answered}  "
        + ("OK (answerable & answered)" if (ny_in_r3 and ny_answered) else "")
        + ("[N/A — not in R3 set]" if not ny_in_r3 else "")
        + ("[REGRESSION — NY abstained!]" if (ny_in_r3 and not ny_answered) else "")
    )

    # PROMOTION GUARD (binding-note #4 as an automated invariant): promote τ* to the ACTIVE
    # runtime gate ONLY if the NY coverage canary stays answered (a degenerate signal that
    # abstains an answerable flagship query must NOT gate production).  When NOT promoted the
    # computed τ* is still recorded, but the artifact's ``tau`` is null so the runtime gate
    # FALLS BACK to the proven cosine threshold (I_R3_3).  The local lexical stand-in is the
    # expected non-promotion case; a real cross-encoder (HF) that answers NY is promoted.
    promoted = (not unachievable) and (coverage_at > 0.0) and (ny_answered if ny_in_r3 else True)
    _verdict = "PROMOTED — calibrated gate ACTIVE" if promoted else "NOT PROMOTED — cosine fallback"
    print(f"PROMOTION: {_verdict}  (provider={provider_name})")
    print("=" * 78)

    # ---- Persist artifact (merge per pipeline-key, preserve others) ---------------------
    # R4-T3 write-guard: under a REAL embedder write to a scratch ``.live.json`` sibling under an
    # embedder-qualified key, so a live recalibration produces EVIDENCE without ever clobbering
    # the committed local-embedder τ* the CI/prod gate reads.  Under local → committed path +
    # bare provider key → BYTE-IDENTICAL to R3.
    out_path = live_artifact_path(ABSTENTION_PATH, embeddings_provider=settings.EMBEDDINGS_PROVIDER)
    key = calibration_key(
        provider_name,
        embeddings_provider=settings.EMBEDDINGS_PROVIDER,
        embeddings_model=settings.EMBEDDINGS_MODEL,
        embeddings_dimensions=settings.EMBEDDINGS_DIMENSIONS,
        contextual_prefixes=settings.RAG_CONTEXTUAL_PREFIXES,
    )
    existing: dict = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    before = json.loads(json.dumps(existing))
    existing[key] = {
        # ``tau`` is the ACTIVE gate threshold the loader reads; null => cosine fallback.
        "tau": round(tau_star, 6) if promoted else None,
        "promoted": promoted,
        "computed_tau": round(tau_star, 6),  # the τ* the sweep selected (recorded either way)
        "coverage_at_tau": round(coverage_at, 6),
        "risk_at_tau": round(risk_at, 6),
        "n_queries": n,
        "target_risk": target_risk,
        "aurc_reranker": cmp["reranker_aurc"],
        "aurc_cosine": cmp["cosine_aurc"],
        "ny_canary_answered": (ny_answered if ny_in_r3 else None),
        "note": (
            "active calibrated gate"
            if promoted
            else "NOT promoted (NY coverage canary abstains / degenerate signal) — cosine fallback"
        ),
    }
    # Rewrite the tracked file only when a value changed: re-running with the same
    # result must not churn it (json.dumps re-escaping a note is a no-op diff).
    if existing != before:
        out_path.write_text(
            json.dumps(existing, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    load_abstention_threshold.cache_clear()  # so a later in-process read sees fresh params
    print(f"wrote {out_path} (keys: {sorted(existing)})")

    # ---- Invariant assertions -----------------------------------------------------------
    # I_R3_1: precision-on-answered ≥ floor at τ*  ==  risk@τ* ≤ target_risk (by construction).
    if not unachievable:
        assert (
            risk_at <= target_risk + 1e-9
        ), f"risk@τ*={risk_at:.4f} exceeds target r*={target_risk} — precision floor violated"
    # Determinism + sane bounds.
    assert min(cal_scores) <= tau_star <= max(cal_scores) or unachievable
    # AURC verdict is SOFT (the local stand-in's calibrated signal may not beat cosine — F-H;
    # HF is the production signal).  Hard-asserting it would be flaky at this sample size.
