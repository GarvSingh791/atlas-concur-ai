# ADR-001 — Hybrid retrieval (dense + sparse) over dense-only

**Status:** Accepted · implemented at R1 · shipped (runs on every deployment, including public staging)
**Decision owner:** Garv Singh Saini (sole author)
**Related:** [ADR-002 (RRF fusion)](ADR-002-reciprocal-rank-fusion.md) · [ADR-003 (reranking)](ADR-003-cross-encoder-reranking.md) · narrative in [ENGINEERING_DECISIONS](../ENGINEERING_DECISIONS.md)

## Context

The original policy copilot retrieved with a single dense (vector) similarity
search. On the rebuilt 208-unit corpus this left recall on the table: for
composite, lexically-specific queries the gold policy units ranked far down the
list (the "trip to New York" query's gold units ranked r33–r184, all outside the
top-10; `R0-T3_T6_asbuilt.md:93`). Dense embeddings match paraphrase well but blur
exact tokens — clause ids, dollar figures, city-tier names — that enterprise policy
questions hinge on. Baseline dense-only recall@10 was **0.726667**
([metrics](../metrics/FINAL_METRICS_SUMMARY.md#2-retrieval-quality--the-three-honest-tiers)).

## Options considered

1. **Dense-only, bigger/better embedder.** Simplest, but does not fix the lexical
   blind spot; an exact clause id is still not reliably matched, and it defers the
   fix to a model swap.
2. **Sparse-only (BM25-style).** Nails exact tokens, but loses all the paraphrase
   recall the copilot depends on for conversational questions.
3. **Hybrid: run dense + sparse and fuse.** Catches both meaning-matches and
   exact-token matches. More moving parts and a fusion step to design.

## Decision

Adopt **hybrid retrieval**: for each query, run a dense pgvector cosine search and a
sparse PostgreSQL full-text search (`tsvector` / `ts_rank_cd`, with the
`plainto_tsquery` AND rewritten to OR so intent-mismatched utterances still match on
any term), each org-scoped and capped at 50 candidates, then fuse
([ADR-002](ADR-002-reciprocal-rank-fusion.md)). Sparse retrieval reuses PostgreSQL —
no new datastore.

## Consequences

**Positive**
- recall@10 **0.726667 → 0.819031** (+0.092) on the deterministic local path, with
  coverage and abstention held byte-identical (`R1-T3_T5_asbuilt.md:56,61–62`).
- The lexical path is a GIN index in the same database — no second system to operate,
  and tenant scoping is the same JOIN.

**Negative / tradeoffs**
- Two retrieval paths to maintain and a fusion step to reason about.
- Broad OR-sparse matching dilutes precision (a known cost, addressed by
  [ADR-003 reranking](ADR-003-cross-encoder-reranking.md)); e.g. `faq` unit-recall
  dipped slightly under OR dilution (`evals/thresholds.py:145`).
- The paths run **sequentially** (logically parallel, not concurrent) — acceptable
  because retrieval is DB-cheap (fused p50 ~4–7 ms).

## Research reference

Companion report [`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md) §1.2
(hybrid search) and Anthropic's *Contextual Retrieval* (combining contextual
embeddings + BM25 cut top-20 retrieval failure 5.7%→2.9% in their benchmark;
directional, validated on our own gold set).
