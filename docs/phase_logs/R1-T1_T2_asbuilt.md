# R1-T1 + R1-T2 — Hybrid Retrieval Plumbing (as-built)

Phase R1, Part 1 of 2. Builds the lexical index (R1-T1) and the parallel dense+sparse
retrieval function (R1-T2). NO fusion, NO live wiring, NO eval delta this cycle (those are
R1-T3..T5). Proof = correctness of the two paths + a baseline-regression guard.

## Reconcile findings + decisions

1. **tsvector strategy → option (a): plain, seeder-populated `chunk_tsv`.** The document
   title is `policies.name` (cross-table), unreachable by a STORED GENERATED column. So
   `chunk_tsv` is a plain nullable `TSVECTOR` column, populated server-side by the seeder
   from `chunk_text + clause_ref + section + policies.name` (title via JOIN). No
   denormalized title column (rejected option c — schema overreach). Lives in a new
   `database/seeds/chunk_tsv.py` (`populate_chunk_tsv(session, org_id)` — a single
   org-scoped `UPDATE ... FROM policies` with the regconfig as a hardcoded SQL literal to
   avoid the `to_tsvector(text,text)` resolution trap). Reused by both the production
   seeder and the eval harness (single seeding path).

2. **Text-search config → `english` + `plainto_tsquery`.** Passes all four exact-term
   probes. Evidence (sparse path, units returned): `$300`→**REF-21** (+FAQ-Q46, MIL-03,
   HOW-06/08); `New York`→**FAQ-Q14, FAQ-Q21** (+LOD-01, DEF-05); `AIR-03`→**AIR-03**
   (+FAQ-Q01, REF-04…); `affidavit`→**EXP-04, REF-10** (+HOW-09, REF-11, FAQ-Q31).
   FINDING (flagged): the default FTS parser tokenizes `"$300/night"` as a single
   `300/night` token, so the bare query token `300` does NOT reach clauses that render
   `$300/night` (LOD-01, FAQ-Q14); REF-21 (`$300;`) is the canonical lodging-cap unit it
   does reach. This lexical gap is exactly what RRF + dense close in R1-T3..T5. The
   `english||simple` union contingency was NOT needed (the parser, not the dictionary,
   splits on `/`, so the union would not change it).

3. **Live-path invariance → new method.** `PolicyRetrievalTool.retrieve()` is byte-for-byte
   unchanged. New `retrieve_parallel()` returns `(dense, sparse)` lists. `_retrieve_dense`
   uses the IDENTICAL embedding + `<=>` operator + `ORDER BY` as `retrieve()`; only the
   projected columns and LIMIT widen (top-50). Verified by a parity test (dense top-5 ==
   retrieve() top-5) and the byte-identical eval baseline.

4. **Org-scoping JOIN.** Both paths replicate `JOIN policies p ON p.id = pe.policy_id
   WHERE p.organization_id = :org` (organization_id only, NO is_active filter). `populate_chunk_tsv`
   is likewise org-scoped. Hard gate G7 (cross-tenant probe, both paths) passes.

5. **Candidate metadata.** Uniform `RetrievalCandidate` on BOTH paths carries
   `embedding_id` (the RRF fusion key — never null), `document_id` (=policy_id),
   `clause_ref`, `chunk_text`, `section`, `parent_section_ref`, `policy_name` (JOIN-derived),
   `score` (path-native), `rank` (1-based within path).

6. **Seeding + idempotency.** `populate_chunk_tsv` runs in `_embed_corpus` after the
   delete-then-insert flush, same transaction; recomputed from source columns each run
   (idempotent). The eval `_seed_corpus_for_org` calls the same helper. `test_p3_seed`
   (seed idempotency) green; reindex skip-guard (`indexer.reindex_policy`) untouched.

7. **Deferred (still queued).** Parent-context resolver live switch → R1-T4. Legacy
   `test_eval_db_retrieval.py` stub-only retarget → R1-T5. RRF/fusion → R1-T3.

## Migration

Revision **0010** (`0010_policy_embedding_chunk_tsv.py`, down_revision `0009`). Additive:
`ADD COLUMN chunk_tsv TSVECTOR NULL` + GIN index `ix_policy_embeddings_chunk_tsv`.
`downgrade()` drops index then column. up/down/re-up clean; `alembic check` → "No new
upgrade operations detected" (drift empty). `vector(1536)` + HNSW index untouched
(confirmed in live DB: both `ix_policy_embeddings_embedding_hnsw` and the new GIN index
present).

## Baseline-regression guard

`make eval-db` prints the R0 baseline UNCHANGED across 3 re-seeds (recall@10=0.727 ·
prec@10=0.121 · hit@10=0.917 · MRR=0.679 · nDCG@10=0.596 · MAP=0.506 · abstention=0.019 ·
AURC=0.1013). No deterministic secondary sort was required (the dense ORDER BY is already
stable at the k-boundary; verified twice pre-change and three times post-change).

## Doc-drift note

`docs/archive/RAG_MASTERPLAN.md` Phase R1 already uses multi-document wording ("across all
documents", "org-scoped, multi-document") — NO drift to flag.

## Files

- `database/models/policy_embedding.py` — chunk_tsv column + GIN index (model-first)
- `database/migrations/versions/0010_policy_embedding_chunk_tsv.py` — additive migration
- `database/seeds/chunk_tsv.py` — `populate_chunk_tsv` + `TSV_REGCONFIG` (no backend import)
- `database/seeds/seed_all.py` — `_embed_corpus` calls `populate_chunk_tsv`
- `backend/tests/eval/test_rag_corpus_eval.py` — `_seed_corpus_for_org` calls it (single path)
- `ai_agents/tools/policy_retrieval.py` — `RetrievalCandidate`, `ParallelRetrievalResult`,
  `retrieve_parallel`, `_retrieve_dense`, `_retrieve_sparse`
- `backend/tests/eval/test_hybrid_retrieval_paths.py` — new db-tier correctness gates

---

# === COMPLETION / OUTCOME (R1-T1 + R1-T2) ===

Status: COMPLETE — verified; uncommitted at authoring time (commit prep is a separate step).

## SCOPE
R1-T1 (lexical tsvector + GIN index over all 208 corpus chunks) + R1-T2 (parallel
dense + sparse retrieval plumbing). Plumbing ONLY — no fusion, no live wiring, no metric
delta (those are R1-T3 → T5).

## DECISIONS (with rationale)
- **tsvector = plain seeder-populated `chunk_tsv` (option a).** The document title is
  `policies.name` — cross-table, unreachable by a GENERATED STORED column.
- **Populated via a dedicated org-scoped UPDATE helper (`populate_chunk_tsv`)** rather
  than inline-in-INSERT — DEVIATION FROM THE APPROVED PLAN, taken to avoid the
  `to_tsvector(text, text)` function-resolution ambiguity (the regconfig must be a SQL
  literal, not a bound text param). Runs in the SAME seeding transaction, so idempotency
  + atomicity are preserved. The source is `coalesce(field,'')`-wrapped per field, so a
  NULL in any field never collapses the concat (NULL-safe by construction).
- **FTS config = `english` + `plainto_tsquery`,** chosen by the four-probe acceptance
  gate. `clause_ref` is included in the indexed source so codes (AIR-03) are always
  present. The `english||simple` union was deliberately NOT applied: the "$300/night"
  split is PARSER-level (the '/' yields one `300/night` token), not dictionary-level, so
  the union cannot fix it.
- **Live-invariance by construction:** a NEW `retrieve_parallel()` instead of a
  shared-helper refactor, so `retrieve()`'s k and output cannot drift.
- **`embedding_id` carried on BOTH paths as the RRF fusion key** (the R1-T3 contract).

## VERIFICATION
| Gate | Result |
|---|---|
| G1 ruff+mypy | clean (touched files) |
| G2 migration | 0009↔0010 up/down clean; autogenerate drift empty; vector(1536)+HNSW untouched (both indexes present live) |
| G3 tsvector | non-NULL on all 208 corpus rows/org; stub has none |
| G4 idempotency | re-seed identical (no dupes); reindex skip-guard intact (`test_p3_seed`) |
| G5 exact-term | $300→REF-21 · New York→FAQ-Q14,FAQ-Q21 · AIR-03→AIR-03 · affidavit→EXP-04,REF-10 (expected clause_ref present each) |
| G6 shape | both lists ≤ 50 |
| G7 org-scoping | cross-tenant probe (both paths, 7 queries): zero foreign chunks |
| G8 metadata | every candidate carries document_id + clause_ref + section + parent_section_ref + score + 1-based rank |
| G9 baseline | byte-identical ×5 (recall@10=0.727 / prec@10=0.121 / hit@10=0.917 / MRR=0.679 / nDCG@10=0.596 / MAP=0.506 / abstention=0.019 / AURC=0.1013) |
| G10 suite | eval-db tier 42 passed; all impacted modules pass in isolation |
| G11 qualitative | sparse ranks AIR-03 #1 vs dense #4 |
| [4] parity | retrieve_parallel dense top-5 == live retrieve() top-5 (3 queries) |
| [3] empty-sparse | all-stopword query → sparse=(), dense still ranks |

NO deterministic secondary sort was needed: no dense ties at the k-boundary today
(baseline byte-identical across 5 fresh re-seeds).

## KNOWN LIMITATION (carried)
The "$300/night" tokenizer gap on the sparse path: a `$300` query reaches REF-21
("$300;") but not LOD-01 / FAQ-Q14 ("$300/night"). The dense path + RRF (R1-T3 → T5)
close this. Recorded as a sparse-path characteristic, not a defect.

## REGRESSION CAVEAT (proven, not a defect)
Full-suite mass-failures reproduce after reverting the ENTIRE diff to head=0009: the same
`test_policy_agent_db` ForeignKeyViolation on `fk_ai_decision_logs_user_id_users` (the
tests construct a non-existent `user_id` and need a specific seeded harness). The lone
migration-chain failure trips on R0's `policy_document_type` enum (migration 0008), which
an enum-free 0010 cannot cause. eval-db tier: 42 passed.

## FORWARD DECISIONS for R1-T3 (corrects the FSR §8 framing)
- To convert the empirical k-boundary stability into a PROVABLE guarantee, add
  `clause_ref ASC` — the STABLE authored business key, NOT `pe.id` (a random per-seed
  UUID that is non-reproducible across re-seeds) — as the dense secondary sort, applied to
  BOTH `retrieve()` AND `_retrieve_dense()` together to preserve parity, then re-confirm
  G9 byte-identical.
- Confirm `embedding_id` as the fusion key and `section`/`parent_section_ref` sufficiency
  for the R1-T4 parent-context wiring.

## DEFERRED (still queued)
parent-context live switch → R1-T4 · legacy `test_eval_db_retrieval.py` retarget → R1-T5
· RRF fusion → R1-T3.
