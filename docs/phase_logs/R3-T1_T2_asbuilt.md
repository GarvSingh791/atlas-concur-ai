# R3-T1 + R3-T2 — Trustworthy Abstention: Calibrated Threshold (as-built)

**Branch:** `r3-calibrated-threshold` (off `main` post-R2 squash) · **Status:** complete, UNCOMMITTED (committed in a separate step)
**Date:** 2026-06-29

## Goal

Replace the brittle hand-tuned `top cosine ≥ 0.20` answer/abstain gate with a **data-derived
threshold τ\*** computed on the **calibrated reranker score** via a risk-coverage
(selective-prediction) sweep — so abstention is statistically defensible, with a precision
floor on answered queries and a fail-safe fall back to cosine.

## What shipped

| Artifact | Path | Notes |
|---|---|---|
| Pure threshold module | `ai_agents/rag/threshold.py` | risk-coverage sweep, AURC, `select_threshold`, `compare_signals`, `load_abstention_threshold` (lru-cached, fail-safe), `CalibrationError`. Pure stdlib. |
| Unit tests | `ai_agents/tests/test_threshold.py` | 15 hermetic tests (purity, AURC, select edge cases, loader fail-safe incl. `tau:null`). |
| Gate swap | `ai_agents/rag/retrieval.py` | calibrated `> τ*` primary; cosine `≥ RETRIEVAL_SIMILARITY_THRESHOLD` fallback. |
| Gate tests | `ai_agents/tests/test_rag_retrieval_calibrated_gate.py` | 4 hermetic tests (V4 None→cosine, V5 absent-artifact→WARNING+cosine, calibrated grounds/abstains). |
| Fitter | `evals/calibration/test_fit_abstention_threshold.py` | `@pytest.mark.db`; seed=42 split (indices 40–196), leakage assert, signal collection, text table, **NY-canary promotion guard**, artifact write. |
| Committed artifact | `ai_agents/rag/abstention_threshold.json` | per-provider τ\* (ships in backend image, beside `rerank_calibration.json`). |
| Settings | `config/settings/base.py` | `ABSTENTION_TARGET_RISK=0.05` added; `RETRIEVAL_SIMILARITY_THRESHOLD` deprecated→fallback. |
| Env doc | `.env.example` | `ABSTENTION_TARGET_RISK=0.05` documented. |
| Make targets | `Makefile` | `threshold-local`, `threshold-hf`, `recalibrate` (file-scoped so they never touch `rerank_calibration.json`). |

## Reconcile findings (CODE WON over the task spec's premises)

- **F-A — unanswerable count.** The task spec read `0.019231 ≈ 1/52` as the unanswerable
  proportion. It is the *current correct-abstention rate*. Ground truth: **197 = 145
  answerable + 52 unanswerable**; the R3 set (157) holds **113 answerable + 44 unanswerable**
  — well-powered, NOT the "≈3–4" the task spec assumed. The AURC is robust; the real caveat is
  in-sample τ\* selection.
- **F-B — NY (ans-142) is `answerable: true`**, in the R3 set. It is a **coverage** canary
  (must be ANSWERED), not an abstention target. The task spec's I_R3_2/V17 mislabelled it.
- **F-C — artifact path.** Masterplan says `ai_agents/eval/` (not shipped). Used
  `ai_agents/rag/abstention_threshold.json` (sibling of `rerank_calibration.json`).
- **F-D — new `threshold.py`** module instead of editing `calibration.py`.
- **F-E — fitter is a pytest file**; named `test_fit_abstention_threshold.py`; make targets
  file-scoped so `threshold-*` never re-runs the Platt fitter (F3).
- **F-F — `evals/harness/metrics.py` already has `risk_coverage_curve`/`aurc`** but isn't
  shipped; `threshold.py` has its own pure copy (same (0,0)-anchored trapezoid + 6dp).
- **eval-db is NOT gate-driven.** `backend/tests/eval/test_rag_corpus_eval.py` computes
  coverage/abstention from raw `max_dense_similarity ≥ _SILENT_GATE (0.20)` directly (not via
  `GroundedRetriever`). So the gate swap leaves its numbers byte-identical; its hard
  assertions (coverage==1.0, abstention==0.019231) remain valid — **no update needed**
  (binding-note #1 resolved by reconciliation, not by editing the test).
- **Empty-chunks guard** already exists at `retrieval.py:204` (`if not result.chunks`) before
  any `chunks[0]` access — no new IndexError path introduced.

## The decisive empirical finding (F-G/F-H)

The **local lexical reranker stand-in produces a degenerate calibrated signal** (scores
clustered in ~[0.20, 0.244]). At r\*=0.05 its τ\*=0.2206 collapses coverage to **0.42** and
**abstains NY** — and breaks `test_eval_db_retrieval.py` (which drives `GroundedRetriever` with
grounded/silent gold labels calibrated to the cosine gate). Binding-note #4: NY abstaining is a
bug, not a phase close.

**Resolution — NY-canary promotion guard.** The fitter only promotes τ\* to the *active*
runtime gate (`tau`) if the NY coverage canary stays answered (and coverage > 0). Otherwise it
records the computed τ\* but writes `tau: null` → the runtime **falls back to the proven cosine
gate** (I_R3_3). This encodes binding-note #4 as an automated, reproducible invariant.

- **local** → NOT promoted (NY abstains) → `tau: null` → cosine fallback → NY answered,
  coverage 1.0, all gate-consuming tests green. **byte-reproducible (V3).**
- **huggingface** → PROMOTED (real cross-encoder, NY answered) → live calibrated gate.

## τ\* results

```
provider=local   (NOT promoted — cosine fallback)
  computed τ* = 0.220598   coverage@τ* = 0.4204   risk@τ* = 0.0455 (precision 95.45%)
  unanswerable abstention recall = 0.9318 (41/44)  leakers: una-037, una-033, una-035
  NY (ans-142): ABSTAINS  →  guard withholds promotion
  AURC reranker 0.085589 vs cosine 0.085770 (Δ −0.000181 — essentially tied; weak signal, F-H)

provider=huggingface  (PROMOTED — live calibrated gate)   [τ* network-derived, not byte-reproducible]
  τ* = 0.039299   coverage@τ* = 0.7419   risk@τ* = 0.0435 (precision 95.65%)
  unanswerable abstention recall = 0.8864 (39/44)  leakers: una-037, una-039, una-035, una-034, una-038
  NY (ans-142): ANSWERED ✓
  AURC reranker 0.046982 vs cosine 0.087511 (Δ −0.040529 — reranker WINS decisively, ~46% lower)
  dropped to API timeouts: 2/157 (all 44 unanswerable present; curve well-powered)
```

## Split / leakage (V2)

seed=42 shuffle of `range(197)`; `idx[:28]`=Platt FIT, `idx[28:40]`=Platt HELD-OUT,
`idx[40:]`=R3 (157). Bidirectional disjointness asserted in the fitter. R3 set must NOT be
reused for further calibration (R4 needs a fresh split) — flagged in the fitter header (F9).

## Invariants

- **I_R3_1** precision-on-answered ≥ 95%: HF risk@τ\*=0.0435 ≤ 0.05 ✓ (95.65%; HARD-asserted in fitter).
- **I_R3_2** unanswerable abstain: reported as recall (HF 0.8864, 39/44); 5 leakers listed honestly.
- **I_R3_3** fail-safe to cosine on `calibrated_score None` / absent artifact / `tau:null`: tests V4/V5 + local path.
- **I_R3_4** `retrieve()` signature byte-identical.
- **I_R3_5** `threshold.py` pure stdlib; no backend import (subprocess proof).

## Known limitations / follow-ups

- HF τ\* is **network-derived, not byte-reproducible** (transient HF Inference timeouts vary
  per run; same caveat R2 accepted for committed HF Platt params). Re-run `make threshold-hf`
  on a healthy API to refresh.
- The `reason_detail` f-string is built in both gate branches (mypy-clean narrowing) rather
  than only on failure — negligible cost, deliberate.
- Confidence/rationale still derived from cosine `top_similarity` (R3-T3+ scope).
- R3-T5 hard eval assertions (recall=1.0 on unanswerable) are OUT OF SCOPE here.

## Phase status — PHASE R3 CLOSES

R3-T1 (threshold.py + fitter + abstention_threshold.json) and R3-T2 (gate swap in
retrieval.py + ABSTENTION_TARGET_RISK + make targets) are complete and verified. **PHASE R3
CLOSES.** The calibrated risk-coverage gate is live for the production cross-encoder (HF);
the local/CI path retains the proven cosine gate via the promotion guard.

## Gate before/after (R3-T2)

```
BEFORE (ai_agents/rag/retrieval.py)
    top_similarity = (result.max_dense_similarity
                      if result.max_dense_similarity is not None
                      else result.chunks[0].similarity)
    if top_similarity < self._threshold:        # hand-tuned 0.20 cosine gate
        return GroundedRetrieval(status=silent, threshold=self._threshold, ...)

AFTER
    tau_star = load_abstention_threshold(self._rerank_provider)   # keyed by RERANK_PROVIDER
    top_calibrated = result.chunks[0].calibrated_score
    if tau_star is not None and top_calibrated is not None:
        gate_passes = top_calibrated > tau_star                   # PRIMARY: calibrated > τ*
        gate_threshold = tau_star
        reason_detail = f"calibrated score {top_calibrated:.4f} <= tau* {tau_star:.4f}"
    else:
        gate_passes = top_similarity >= self._threshold           # FAIL-SAFE: cosine ≥ 0.20
        gate_threshold = self._threshold
        reason_detail = f"top similarity {top_similarity:.4f} < threshold {self._threshold:.4f}"
    if not gate_passes:
        return GroundedRetrieval(status=silent, threshold=gate_threshold,
                                 reason=f"below_threshold: {reason_detail}", ...)
```

## eval-db (V9)

`make eval-db` → **47 passed** (15 deselected). `answerable_coverage=1.0` /
`unanswerable_abstention=0.019231` are **byte-identical to R0/R2**. This is correct, not a
gap: `backend/tests/eval/test_rag_corpus_eval.py` decides answer/abstain from raw
`max_dense_similarity ≥ _SILENT_GATE (0.20)` directly (not via `GroundedRetriever`), so the
R3 gate swap cannot move its numbers. The gate-consuming `test_eval_db_retrieval.py` (which
DOES drive `GroundedRetriever`) passes because local is not promoted → cosine fallback →
its grounded/silent gold labels (calibrated to the 0.20 gate) still hold.

## QA findings (deferred — carried forward)

- **#1 — Calibrated abstention is HF-only.** With `RERANK_PROVIDER=local` (the default/CI
  value) the degenerate lexical signal is not promoted, so the runtime keeps the R2 cosine
  gate (abstention 0.019231 — unchanged). Trustworthy calibrated abstention is realized ONLY
  with `RERANK_PROVIDER=huggingface`. **Recommendation:** set `RERANK_PROVIDER=huggingface`
  in staging/production env vars so the calibrated gate is actually exercised in deployment.
- **#3 — Observability asymmetry.** On a *grounded* result the `threshold`/`confidence`
  fields still report the cosine scale (`self._threshold`) even when τ\* was the deciding
  gate. Deliberate (minimal blast radius for R3-T2); the clean fix belongs with the R3-T3
  confidence/rationale work. Not a mis-gate (the answer/abstain decision is correct) — only
  the reported observability scalar is cosine-scaled.

## Deferred forward (explicit)

- **R3-T3** — confidence/rationale fix (resolve the observability asymmetry above).
- **R3-T5** — abstention recall=1.0 on unanswerable NOT achieved (HF abstains 39/44; 5
  leakers una-037/039/035/034/038). Expected under a 95% *precision* floor (≠ 100% recall);
  out of scope here, flagged.
- **R4** — any embedder/corpus change MUST re-run `make recalibrate`; the R3 calibration set
  (seed=42 indices 40–196) must NOT be reused — R4 needs a fresh split or cross-validation.
- **HF refresh** — HF τ\* is network-derived (free-tier timeouts are a known reality, not a
  bug). Re-run `make threshold-hf` on a healthy API to refresh if desired.
