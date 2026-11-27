"""Documented threshold constants for the AI-eval gates (BUILD BRIEF section 5).

These are the assertion bars the pytest entrypoints check ``metrics.json``
against.  Two are HARD invariants (a violation BLOCKS the phase); the rest are
FLOORS that must bite on regression.

IMPORTANT — these are STARTING values.  The entrypoint authors MUST measure the
actual metric on the local provider first, then set each floor to
``measured - margin`` so that:
  (a) the gate passes on today's behavior, and
  (b) it FAILS if the system regresses by more than the margin.
A floor pinned far below the measured value is a trivially-true assertion and is
forbidden by the fences.  When tightening, replace the starting value here and
note the measured value + date in the comment next to it.

Nothing in this module imports production code; it is pure constants so it can
be read by both tiers and by the report writer.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# M-A1 — receipt field extraction (per-field RECALL floors)
# ---------------------------------------------------------------------------
# Rationale: amount/currency/date are deterministic, normalized comparisons and
# should be near-perfect on the seeded/stubbed pipeline -> high bars.
# expense_type/vendor/location include gap-filled (LLM-inferred) values and
# reflection-corrected locations -> graduated, lower bars.  Each field is
# asserted SEPARATELY so a regression in any one field cannot hide inside an
# average.  (Per BRIEF: report each field; never a single hidden average.)
#
# MEASURED (Tier-2 DB, full real run_receipt_pipeline over all 85 golden cases
# with the harness EvalOcr/EvalJsonLlm/EvalItinerary stubs; LLM_PROVIDER=local
# path is deterministic + hash-seed-independent, 2026-06-20):
#   amount  P/R/F1 = 1.0000 (support 85)   currency P/R/F1 = 1.0000 (85)
#   date    P/R/F1 = 1.0000 (85)           vendor   P/R/F1 = 1.0000 (84)
#   location P/R/F1 = 1.0000 (82)          expense_type P/R/F1 = 1.0000 (85)
#   processing_path accuracy = 85/85 ; field_provenance accuracy = 510/510.
# Every floor passes with margin and bites on regression: with ~82-85 support
# per field, the deterministic-field floors (0.95) trip after ~4 wrong fields and
# the graduated floors (0.90/0.85/0.80) trip after a sustained regression — while
# a SINGLE perturbed gold value already moves the per-field number (proven in the
# self-verification probe), so the gate is non-trivial.  Floors held (not raised
# to 1.0) to absorb a one-off without a CI block while still catching real drift.
MA1_FIELD_RECALL_FLOORS: Final[dict[str, float]] = {
    "amount": 0.95,  # measured 1.0000
    "currency": 0.95,  # measured 1.0000
    "date": 0.95,  # measured 1.0000
    "expense_type": 0.90,  # measured 1.0000
    "vendor": 0.85,  # measured 1.0000
    "location": 0.80,  # measured 1.0000 (after dataset fix; see QA finding)
}

# Precision floors mirror recall for the deterministic fields (a wrong amount is
# both an FP and an FN).  Documented separately so the entrypoint can assert both
# precision and recall per field rather than F1 only.  Measured precision = 1.0000
# for every field (no hallucinated/wrong values on the deterministic local path).
MA1_FIELD_PRECISION_FLOORS: Final[dict[str, float]] = {
    "amount": 0.95,  # measured 1.0000
    "currency": 0.95,  # measured 1.0000
    "date": 0.95,  # measured 1.0000
    "expense_type": 0.90,  # measured 1.0000
    "vendor": 0.85,  # measured 1.0000
    "location": 0.80,  # measured 1.0000
}

# Path/provenance are EXACT-match structural gates (not graduated): the routing
# decision (deterministic vs agent_gap_fill) and per-field provenance label
# (deterministic vs ai_inferred) are fully determined by the OCR confidences and
# the gap-fill outcome, so they must be perfectly reproduced.  Measured 1.0000.
MA1_PROCESSING_PATH_ACCURACY_FLOOR: Final[float] = 1.0
MA1_PROVENANCE_ACCURACY_FLOOR: Final[float] = 1.0

# ---------------------------------------------------------------------------
# M-A2 — smart-match linkage
# ---------------------------------------------------------------------------
# Rationale: matching is pure deterministic logic (amount +/-0.01, currency,
# date +/-1 day, vendor token-containment).  A single F1 floor over the linkage
# triples; FP (wrong link) and FN (missed link) both pull it down.
#
# MEASURED (Tier-1 hermetic, real smart_match over the full m_a2 dataset =
# 65 linkage tuples, deterministic + hash-seed-independent, 2026-06-20): F1 =
# 1.0000 (P=1.0000, R=1.0000).  Per the brief's measured-minus-margin rule the
# floor is raised from the 0.90 PRD starting bar to 0.95: with 65 tuples a single
# FP or FN drops F1 to ~0.985 (still above 0.95, absorbing one-off curated-dataset
# growth without a CI block) while a real linkage regression (a handful of wrong /
# missed links) trips it.  0.95 bites; 0.90 left ~5 free regressions before
# failing, which is too slack for a fully deterministic matcher.
MA2_F1_FLOOR: Final[float] = 0.95  # measured 1.0000 over 65 linkage tuples

# ---------------------------------------------------------------------------
# M-A3 — policy-decision correctness (THE hard gate)
# ---------------------------------------------------------------------------
# VIOLATION-class recall is a HARD invariant: every gold "violation" MUST be
# predicted "violation".  "No silent in-policy false-pass" == zero real
# violations slip through as in_policy (or even as needs_review).  Any value
# below 1.0 BLOCKS the phase — this is not a tunable floor.
MA3_VIOLATION_RECALL_HARD_GATE: Final[float] = 1.0

# FLAG-class precision floor bounds false alarms.  A "flag" is a predicted label
# in {violation, needs_review}; it is correct iff the gold label is also in that
# set.  >= 0.80 means at most 1 in 5 flags is a false alarm.  Tighten to
# measured-minus-margin; never below, or the system could regress to flagging
# everything and still pass.
MA3_FLAG_PRECISION_FLOOR: Final[float] = 0.80

# ---------------------------------------------------------------------------
# RAG — grounded retrieval + org-scoping
# ---------------------------------------------------------------------------
# k for the @k retrieval metrics (top-k chunks; production default top_k = 5).
RAG_K: Final[int] = 5
# Recall@5: fraction of gold clause refs retrieved in the top-5 (mean over
# grounded cases).  Hit@1: the single best chunk is a gold clause ref.
# Precision@5 is REPORTED, not gated (with ~8 chunks total and 1-2 gold per
# query, precision@5 is structurally bounded and not a meaningful floor).
RAG_RECALL_AT_K_FLOOR: Final[float] = 0.80
RAG_HIT_AT_1_FLOOR: Final[float] = 0.70

# Org-scoping is a HARD invariant: a retrieval scoped to org A must return ZERO
# chunks belonging to org B.  ``policy_embeddings`` carries no organization_id,
# so a cross-tenant chunk is both a data leak AND a correctness failure.  Any
# value > 0 BLOCKS.
RAG_MAX_CROSS_TENANT_CHUNKS: Final[int] = 0

# ---------------------------------------------------------------------------
# RAG CORPUS (multi-document Helios) — R1 hybrid (dense + sparse + RRF) gates.
# ---------------------------------------------------------------------------
# The phase gate: hybrid RRF must BEAT the R0 dense-only baseline (recall@10 = 0.726667)
# AND hold the answerability signal (coverage + abstention) byte-for-byte identical.
RAG_CORPUS_R0_RECALL_AT_K: Final[float] = 0.726667  # dense-only baseline; hybrid must beat
# Durable regression floor (set just below the measured R1 recall@10 = 0.819; R2 must hold).
# A FIXED value beating dense-only — same discipline as the NY floor, NOT the exact measured run.
RAG_CORPUS_RECALL_AT_K_FLOOR: Final[float] = 0.80
# Trip-to-NY (ans-142): dense-only buries all 7 gold units (0 in top-10, 0 in top-25 — they
# rank r33..r176).  Fusion lifts the lexically-matchable ones (HOW-03, PTA-01/02, LOD-01).
# The regression asserts a FIXED before/after floor: >= this many gold units in the FUSED
# top-25 (dense-only = 0), AND fused top-10 strictly beats dense-only top-10.  The masterplan's
# "3 in the top-10" target was based on stale data; the corpus+local-embedder reality is
# 1 in top-10 / 3 in top-25 (see FSR).
RAG_CORPUS_NY_MIN_GOLD_IN_TOP25: Final[int] = 3
# Per-document-type unit-recall@10 durable floors (set just below the measured R1 values:
# faq 0.969, howto 0.962, policy 0.571, reference 0.667).  These catch a single doctype
# silently rotting while the overall mean stays green — notably faq, which REGRESSED -0.031
# (1.0 -> 0.969) under broad OR-sparse dilution (a candidate for R2 reranking).  R2 must hold.
RAG_CORPUS_BY_DOCTYPE_FLOOR: Final[dict[str, float]] = {
    "faq": 0.95,
    "howto": 0.95,
    "policy": 0.55,
    "reference": 0.65,
}

# ---------------------------------------------------------------------------
# R6-T4 — committed regression floors/ceilings that FAIL CI (eval-db) on a drop.
# ---------------------------------------------------------------------------
# Anchored just below/above the measured LOCAL-PATH run (LLM_PROVIDER=local, PYTHONHASHSEED=0):
#   recall@10 0.819031 | nDCG@5 0.658420 | precision@5-on-answered 0.244138 | AURC 0.084096.
# These lock the DETERMINISTIC local-path plumbing (blake2b + cosine gate + RRF-anchored local
# reranker), NOT the production calibrated numbers (tau*=0.039299, proven on the keyed run).  Fixed
# round values with headroom (same discipline as RAG_CORPUS_RECALL_AT_K_FLOOR), not exact-match.
RAG_CORPUS_NDCG_AT_5_FLOOR: Final[float] = 0.63
# precision-on-answered is structurally bounded here (~1-2 gold per query among the top-5), so this
# is a COARSE collapse-guard — it trips on a precision COLLAPSE, not as a quality target.
RAG_CORPUS_PRECISION_ON_ANSWERED_FLOOR: Final[float] = 0.20
# AURC (LOWER is better): ceiling above the measured 0.084096, below the R0 dense-only 0.101279.
RAG_CORPUS_AURC_CEILING: Final[float] = 0.09

# RAGAS faithfulness floor — enforced ONLY on the keyed eval-live-r5 run (an LLM judge is required;
# the deterministic CI build has none, so faithfulness is NEVER a CI-on-local gate).  Keyed baseline
# MEASURED with the statement-level decomposed judge (attribution-excluded).  Canonical (C7R,
# 2026-11-11, after the per-sentence support gate): faithfulness 0.981 at 74% grounded coverage
# (108/145 grounded; 411/415 claims supported; no confirmed model hallucinations).  The first
# measurement (R6) was 0.991 at 78% (113/145).  Floor 0.95 sits below the canonical baseline
# (~3pt headroom) — strict enough to catch a real regression, loose enough for keyed
# LLM/generation non-determinism.  Sources: docs/RAG/metrics/FINAL_METRICS_SUMMARY.md and
# docs/phase_logs/C7R_faithfulness_rerun.md; harness: evals/manual/test_rag_corpus_ragas_live.py.
RAG_FAITHFULNESS_FLOOR: Final[float] = 0.95
# Grounded-coverage floor, same keyed run.  Faithfulness is judged over grounded answers only, so
# without this a run that grounds 5 of 145 questions faithfully would pass.  0.70 means at least
# 102 of 145; the recorded keyed runs grounded 113 (R6) and 108 (C7R).
RAG_GROUNDED_COVERAGE_FLOOR: Final[float] = 0.70

# ---------------------------------------------------------------------------
# Routing / pipeline gate threshold mirrored from production (read-only).
# Field is kept deterministic if OCR confidence >= this; else it is gap-filled.
# Mirrored here for dataset authoring (NOT a re-definition of production).
# Source: ai_agents/guardrails/citations.py:27.
# ---------------------------------------------------------------------------
ROUTE_CONFIDENCE_THRESHOLD: Final[float] = 0.70
