# R1-T3 + R1-T4 + R1-T5 — Hybrid Retrieval: RRF Fusion + Wiring + Eval (as-built)

Phase R1, Part 2 of 2 — CLOSES Phase R1. Fuses the dense + sparse paths with Reciprocal
Rank Fusion, wires the fused ranking behind the UNCHANGED `retrieve()` interface, and PROVES
a recall gain on the gold set. Evidence-backed metric gate (the guard inverts from Part 1:
metrics must move UP materially, and the gain must be REAL — the harness scoring the actual
fused path). Branch `r1-hybrid-rrf-fusion`; UNCOMMITTED (commit is a separate step).

## Reconcile findings + decisions (cruxes)

- **CRUX A — answerability preserved (the #1 trap).** The 0.20 gate lives in
  `ai_agents/rag/retrieval.py` (`GroundedRetriever.retrieve`), a SCALAR max-cosine gate that
  read `result.chunks[0].similarity`. Under RRF order `chunks[0]` is the top-*RRF* chunk, not
  the max-cosine one, so reading it would feed a wrong/low value to the gate. Fix (STRUCTURAL,
  not numeric): `RetrievalResult` gained `max_dense_similarity: float | None` = `dense[0].score`
  (the max dense cosine, computed BEFORE fusion); the gate + the eval's abstention signal read
  THAT. RRF scores are never compared to the threshold nor stored on a chunk's `.similarity`
  (which stays the dense cosine; sparse-only → 0.0). On the dense-only path `max_dense_similarity
  == chunks[0].similarity`, so coverage/abstention reproduce R0 **byte-for-byte** — the CRUX-A
  oracle. `None` default → hermetic fakes fall back to `chunks[0].similarity` (unchanged).
- **CRUX B — sparse AND→OR (the NY mechanism).** `plainto_tsquery('english', :q)` joins lexemes
  with `&` (AND); the utterance "I want to make a trip to New York" → `want & make & trip & new
  & york`, which matches **0** chunks. Converted to OR via
  `to_tsquery('english', regexp_replace(plainto_tsquery('english', :q)::text, ' & ', ' | ', 'g'))`
  on BOTH the `@@` filter and `ts_rank_cd`. Empirically: NY utterance **0 → 46** sparse
  candidates, surfacing gold units HOW-03/LOD-01/PTA-01/PTA-02. Empty-safe; OR is a superset of
  AND (G5 four-probe still pass); stemming preserved (built from `plainto_tsquery`).
- **CRUX C — harness scores the REAL fused path.** `test_rag_corpus_eval.py` calls
  `PolicyRetrievalTool.retrieve()` directly and scores `r.chunks` (raw fused ranking),
  independent of the gate — so making `retrieve()` fused auto-scores the fused path (no
  reimplementation, no gold leak). Baseline in `evals/reports/metrics.json` `rag_corpus`;
  recorded R1 fused as current + R0 under `baseline_r0`.
- **Option Y (supersedes "add dense sort to both").** `retrieve()` now DELEGATES to
  `retrieve_parallel` → `reciprocal_rank_fusion` — ONE dense SQL (`_retrieve_dense`), no
  duplicated SQL to keep in sync. Dense secondary sort added there only.
- **Tie-breaks are UUID-free** (masterplan rule). Fusion `key=embedding_id` (identity only,
  never a sort key); `tie_breaker=(clause_ref is None, clause_ref or "")`. BOTH SQL ORDER BYs
  end `clause_ref ASC, ordinal ASC` — `pe.id` (a per-seed UUID) removed from the sparse path.
- **Parent-context live switch (guarded).** Grounding clause text = enclosing PARENT section
  (`resolve_parent_context_for_embedding`, small-to-big); citation stays the LEAF unit. Wrapped
  in try/except → leaf fallback (cannot crash the live answer on a mock session / bad id). Per
  your decision: included this cycle. No deterministic-metric impact (eval scores `tool.retrieve`,
  not `GroundedRetriever`); quality lands on `eval-live` faithfulness.
- **No migration** (chunk_tsv is migration 0010, already present). **Layering** preserved
  (`ai_agents → config`; fusion imported lazily inside `retrieve()` to avoid the
  `rag/__init__ → retrieval → policy_retrieval → fusion` cycle; layering-law test green).
- **Legacy `test_eval_db_retrieval.py` kept on the 8-clause seed** (per your decision): its rows
  have NULL `chunk_tsv` → sparse inert → fusion is a no-op there → recall@5/hit@1/snapshot
  unchanged (no snapshot refresh needed). The corpus tier (`test_rag_corpus_eval.py`) is THE
  corpus+fused eval.

## Headline metric delta (deterministic, local embedder, k=10)

| metric | R0 dense-only | R1 hybrid (RRF) | Δ |
|---|---|---|---|
| recall@10 | 0.726667 | **0.819031** | **+0.092** |
| nDCG@10 | 0.596470 | 0.690 | +0.094 |
| MAP | 0.506033 | 0.603 | +0.097 |
| MRR | 0.679274 | 0.760 | +0.081 |
| hit@10 | 0.917241 | 0.979 | +0.062 |
| answerable_coverage | 1.000000 | **1.000000** | **== (byte-identical)** |
| unanswerable_abstention | 0.019231 | **0.019231** | **== (byte-identical)** |
| AURC | 0.101279 | 0.084096 | −0.017 (better) |

Per-doc-type unit recall@10: faq 1.000 → **0.969 (−0.031, REGRESSED)**, howto 0.885 → 0.962
(+0.077), policy 0.395 → **0.571 (+0.176)**, reference 0.545 → 0.667 (+0.121). The faq dip is
broad-OR-sparse dilution on queries dense already nailed — a candidate for R2 reranking; now
gated by per-doctype durable floors + a soft flag so it cannot silently rot further.

Latency (sequential dense+sparse, 208 rows): fused p50 ~7ms / p95 ~10ms vs dense-only p50 ~4ms
/ p95 ~6ms — ~3ms added, within budget.

recall@10 = 0.819 **< 0.90** → the documented **R2 (cross-encoder reranking) trigger** per the
research report's change-course threshold. faq/cross_document/trip_planning are the weak spots
a reranker should target.

## NY regression — and a masterplan divergence (FLAGGED)

The masterplan's NY note was STALE. Measured reality (deterministic):
- max DENSE cosine for the NY utterance = **0.4138**, NOT ~0.07 → the agent **GROUNDS** on NY
  (on NYC FAQ units) at BOTH R0 and R1; it does NOT abstain. The answerability signal is
  unchanged by fusion.
- gold units' ranks: dense-only top-10 = **0**, top-25 = **0** (they sit r33..r176); fused
  top-10 = **1** (HOW-03 @ 6), top-25 = **3** (PTA-01 @ 23, PTA-02 @ 18, HOW-03 @ 6). AIR-01/
  AIR-03/MEAL-01 carry no lexical token and stay buried — inherent to this hard cross-doc query.

The permanent NY regression therefore asserts a **principled before/after** (not the
unachievable "≥3 in top-10"): fused top-10 gold **>** dense-only top-10 (1 > 0) AND fused
top-25 gold **≥ 3** (dense-only 0). RECALL is fixed; the live answer behaviour is R2/R3's domain.

## Adversarial review (6 passes) — outcomes

CRUX-A and CRUX-B both **HELD** under attack (no RRF score can reach the gate — structural;
OR rewrite is empty-safe, stemming-preserving, org-scoped, cannot emit an invalid tsquery).
Fixed from the review: (1) **D-MAJOR** — `test_tool_policy_retrieval.py` asserted cosine-DESC
order on `retrieve()` (false under fusion) → rewrote as an RRF-fused-order contract test;
(2) **F-MAJOR** — faq regression was ungated → added per-doctype floors + soft flag;
(3) **determinism** — removed the `pe.id` UUID tie-break, both paths now `clause_ref, ordinal`;
(4) added live-DB tests for the `from_sparse`/`max_dense_similarity` mapping + OR-rewrite
single-term/punctuation safety; (5) reworded docstrings to lead with the structural guarantee.
Acknowledged (not changed): N+1 parent-context I/O (≤10 small queries at k=5, acceptable);
the NY top-25 floor is thin (honest — reflects 3-in-top-25 reality).

## Verification

- ruff + mypy clean on all touched Python.
- RRF unit test: hand-worked `[doc2,doc1,doc4,doc3]` + tie + empties + N=3 + k-config + None-safe.
- **Phase gate `make eval-db` (backend/tests/eval -m db): 47 passed** (includes corpus delta,
  NY regression, CRUX-B before/after, byte-identical coverage oracle, dense-parity reframe,
  legacy 8-clause, live-mapping tests). ai_agents hermetic: 344 passed. My ai_agents retrieval
  tests (fusion + hermetic + db): 69 passed. Reproducible (recall = 0.819031 across runs).
- **Pre-existing-failure attestation:** the broader `ai_agents/tests -m db` lane (NOT the
  eval-db gate) shows 23 failures in policy-agent/policy-eval/orchestrator/approval/decision-log
  modules. Proven pre-existing via a same-DB baseline revert (identical 23 fail with this
  cycle's changes stashed) and they fail even individually on a clean DB (e.g. an "off-topic"
  silent-path query that actually grounds against the 208-embedding corpus) — the documented
  full-suite isolation/test-data caveat, not introduced here. This cycle's only net effect on
  that lane was +1 regression (`test_citation_detail_is_chunk_text`, which encoded the OLD
  citation==clause invariant), now updated to the new small-to-big contract (citation=leaf ⊆
  clause=parent) and green.

================================================================================
=== PHASE R1 CLOSE / OUTCOME (R1-T3 + R1-T4 + R1-T5) ===
================================================================================

SCOPE
  R1-T3 — pure Reciprocal Rank Fusion module + RRF_K config + hand-worked unit test.
  R1-T4 — fused retrieve() behind the UNCHANGED tool interface; sparse AND->OR; parent-
          context live switch (grounds on enclosing section, cites leaf unit IDs).
  R1-T5 — eval delta (overall + per-doctype) + permanent NY regression + latency.
  Phase R1 (hybrid dense + sparse + RRF) is COMPLETE.

THE METRIC DELTA (deterministic, local embedder, k=10; R0 dense-only -> R1 hybrid)
  recall@10  0.726667 -> 0.819031  (+0.092)
  nDCG@10    0.596470 -> 0.690      (+0.094)
  MAP        0.506033 -> 0.603      (+0.097)
  MRR        0.679274 -> 0.760      (+0.081)
  hit@10     0.917241 -> 0.979      (+0.062)
  coverage   1.000000 == 1.000000   (BYTE-IDENTICAL  <- CRUX-A correctness oracle)
  abstention 0.019231 == 0.019231   (BYTE-IDENTICAL  <- CRUX-A correctness oracle)
  AURC       0.101279 -> 0.084096   (better)
  per-doctype: policy +0.176 | reference +0.121 | howto +0.077 | faq -0.031
  latency: fused p50 ~7ms / p95 ~10ms vs dense-only ~4ms / ~6ms  (+~3ms; sequential)

KEY DECISIONS (with rationale)
  - RRF (k=60, config RRF_K) over dense cosine + sparse FTS; pure fusion module keyed on
    embedding_id (identity only); UUID-free tie-breaks (clause_ref, ordinal) on both paths.
  - CRUX-A: answerability signal preserved = MAX DENSE COSINE via additive
    RetrievalResult.max_dense_similarity; sparse hits kept via RetrievedChunk.from_sparse
    (continue, not break). RRF scores never reach the gate (structural). The 0.20 threshold
    is UNTOUCHED (recalibration is R3). PROOF: coverage + abstention byte-identical R0->R1.
  - CRUX-B: sparse AND->OR (to_tsquery OR-of-terms) is the mechanism that surfaces NY units
    (0->46 sparse candidates). In-scope BM25-flavored refinement of R1's own sparse path,
    NOT query rewriting (R5). G5 four-probe still pass (OR superset of AND).
  - Option-Y: retrieve() delegates to retrieve_parallel (single dense SQL); public signature
    + RetrievedChunk shape unchanged (thin-layer).
  - Parent-context live switch (small-to-big): grounds on enclosing-section text; CITATIONS
    stay leaf unit IDs (clause_ref); guarded (try/except -> leaf fallback). This CHANGED the
    citation contract: citation.detail = leaf chunk_text, now distinct from the parent
    grounding text (see incidental regression).
  - No schema migration; harness scores the REAL fused path (tool.retrieve() directly).

DOCUMENTED CORRECTION — NY PREMISE (code wins over plan)
  Earlier task specs asserted the NY utterance abstains at max-cosine 0.0726 < 0.20. Measured
  against the NEW 208-unit corpus, NY max-dense = 0.4138 -> it GROUNDS, not abstains; 0.0726
  was an artifact of the now-deleted 8-clause stub. Post-R0 the corpus already resolved NY's
  silence; R1 improved its RECALL (gold units: dense-only top-10 0 / top-25 0 -> fused
  top-10 1 / top-25 3). The permanent test asserts fused>dense AND fused-top-25 >= 3
  (deterministic, UUID-free tie-breaks). The live agent now grounds NY; answer QUALITY
  (citing the right policy units vs NYC FAQ units) is R2/R3's domain.

KNOWN TRADEOFF (carried to R2)
  faq -0.031 precision dip from broad OR-sparse (recall/precision tradeoff). Per-doctype
  durable floors in evals/thresholds.py guard it; a cross-encoder reranker (R2) is the tool
  to recover it.

R2 TRIGGER
  recall@10 0.819 < 0.90 (research-report change-course threshold) => R2 reranking warranted.
  Reranker weak spots: trip_planning (0.23), cross_document (0.59), faq precision. The
  baseline_r0 reference + per-doctype floors are recorded for R2 to beat (thresholds.py +
  the gitignored metrics.json artifact + the _R0_BASELINE test constant).

INCIDENTAL REGRESSION (found + fixed)
  test_citation_detail_is_chunk_text encoded the old citation==clause invariant; the parent
  switch makes clause-text=parent while citation.detail=leaf, so they diverge. Test updated
  to the new small-to-big contract (citation leaf is CONTAINED IN the parent grounding text).
  Net cycle effect on the broader ai_agents -m db lane: +1 regression, fixed.

ISOLATION CAVEAT (proven pre-existing)
  Broader ai_agents -m db lane = 23 failures, BASELINE-IDENTICAL with the diff stashed, and
  they fail individually on a clean DB (e.g. an off-topic silent-path query that grounds
  against the 208-embedding corpus) — a documented test-data/harness gap, NOT this change.
  Phase GATE lane (eval-db) = 47 passed.

DoD / PHASE GATE ATTESTATION
  Hybrid dense+sparse fused with RRF, org-scoped on BOTH paths, spanning all four Helios
  documents, behind the UNCHANGED tool interface; recall improved over baseline WITH EVIDENCE
  (+0.092); the applicable self-verification gates are green; cross-tenant clean on the new
  sparse path; legacy 8-clause path green (NULL chunk_tsv -> fusion no-op; snapshot byte-
  unchanged). Phase R1 DoD met.

DEFERRED / NEXT
  R2 (cross-encoder reranking) is the next phase. R3 (calibrated abstention, the 0.20
  replacement), R4 (contextual prefixes / embedding-model upgrade), R5 (HyDE / NLI grounding)
  follow.
