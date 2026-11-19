# R6-T1 + R6-T2 — As-Built (Latency Hardening + Frontend Trust Surface)

Date: 2026-07-02. Branch (intended): `r6-latency-trust-surface` off `main@7e31aba`
(R5 closed). Author: Garv Singh Saini. Phase R6 is **NOT** closed — R6-T3 (audit trail) + the public
writeup remain; live p50/p95, G5-live, and the live prompt-split re-validation defer.

## Honesty frame (binding)
Both latency levers are **forward-looking** at the current **208-row/org** corpus, not
measured wins. A live `EXPLAIN (ANALYZE, BUFFERS)` at 208 rows shows the planner
**EXACT-scans + top-N heapsort** (org-filtered via `ix_policy_embeddings_policy_id`),
**not** an HNSW index scan — identical plan whether `hnsw.ef_search` is unset (0.844 ms) or
100 (0.686 ms). So `ef_search` is **inert** here and `recall@10=0.819` is exact-search
recall. The prompt-cache is **inert** at the ~150-token static block (< ~1024 Gemini floor)
and **not live-verified**. The shipped win is the trust surface.

## Reconciliation (CODE-WINS discoveries)
- **No separate system prompt:** `policy_qa._SEED_PROMPT_V2` fused static + clauses +
  question into one `role="user"` message; static block ~150 tokens.
- **The trust surface consumes the `/conversations/{id}/messages` SSE stream**
  (`backend/app/modules/ai/`), not `/policy/query`; it already streams. Citations already
  carried `ref`+`detail`(snippet)+`source`(doc name) but **not** `document_type`;
  `confidence`+`confidence_source` already rode `result.output`; the status-frame confidence
  is the **routing** confidence, not the calibrated score.
- **Corpus = 208 rows/org** authoritative (`chunk_document`: policy 98 + faq 50 + howto 35 +
  reference 25, one chunk/unit). An earlier recon count of "216" was an overcount.
- **`chunk.document_type`** trivially present at the citation construction (no new JOIN);
  `confidence_source` reliably `"calibrated"` on the live Novita path.

## R6-T1 — latency hardening
- **Migration `database/migrations/versions/0012_hnsw_ef_construction_200.py`** (the single
  authorized drift-guard exception): `ef_construction` 64→200 (m=16, `vector_cosine_ops`,
  `vector(1536)` unchanged) via `CREATE INDEX CONCURRENTLY` build-then-swap inside
  `op.get_context().autocommit_block()`, reversible, canonical index name preserved.
  ORM `database/models/policy_embedding.py` Index gains
  `postgresql_with={"m":16,"ef_construction":200}`. **Applied + validated on the eval DB**
  (`alembic upgrade head` → index now `WITH (m='16', ef_construction='200')`; full-chain
  drift test green through up/down/re-up).
- **`hnsw.ef_search=100`** (`config/constants/embeddings.py::HNSW_EF_SEARCH`) set
  transaction-local via `SELECT set_config('hnsw.ef_search',:ef,true)` in
  `ai_agents/tools/policy_retrieval.py::_retrieve_dense`, resilient `try/except`, dense-only.
  Inert at 208 rows (see EXPLAIN); forward-looking. halfvec **documented-not-applied**.
- **Static-system-prompt cache (live-only):** additive `LlmMessage.cache`
  (`services/llm_provider/interface.py`) + OpenRouter `cache_control:{type:"ephemeral"}`
  content block (`services/llm_provider/openrouter.py`). `policy_qa` splits
  `_STATIC_INSTRUCTIONS` (system, `cache=True`) from `_QUERY_TEMPLATE` (user) when
  `LLM_PROVIDER != "local"`; the local path returns the byte-identical single fused
  `_SEED_PROMPT_V2` message (empirically proven). `PolicyQaCritic`/`_CRITIC_PROMPT`
  unchanged. INERT at ~150 tokens; wired-not-live-verified. `services/*` touched — FLAGGED.
- **Profiler `evals/manual/test_rag_latency_profile_live.py`** + `make eval-profile-live`:
  per-stage retrieval/rerank/generation timing, 2.1s Novita throttle, optional verbatim
  EXPLAIN (reports index-scan vs seq/exact-scan), plus citation_completeness +
  nli_entailment_rate to re-validate the live prompt-split generation-output change. Self-
  skips keyless (`_require_live_key`). No invented p50/p95.

## R6-T2 — frontend trust surface
- **Seam (2 additive, non-breaking fields; no `backend/app` schema/contract change):**
  `document_type` on `Citation.to_dict` (only when not None); `tau_star` on
  `GroundedRetrieval` emitted as policy_agent output key `"threshold"`. CODE-WINS naming
  collision: an existing required `threshold: float` (applied gate threshold) is kept
  distinct from the display anchor `tau_star` (None on cosine/local/silent).
- **Document-qualified chips** (`SHORT_PREFIX` doc name + unit id, e.g. "T&E Policy §AIR-01")
  reusing existing chip tokens; keyboard-accessible hover snippet from `citation.detail`
  (Radix Tooltip; touch tap fallback).
- **Calibrated-confidence display — `displayConfidence` (τ-anchored log-odds normalization):**
  `0.70 + 0.30·clamp((logit s − logit τ*)/(logit 0.90 − logit τ*), 0, 1)`, FLOOR=0.70,
  P_FULL=0.90, τ*=0.039299. Verified bands: τ*→70.0% · s=0.06→72.5% · s=0.08→74.2% (amber) ·
  s=0.5→87.8% (amber) · green only s≳0.60 · monotonic · grounded never <70% (never red).
  Applied only when `confidence_source==="calibrated"` (defensive: also any sourceless
  `<0.5`); cosine path passes through. Raw score + τ* in the tooltip; label
  "Grounding confidence". Shared `ConfidenceIndicator` / OCR `field_confidence` untouched —
  transform applied upstream on the policy surface.
- **Graceful abstention + escalation:** calm "Policy is silent on this." kept; a semantic,
  non-alarming escalation CTA on the silent state (info/trust vocabulary). Semantic-only
  selectors; **zero data-testid** on production components.

## Gates (all green)
- ruff/format clean (356); mypy `backend config evals` clean (148) + the 8 R6 production
  files clean.
- Hermetic **755 passed / 0 failed** (576 ai_agents+services, 179 backend), additive.
- `make eval-db` **byte-identical**: 47 passed, recall@10=0.819 held, coverage=1.000000,
  abstention=0.019231. Migration-chain drift green (validates 0012 up/down/re-up).
- Frontend type-check + lint + format + build green; **201 vitest passed** (exact τ* bands,
  non-red grounded, chip+hover, escalation CTA).
- Layering: `test_no_backend_import` subprocess check green (zero backend imports in
  ai_agents). data-testid on production components = 0.
- Adversarial review (5 skeptics): Layering/Determinism/Prompt-cache/Frontend PASS; Honesty
  = 1 real overclaim found & fixed (profiler NFR-P1 wrongly said "HNSW index is used /
  index-bound" → corrected to exact-scan / forward-looking).
- **Live p50/p95 DEFERRED** — profiler self-skips (1 passed, 1 skipped).

## Deviations (documented, not silently absorbed)
1. `services/llm_provider/{interface,openrouter}.py` touched for `cache_control` (additive,
   live-gated) — outside the stated ai_agents/config diff line. FLAGGED.
2. Two test-only fixes in `backend/tests/test_migration_chain.py`, NOT R6-caused, unmasked by
   validating 0012: stale `EXPECTED_ENUMS` (missing `policy_document_type` from 0008) and a
   stale "pgvector survives downgrade-base" expectation (0001 symmetrically DROPs it since
   P8-T2). Expectation constants only — no assertion weakened.
3. R5 landed as two commits (979401b + 7e31aba), not one squash; prior memory
   "R5-T3/T4 uncommitted" was stale.

## Carried-forward
- Live p50/p95 + G5-live + live prompt-split re-validation → keyed run of
  `make eval-profile-live` / `make eval-live-r5`.
- NLI 0.70 near-extractive risk (from R5) still open — watch `nli_entailment_rate`.
- Sub-200ms reranker path = one-file `RerankProvider` swap to a colocated endpoint —
  documented resolution, NOT done.
- Prompt cache only bites once the instruction block grows past the ~1024-token floor.
- R6-T3 (audit-trail completion) + the public write-up remain.

τ*=0.039299, Platt params, HYDE_ENABLED=False, NLI_ENTAILMENT_THRESHOLD=0.70 UNCHANGED.
