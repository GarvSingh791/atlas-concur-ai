# Performance Profile — the latency lens

*The latency view of the RAG engine, for engineers asking "where does the time go?".
This document covers latency only. Quality/accuracy numbers live in
[FINAL_METRICS_SUMMARY](metrics/FINAL_METRICS_SUMMARY.md); the system design lives in
[RAG_ARCHITECTURE](RAG_ARCHITECTURE.md). Every figure here is cited from a repository
artifact, and — importantly — this document is explicit about **what was measured and
what was deferred**.*

---

## The honest frame

Latency has three stages: **retrieval → rerank → generation**. Only retrieval has a
committed, reproducible measurement. The reranker network hop and the generation call
were built into a live per-stage profiler (`evals/manual/test_rag_latency_profile_live.py`)
that emits **min/median/max**, but **its keyed run was deferred** — "Live p50/p95
DEFERRED — profiler self-skips" (`R6-T1_T2_asbuilt.md:89`). So:

- **Retrieval** — measured (fast).
- **Rerank** — measured as a committed figure only on the HuggingFace free tier; the
  Novita reranker used by the keyed runs has a single "sub-second" live smoke in the
  committed record, not a per-stage median.
- **Generation** — not separately profiled.

We report exactly that, and we do **not** invent per-stage medians to fill the gap. (A
set of round figures — "retrieval ~12 ms / rerank ~911 ms / generation ~4.6 s" — has
circulated in planning; none of them appears in any repository artifact, so none is
stated here as fact. See [the gap](#the-measurement-gap-stated-plainly).)

A note on units and labels: the **committed retrieval numbers are labeled `p50/p95`**
(the eval harness), while the **live profiler emits `min/median/max`**. Where a figure
is a median we call it a median; where the artifact says `p50` we keep `p50` (over
n=197 the two coincide in practice). We never dress a `p50` up as a percentile it isn't.

---

## Retrieval — measured, and genuinely fast

Retrieval is pure database work (no external hop): dense pgvector cosine ∥ sparse
full-text, fused by RRF.

| Measurement | Value | Source |
|---|---|---|
| Dense read, local path (n=197) | p50 **2.049 ms** · p95 2.982 ms | `metrics.json rag_corpus.latency_ms` (one local run; regenerated each run, machine-dependent) |
| Fused (dense + sparse + RRF), local path (n=197) | p50 **3.9 ms** · p95 5.948 ms | same run |
| Fused, R1 as-built at 208 rows | p50 ~7 ms · p95 ~10 ms | `R1-T3_T5_asbuilt.md:70–71` |
| Fused, R4 contextual-ON run | p50 4.8 ms · p95 8.3 ms | `R4-T1_T2_asbuilt.md:124` |

Fused retrieval sits at roughly **p50 4–7 ms** across runs — RRF itself is negligible
(the research predicts, and the profiler confirms, that fusion cost is dominated by the
two reads it combines). Retrieval is not the bottleneck.

### The live EXPLAIN — why the HNSW index is inert (and honest about it)

A live `EXPLAIN (ANALYZE, BUFFERS)` of the org-scoped dense read at the real 208-row
corpus shows the planner **exact-scans + top-N heapsorts** the org-filtered rows —
**not** an HNSW index scan. The plan is *identical* whether `hnsw.ef_search` is unset
(**0.844 ms**) or set to 100 (**0.686 ms**) (`R6-T1_T2_asbuilt.md:11`). At 208 rows,
PostgreSQL reads the whole (org-filtered) set faster than it would descend the HNSW
graph, so:

- `hnsw.ef_search` is **inert** here — a forward-looking knob, not a live lever.
- recall@10 = 0.819 is **exact-search** recall, not an approximate-nearest-neighbor
  result.

This matters for honesty: it would be easy to claim an "ANN speedup." There isn't one
yet. The HNSW index (`m=16`, `ef_construction=200`) and `ef_search` bind only once the
corpus grows large enough that the planner prefers the graph — a real property at
production scale, correctly labeled forward-looking today.

---

## Rerank — the dominant *retrieval* cost, and a known latency debt

The cross-encoder is one batched network call over ≤50 candidates. It is the dominant
cost *within retrieval* and the most latency-sensitive external dependency. The measured
points, across the provider journey ([ADR-003](adr/ADR-003-cross-encoder-reranking.md)):

| Provider | Rerank latency | Status | Source |
|---|---|---|---|
| HuggingFace free tier (`bge-reranker-v2-m3`) | p50 ~5.9 s · p95 ~90 s (cold-start inflated) — measured | **dropped** (unreliable under load); adapter kept | `R2-T1_T2_asbuilt.md:235–241`; `rag_corpus_hf_rerank_live.json` |
| Novita (`bge-reranker-v2-m3`) — **keyed configuration** | "sub-second" (single live smoke) — measured | used by the keyed runs; per-stage median **not captured** in the committed record | `R4-live-matrix_asbuilt.md:56` |
| Cohere Rerank (managed) | ~595 ms — **a planning reference figure, not measured by Atlas** (the adapter never ran live: no key) | adapter kept as a documented swap | `R2-T1_T2_asbuilt.md:129,241` |

Public staging and CI run the deterministic `local` reranker (no network hop).

Two honest caveats here:

- **The ~5.9 s figure is the *free-tier* HuggingFace endpoint, not the built system.**
  It is precisely *why* that provider was dropped: it "proved unreliable under the live
  matrix's sustained load (HTTP 502 + persistent 60 s timeouts)." The current system
  uses **Novita** serving the same cross-encoder behind a stable API; do not read 5.9 s
  as ARE's production rerank latency.
- **Novita's per-stage latency was never profiled** (the live profiler run was
  deferred), and the committed record holds only a "sub-second" single-call smoke. A
  later batch run of `make eval-rerank-live-novita` (2026-11-11; report gitignored, not
  committed) recorded p50 ≈ 1.35 s / p95 ≈ 1.64 s per batch of up to 50 candidates over
  197 queries — so read "sub-second" as one call, not a steady-state median
  ([metrics, not-committed note](metrics/FINAL_METRICS_SUMMARY.md#untraceable--not-yet-measured)).
  The service is rate-limited (~30 req/min), absorbed by exponential backoff.

### Why the reranker earns its cost

Even at a network hop, the second stage is worth it: on the keyed path it lifts nDCG@5
**0.658 → 0.873** and recall@10 **0.819 → 0.959**
([metrics](metrics/FINAL_METRICS_SUMMARY.md#2-retrieval-quality--the-three-honest-tiers)).
And it is **fail-safe** — if the reranker is slow or errors, the pipeline returns the
proven RRF order (`applied=false`) rather than blocking, so a reranker outage degrades
*quality* gracefully, never *availability*.

### The documented sub-second path (forward-looking, not done)

Getting rerank to a reliable sub-200 ms p50 is a **one-file `RerankProvider` swap** to a
colocated / hosted low-latency endpoint (the Cohere adapter is kept intact as one such
option) — documented as the resolution, **not yet built**
(`R6-T1_T2_asbuilt.md:105`).

---

## Generation — dominant end-to-end, and hidden by streaming

The generation call (Gemini synthesis + citation and NLI gates) is expected to dominate
*end-to-end* wall-clock — the research's §5.3 prediction — but it was **not separately
profiled** on the keyed path. Two design facts shape how it's experienced:

- **The endpoint streams.** The user-facing metric is **time-to-first-token**, and the
  answer streams token by token, so perceived latency is *far* below any whole-answer
  number. An end-to-end generation median (had it been captured) would be an **upper
  bound** on the experience, not the experience.
- **Abstention skips the LLM.** A silent/abstain decision never calls the generator, so
  those queries pay near-zero generation cost.

There is also a wired-but-inert **prompt cache**: the static instruction block is ~150
tokens, below Gemini's ~1024-token cache floor, so `cache_control` does nothing yet — it
starts saving cost only once the instruction block grows past that floor
(`R6-T1_T2_asbuilt.md:13,107`). Wired, not "saving cost today."

---

## Corpus-scale caveat

Every number here is **corpus-appropriate for 208 units** (219 today), not a production-scale claim.
At this size retrieval exact-scans (the HNSW index is forward-looking), the reranker
dominates retrieval, and generation dominates end-to-end. At production corpus size the
index binds, `ef_search` becomes a live recall/latency lever, and `halfvec` /
production HNSW tuning become relevant ([FUTURE_WORK](FUTURE_WORK.md)). We do not
extrapolate the 208-row timings to production.

---

## The measurement gap, stated plainly

For completeness and honesty, the latency figures that are **not** in any repository
artifact and are therefore **excluded** from this package:

- a **Novita per-stage rerank median**,
- an **end-to-end generation median / time-to-first-token measurement**.

Both await a keyed run of the (already-built, currently self-skipping) live profiler.
Planning figures of "retrieval ~12 ms / rerank ~911 ms / generation ~4.6 s" are **not**
traceable to the tree and are not stated as fact anywhere in this documentation. The
profiler that would produce them is committed and ready; the keyed run is the
outstanding work.

---

## Summary

| Stage | What we can say (traceable) | What we can't (deferred) |
|---|---|---|
| Retrieval | fused p50 ~4–7 ms; exact-scan ~0.7 ms; HNSW inert/forward-looking | — |
| Rerank | HF free tier ~5.9 s (why it was dropped); Novita "sub-second" single-call smoke | Novita per-stage median (an uncommitted batch run suggests ~1.35 s p50 per 50-candidate batch) |
| Generation | dominates end-to-end (research §5.3); streams (TTFT ≪ full); abstain skips LLM | end-to-end / TTFT measurement |
