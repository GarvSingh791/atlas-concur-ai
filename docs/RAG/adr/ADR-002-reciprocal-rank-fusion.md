# ADR-002 — Reciprocal Rank Fusion over weighted-score fusion

**Status:** Accepted · implemented at R1 · shipped (runs on every deployment, including public staging)
**Decision owner:** Garv Singh Saini (sole author)
**Related:** [ADR-001 (hybrid retrieval)](ADR-001-hybrid-retrieval.md)

## Context

Hybrid retrieval ([ADR-001](ADR-001-hybrid-retrieval.md)) produces two ranked
candidate lists — dense cosine similarities and sparse `ts_rank_cd` scores. These
scores live on **incomparable scales**: a cosine similarity of 0.4 and a
`ts_rank_cd` of 0.08 cannot be added or averaged meaningfully. We need one combined
ranking that is robust and reproducible.

## Options considered

1. **Weighted score fusion** (`α·cosine + (1−α)·bm25`). Requires normalizing two
   different score distributions and tuning `α` per corpus — brittle, and the weight
   silently drifts as the corpus changes.
2. **Learned fusion** (a small model over both signals). Highest ceiling, but needs
   training data and adds a model to maintain — unjustified at this corpus size.
3. **Reciprocal Rank Fusion (RRF).** Uses only each item's *rank* in each list, so
   raw-score scales never enter. One constant `k`, no per-corpus tuning, order-only.

## Decision

Adopt **RRF**: fuse by `RRF(d) = Σ_lists 1 / (k + rank_d)` with `k = 60`
(`RRF_K`, `config/settings/base.py:163`), rank 1-based, an item absent from a list
contributing nothing. Fuse on a stable chunk key (`embedding_id`) so a dense hit and
a sparse hit for the same chunk sum their terms; break ties by a reproducible key
(`clause_ref`), never a per-seed UUID, so results are byte-identical run-to-run.
Implemented as a pure, dependency-free module (`ai_agents/rag/fusion.py`).

## Consequences

**Positive**
- No score normalization, no `α` to tune, no per-corpus retuning — the single
  documented default `k = 60`.
- Fully deterministic and reproducible (rank-only + reproducible tie-break), which is
  what lets CI gate the fused output byte-for-byte.
- Robust to one path being noisy: a candidate strong in *either* list still surfaces.

**Negative / tradeoffs**
- Discards score *magnitude* — a dense hit that is overwhelmingly close and one that
  barely clears threshold contribute the same at equal rank. This precision loss is
  intentionally recovered downstream by the cross-encoder
  ([ADR-003](ADR-003-cross-encoder-reranking.md)), which *does* read content.
- `k = 60` is a convention, not a fitted optimum for this corpus (accepted; it is the
  field-standard default and the corpus is too small to justify tuning it).

## Research reference

Companion report [`docs/RAG_RESEARCH_REPORT.md`](../../RAG_RESEARCH_REPORT.md) §1.2,
citing Cormack, Clarke & Büttcher, *"Reciprocal Rank Fusion outperforms Condorcet and
individual rank learning methods"* (SIGIR 2009) — the default fusion in Elasticsearch,
OpenSearch, and Azure AI Search.
