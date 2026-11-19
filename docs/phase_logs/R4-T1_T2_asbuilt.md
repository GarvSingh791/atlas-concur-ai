# R4-T1 + R4-T2 — Contextual Retrieval, Part 1 of 2 (as-built)

**Date:** 2026-06-29
**Author:** Garv Singh Saini
**Branch:** r4-contextual-retrieval (off main @ R3-closed; uncommitted at authoring)

Builds the contextual-retrieval MECHANISM — a document-aware situating prefix prepended
to each corpus chunk before embedding + lexical indexing (Anthropic "contextual
retrieval", adapted deterministic-first for hermetic CI) — stores it additively,
re-embeds + re-indexes with prefixes, and MEASURES the delta vs R3 on the LOCAL embedder.
It does NOT decide adoption: the keep/gate/revert call is deferred to R4-T3/T4. Ships
default-OFF, so the committed tree reproduces R3 byte-for-byte.

## Scope
R4-T1 (prefix generator: deterministic template + production LLM path + dispatcher;
additive storage) + R4-T2 (re-embed + re-index with prefixes, idempotent, 1536-d
unchanged). OUT: R4-T3 (embedder-model upgrade), R4-T4 (formal adopt assertion), τ*
recalibration, calibration.py / the 0.20 gate constant, fusion.py / RRF k / reranker /
confidence source, query-side prefixing, frontend / backend DTOs.

## Decisions (FORK-A…D)
- **FORK-A (placement).** Canonical impl lives in the SEED layer at
  `database/seeds/corpus/contextual.py` (`deterministic_context_prefix` pure-stdlib;
  `llm_context_prefix` fail-safe; `context_prefix` dispatcher). `ai_agents/rag/contextual.py`
  is a thin DOWNWARD re-export (masterplan-literal path + future ai_agents consumers).
  Deviation from the task spec's literal "LLM path in ai_agents": `_embed_corpus` is in the
  seed layer and `seed → ai_agents` is an illegal UPWARD import (P4-T5). `database → services`
  is legal and already used (`_local_embedder.py:20`), so the LLM path sits cleanly in the
  seed layer; the ai_agents shim mirrors the `_local_embedder.py` promoted-canonical+shim
  precedent. Layering proof: `ai_agents.rag.contextual` imports only `database.*`/`services.*`
  (no `backend.*`); the new module is added to `test_no_backend_import.py::_IMPORT_SCRIPT`
  and the subprocess test passes (20/20).
- **FORK-B (storage, τ*-invariance).** Migration 0011 adds nullable `context_prefix TEXT`
  (model-first, mirroring 0009/0010). Embedding input = `f"{prefix}\n\n{chunk_text}"` when a
  prefix exists; `chunk_text` column stays PRISTINE; `chunk_tsv` concat gains
  `coalesce(pe.context_prefix,'')`. vector(1536) + HNSW + GIN untouched (Fence 1). The
  prefix reaches ONLY the dense vector and the tsvector — `policy_retrieval.py` is untouched,
  both retrieval SQLs still SELECT `pe.chunk_text`, and the reranker is still fed
  `text_of=lambda c: c.chunk_text`. Reranker input is byte-identical pre/post → the
  calibrated reranker-score scale and τ*=0.039299 need no recalibration; citations read
  pristine `chunk_text`. (Honest caveat, gate untouched: on the local cosine-FALLBACK gate,
  `max_dense_similarity` does shift — measured + reported below; calibration.py and the 0.20
  constant are fenced OUT, not retuned.)
- **FORK-C (toggle).** `RAG_CONTEXTUAL_PREFIXES: bool = False`. OFF → prefix `""` → bare
  `chunk_text` embedded, `context_prefix` NULL, `coalesce` → identical tsv. Committed tree =
  R3 byte-identical (proven below). Measurement run forced it ON via env. Selection when ON:
  `LLM_PROVIDER=local` → deterministic template; production → LLM (persist+reuse). Default not
  pre-committed.
- **FORK-D (LLM idempotency).** `_embed_corpus` captures `{clause_ref: context_prefix}`
  before the per-policy delete and passes it as `cached=`; the LLM branch reuses it
  (regenerate only on cache-miss or `refresh_context=True`) → true no-op reindex in
  production. Deterministic branch ignores the cache (regeneration is free + byte-identical).
  Dormant on CI (local → deterministic).

## Headline metric delta (prefixes-ON, local deterministic embedder, vs committed R3)

| signal | metric | R4 ON | Δ vs R3 |
|---|---|---|---|
| fused | recall@10 | 0.827077 | **+0.0080** |
| fused | nDCG@10 | 0.698527 | +0.0084 |
| fused | MAP | — | +0.0083 |
| fused | MRR | — | −0.0077 |
| reranked | nDCG@5 | 0.658401 | −0.00002 (flat) |
| reranked | recall@5 | 0.727652 | **−0.0105** |
| answerability | coverage | 0.979310 | **−0.0207** |
| answerability | abstention | 0.153846 | +0.1346 |
| answerability | AURC | 0.118238 | **+0.0341** |

Per-doctype unit recall@10: faq 0.969 (+0.000) · howto 1.000 (+0.038) · policy 0.563
(−0.008) · reference 0.697 (+0.030). By query-type recall@10: **cross_document 0.733
(+0.144)** · conversational (+0.020) · rule (−0.018) · threshold (−0.038) · trip_planning
(+0.000). Hard gates hold: policy 0.563 ≥ 0.55 floor; reference 0.697 ≥ 0.65 floor; overall
recall@10 0.827 ≥ 0.80 and > R0 0.727.

## Default-OFF safety (empirical)
Under `RAG_CONTEXTUAL_PREFIXES=False` the existing eval is 6/6 pass and every `rag_corpus`
metric is byte-identical to the R3 backup (recall@10 0.819031, coverage 1.0, abstention
0.019231). The additive column + tsv concat + `if prefix else` guard leave the OFF path
byte-identical to R3; the tsvector leading-space (when the column is NULL) is discarded by
`to_tsvector` and is harmless.

## The honest read (what the numbers mean)
- **AURC +0.0341 is the single honest arbiter, and it regressed.** The local cosine
  FALLBACK gate got a worse selective-prediction tradeoff. NOTE the abstention rise
  (+0.1346) is **not** the harm in isolation — declining more of the unanswerable set is
  correct; the net damage is **coverage −0.0207** (≈3 answerable queries flipped to false
  abstention) **plus AURC +0.0341**. Cause (FORK-B caveat): the prefix lengthens the
  embedded text; on a lexical/hashing embedder the prefix tokens are NOISE for queries that
  don't name the document, diluting `max_dense_similarity` into the fixed 0.20 gate so
  borderline queries flip to abstain. Not retuned (fence).
- **The +0.144 cross-document win is a FUSED recall@10 number — measured one stage upstream
  of where it must pay off.** Reranked recall@5 went −0.0105 and reranked nDCG@5 is flat, so
  the cross-doc gold units the prefix surfaced are landing at ranks ~6–10, not in the
  reranked top-5–8 the generator actually consumes. The FSR did not isolate *reranked*
  cross_document recall@5 — that is the number R4-T3 MUST report, because it tells you
  whether prefixes help the ANSWER, not just the candidate set.
- **`policy` recall@10 0.563 is 0.013 above its 0.55 floor**, and the prefix moved it the
  wrong way (−0.008) on the largest document type. R4-T3 MUST re-verify it under the
  production embedder; if it dips below 0.55 there, that floor becomes the binding
  constraint on adopting-on.
- **Production-semantic-embedder caveat.** `_embed_corpus` embeds with the LOCAL embedder
  regardless of `LLM_PROVIDER` (the toggle chooses prefix TEXT, not the embedder), so this
  run cannot show the prefix's true value. A semantic embedder (text-embedding-3-small)
  encodes the prefix as coherent signal rather than hash-collision noise, which should
  preserve the cross-doc win and largely dissolve the gate regression. That only lands under
  R4-T3 (production-embedder reindex) + eval-live RAGAS.

**Recommendation (decision deferred to R4-T3/T4, NOT pre-committed): production-gate, keep
default OFF.** Ship the mechanism (done, OFF byte-identical to R3); validate adopt-on under
the production embedder + eval-live before flipping. Do not adopt-on on the local embedder —
the AURC/coverage regression is real on the path CI runs. The +0.144 fused cross-document
signal justifies the production trial; it does not justify flipping the default on a lexical
embedder that treats the prefix as noise.

## Migration 0011 (live pg16)
up (0010→0011): `context_prefix` present, dim 1536, HNSW + GIN intact. down: column absent,
indexes intact. re-up: present again, dim 1536, both indexes intact. `alembic check` → "No
new upgrade operations detected" (exit 0).

## Verification (Section 10)
G1 ruff/format clean (10 files). G2 mypy clean (new modules + touched db/config). G3 20
hermetic unit tests pass (purity, doctype-awareness, band, routing, fail-safe, zero-LLM) +
layering test 20/20. G4 eval-db on local embedder, zero external calls. G5 delta table above.
G8 zero added query-time latency (policy_retrieval.py untouched; ON-run fused p50 4.8ms /
p95 8.3ms ≈ R3); indexing cost: deterministic ≈ free, LLM = 1 cached call/chunk offline.
G9 idempotency under ON: 208 keys, 0 vector-hash mismatches across re-seed; deterministic
byte-identical; LLM no-op via persist-reuse. G11 0011 up/down/re-up clean, drift empty,
1536+HNSW+GIN intact. G12 doc-disambiguation lands in the right document; chunk_text pristine
(asserted not-substring); reranker reads chunk_text; prefixes inspectable. R4-extra: every
embedding len==1536, L2≈1.0; reranker input byte-identical → τ* untouched. Prefix evidence:
208/208 rows non-NULL context_prefix (policy 98 / faq 50 / howto 35 / reference 25); a
get_llm_provider-raising spy never fired on the ON measurement; unit test (g) proves
local→deterministic makes zero LLM calls.

## Files (10 code + this log)
database/seeds/corpus/contextual.py (new) · ai_agents/rag/contextual.py (new) ·
database/migrations/versions/0011_policy_embedding_context_prefix.py (new) ·
backend/tests/unit/test_unit_corpus_contextual.py (new) ·
database/models/policy_embedding.py · database/seeds/seed_all.py ·
backend/tests/eval/test_rag_corpus_eval.py · database/seeds/chunk_tsv.py ·
config/settings/base.py · ai_agents/tests/test_no_backend_import.py.

## Carried-forward / deferred
- **R4-T3 (the real semantic win):** production-embedder reindex; MUST isolate **reranked
  cross_document recall@5** (not fused recall@10) and **re-verify policy recall@10 ≥ 0.55**;
  recalibrate τ* if the embedder change shifts the score distribution.
- **R4-T4:** formal adopt/gate/revert assertion (the decision is mine).
- **gate-8 HF-reranker latency** (~5.9s p50 free tier) — unchanged; one-file Cohere/TEI swap.

## Process note
2/4 adversarial-review passes did not complete because of a transient tooling error (not a
finding); both reviews (τ*-invariance, prefix/idempotency) re-performed by direct diff
inspection — both pass. G1/G2/G3 independently re-run rather than trusting the review
self-report.
