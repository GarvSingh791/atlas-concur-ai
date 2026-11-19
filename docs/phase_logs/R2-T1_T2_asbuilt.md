# R2-T1 + R2-T2 — Cross-Encoder Reranking: Provider + Two-Stage Pipeline (as-built)

Phase R2, **Part 1 of 2** — does NOT close the phase. R1's hybrid+RRF recall@10 = 0.819 (< 0.90)
triggered the research report's change-course rule → the precision stage. This part adds a
swappable rerank **provider** (R2-T1) and a two-stage **retrieve-then-rerank pipeline** (R2-T2)
that rescores the RRF top-N and reorders down to a high-precision top-k. Branch
`r2-cross-encoder-rerank` (off main/R1 tip `7e05171`); UNCOMMITTED. The phase-closing gold-set
delta, the per-doctype weak-spot breakdown, and the Cohere live eval are **Part 2** (a later
step). Naming note: this is a NEW per-task-range `_asbuilt.md` file, matching the R0/R1
convention (there is no single shared RAG log) — nothing appended/overwritten.

## What was built

- **`services/rerank_provider/`** (mirrors `services/llm_provider/`):
  - `interface.py` — `RerankProvider` ABC: `rerank(query, documents) -> list[(int, float)]`,
    desc by score, ties→idx asc, indices a unique subset of `range(len(documents))`. Takes/returns
    **plain str/int only** — never imports an `ai_agents` type (services stays `ai_agents`-free).
  - `local.py` — `LocalRerankProvider`: deterministic lexical score (coverage + Jaccard +
    exact-term bonus), **RRF-anchored** by a per-rank penalty `_RANK_PENALTY = 0.5`. No torch, no
    network, no model weights — pure stdlib, CI-hermetic.
  - `cohere.py` — `CohereRerankProvider`: httpx POST `/v2/rerank`, lazy import, `top_n=len(docs)`,
    timeout + ONE retry on transient, raises `ProviderError` on persistent failure (never a
    sentinel). Validates index bounds + uniqueness; rejects malformed responses.
  - `factory.py` — `get_rerank_provider()`: local default; lazy-imports the cohere adapter INSIDE
    its enum branch so the default/CI path never loads it.
- **`config/settings/base.py`** — `RERANK_PROVIDER` (local|cohere), `RERANK_API_KEY`,
  `RERANK_MODEL` (rerank-v3.5), `RERANK_BASE_URL`, `RERANK_TOP_K` (6), `RERANK_CANDIDATE_CAP` (50),
  `RERANK_TIMEOUT_SECONDS` (10). Default path adds **no new required env var** (key required only
  when cohere is selected, raised at call time). `.env.example` block documents all with safe
  defaults.
- **`ai_agents/rag/rerank.py`** — `apply_rerank(query, candidates, *, provider, top_k,
  candidate_cap, text_of)`: caps to `candidate_cap` (RRF top-50), ONE batched provider call,
  reorders the head, appends the un-reranked tail in RRF order, returns a `RerankOutcome(items,
  scores, applied)`. Generic over the candidate type (like `fusion.py`) — no `RetrievalCandidate`
  import, no cycle. Subset reranks drop no candidate (omitted indices appended with `None` score).
- **`ai_agents/tools/policy_retrieval.py`** — wired into `retrieve()` after RRF fusion via
  `_apply_rerank_stage` (double-guarded). Additive, defaulted fields only:
  `RetrievedChunk.rerank_score: float | None = None` and `RetrievalResult.rerank_applied: bool =
  True`. `retrieve()`'s public signature is byte-identical.

**Production-adapter decision — Cohere over httpx.** The `cohere` SDK is absent and `httpx` is
already a dependency (the OpenRouter LLM/OCR adapters use it). Cohere Rerank is a hosted
cross-encoder, so the adapter is a pure HTTP call — **zero new deps, no torch /
sentence-transformers** (fence F7: the production container stays minimal; a local torch reranker
would break the Render build). The OpenRouter adapter's timeout+retry+raise shape is mirrored.

## I2 — gate-preservation proof (the whole game)

Reranking is a permutation **π** applied to the fused candidate list *before* `RetrievalResult`
is built. The answerability gate (`ai_agents/rag/retrieval.py`) decides
`G = (max_dense_similarity if not None else chunks[0].similarity) < 0.20`. On the live path
`max_dense_similarity = parallel.dense[0].score` (`policy_retrieval.py`, the line that sets
`max_dense`) — computed from the DENSE path, BEFORE fusion, and **invariant under π**; it is
always a float (never `None`) on the real path, so the `chunks[0]` fallback never fires. Therefore
`G` depends only on a quantity π leaves fixed ⇒ coverage and abstention are **byte-identical** for
every query, regardless of rerank order. Per-chunk `similarity` stays the dense cosine
(`dense_cosine.get(...)`, sparse-only → 0.0); a rerank score is never compared to the threshold.
**Empirically confirmed** (eval-db, 47 passed): coverage **1.000000**, abstention **0.019231**,
recall@10 **0.819031** — all byte-identical to R1.

## Headline metric (deterministic local stand-in, k=10) — flat by design

| metric | R1 hybrid (RRF) | R2 Part 1 (RRF + local rerank) | Δ |
|---|---|---|---|
| recall@10 | 0.819031 | **0.819031** | **0.000000** |
| nDCG@10 | 0.690157 | 0.690157 | 0.000000 |
| MAP | 0.603025 | 0.603025 | 0.000000 |
| MRR | 0.760496 | 0.760496 | 0.000000 |
| coverage / abstention | 1.0 / 0.019231 | 1.0 / 0.019231 | 0 / 0 |

## Honesty paragraph — flat recall@10 is set-recall, not "reranking did nothing"

The local reranker is an **RRF-anchored DETERMINISM STAND-IN**, not a quality lever. recall@10
(and hit@10, per-doctype unit-recall) are **SET** metrics: they depend on *which* units land in
the top-k, not their order within it. Reranking changes ORDER within the top-k, so a flat Δ0.000000
recall is the EXPECTED signature — **nDCG@10 is the order-sensitive metric that would show the
gain**, and it is deferred to Part 2's Cohere cross-encoder. This is NOT "reranking already works
and changed nothing." Moreover, on THIS corpus the order-sensitive metrics (nDCG/MAP/MRR) are ALSO
byte-identical and the NY deep-rank probe is unchanged (HOW-03 still rank 6; PTA-01 23, PTA-02 18,
LOD-01 32) ⇒ the local stand-in produced **no top-k reorder on the gold set at all**, because a
lexically-aware RRF ranking already aligns with the lexical signal within the `_RANK_PENALTY = 0.5`
tolerance (a deeper item only climbs when its lexical score is >0.5 higher — which RRF rarely
leaves on the table). The pipeline is nonetheless **active and correct**: the V4 crafted fixture and
a live diagnostic both show it reorders (`applied=True`) when the lexical signal decisively
disagrees with input order. The real semantic reordering gain requires a true cross-encoder (Cohere)
and lands in Part 2. (Per-query reorder counts were NOT instrumented — no code added to compute
them; the byte-identical order-sensitive metrics are the qualitative proof.)

## Cut reconciliation — recall@k window preserved

The reranker is called with `top_k = max(intent.top_k, RERANK_TOP_K)`, so it never truncates
BELOW the caller's requested top_k — the recall@k measurement **window** is not narrowed. The
existing `fused[: intent.top_k]` slice in `retrieve()` remains the **sole final truncation**
(unchanged from R1). (Order WITHIN the window is reranked; window size is not.) The eval calls
`retrieve(top_k=25)` and measures recall@10, so the window is fully preserved.

## Fail-SAFE, not fail-abstain (I4)

`apply_rerank` catches `ProviderError` / timeout / malformed response / any unexpected error and
returns the input RRF order with `applied=False` + a logged warning; `_apply_rerank_stage` adds a
second outer guard around provider acquisition. The reranker NEVER raises out of `retrieve()` and
NEVER causes an abstention — it degrades to the proven R1 RRF baseline.

## Design deviations (with rationale)

- **`interface.py`, not `base.py`** — mirrors the as-built provider convention
  (`services/llm_provider/interface.py`); "mirror exactly" = match what is on disk.
- **Cohere over httpx (not the cohere SDK)** — zero new deps, no torch/SDK; F7 keeps the prod
  image minimal.
- **`RERANK_TIMEOUT_SECONDS`** (not the task spec's literal `RERANK_TIMEOUT_S`) — parity with
  `LLM_TIMEOUT_SECONDS` / `OCR_TIMEOUT_SECONDS`.
- **`RERANK_BASE_URL`** added net-new — matches the `LLM/EMBEDDINGS/OCR_BASE_URL` convention
  (proxy / live-test override).
- **RRF-anchored local reranker (`_RANK_PENALTY = 0.5`)** — a gentle stand-in that keeps the eval's
  set-based gates green by construction; the gentleness lives in `local.py`, NOT the pipeline, so
  the production adapter carries no such prior.

## Verification

- Static: `ruff check .` ✅, `ruff format --check .` ✅, `mypy backend config evals` ✅ (139 files).
  Hermetic `pytest` ✅ **548 passed, 740 skipped** (db/live tier self-skips, as in CI's backend job).
- DB: eval-db (`pytest backend/tests/eval -m db`) ✅ **47 passed**; gate byte-identical (above).
- Invariants: **I1** signature byte-identical · **I2** gate untouched (proof above) · **I3**
  deterministic-first (subprocess: default path imports neither cohere nor httpx) · **I4** fail-safe
  (double-guarded). Layering: `services/rerank_provider` imports 0 `ai_agents`; `ai_agents.rag.rerank`
  imports 0 `backend` (both subprocess-proven + regression tests).
- V1 contract · V2 factory/lazy-import · V3 layering+signature · V4 rerank-improves fixture +
  determinism · V5 gate byte-identical · V6 fail-safe · V7 cap+batch (1 call, ≤50 docs) — all ✅.
  **V8 live Cohere smoke — SKIPPED (no key)**: the Cohere adapter is contract-tested but
  **LIVE-UNVERIFIED**; it MUST be exercised in Part 2's `eval-live` before anything relies on it.

## 6-pass adversarial verification

**0 blockers.** Dimensions I1/I4, I2, I3/F6/F7, and all out-of-scope fences (F1/F2/F3/F5) returned
CLEAN. Five findings, all addressed in-branch: (1) corrected an over-reaching "recall@k preserved"
comment (the `max()` slice prevents narrowing; preservation is the stand-in's empirical gentleness);
(2) Cohere `_parse_results` now rejects duplicate indices (+test); (3) added a services↛ai_agents
layering regression test; (4) added hermetic `_apply_rerank_stage` wiring tests (the integration
point was previously DB-only); (5) `RERANK_TIMEOUT_S → RERANK_TIMEOUT_SECONDS` convention rename.

## CI hygiene applied this commit

`ruff format` reflowed 4 newly-authored files (`ai_agents/rag/rerank.py`,
`ai_agents/tests/test_rerank_pipeline.py`, `services/rerank_provider/local.py`,
`services/tests/test_rerank_provider.py`) — whitespace only, lint already passed; `ruff format
--check .` is now clean (320 files). No marker registration was needed (`db`/`live` already in
`pyproject.toml`).

## Deferred to Part 2 (phase close)

- Gold-set nDCG@10 / recall@10 delta print with the production reranker.
- Per-doctype weak-spot breakdown (trip_planning, cross-document, FAQ).
- `make eval-live` with the Cohere cross-encoder — **and the live-verification of the Cohere
  adapter** (V8 was skipped, no key).
- The change-course evaluation (did reranking close the 0.819 → 0.90 gap?).

---

# R2 Part 2 of 2 — HF Reranker Swap + Platt Calibration + Eval Close (as-built, 2026-06-29)

**Phase R2 CLOSES** (with an honest gate-8 caveat). Part 2 swaps the production reranker to
HuggingFace `BAAI/bge-reranker-v2-m3` (R2-T1b), adds deterministic Platt score calibration
(R2-T3), and re-runs the eval to prove the precision gain and document latency (R2-T4). Branch
`r2-cross-encoder-rerank`, second/final commit; UNCOMMITTED until the commit step stages it.

## 1. Scope

R2-T1b (Cohere → HF adapter, same `RerankProvider` interface, Cohere kept as a committed
swap-target skeleton) + R2-T3 (calibration, exposed not enforced) + R2-T4 (gold-set delta +
latency + near-miss checkpoint). The change-course question from Part 1 ("did reranking close
the 0.819→0.90 gap?") is answered: the cross-encoder lifts ordering hard (nDCG@5 +0.2146) and
recall@10 to 0.959 — the precision stage works.

## 2. Wire-format discovery (a production-debugging discipline win, not an error)

The masterplan assumed a TEI shape `{"inputs":{"query","texts"}} -> [{score,index}]`; the
binding note corrected it to `[[query,doc]] -> flat list[float]`. **Both were wrong for the
free serverless tier.** Three live probes BEFORE writing any adapter code established the truth:
the model is served as a sentence-pair **text-classification** pipeline. Probe 1 (`[[q,d]]`)
returned HTTP 400 with the endpoint's own hint ("send a dictionary {text, text_pair}"); the
"stop-and-fix-on-4xx" path was exercised exactly as designed (no silent fall-through). Probe 2
found the working body; probe 3 confirmed it scales (outer list always length 1, inner length
N, input order). Final format:

    POST {base}/{model}
    body: {"inputs":[{"text":q,"text_pair":doc},...],"parameters":{"function_to_apply":"sigmoid"}}
    -> 200: [[{"label":"LABEL_0","score":<prob in [0,1]>}, ...]]   # single-wrapped, input order

`_extract_scores` parses this primary shape and keeps two defensive fallbacks (flat `list[float]`
and `[{index,score}]`) in case the endpoint behaviour changes. Lesson logged: probe the live
contract first; never trust a documented wire format for a free/community endpoint.

## 3. Calibration (R2-T3)

`ai_agents/rag/calibration.py` — pure-stdlib Platt `P=1/(1+exp(a*s+b))` via fixed-step gradient
descent (Lin-Lin-Weng 2007 target padding, `a<=0` enforced; no numpy/sklearn/scipy — numpy is
not a declared dep). Fit on a FIXED, SEEDED (42) **28-query FIT / 12-query HELD-OUT** disjoint
slice of the 197 gold queries; the retrieval eval still runs on all 197 (calibration is
non-reordering, so the slice/eval overlap cannot move recall/nDCG). Both param sets committed to
`ai_agents/rag/rerank_calibration.json` (shipped in the backend image; `evals/` is not):
`local {a:-0.284215, b:1.375336}`, `huggingface {a:-2.469343, b:3.580282}`. Held-out
Spearman(calibrated, relevance): local 0.2125, HF 0.265 (>0); cross-doctype mean calibrated is
higher for relevant than irrelevant in every doctype. **Runtime:** `retrieve()` sets
`RetrievedChunk.calibrated_score` per chunk (Platt of `rerank_score` for the active provider) —
EXPOSED for observability + R3's future threshold; it is **annotation only**, never reorders
(I5), and is never read by the answerability gate (I2). FORWARD FLAG: R3's abstention
calibration needs a CLEAN held-out split not reused from this 28-query fit slice.

## 4. Results (R2-T4)

- **nDCG@5: 0.6584 -> 0.8731 (+0.2146)** — the phase-closing precision evidence. This is a
  **LOWER BOUND**: 17/197 queries hit cold-start timeouts and fell back to the RRF order
  (no improvement for those), so the warm-model gain is larger.
- **Precision@5: 0.2441 -> 0.3241 (+0.0800)**; recall@5 0.7382 -> 0.9204; recall@10 0.8190 ->
  0.9589 (+0.1399).
- Weak spots improve most (per-query-type nDCG@5 / per-doctype recall@5):
  **cross_document +0.3565, trip_planning +0.2426, threshold +0.3411; policy-recall +0.3613,
  reference +0.2727** — exactly the lexically-distant cases a cross-encoder targets.
- **Gate: answerable_coverage 1.000000 / unanswerable_abstention 0.019231 — BYTE-IDENTICAL to
  R1** (the reranker reorders the context list only; the answerability signal is the dense
  cosine via `max_dense_similarity`, untouched). nDCG@10 0.690 / recall@10 0.819 unchanged on
  the deterministic local-identity run; `calibrated_score` populated on 4925/4925 chunks once
  the local artifact exists.
- **Near-miss checkpoint** (`evals/datasets/checkpoint_queries.jsonl`, OUTSIDE the 197
  denominator): near-002 "How do I submit an expense report?" PASS — HOW-01 (procedure) RRF
  rank 2 -> HF rank 1; EXP-01 (policy submission-deadline clause) RRF rank 11 -> HF rank 3
  (relevant outranks distractor). near-001 "alcohol at a team dinner": the per-diem distractor
  MEAL-01 was not retrieved into the candidate pool at all (not a real competitor); the relevant
  FAQ-Q25 held rank 1. FORWARD FLAG: R3's near-miss regression suite needs a near-001 distractor
  that actually competes.
- **Section-10 gates: 1-5 PASS, 6 N/A (R3), 7 PASS (trip-to-NY), 8 NOT MET (latency), 9 N/A,
  10 PASS (secret scan), 11 N/A (no schema change).**

## 5. Gate-8 caveat (NOT softened — gate-8 stays RED)

HF free-tier rerank latency: **p50 ~5.9s, p95 ~90s** over a 50-doc batch, versus the Section-6
target of +80-200 ms p50. The p95 is inflated by ~17 cold-start fallbacks (each a 45s + 45s
retry before fail-safe); steady-state warm latency is ~5s for 50 docs (empirically measured at
~2s for 20 docs, ~3s for 30). This is **non-blocking**: any rerank failure (timeout/429/503)
degrades to the proven R1 RRF order (`rerank_applied=False`), so no query breaks and the
answerability gate is untouched. **Resolution path:** the `RerankProvider` interface makes a
swap to Cohere (~595 ms managed) or a dedicated TEI endpoint a one-file change — deferred to an
R3 / infrastructure decision. Gate-8 remains red and is recorded as such.

## 6. Invariants + adversarial verification

I1 (retrieve() signature byte-identical — `od -c` empty diff), I2 (gate untouched — dense-cosine
only; hermetic smoke confirms a high rerank/calibrated score cannot move the gate), I3
(deterministic-first — live HF eval is `@pytest.mark.live`, never in push CI), I4 (fail-safe at
three layers + 14 HF + 10 pipeline tests), I5 (non-reordering — order is the reranker's,
calibrated_score annotation-only, apply_platt monotone) — **all HELD**. A 6-pass adversarial
verification (each pass trying to falsify one invariant/gate by reading code + running
hermetic checks) returned **6/6 HOLD, 0 violations**.

## 7. Deferred forward

- **Gate-8 latency** resolution (Cohere / dedicated TEI endpoint via the one-file swap).
- **R3 abstention calibration** needs a clean held-out split (not the 28-query fit slice).
- **near-001** checkpoint distractor replacement (one that competes in the candidate pool).
- **R3 tau-selection** on the calibrated reranker score (the threshold this phase only exposed).

## 8. Settings change note

`RERANK_MODEL` and `RERANK_BASE_URL` defaults now point to the HF values
(`BAAI/bge-reranker-v2-m3`, `https://router.huggingface.co/hf-inference/models`). The local/CI
path ignores them (the local reranker is pure lexical), so the deterministic gate is unaffected.
Selecting Cohere now requires explicit `RERANK_MODEL=rerank-v3.5` and `RERANK_BASE_URL=`
(empty -> Cohere default) — documented in `.env.example` and the `cohere.py` skeleton docstring.
`HF_API_TOKEN` is optional (required only when `RERANK_PROVIDER=huggingface`); no new required
env var on the CI path. Live/calibrate-hf use `RERANK_TIMEOUT_SECONDS=45` to absorb cold-start;
the production default stays 10 (fail-fast to RRF).
