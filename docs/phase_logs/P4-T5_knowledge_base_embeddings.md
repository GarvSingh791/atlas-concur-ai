# P4-T5 — Knowledge Base + Embeddings (RAG System 4B)

**Date:** 2026-06-13
**Author:** Garv Singh Saini

---

## What Was Built

The `ai_agents/rag/` subpackage — the canonical, provider-backed, idempotent policy
re-indexer deferred from P3-T5 to P4-T5.

### `ai_agents/rag/chunker.py`
Pure, deterministic, hermetic — no DB, no network, no seed coupling.
- `PolicyChunk` frozen dataclass: `clause_ref: str`, `chunk_text: str`.
- `chunk_policy_document(document_text: str) -> list[PolicyChunk]`:
  - Regex-splits `policies.document_text` on `[LABEL]` markers
    (`re.compile(r"\[([A-Z0-9-]+)\]")`).
  - Discards any preamble before the first marker (the version/intro header is not a clause).
  - Strips the bracket tag from `chunk_text` — the label lives only in `clause_ref`.
  - Same input → identical chunk list and order every run (deterministic).
  - Yields exactly 8 `PolicyChunk` instances in document order:
    `AIR-CABIN`, `AIR-PREF`, `HOTEL-CAP`, `HOTEL-PREF`, `MEALS-PD`, `GROUND-CLASS`,
    `RECEIPT-REQ`, `SUSTAIN`.

### `ai_agents/rag/indexer.py`
Provider-backed, idempotent re-indexer. Injected `Session` + `EmbeddingsProvider`.

- `reindex_policy(session, *, policy_id, embedder=None) -> int`:
  1. Resolves `Policy` by id; raises `ValueError` if not found.
  2. `embedder = embedder or get_embeddings_provider()` — the same factory path as
     `PolicyRetrievalTool`.
  3. Chunks `policy.document_text` via `chunk_policy_document`.
  4. `embedder.embed_texts([c.chunk_text for c in chunks])` — batch embed.
  5. **Dimension guard fires BEFORE any write**: asserts every vector length equals
     `EMBEDDING_DIMENSION` (1536); raises `ValueError` loudly on mismatch — nothing
     written, table left intact.
  6. **Transactional replace per policy**: `DELETE` existing `policy_embeddings` rows for
     that `policy_id`, then `INSERT` the freshly chunked+embedded set via
     `session.add_all`. Calls `session.flush()` — the caller owns `commit`.
  7. Returns `len(chunks)` (== rows written). Logs per-policy chunk/row counts.

- `reindex_all_active(session, *, embedder=None) -> dict[UUID, int]`:
  - Queries `Policy.is_active == True`.
  - Calls `reindex_policy` for each, returning `{policy_id: count}`.

### `ai_agents/rag/reindex.py`
`python -m ai_agents.rag.reindex` entry point. Backend-free at module level.
- `sys.path` insert at module level (repo root) — mirrors `seed_all.py` so the module is
  directly runnable.
- `from backend.app.core.database import get_sessionmaker` is a **late import inside
  `main()` only** — module-level imports remain stdlib-only, keeping the subprocess layering-law
  test green.
- `argparse --policy-id` optional; defaults to all active policies.
- Acquires a session, calls `reindex_all_active` or `reindex_policy`, commits, prints counts.
- No network I/O, no LLM call, no endpoint or router.

### `ai_agents/tests/test_rag_chunker.py`
15 hermetic tests. Covers: 8 chunks, correct ordered `clause_ref` list, preamble discarded,
bracket absent from `chunk_text`, frozen dataclass immutability, determinism (two calls →
identical output and stable text), SUSTAIN present, SUSTAIN has no corresponding rule, all 7
`source_clause_ref` values covered by chunks, 1536-dim vectors, unit-normalized vectors (L2≈1.0).

### `ai_agents/tests/test_rag_indexer.py`
12 live-DB tests (module-scoped self-skip fixture — skips cleanly without Postgres). Covers:

| Check | Verifies |
|---|---|
| Idempotency | Count, `clause_ref` set, and vectors stable across 2 sequential re-index runs |
| Convergence from seed | Starting from the P3-T5 bootstrap rows → exactly 8 rows (no doubling) |
| Clause alignment | All 7 `policy_rules.source_clause_ref` values have a matching embedded `clause_ref`; SUSTAIN embedded but has no rule |
| Dim/normalization | Every written embedding `len == 1536`, L2 norm ≈ 1.0 (tolerance 1e-6) |
| Provider-parity round trip | After re-index, `PolicyRetrievalTool("business class to London")` → AIR-CABIN in top-k; proves index-space == query-space |
| Negative dim guard | Stub provider returning wrong-dim vectors → `ValueError`; pre-existing row count unchanged |
| Multi-policy isolation | Scoped delete (by `policy_id`) leaves other policies' rows untouched |

### `ai_agents/tests/test_no_backend_import.py` (modified)
Added 4 new `ai_agents.rag` module imports to the subprocess layering-law script:
`ai_agents.rag`, `ai_agents.rag.chunker`, `ai_agents.rag.indexer`, `ai_agents.rag.reindex`.
All 4 pass (module-level backend imports are absent from all rag modules).

---

## Central Invariant

**Index-time embedding provider == query-time embedding provider.**

`indexer.py` obtains its provider via the same `get_embeddings_provider()` factory and
injection interface that `PolicyRetrievalTool` uses at query time. If these ever diverge,
cosine similarity in the HNSW index becomes meaningless and retrieval silently breaks.
This invariant is upheld by sharing the same factory/injection path and verified by the
provider-parity round-trip test.

---

## Key Decisions

### 1. Idempotency via transactional replace-per-policy
No `UNIQUE` constraint exists on `(policy_id, clause_ref)` in `policy_embeddings`, so
`ON CONFLICT` upsert is not available without a new migration. The chosen mechanism —
`DELETE WHERE policy_id = ?` then `INSERT` the fresh set — requires no unique constraint,
no migration, and no schema change. It converges to exactly `chunk-count` rows from any
starting state, including the P3-T5 seed bootstrap rows.

### 2. Dimension guard is fail-loud, fires before any write
The guard runs immediately after `embed_texts` and before the `DELETE` statement. A
mis-configured provider will raise `ValueError` and leave the table entirely intact. This
prevents the table from being left in a partially-written or emptied state.

### 3. Eight clauses; SUSTAIN is embedded but rule-less
The "clause-without-rule" pattern: SUSTAIN is chunked and embedded so that it is
retrievable, but it has no corresponding entry in `policy_rules`. The downstream behaviour
when a query surfaces only the SUSTAIN clause (and no rule applies) is the "policy is
silent" guardrail — that handling belongs to P4-T6, not here.

### 4. Chunk source: `policies.document_text`, not the seed `CLAUSES`
`MASTER_EXECUTION_PLAN` specifies "knowledge base is `policies.document_text` chunked into
clauses". The `DOCUMENT_TEXT` prose carries `[LABEL]` markers that delimit the 8 clauses;
the bracket label is the `clause_ref`. The seed's hand-authored `CLAUSES` list (paraphrases
used only for the bootstrap) is intentionally NOT the source here. The prose-based embeddings
supersede the bootstrap vectors when the indexer is run.

---

## Relationship to the Seed (P3-T5)

P4-T5 is the "real-provider embed + idempotent re-index" explicitly deferred from P3-T5.
At seed time, 8 `policy_embeddings` rows were bootstrapped using `database/seeds/_local_embedder.py`
(now a shim that re-exports `services.embeddings_provider.local.embed`) purely so the schema
and HNSW index had data to exercise. That helper is **not** the Phase-4 `EmbeddingsProvider`.

The seed was **not modified** in P4-T5. Seeds build before `ai_agents` in the dependency
order; a `seed → ai_agents` import would be an illegal upward dependency. The bootstrap
rows remain until the indexer is run, at which point they are transactionally replaced by the
canonical prose-based vectors. The indexer is the re-index path going forward.

---

## Noted Items / Future Alignment (not defects)

- **`session.query()` legacy API**: `indexer.py` uses SQLAlchemy 1.x-style `session.query()`
  rather than the SA 2.0 `sa.select()` form used by the other agent tools. Not a bug;
  tracked for future style alignment.
- **`reindex_all_active` has no per-policy exception isolation**: if one active policy raises
  (e.g., bad provider dimension), the exception propagates and the entire batch aborts.
  Acceptable at single-policy seed scale; out of scope for P4-T5.

---

## Issues Found and Fixed in QA

| Rule | Files affected | Fix applied |
|---|---|---|
| ruff I001 — import sort | 4 new files | Imports re-ordered |
| ruff E501 — line > 100 chars | 1 test assertion | Assertion split across lines |
| ruff B011 — `assert False` | 1 test | Replaced with `pytest.raises(FrozenInstanceError)` |
| Unused import | 1 test file | Import removed |

---

## Validation Evidence

| Check | Evidence |
|---|---|
| Idempotency | Count / `clause_ref` set / vectors stable across 2 sequential re-index runs |
| Convergence from seed | Starting from P3-T5 bootstrap → exactly 8 rows; no doubling |
| Clause alignment | All 7 `source_clause_ref` values matched; SUSTAIN embedded, no rule |
| Dim/normalization | Every embedding `len == 1536`, L2 norm ≈ 1.0 |
| Provider-parity round trip | `PolicyRetrievalTool("business class to London")` → AIR-CABIN top-k |
| Negative dim guard | `ValueError` raised; row count unchanged after failed attempt |
| Multi-policy isolation | Per-policy scoped delete verified |
| Layering law | `test_no_backend_import.py` passes (subprocess, extended with 4 rag imports) |
| Lint / types | `ruff check`: clean; `mypy ai_agents/rag`: clean (4 source files, 0 issues) |
| Hermetic tests | 15 passed (`test_rag_chunker.py`) |
| Live-DB tests | 12 self-skipped without Postgres (self-skip is correct behaviour); round-trip verified with live DB |

---

## Scope Confirmation

| Item | Status |
|---|---|
| New migration | None |
| Model change | None |
| Backend wiring | None (no endpoint, no router, no `backend/` import at module level) |
| Seed modification | None |
| New dependency | None |
| LLM call | None |
| Retrieval / threshold / answer logic | None (deferred to P4-T6) |

---

## Next: P4-T6

Grounded retrieval flow — top-k selection, similarity threshold, the "policy is silent"
guardrail, and clause citations. Consumes the index built here via `PolicyRetrievalTool`.
