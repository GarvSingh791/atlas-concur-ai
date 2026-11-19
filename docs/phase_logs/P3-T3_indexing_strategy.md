# Phase Summary

Phase:       Phase 3 — Database Implementation
Sub-Phase:   P3-T3 — Indexing Strategy Pass (migration 0005)
Date:        2026-06-12
Author: Garv Singh Saini

## Objective

Realize the full DATABASE_SCHEMA.md Indexing Strategy section as a delta over
migrations 0001–0004. Produce one index-only migration (`0005`) chained on 0004,
with every index declared model-first in `__table_args__` so the P3-T2 drift guard
(`compare_metadata`, scoped to `EXPECTED_TABLES`) remains empty. No table shapes,
columns, enums, or data were touched.

## What Was Built

### Migration 0005 — index-only, chained on 0004

`database/migrations/versions/0005_indexing_strategy.py` — adds 38 indexes in
`upgrade()` and drops exactly those 38 in `downgrade()`, in reverse order. No
`create_table`, `add_column`, `create_type`, or `op.execute(INSERT/UPDATE/DELETE)`
calls appear anywhere in 0005.

### Model-first index declarations

Every index that 0005 creates is also declared on the owning model's
`__table_args__` using `sa.Index` with an explicit `name=`. This is the invariant
that keeps the drift guard empty: Alembic's `compare_metadata` sees no difference
between the migration-produced schema and the ORM models.

Files modified (index declarations added or extended):

| File | Change |
|---|---|
| `database/models/identity.py` | `ix_users_manager_id`; `ix_role_permissions_permission_id` |
| `database/models/audit_log.py` | `ix_audit_log_organization_id`, `ix_audit_log_actor_id` (composite from 0001 kept) |
| `database/models/trip.py` | `ix_trips_traveler_id_status`, `ix_trips_start_date`, `ix_trips_organization_id` |
| `database/models/trip_segment.py` | `ix_trip_segments_trip_id` |
| `database/models/expense_report.py` | `ix_expense_reports_owner_id_status`, `ix_expense_reports_organization_id`, `ix_expense_reports_trip_id` |
| `database/models/expense.py` | `ix_expenses_report_id_status`, `ix_expenses_organization_id`, `ix_expenses_trip_segment_id` |
| `database/models/approval.py` | `ix_approvals_organization_id_current_status`, `ix_approvals_submitter_id`, `ix_approvals_org_pending` (partial) |
| `database/models/approval_step.py` | `ix_approval_steps_approval_id`, `ix_approval_steps_approver_id` |
| `database/models/receipt.py` | `ix_receipts_extracted_fields` (GIN), `ix_receipts_org_ocr_pending` (partial), `ix_receipts_organization_id`, `ix_receipts_uploaded_by`, `ix_receipts_expense_id` |
| `database/models/policy_rule.py` | `ix_policy_rules_definition` (GIN), `ix_policy_rules_policy_id` |
| `database/models/policy_embedding.py` | `ix_policy_embeddings_policy_id`, `ix_policy_embeddings_embedding_hnsw` (HNSW) |
| `database/models/card_transaction.py` | `ix_card_transactions_organization_id`, `ix_card_transactions_user_id`, `ix_card_transactions_expense_id` |
| `database/models/calendar_event.py` | `ix_calendar_events_organization_id`, `ix_calendar_events_user_id` |
| `database/models/conversation.py` | `ix_conversations_organization_id`, `ix_conversations_user_id` |
| `database/models/conversation_message.py` | `ix_conversation_messages_conversation_id` |
| `database/models/ai_decision_log.py` | `ix_ai_decision_logs_organization_id`, `ix_ai_decision_logs_user_id` |

### Test module

`backend/tests/test_p3_indexes.py` — live-DB test that:
1. Creates a throwaway database (`atlas_index_check`), applies `alembic upgrade head`, and cleans up on teardown.
2. Asserts all 39 strategy indexes (38 from 0005 + 1 from 0001) exist in `pg_indexes` by exact name.
3. Asserts no near-duplicate consolidated indexes are present.
4. Runs 12 EXPLAIN plan-shape checks (one per index family) using `SET LOCAL enable_seqscan=off` and FORMAT JSON plan-tree walking — confirming the planner CAN use each index even on empty tables.
5. Runs 2 negative partial-index checks — confirming a non-pending status value does NOT use the partial approvals or receipts index.
6. Asserts pgvector extension version ≥ 0.5.0 (HNSW availability proof).

Self-skips when Postgres is unreachable; hermetic CI stays green.

## Index Inventory

### Already existed before 0005 (created by 0001)

| Index | Table | Columns | Type |
|---|---|---|---|
| `ix_audit_log_entity_type_entity_id_created_at` | audit_log | (entity_type, entity_id, created_at) | composite |

### Added by migration 0005 (38 total)

**FK indexes — 28**

| Index | Table | Column |
|---|---|---|
| `ix_users_manager_id` | users | manager_id |
| `ix_role_permissions_permission_id` | role_permissions | permission_id |
| `ix_audit_log_organization_id` | audit_log | organization_id |
| `ix_audit_log_actor_id` | audit_log | actor_id |
| `ix_trips_organization_id` | trips | organization_id |
| `ix_trip_segments_trip_id` | trip_segments | trip_id |
| `ix_expense_reports_organization_id` | expense_reports | organization_id |
| `ix_expense_reports_trip_id` | expense_reports | trip_id |
| `ix_expenses_organization_id` | expenses | organization_id |
| `ix_expenses_trip_segment_id` | expenses | trip_segment_id |
| `ix_approvals_submitter_id` | approvals | submitter_id |
| `ix_approval_steps_approval_id` | approval_steps | approval_id |
| `ix_approval_steps_approver_id` | approval_steps | approver_id |
| `ix_receipts_organization_id` | receipts | organization_id |
| `ix_receipts_uploaded_by` | receipts | uploaded_by |
| `ix_receipts_expense_id` | receipts | expense_id |
| `ix_policy_rules_policy_id` | policy_rules | policy_id |
| `ix_policy_embeddings_policy_id` | policy_embeddings | policy_id |
| `ix_card_transactions_organization_id` | card_transactions | organization_id |
| `ix_card_transactions_user_id` | card_transactions | user_id |
| `ix_card_transactions_expense_id` | card_transactions | expense_id |
| `ix_calendar_events_organization_id` | calendar_events | organization_id |
| `ix_calendar_events_user_id` | calendar_events | user_id |
| `ix_conversations_organization_id` | conversations | organization_id |
| `ix_conversations_user_id` | conversations | user_id |
| `ix_conversation_messages_conversation_id` | conversation_messages | conversation_id |
| `ix_ai_decision_logs_organization_id` | ai_decision_logs | organization_id |
| `ix_ai_decision_logs_user_id` | ai_decision_logs | user_id |

**Named composites — 5**

| Index | Table | Columns | Access pattern |
|---|---|---|---|
| `ix_expense_reports_owner_id_status` | expense_reports | (owner_id, status) | "my reports by status" |
| `ix_approvals_organization_id_current_status` | approvals | (organization_id, current_status) | org-wide approval queues |
| `ix_trips_traveler_id_status` | trips | (traveler_id, status) | trip list by status |
| `ix_trips_start_date` | trips | (start_date) | date-range trip queries |
| `ix_expenses_report_id_status` | expenses | (report_id, status) | report detail / expense filter |

**JSONB GIN — 2**

| Index | Table | Column | Use |
|---|---|---|---|
| `ix_receipts_extracted_fields` | receipts | extracted_fields | `@>` containment on OCR output |
| `ix_policy_rules_definition` | policy_rules | definition | `@>` containment on rule bodies |

**Partial — 2**

| Index | Table | Leading column | Predicate |
|---|---|---|---|
| `ix_approvals_org_pending` | approvals | organization_id | `current_status = 'pending'::approval_status` |
| `ix_receipts_org_ocr_pending` | receipts | organization_id | `ocr_status = 'pending'::ocr_status` |

**HNSW ANN — 1**

| Index | Table | Column | Type / opclass | Build params |
|---|---|---|---|---|
| `ix_policy_embeddings_embedding_hnsw` | policy_embeddings | embedding | HNSW / vector_cosine_ops | default (m=16, ef_construction=64) |

## Key Decisions

### Tenant-prefix rule (resolving the doc tension)

DATABASE_SCHEMA.md states "lead composite indexes with `organization_id`" but also
lists composites that lead with selective FK columns. Resolved consistently:

> **Lead a composite with `organization_id` only where no more-selective
> tenant-bound column already leads it.**

A foreign key to a tenant-owned entity (`owner_id`, `traveler_id`, `report_id`)
implies exactly one organization. Prefixing `organization_id` would add a redundant
leading column and produce a wider index with no selectivity benefit. The approvals
queue naturally leads with `organization_id` because there is no single-owner FK —
managers approve across the org. All composites match the doc's listed shapes.

### ANN index: HNSW + vector_cosine_ops

- **HNSW over IVFFlat**: HNSW requires no training rows — correct for an empty,
  incrementally-populated table. IVFFlat requires a representative row set at
  `CREATE INDEX` time; an empty table produces a degenerate index.
- **vector_cosine_ops**: matches the cosine-space embeddings produced by
  `text-embedding-3-small` (OpenAI, 1536D), the provider anticipated by
  `EMBEDDING_DIMENSION = 1536`.
- **Default build params** (`m=16`, `ef_construction=64`): no micro-tuning at
  this scale. Note intentionally in the migration docstring.

### Partial-index predicate form

Model `postgresql_where` text uses the `::enum_type` cast
(e.g. `current_status = 'pending'::approval_status`) to match the predicate form
Postgres stores in `pg_indexes.indexdef`. Using the bare string `'pending'` without
the cast risks Alembic's `compare_metadata` detecting a predicate mismatch and
producing false drift on re-inspection.

### Redundant-index consolidations (no near-duplicates shipped)

| Excluded single-column index | Reason |
|---|---|
| `ix_users_organization_id` | org_id is the leading column of `uq_users_organization_id_email` |
| `ix_policies_organization_id` | org_id is the leading column of `uq_policies_organization_id_name_version` |
| `ix_expense_reports_owner_id` | owner_id leads `ix_expense_reports_owner_id_status` |
| `ix_trips_traveler_id` | traveler_id leads `ix_trips_traveler_id_status` |
| `ix_expenses_report_id` | report_id leads `ix_expenses_report_id_status` |
| `ix_approvals_organization_id` | org_id leads `ix_approvals_organization_id_current_status` |

### EXPLAIN verification strategy

Empty tables always seq-scan regardless of indexes (cost is near-zero).
`SET LOCAL enable_seqscan=off` makes the planner treat seq scans as prohibitively
expensive, forcing it to use available indexes. Combined with `EXPLAIN (FORMAT JSON)`
and recursive plan-tree walking this proves the planner CAN use each index — without
inserting permanent data (P3-T5 responsibility).

## Deviation from Design Docs

**Authorship FKs (`created_by` / `updated_by`) not indexed.**

DATABASE_SCHEMA.md's Indexing Strategy prose says "index every FK column" but its
own illustrative example list omits `created_by` and `updated_by`. No PRD access
pattern or dashboard query filters on these columns — they exist for audit lineage,
not as query predicates. Indexing them would add ~12 indexes (one per table × 2
columns) with no demonstrated query benefit, at a write-cost tax on every
insert/update to those tables.

Decision: exclude. If an "audit by author" query pattern emerges, the fix is a
two-line incremental migration. Confirmed explicitly with the project owner.

## Tests Added

| Test module | Type | Coverage |
|---|---|---|
| `backend/tests/test_p3_indexes.py` | Live-DB (self-skips) | pg_indexes presence (38+1), no-near-duplicate assertion, 12 EXPLAIN plan-shape checks, 2 negative partial checks, pgvector version ≥ 0.5.0 |
| `backend/tests/test_migration_chain.py` | Live-DB (re-run, not modified) | Full chain 0001→0005 up / downgrade base / re-up; drift guard empty |

## Issues Found and Fixed During QA

1. **Import sort in `receipt.py`** — `sqlalchemy.sql.text` appeared after
   `sqlalchemy.orm`, violating the project's import ordering convention. Fixed by
   moving `text` import to the correct position. ruff I001.

2. **EXPLAIN helper double-deserialize** — `psycopg2` automatically deserializes
   PostgreSQL JSON columns into Python lists. The initial `_explain_json` helper
   called `json.loads()` on a value already parsed into a Python list, raising
   `TypeError`. Fixed to accept both a Python list and a raw JSON string.

3. **ruff lint in test file** — f-string without any placeholders (F541) on an
   assertion message; seven SQL strings exceeding the 100-character line limit
   (E501). Fixed by removing the spurious `f` prefix and wrapping long SQL strings
   with string concatenation.

## Validation Evidence

| Check | Result |
|---|---|
| `alembic upgrade head` (0001→0005) | ✔ clean |
| `alembic downgrade base` | ✔ clean — zero Atlas tables, zero enum types |
| `alembic upgrade head` (re-apply) | ✔ clean |
| `alembic downgrade -1` from head (0005→0004) | ✔ all 38 indexes dropped, nothing else changed |
| `alembic upgrade head` again | ✔ clean |
| Drift guard (`compare_metadata`) | ✔ empty diff |
| pg_indexes presence (exact names) | ✔ 39 indexes present, no near-duplicates |
| EXPLAIN plan-shape checks | ✔ 15/15 — FK, composite, GIN, partial (×2 positive + ×2 negative), HNSW |
| pgvector extension version | ✔ ≥ 0.5.0 (HNSW available) |
| Full test suite | ✔ 175/175 passed |
| ruff check | ✔ clean |
| ruff format | ✔ clean |
| mypy | ✔ 0 issues (51 source files) |
