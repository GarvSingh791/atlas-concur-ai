# R4-T3 + R4-T4 — Embedding Upgrade, Live Matrix, Decision (as-built)

**Date:** 2026-06-29
**Author:** Garv Singh Saini
**Branch:** r4-embedding-upgrade (off main @ a7a76a8 = merged R4 Part 1; uncommitted at authoring)

Builds the apparatus to DECIDE the R4 adopt question — a config-driven 1536-d embedder
upgrade, a live retrieval-eval matrix, embedder-conditional prefix gating that makes
adoption CI-safe, and τ* recalibration machinery — and makes the consolidated adopt/gate/
revert decision. **R4 is mechanism-complete but decision-PENDING-KEY: the live matrix did
not run (no EMBEDDINGS_API_KEY in env), so the embedder upgrade is unmeasured and the
prefix adopt-decision (deferred from Part 1) remains deferred.** The TASK closes on the
DoD-blessed "revert with a recorded reason (live measurement pending key)" exit; Phase R4
substantively closes pending the one key-gated `make eval-live-matrix` run.

## Scope
R4-T3 (config-driven embedder upgrade behind the factory; live matrix lane; τ* recalibration
machinery; FORK-H CI-safe gating) + R4-T4 (the consolidated adopt/gate/revert decision —
B1-provisional, key absent). OUT: any pgvector migration / dimension change; reranker
scoring / fusion.py / RRF-k / query-side prefixing; Cohere embed-v4; frontend / backend DTOs;
breaking CI determinism or the R3-byte-identical local baseline.

## Decisions (FORK-E…I)
- **FORK-E (live lane).** Generalized the existing `production_baseline_live` test into the
  C0..C3 matrix runner `evals/manual/test_rag_corpus_embedding_matrix_live.py`
  (@pytest.mark.live+db) + `make eval-live-matrix`. Self-skips without the embeddings key;
  writes a scratch snapshot only (never clobbers committed metrics.json). Implements every
  demanded column per cell — recall@10, nDCG@10, reranked nDCG@5, reranked recall@5, AURC,
  reranked cross_document recall@5, policy recall@10 (by doctype), coverage, abstention —
  plus the three approval-addendum additions: the C0 pre-rerank FUSED control column, the
  per-cell prefix-free + C0 determinism check, the reranker-status row, and inline τ*
  recalibration (0.039299 → new).
- **FORK-F (candidate model).** text-embedding-3-large @ dimensions=1536 + L2-renormalize
  (OpenAI does NOT renormalize truncated Matryoshka vectors — the renorm is load-bearing for
  the cosine contract). The provider needed `dimensions` ADDED (was absent) +
  `EMBEDDINGS_DIMENSIONS: int = 1536`. Cohere embed-v4 documented, not pursued.
- **FORK-G (corpus-embedder plumbing — wider than the task spec scoped, deliberately).**
  Factory-routed FOUR sites: both corpus seeders (`_embed_corpus`, `_seed_corpus_for_org`)
  AND both calibration fitters (`test_fit_abstention_threshold.py`,
  `test_fit_rerank_calibration.py`) — the fitters embed the corpus too, so leaving them
  hardcoded-local would make a live τ* fit impossible. `_local_embedder.py` untouched.
  Byte-identity hinge: `LocalEmbeddingsProvider().embed_texts([x])[0]` IS `[embed(x)]`
  (local.py:65), so the factory→local path is byte-identical to the legacy bare `embed(x)`.
  Deviation from "config-only": both seeders + both fitters now take the factory; "config-
  only" was not literally true (the seed hardcoded local; the factory-routed reindexer
  cannot touch the structurally-chunked corpus — indexer.py:71-77).
- **FORK-H (embedder-conditional prefix gating — the CI-safety enabler).** `context_prefix`
  returns "" when EMBEDDINGS_PROVIDER==local EVEN IF RAG_CONTEXTUAL_PREFIXES=True. Proof: 0
  non-empty prefixes over all 208 chunks with the flag flipped ON under local. So adopting
  prefixes (flipping the default True) cannot perturb `make eval-db` — CI stays prefix-free
  and R3-byte-identical. Part-1 contextual tests stay green; 3 new FORK-H tests added.
- **FORK-I (decision handling).** Tree left in B1-provisional: committed default = R3,
  machinery built and default-OFF/inert, ZERO production-default lines flipped. Nothing
  committed; the later commit ratifies the machinery, not an adoption.

## τ* artifact keying — the resolution (build now, pair on adopt)
`abstention_threshold.json` is keyed by RERANK_PROVIDER only (huggingface→0.039299, local→
null cosine fallback). A live τ* fit under a different embedder would share the huggingface
key and CLOBBER the CI-local entry. Resolution: pure helpers `calibration_key()` +
`live_artifact_path()` in calibration.py; the live fitters write only to a `.live.json`
scratch path (`.gitignore`d), so the committed gate artifact can never be clobbered during
measurement. The PAIRED INVARIANT — an embedder-qualified composite-key WRITE to the
committed artifact AND the gate-side READ that consumes it must change TOGETHER in the same
change set — is recorded as the ADOPT step (B2/B3/B4 only). B1 = report-only, artifact
untouched. A key written but not read = a silent fallback-to-cosine in production; if the
write and read can't be paired cleanly at adopt time, that is a STOP-and-FLAG, not a half-adopt.

## The live matrix (G5) — PENDING KEY
Not run (no EMBEDDINGS_API_KEY / no Postgres). The runner self-skips cleanly and collects
without error. Live measurement + the adopt decision (B1/B2/B3/B4) are pending the key
(+ HF_API_TOKEN for the reranked columns; absent it, the reranker fail-safes to RRF and
reranked==fused — documented, not a "reranker showed no gain"). The expected shape from the
Part-1 evidence: C1 (3-small + prefixes-on-SEMANTIC) is the candidate to watch (does the
+0.144 fused cross-doc win survive into reranked recall@5 once a semantic embedder treats the
prefix as signal, not hash-noise?); 3-large@1536 is a likely modest-or-not-worth-cost bump.
If C1 helps FUSED recall@5 but the HF reranker eats it (the C0 fused control column will
show this), that is an R5 finding — the reranker, not the embedder, is the bottleneck — and
must be recorded as such, not buried as "prefixes flat".

## Smoke-test (real-API deferred; mocked-client proxy)
No EMBEDDINGS_API_KEY ⇒ real-API smoke deferred. Mocked-client proxy (runs here):
dimensions=1536 passed to embeddings.create; a non-unit returned vector (norm 2.0) renorms to
L2≈1.0; len==1536; query+doc use the same EMBEDDINGS_MODEL; non-1536 → ProviderConfiguration-
Error; zero-norm → ProviderError (fail-loud). No migration (head still 0011); alembic check
deferred (no DB).

## Verification (Section 10 — run on LLM_PROVIDER=local + EMBEDDINGS_PROVIDER=local)
G1 ruff/format clean (11 files). G2 mypy clean (backend config evals, 146 files; + real.py /
calibration.py / seed_all.py / contextual.py local). G3 34 hermetic unit tests pass; services
tests re-run with `import openai` BLOCKED (all 11 pass SDK-absent — the CI condition). G4
(HARD, VECTOR-LEVEL PROXY) over all 208 chunks under the committed default: 0 non-empty
prefixes, 0 vector mismatches vs legacy embed, counts 98/50/35/25=208; FORK-H: 0 prefixes
with the flag ON. → vector-level byte-identity PASS; **full make eval-db metric equality
follows BY IMPLICATION (identical vectors ⇒ identical metrics), to be confirmed on the
Postgres lane — NOT measured here.** G9 counts 98/50/35/25=208; full re-seed idempotency
deferred (no Postgres). G11 no new migration; dim invariant enforced in code (reject !=1536,
assert len==1536 / L2≈1.0); alembic check deferred (no DB). τ* machinery built (recalibrate-
live + inline risk-coverage); NOT triggered (B1-provisional; committed τ*=0.039299 unchanged).
G5/G8/G12 = live-lane, pending key.

## Files (13 + this log)
config/settings/base.py · services/embeddings_provider/real.py · database/seeds/seed_all.py ·
backend/tests/eval/test_rag_corpus_eval.py · evals/calibration/test_fit_abstention_threshold.py ·
evals/calibration/test_fit_rerank_calibration.py · ai_agents/rag/calibration.py ·
database/seeds/corpus/contextual.py · backend/tests/unit/test_unit_corpus_contextual.py ·
services/tests/test_embeddings_provider.py · Makefile · .gitignore ·
evals/manual/test_rag_corpus_embedding_matrix_live.py · docs/phase_logs/R4-T3_T4_asbuilt.md.

## Consolidated decision — B1-provisional (DoD-blessed)
No key/DB ⇒ live matrix unmeasurable ⇒ no adopt flip justified. Committed default = R3 (byte-
identical, vector-proven); τ* unchanged. Production-default lines flipped: NONE. The machinery
to decide B2/B3/B4 is one `make eval-live-matrix` away; the τ* paired-invariant is cleanly
pairable via `calibration_key()`, with the live write isolated to `.live.json`.

## Carried-forward
- **Live matrix run + adopt flip** — pending EMBEDDINGS_API_KEY (+ HF_API_TOKEN for reranked).
  First action of the follow-up: run `make eval-live-matrix`, then decide B1/B2/B3/B4.
- Full eval-db metric byte-identity / re-seed idempotency / alembic check / recalibrate-live —
  pending Postgres.
- τ* gate-read pairing — the adopt step (paired write+read invariant above).
- Reranker-as-bottleneck — if the matrix shows C1 helps fused but not reranked recall@5, that
  is an R5 finding (carry to R5, not buried in R4).
- Cohere embed-v4 (not pursued); R5 (HyDE / citation enforcement / NLI); gate-8 HF-reranker
  latency (~5.9s p50 free tier; one-file Cohere/TEI swap).

## Process note
Real-API + Postgres gates honestly deferred (no key/DB in the build env), mirroring the R3
no-Postgres caveat; vector-level + hermetic proxies proven in-environment. 0 production-default
lines flipped — the commit ships inert machinery, not a behavior change.
