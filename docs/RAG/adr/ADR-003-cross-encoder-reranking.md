# ADR-003 — Cross-encoder reranking as a second stage

**Status:** Accepted · implemented at R2 · keyed configuration uses Novita; CI and public staging use the deterministic local reranker
**Decision owner:** Garv Singh Saini (sole author)
**Related:** [ADR-001](ADR-001-hybrid-retrieval.md) · [ADR-002](ADR-002-reciprocal-rank-fusion.md) · [ADR-004 (calibration reads the reranker score)](ADR-004-confidence-calibration.md)

## Context

RRF fusion ([ADR-002](ADR-002-reciprocal-rank-fusion.md)) gives good recall but
mediocre precision — it fuses by rank and never reads passage content, and the
broadened OR-sparse match adds low-relevance candidates. The top of the list needs a
model that actually reads query and passage together. Cross-encoders do exactly this
but are too slow to run corpus-wide, so they belong in a second stage over a small
candidate set.

## Options considered

1. **No rerank** (ship RRF order). Cheapest; leaves precision/nDCG on the table.
2. **Cohere Rerank 3.5** (managed API). Strong quality, low latency (~595 ms quoted as
   a planning reference in `R2-T1_T2_asbuilt.md:241`; never measured by Atlas — the
   adapter's live smoke was skipped for lack of a key); a paid dependency per query.
3. **Local cross-encoder** (`bge-reranker-v2-m3` self-hosted with torch weights).
   No per-call cost, but ships model weights + torch into the image, breaking the
   minimal-image constraint.
4. **`bge-reranker-v2-m3` via a hosted inference API** (HuggingFace serverless, then
   Novita). Same open cross-encoder, no weights in the image, one batched HTTP call.

## Decision

Adopt a **two-stage retrieve-then-rerank** design: RRF proposes the top ≤50, a
cross-encoder rescores them in **one batched call** and returns a high-precision
top-k (`RERANK_CANDIDATE_CAP = 50`, `RERANK_TOP_K = 6`). The reranker is reached
through a swappable `RerankProvider` interface with four implementations:
- **`local`** — a deterministic, RRF-anchored lexical stub (default; CI runs this so
  the pipeline is byte-reproducible with no network).
- **`novita`** — **the keyed configuration** (what `make eval-live-r5` and a keyed
  deployment use): `BAAI/bge-reranker-v2-m3` over a stable hosted API
  (`services/rerank_provider/novita.py`).
- **`huggingface`** — same model on the free serverless API; it produced the keyed
  retrieval numbers and the calibration fit, then was **dropped as the live provider**
  after it proved unreliable under sustained load (HTTP 502 + persistent 60 s timeouts,
  p50 ~5.9 s on the free tier). The adapter is kept.
- **`cohere`** — kept intact as the documented one-file swap if sub-200 ms p50 is
  ever required.

Reranking is **fail-safe** (invariant I4): any provider error, timeout, or malformed
response returns the proven RRF order with `applied=False` — it never raises and
never abstains. With a calibrated provider the abstention gate reads the top chunk's
calibrated reranker score; if the reranker fails, the gate falls back to the dense-cosine
gate ([ADR-004](ADR-004-confidence-calibration.md)).

## Consequences

**Positive**
- Large precision/quality lift on the keyed path: nDCG@5 **0.658 → 0.873** (+0.215),
  recall@10 **0.819 → 0.959** (`R2-T1_T2_asbuilt.md:211,214`;
  [metrics](../metrics/FINAL_METRICS_SUMMARY.md#2-retrieval-quality--the-three-honest-tiers)).
- Provider abstraction: provider swaps (Cohere → HuggingFace → Novita) were config
  changes, not pipeline rewrites; the same provider key aligns rerank scores, Platt
  params, and τ\*.
- The image stays minimal — no torch, no model weights (httpx only).

**Negative / tradeoffs**
- The reranker network hop is the dominant *retrieval* cost and is rate-limited
  (Novita ~30 req/min, absorbed by exponential backoff). A dedicated low-latency
  endpoint (sub-200 ms) is documented but not yet built —
  [PERFORMANCE_PROFILE](../PERFORMANCE_PROFILE.md).
- On the deterministic local path the RRF-anchored stub preserves RRF order, so the
  cross-encoder's quality lift is a **keyed-path** result, not a CI number (stated
  plainly in [metrics](../metrics/FINAL_METRICS_SUMMARY.md)).

## Research reference

Companion report [`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md) §1.3 —
Rosa et al. (arXiv:2212.06121) and Pandit et al. (2025) on cross-encoders beating
bi-encoders by several nDCG points; latency envelope for `bge-reranker-v2-m3`
(80–200 ms / 100 docs) and Cohere Rerank (80–150 ms p50).
