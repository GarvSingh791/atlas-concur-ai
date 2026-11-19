# ADR-004 — Calibrated abstention: Platt scaling + risk-coverage (not conformal)

**Status:** Accepted · implemented at R2-T3 (Platt) / R3 (τ\*) · shipped; τ\* is active only with a calibrated reranker (`novita` / `huggingface`), so CI and public staging (local reranker) use the cosine fallback
**Decision owner:** Garv Singh Saini (sole author)
**Related:** [ADR-003 (reranker supplies the signal)](ADR-003-cross-encoder-reranking.md) · debugging arc in [RAG_ARCHITECTURE §Measurement](../RAG_ARCHITECTURE.md#measurement--honest-evaluation)

## Context

The system must decide *per query* whether to answer or abstain. The original gate
was a single hand-tuned cosine threshold (`RETRIEVAL_SIMILARITY_THRESHOLD = 0.20`) —
a magic number with no error guarantee, brittle across query types, and on the wrong
signal (raw cosine is a weaker discriminator than the reranker score). We need a
threshold *derived from data* with a target error rate on answered queries.

## Options considered

1. **Keep the hand-tuned cosine gate.** Zero work; no guarantee; the number that
   started this project (a mis-calibrated 0.20-scale gate) is exactly the failure
   mode to retire.
2. **Raw reranker score, hand-thresholded.** Better signal, but reranker scores are
   not comparable across queries until calibrated — a fixed cutoff is still a guess.
3. **Split-conformal prediction.** Gives a finite-sample coverage guarantee under
   exchangeability (Angelopoulos & Bates 2021; CONFLARE; TRAQ; C-RAG). Rigorous and
   attractive — but assumes calibration/production exchangeability, needs care with
   the finite-sample-corrected quantile, and is heavier than the corpus justifies.
4. **Platt calibration + risk-coverage threshold selection (SGR).** Calibrate the
   reranker score into a probability, sweep the risk-coverage curve, pick the
   threshold meeting a target risk. Simple, dependency-light, and directly optimizes
   the answered-precision floor we care about.

## Decision

Adopt **Platt scaling + risk-coverage / AURC**, explicitly **not conformal
prediction** (conformal was researched and endorsed as a co-equal option but not
built).

- **Platt scaling** (`ai_agents/rag/calibration.py`): map the reranker's raw score to
  a probability `P(rel|s) = 1 / (1 + exp(a·s + b))`, fit by deterministic fixed-step
  gradient descent (Lin-Lin-Weng padding, `a ≤ 0` enforced), pure-stdlib (no
  numpy/sklearn) so it ships in the backend image. Params committed per provider
  (`rerank_calibration.json`).
- **Risk-coverage selection** (`ai_agents/rag/threshold.py`): sweep τ, trace
  `coverage(τ)` and `risk(τ)` on a labeled set, integrate **AURC** (lower = better,
  a *threshold-independent* signal-quality comparison), and select
  `τ* = min{τ : Risk(τ) ≤ r*}` for target risk `r* = 0.05` (⇒ ≥95% precision on
  answered). Committed as `abstention_threshold.json`. The fit was run on the
  HuggingFace-hosted model; the `novita` entry carries the same values forward because
  it serves the identical model (not re-fitted on Novita).
- **The gate** answers iff the top chunk's calibrated score `> τ*` on the calibrated
  path, falling back to the cosine `≥ 0.20` gate when no τ\* exists for the provider
  (the deterministic local provider is `not promoted`, so CI stays byte-identical).

Selected operating point: **τ\* = 0.039299**, coverage@τ\* = 0.742, risk@τ\* = 0.043,
reranker AURC **0.046982** vs cosine 0.087511
([metrics §3](../metrics/FINAL_METRICS_SUMMARY.md#3-calibration--abstention-keyed-reranker-signal)).

## Consequences

**Positive**
- The abstention threshold is now data-derived with a stated precision floor, not a
  magic number; AURC roughly **halves** vs cosine on the keyed set.
- Pure-stdlib, deterministic, committed artifacts — reproducible and image-friendly.
- The calibrated probability doubles as the user-facing confidence signal.

**Negative / tradeoffs**
- τ\* is selected **in-sample** (standard SGR); the honest, more trustworthy claim is
  the threshold-*independent* AURC comparison (documented in `threshold.py`).
- **The calibrated scale (~0.039) is far from the cosine scale (~0.20–0.70)** — a
  hazard that later caused a real production bug when a `0.70`-scale citation floor
  was applied to the calibrated signal (grounding fell to 0 while CI stayed green).
  Root-caused and fixed with scale-aware `resolve_citation_threshold`; this is the
  central story in [RAG_ARCHITECTURE §Measurement](../RAG_ARCHITECTURE.md#measurement--honest-evaluation).
  C7R: on the uncalibrated LOCAL reranker's cosine path the floor is 0.55 and, below 0.70,
  an answer also has to pass an answer-vs-question relevance gate to be grounded (same
  section); a calibrated provider that falls back to cosine keeps 0.70.
- Conformal's finite-sample guarantee is not obtained (accepted; revisit at
  production scale — [FUTURE_WORK](../FUTURE_WORK.md)).

## Research reference

Companion report [`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md) §2.2–2.3
— Geifman & El-Yaniv, *"Selective Classification for Deep Neural Networks"* (NeurIPS
2017, arXiv:1705.08500) for risk-coverage/SGR; Platt (1999) / Lin-Lin-Weng (2007) for
the sigmoid fit; conformal alternatives (Angelopoulos & Bates 2021, arXiv:2107.07511;
CONFLARE arXiv:2404.04287) presented there as the considered-not-adopted path.
