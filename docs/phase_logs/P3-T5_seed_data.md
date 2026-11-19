# P3-T5 — Seed Data

**Sub-phase:** P3-T5  
**Title:** Seed Data (Phase 3 Closer)  
**Date:** 2026-06-12  
**Author:** Garv Singh Saini

---

## What Was Seeded

Single idempotent command: `python -m database.seeds.seed_all`  
(Run after `alembic upgrade head`.)

**Identity**
- One organization: `Atlas Demo Corp` (base_currency `USD`).
- Three users, all bcrypt-hashed via `hash_password` (never plaintext in DB):
  - `manager@atlas-demo.internal` — role `manager`, no manager_id (top-level)
  - `employee@atlas-demo.internal` — role `employee`, manager_id → manager
  - `finance@atlas-demo.internal` — role `finance_admin`, manager_id → manager
- RBAC catalog (roles / permissions / role_permissions) seeded add-only via the
  existing identity_seed; not duplicated.

**Policy**
- One policy document: `"Atlas Global Travel & Expense Policy"`, version 1,
  `is_active=true`, `effective_from=2026-01-01`, created_by/updated_by = finance_admin.
- 8 clause chunks → 8 `policy_embeddings` rows (`chunk_text` + `clause_ref`):

> **Historical note (2026-11-15).** This log records the June 2026 seed. Migration
> `0015_canonical_policy_figures` later rewrote the HOTEL-CAP and MEALS-PD rules to the
> published Helios TEP-FY26 figures: hotel **Tier 1 (incl. New York) $300/night**,
> Tier 2 $225, Tier 3 $160 (LOD-01); meals $85 / $75 / $65 per day (MEAL-01). The
> $250 New York cap and the $180 domestic figure below are the retired stub values.
> Migrations 0017–0019 and 0021 added the later policy rules (see the phase 9 and
> phase 10 logs).

| clause_ref   | Topic                                               |
|--------------|-----------------------------------------------------|
| `AIR-CABIN`  | Cabin class by flight duration (Economy/PE/Business)|
| `AIR-PREF`   | Preferred carriers (UA/DL/LH) within 10% fare      |
| `HOTEL-CAP`  | Nightly rate caps by city tier (USD 180–300)        |
| `HOTEL-PREF` | Preferred hotel brands (Marriott, Hilton)           |
| `MEALS-PD`   | Per diem USD 75 domestic / USD 100 international   |
| `GROUND-CLASS`| Rental car class (Economy/Standard only)           |
| `RECEIPT-REQ`| Receipt required ≥ USD 25; always for airfare/lodging|
| `SUSTAIN`    | Sustainability guidance (no structured rule — intentional) |

- 7 `policy_rules` rows (`status='active'`, each with `source_clause_ref` → a real
  clause above). The `SUSTAIN` clause has no structured rule by design — see Key
  Decisions.

**Mock Travel Inventory** (`services/mock_inventory/`)
- Static `FLIGHTS` (8 entries) and `HOTELS` (7 entries) datasets with varied price,
  cabin class, supplier, and sustainability score covering SFO↔JFK, SFO→LHR,
  JFK→ORD routes.
- Includes at least one out-of-policy option (business-class <6h flight; $400/night
  NYC hotel exceeding the $250 Tier-1 cap) for later policy in/out testing.
- Query helpers: `search_flights(origin, destination, date)` and
  `search_hotels(city, check_in, check_out)` — data helpers only, not the Phase-4
  Inventory agent tool.

**Grounding Data (aligned to sample trip)**
- One sample trip: `SFO → JFK`, 2026-06-10 to 2026-06-14, `status=completed`,
  `recommendation_context=NULL`, employee as traveler.
- Two `trip_segment` rows (flight + hotel); `in_policy / policy_notes = NULL`
  (policy-eval outputs, not fabricated).
- 3 `card_transactions`: airline ($320), hotel ($956), meal ($38.50), all
  `matched=false`, `expense_id=NULL`.
- 2 `calendar_events`: Q2 Engineering Summit Day 1 and Day 2, NYC.

**Receipts**
- 3 `receipts` rows: `uploaded_by=employee`, `ocr_status='pending'`,
  `processing_path=NULL`, and all four AI JSONB columns
  (`extracted_fields / field_confidence / field_provenance / grounding_context`)
  explicitly `NULL` — pipeline inputs only; no fabricated outputs.
- `image_url` values reference keys in the dev MinIO bucket
  (`receipts/seed/employee-{airfare,hotel,meal}-001.png`).
- A minimal valid PNG (89-byte byte literal, no binaries committed) is uploaded to
  MinIO at seed time via a direct `boto3` client; the upload self-skips gracefully
  if `STORAGE_SECRET_ACCESS_KEY` is not set.

---

## Embeddings Decision (Flagged)

**Local deterministic embedder:** `database/seeds/_local_embedder.py`

Implements a feature-hashing trick over `numpy`:
1. Tokenize text (lowercase, split on non-alphanumeric).
2. For each token, derive a bucket index (0..1535) and sign (+1/−1) via two
   independent hashes.
3. Accumulate into a 1536-dim vector; L2-normalize.

Properties:
- **Deterministic:** same input → same vector, always.
- **Normalized:** unit L2 norm (matches cosine-space HNSW index).
- **Lexically similar:** shared tokens pull toward the same dimensions, so ANN
  queries return related clauses.
- **No provider, no API key, no network call at seed time.**

**FLAG:** Semantic quality is intentionally lexical-only. P4-T5 swaps the real
embeddings provider (e.g. `text-embedding-3-small`) and re-indexes
`policy_embeddings` idempotently. This helper is NOT the Phase-4
`EmbeddingsProvider` abstraction.

---

## Key Decisions

**1. Grounding coherence via a minimal sample trip**  
A completed SFO→JFK trip anchors the card transactions, calendar events, and receipts
so future agents have realistic, internally-consistent grounding context. No AI-derived
trip fields are populated: `recommendation_context=NULL`, `in_policy=NULL`,
`policy_notes=NULL`. The Phase-2 CRUD checkpoint test creates its own independent trip.

**2. Idempotency strategy**  
Guard-if-exists on stable natural keys per block:
- RBAC catalog: add-only upsert (`upsert_roles` / `upsert_permissions` / `sync_role_permissions`).
- Org + users: org-name guard ("Atlas Demo Corp"); skips entire block if present.
- Policy: guarded by `(org_id, name, version)` tuple.
- Trip: guarded by `(traveler_id, origin, destination, start_date)`.
- Card transactions, calendar events, receipts: per-row guards on composite natural keys.

Re-running `seed_all` produces zero new rows and no constraint errors.

**3. SUSTAIN clause has no structured rule (intentional)**  
The sustainability guidance clause (`SUSTAIN`) does not derive a `policy_rules` row.
This demonstrates the "clause-without-rule / policy is silent" pattern: a clause exists
in the document and is retrievable via ANN search, but the policy evaluator has no
machine-evaluable rule for it. The Phase-4 Policy Agent will handle this by citing the
clause as guidance rather than a hard rule.

**4. boto3 in requirements-dev.txt**  
Seeding is a dev/ops task; production dependencies stay lean until Phase 4 (P4-T11)
formally introduces the storage layer. `boto3` is imported lazily inside
`_upload_receipt_images`; the step self-skips if boto3 is absent or MinIO is unreachable,
keeping the DB seed independent of storage availability.

---

## Deviation From Design Docs

**`_resync_user_passwords` added to `seed_all.py`**

Not in the original P3-T5 plan. Added to fix a stale bcrypt hash issue discovered
during QA: the P2-T3 identity seed was run under a different `SEED_DEFAULT_PASSWORD`
value in the dev environment, producing password hashes that did not match the current
`.env` value. Auth tests failed with 401 until the resync step was added.

**ROOT CAUSE:** Two seed runs disagreed on the dev password env var. The guard that
skips the org+users block on re-run (`if existing_org: return`) also skips re-hashing,
so stale hashes silently persist. The resync step calls `verify_password` and only
re-hashes when a mismatch is detected — idempotent no-op in the normal case.

This is not core seed logic; it is a reconciliation safeguard for dev environments
where the password env var may have changed between runs.

---

## Issues Found and Fixed in QA

| # | Issue | Fix |
|---|-------|-----|
| 1 | ruff E501 line-length in `policy_content.py` (2 lines > 100 chars) | Split long string literals across lines |
| 2 | ruff E501 in `seed_all.py` — import line > 100 chars | Split `PolicyRuleType` import into multi-line block |
| 3 | ruff I001 unsorted import block in `seed_all.py` | Reorganized import order to satisfy isort |
| 4 | ruff F401 unused `field` import in `dataset.py` | Removed unused import |
| 5 | mypy: `norm` inferred as `np.floating[Any]`, not `float` | Added `float()` cast: `norm: float = float(np.linalg.norm(vec))` |
| 6 | mypy: `list(emb.embedding)` — pgvector column typed as `object` | Added `# type: ignore[arg-type, call-overload]` in test |
| 7 | Auth tests 401 — stale P2-T3 bcrypt hashes in dev DB | Added `_resync_user_passwords` to `seed_all.py` (see Deviation above) |

---

## Validation Evidence

| Check | Result |
|-------|--------|
| Seed runs clean on freshly migrated DB | ✔ All entities present; dependency order correct |
| Seed idempotent (run ×3) | ✔ Row counts identical across all runs; no duplicate-key errors |
| `policy_embeddings` count | ✔ 8 rows; each `embedding` is VECTOR(1536); each has `clause_ref` |
| ANN cosine query (`ORDER BY embedding <=> :q LIMIT 3`) | ✔ Returns ≥1 clause with `clause_ref` (HNSW path end-to-end) |
| Policy rules consistency | ✔ 7 rules; every `source_clause_ref` resolves to a real seeded clause |
| Seeded login | ✔ POST /auth/login → token; GET /me → user; bad password → standard 401 |
| Phase-2 CRUD checkpoint | ✔ Create trip as seeded employee → 201 draft |
| Mock inventory | ✔ `search_flights` and `search_hotels` return options; out-of-policy option confirmed |
| Receipts | ✔ 3 rows `ocr_status='pending'`; all AI JSONB columns NULL |
| Drift guard | ✔ Empty — no model changes; `test_full_chain_and_drift` passed |
| Migration chain | ✔ 0001→0006 clean; no migration 0007 created |
| Full test suite (live DB) | ✔ **209 passed, 1 skipped (MinIO — STORAGE_SECRET_ACCESS_KEY not set)** |
| Hermetic test suite (no DB) | ✔ 53 passed, 157 skipped (live-DB tests self-skip) |
| ruff check + ruff format --check | ✔ Clean |
| mypy backend config | ✔ Success: no issues found in 57 source files |

---

## Phase 3 Closure Statement

Phase 3 — Database Implementation is **complete**.

The full Atlas schema is migrated (`0001_initial_identity` through `0006_integrity_rules`),
indexed (38 indexes including the HNSW ANN index on `policy_embeddings.embedding` using
`vector_cosine_ops`, migration 0005), constrained with real integrity rules (migration 0006),
and now seeded with a realistic single-tenant dataset (P3-T5).

**Fresh clone path:**
```bash
alembic upgrade head              # migrations 0001→0006 (includes pgvector, HNSW)
python -m database.seeds.seed_all # org → users → policy+rules+embeddings → trip → grounding → receipts
# App ready for end-to-end CRUD as a seeded user role
```

Phase 3 DoD: full schema migrated, indexed, constrained, and seeded; all Phase-2 CRUD
runs against real tables. ✔

Phase 3 Checkpoint: a fresh clone can migrate → seed → run end-to-end CRUD. ✔

**Next: Phase 4 — Intelligence Plane** (orchestrator, four agents, RAG policy engine,
receipt gap-fill pipeline).
