# R0-T2 — Multi-Document Helios T&E Corpus (RAG Masterplan, Phase R0)

Author, seed, and structurally-validate the four-document Helios Dynamics T&E corpus
in the `policies` table (demo org).  This is the retrieval-quality foundation for
R0-T3 (chunking), R0-T4 (embedding), R0-T5 (gold eval set), and R0-T6 (eval harness).

## What was authored and seeded

| Doc | document_type | doc_id | Title | Units | ID scheme |
|---|---|---|---|---|---|
| (a) Policy | `policy` | TEP-FY26 | Helios Global Travel & Expense Policy | 98 | GEN-01…CONT-02 (section-namespaced) |
| (b) FAQ | `faq` | FAQ-FY26 | Helios T&E FAQ | 50 | FAQ-Q01…FAQ-Q50 |
| (c) How-To | `howto` | HOW-FY26 | Helios T&E How-To Guide | 35 | HOW-01…HOW-35 |
| (d) Reference | `reference` | REF-FY26 | Helios T&E Quick Reference | 25 | REF-01…REF-25 |

**Total: 208 units** across four documents.

Doc (a) is ported from the project's own research report
(`docs/RAG_RESEARCH_REPORT.md`, Part 4,
lines 193–337) — the Helios TEP-FY26 v4.0 policy text.  No third-party document text is
present anywhere in the corpus.  Docs (b)(c)(d) are authored fresh under the Helios
Dynamics fictional company.

## Package layout (`database/seeds/corpus/`)

```
database/seeds/corpus/
├── __init__.py      ALL_DOCUMENTS tuple (POLICY, FAQ, HOWTO, REFERENCE)
├── _types.py        CorpusDocument / CorpusUnit dataclasses; DOCUMENT_TYPES contract
├── figures.py       ~68 canonical numeric constants — the cross-doc single source of truth
├── policy.py        DOCUMENT: CorpusDocument  (policy / 98 units)
├── faq.py           DOCUMENT: CorpusDocument  (faq / 50 units)
├── howto.py         DOCUMENT: CorpusDocument  (howto / 35 units)
└── reference.py     DOCUMENT: CorpusDocument  (reference / 25 units)
```

## figures.py — single source of truth (SSOT)

All canonical numeric figures (lodging caps, per diem, receipt threshold, deadlines,
affidavit limits, advance-booking windows, cabin-class thresholds, reimbursement timeline,
contact addresses) are defined **once** in `figures.py` and interpolated via `F.usd(...)` or
named constants by all four document modules.  Cross-document numeric contradictions are
structurally impossible.

Coverage check (string-`in` on rendered `document_text`): **15/15 critical tokens present,
0 missing** (per diem all tiers, lodging caps all tiers, receipt threshold, affidavit max,
submission deadline, stale-report threshold, card-loss window, advance-booking windows,
reimbursement timeline, quarterly affidavit cap).

## Schema change — additive enum column (migration 0008)

- **New Postgres ENUM:** `policy_document_type {policy, faq, howto, reference}`
  — matches codebase idiom from migration 0004 (`PolicyRuleType`/`PolicyAppliesTo`):
  `create_type=False` + explicit `.create(checkfirst=True)` / `.drop(checkfirst=True)`.
- **New column:** `policies.document_type` — nullable, so the legacy stub row stays `NULL`.
- **No new table.** Multiple docs per org was already supported by `UNIQUE(org_id, name, version)`.
- **`VECTOR(1536)` untouched.** Dimensionality unchanged.
- **Model enum:** `PolicyDocumentType` in `database/models/policy.py` alongside existing policy enums.
- **Migration head:** `0007 → 0008` (applied on dev DB; up/down round-trip verified).

## Seeding — `_seed_corpus()` in seed_all.py

Added as Step 2a, called after `_seed_data_models()` in `seed()`.  Iterates
`ALL_DOCUMENTS`; for each document guards on `(org.id, doc.title, doc.version)` (the same
composite key as the UNIQUE DB constraint) and inserts a `Policy` row with:
- `name` = `doc.title` (document discriminator; the `name` column doubles as title)
- `version` = `doc.version` (integer, currently 1 for all four)
- `effective_from`, `document_text` = assembled header + all unit texts
- `document_type` = `PolicyDocumentType(doc.document_type)` (PG enum)
- `is_active = True`; `created_by/updated_by = finance_admin.id`
- **No `policy_rules`, no `policy_embeddings`** — chunking is R0-T3; embedding is R0-T4.

**Idempotent reseed proof:** `seed()` ×2 → identical counts (4 corpus rows, 0 dupes).
Per-type breakdown after two runs: `{policy: 1, faq: 1, howto: 1, reference: 1}`.

## Demo DB state (migration 0008, post-seed)

| Slot | `policies.document_type` | Notes |
|---|---|---|
| 1 | `NULL` | Legacy 8-clause stub ("Atlas Global Travel & Expense Policy"); 8 embeddings; 7 rules. Kept additive through R0-T2/T3. |
| 2 | `policy` | Helios TEP-FY26 (98 units) — no embeddings yet |
| 3 | `faq` | Helios FAQ (50 units) — no embeddings yet |
| 4 | `howto` | Helios How-To (35 units) — no embeddings yet |
| 5 | `reference` | Helios Quick Reference (25 units) — no embeddings yet |

Until R0-T4 runs, retrieval is served entirely by the stub's 8 legacy embeddings.

## Verification results

| Gate | Result |
|---|---|
| ruff + mypy on all 10 touched Python files | ✔ clean |
| Migration 0008 `alembic upgrade 0008` | ✔ applied; VECTOR(1536) unchanged |
| Migration 0008 `alembic downgrade 0007` | ✔ column + type dropped; re-runnable |
| `python -m pytest backend/tests/test_corpus_consistency.py` | ✔ 11 passed, 1 DB-skipped |
| Idempotent reseed (seed ×2) | ✔ 4 corpus rows, no dupes |
| figures.py coverage check (15 key tokens via string-in) | ✔ 0 missing |
| Doc spot-read (FAQ / How-To / Reference samples) | ✔ correct figures + clause cites throughout |
| Secret scan (5-check: entropy / API keys / credentials / emails / cred-URLs) | ✔ clean |

## Tests (`backend/tests/test_corpus_consistency.py`)

11 pure-Python tests (no DB / no LLM / no network) + 1 DB-gated idempotency test:

- **Structure:** all four doc types present; unit counts match authored (98/50/35/25);
  IDs unique + namespaced per type.
- **Presence:** 5 parametrized tests confirm `$85`, `$300`, `$25`, `$75`, `30 days` appear
  in each expected document.  Token derived from `figures.py` at test time (greps rendered
  prose, never compares constants to themselves).
- **Anti-contradiction:** per-unit scan for New York + per-diem and New York + lodging
  mentions; any such unit must carry the Tier-1 canonical figure.
- **Stub-leak guard:** `"$250"` (legacy stub's Tier-1 cap) must not appear in any new document.
- **DB-gated idempotency:** `seed()` ×2 → `{policy:1, faq:1, howto:1, reference:1}`,
  self-skips when Postgres/migration-0008 not available.

## Key decisions

1. **Additive enum column, no new table.** `policies` already holds multiple docs per org.
   A nullable type discriminator is the minimum schema change (Fence 7 / Masterplan §11).
2. **Stub kept additive.** Legacy 8-clause stub remains `is_active=True` and its embeddings
   continue to serve retrieval through R0-T2/T3.  Retired at R0-T4.
3. **`name` reused as title.** No new `title` column; `(org_id, name, version)` uniqueness
   already distinguishes documents.
4. **figures.py SSOT.** All four documents `import figures as F`; numeric consistency is
   structurally enforced, not tested into existence.
5. **Receipt inbox address** (`HOW-02`) — `receipts@heliosdynamics.com` is a plausible
   product-level inbox that does not contradict any figure; left as a judgment-call detail,
   not added to `figures.py`.

## ⚠ Forward-notes for R0-T3 / R0-T4

- **Chunking (R0-T3):** `CorpusUnit` objects are already the natural chunk boundary.
  R0-T3 iterates `doc.units` to produce `(clause_ref=unit.id, chunk_text=unit.text,
  metadata={document_type, section})` rows in `policy_embeddings`.
- **Stub retirement (R0-T4 — critical):** retrieval SQL (`ai_agents/tools/policy_retrieval.py`)
  JOINs on `organization_id` only and does **NOT filter `policies.is_active`**.  Setting
  `is_active=False` on the stub will NOT remove its embeddings from retrieval.  R0-T4 must
  **physically DELETE** the stub's `policy_embeddings` rows (cleanest: cascade-delete via
  `DELETE FROM policies WHERE document_type IS NULL AND organization_id = :org_id`).  See
  [R0-T1 as-built](R0-T1_asbuilt.md) for the SQL path detail.
- **1536-d invariant:** `EMBEDDING_DIMENSION=1536` must remain unchanged through R0-T4.
