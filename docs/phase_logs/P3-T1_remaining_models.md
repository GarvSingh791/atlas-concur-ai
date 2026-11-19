# P3-T1 — Remaining Models in Dependency Order (+ Migration 0004, pgvector)

**Sub-phase:** P3-T1
**Date:** 2026-06-12
**Author:** Garv Singh Saini

---

## What Was Built

### Eleven ORM Models (FK Dependency Order)

One module per entity under `database/models/`:

| Order | Model | File | Tables |
|---|---|---|---|
| 1 | `Policy` | `policy.py` | policies |
| 2 | `PolicyRule` | `policy_rule.py` | policy_rules |
| 3 | `PolicyEmbedding` | `policy_embedding.py` | policy_embeddings |
| 4 | `CardTransaction` | `card_transaction.py` | card_transactions |
| 5 | `CalendarEvent` | `calendar_event.py` | calendar_events |
| 6 | `Receipt` | `receipt.py` | receipts |
| 7 | `Approval` | `approval.py` | approvals |
| 8 | `ApprovalStep` | `approval_step.py` | approval_steps |
| 9 | `Conversation` | `conversation.py` | conversations |
| 10 | `ConversationMessage` | `conversation_message.py` | conversation_messages |
| 11 | `AiDecisionLog` | `ai_decision_log.py` | ai_decision_logs |

Thirteen new Python enums co-located with their models (all `class X(str, enum.Enum)` per codebase convention): `PolicyRuleType`, `PolicyAppliesTo`, `PolicyRuleStatus`, `OcrStatus`, `ProcessingPath`, `ApprovalSubjectType`, `ApprovalStatus`, `ApprovalDecision`, `ConversationContextType`, `MessageRole`, `AiAgent`, `AiRoute`, `ReflectionOutcome`.

All eleven models and all thirteen enums re-exported from `database/models/__init__.py` so Alembic's side-effect import (`import database.models`) registers them and `configure_mappers()` resolves the full graph.

### Migration 0004 (`database/migrations/versions/0004_remaining_models.py`)

Single incremental Alembic revision chained on 0003 (`down_revision = "0003"`). Contents:

- `CREATE EXTENSION IF NOT EXISTS vector` (idempotent pgvector enablement).
- Thirteen `postgresql.ENUM(..., create_type=False)` objects created with `.create(bind, checkfirst=True)` before the first table that references them (matching the established pattern from 0001–0003).
- Eleven `op.create_table(...)` calls in FK dependency order, with all structural constraints: PK, FK (CASCADE on true children, RESTRICT on ownership refs, SET NULL on optional links), and the `policies(organization_id, name, version)` unique constraint.
- `Vector(EMBEDDING_DIMENSION)` column in `policy_embeddings` (imported from `pgvector.sqlalchemy`; dimension imported from `config.constants.embeddings`).
- No `op.create_index` calls — that is the P3-T3 indexing-strategy pass.
- Downgrade drops all eleven tables in reverse dependency order, then drops all thirteen enum types with `.drop(bind, checkfirst=True)`. The `vector` extension is **not** dropped (see Key Decisions).

### EMBEDDING_DIMENSION Constant (`config/constants/embeddings.py`)

```python
EMBEDDING_DIMENSION: int = 1536
```

New standalone module mirroring the `config/constants/permissions.py` style — a module-level integer constant, not a `Settings` field, so no new required environment variable is introduced. Both the ORM model and the migration import it from this single source of truth.

### Dependency Addition (`backend/requirements.txt`)

`pgvector>=0.3,<0.4` added under the Database section. The PostgreSQL image (`pgvector/pgvector:pg16`) already ships with the extension; the Python package provides the `Vector` SQLAlchemy type used in `PolicyEmbedding.embedding`.

### Tests

- `backend/tests/test_p3_mappers.py` — **hermetic** (no `db` marker): imports all models, calls `configure_mappers()`, verifies all 21 ORM models (10 existing + 11 new) configure without error. Runs in hermetic CI.
- `backend/tests/test_p3_models.py` — **live-DB** (`pytestmark = pytest.mark.db`): module-scoped self-skipping `db_engine` fixture (same pattern as `test_audit.py`); 18 test cases covering:
  - Round-trip create/read for each new model.
  - Vector value persists and reads back at the correct dimension (1536).
  - JSONB columns round-trip faithfully.
  - Cascade: policy→rules, policy→embeddings, approval→steps, conversation→messages.
  - Polymorphic `approvals.subject_id` with orphan UUID persists (app-layer integrity confirmed).
  - RESTRICT: deleting a user referenced by `card_transactions`/`receipts` raises `IntegrityError`.
  - Unique violation on `policies(organization_id, name, version)`.
  - Structural inspection: `policy_rules` has no `organization_id`; `approvals` has no `created_by`/`updated_by`; the five created_at-only tables have no `updated_at`.

---

## Key Decisions

### Non-Uniform Mixin Application

Mixins applied exactly per the entity table spec in `DATABASE_SCHEMA.md`, not uniformly:

| Table | UUID PK | Timestamps | Tenant | Authorship | Version | Note |
|---|---|---|---|---|---|---|
| policies | ✓ | ✓ | ✓ | ✓ | — | version = domain Integer (see below) |
| policy_rules | ✓ | ✓ | — | ✓ | — | No org scope; scoped via policy_id CASCADE FK |
| policy_embeddings | ✓ | created_at only | — | — | — | Append-only RAG index |
| card_transactions | ✓ | ✓ | ✓ | — | — | No authorship/version/soft-delete |
| calendar_events | ✓ | ✓ | ✓ | — | — | System-seeded grounding data |
| receipts | ✓ | ✓ | ✓ | — | — | uploaded_by is an explicit FK, not the mixin pair |
| approvals | ✓ | ✓ | ✓ | — | ✓ | No authorship; version = optimistic lock |
| approval_steps | ✓ | created_at only | — | — | — | Append-only decision record |
| conversations | ✓ | ✓ | ✓ | — | — | No authorship |
| conversation_messages | ✓ | created_at only | — | — | — | Append-only message log |
| ai_decision_logs | ✓ | created_at only | ✓ | — | — | Append-only audit/eval log |

**`policies.version` as a domain Integer:** `DATABASE_SCHEMA.md` describes this as a document version number (auditable versioning of the policy text), not an optimistic-lock concurrency counter. The `VersionMixin` (which wires `server_default=1` and is intended for mapper-level locking in P3-T4) was deliberately not used. The column is a plain `Integer NOT NULL` with no server default — callers must supply the version when creating a policy, which is correct for explicit document versioning.

**`receipts.uploaded_by` as an explicit FK:** `DATABASE_SCHEMA.md` lists a single `uploaded_by` actor, not the `AuthorshipMixin`'s `created_by`/`updated_by` pair. The explicit column is used to avoid importing the mixin for a semantically different purpose.

### EMBEDDING_DIMENSION = 1536

Chosen to match the default embeddings provider in `.env.example`: OpenAI `text-embedding-3-small`, which produces 1536-dimensional vectors. The `policy_embeddings.embedding` column stores vectors; nothing generates embeddings yet — that is Phase 4 (Policy Agent / RAG pipeline). The constant is defined once in `config/constants/embeddings.py` and imported wherever the dimension must be referenced (model + migration), so changing the provider in a future phase requires editing a single line.

### Migration 0004 Downgrade Retains the pgvector Extension

Dropping a PostgreSQL extension in a downgrade migration is destructive: it cascades to all objects that depend on the extension (in any schema), is non-reversible without reinstallation, and is not idiomatic for per-revision rollbacks in a shared-database environment. The standard practice — followed here — is to enable extensions once and leave them in place. A comment in the downgrade function documents this decision for future maintainers.

### Polymorphic `approvals.subject_id` — No DB FK

`DATABASE_SCHEMA.md` explicitly documents this as a deliberate tradeoff: the `approvals` table can reference either a `trip` or an `expense_report`, and a single DB FK cannot express this duality without a union-table pattern. Referential integrity for the polymorphic link is enforced at the application layer (P3-T4). This keeps the workflow engine generic and the schema clean. The design is proven by a live-DB test that inserts an `Approval` row with a `subject_id` that has no matching row in any table and confirms it is accepted by the database.

---

## Authorized Adjustment

Migration 0004 was produced as a single incremental Alembic revision covering all eleven new tables, pulling forward the per-slice migration work originally scheduled for P3-T2. This is consistent with the incremental-per-slice pattern established in migrations 0001–0003. The ten Phase-2 models (`organizations`, `users`, `roles`, `permissions`, `role_permissions`, `audit_log`, `trips`, `trip_segments`, `expense_reports`, `expenses`) were not modified.

---

## Deviations from Design Docs

**1. Bare ownership FKs into User/Expense — no reverse collections on existing models.**
The task spec described `users 1:M card_transactions/calendar_events/receipts/conversations` relationships. However, the codebase convention — established in P2-T6/P2-T7 and confirmed by reading every existing model — is that user-ownership FKs are bare `mapped_column` declarations with no reverse `relationship()` on the `User` side (e.g. `Trip.traveler_id`, `Expense.trip_segment_id`). Bidirectional `back_populates` is wired only among true parent/child pairs (Org↔User, Trip↔TripSegment, ExpenseReport↔Expense). Following this convention avoids editing `identity.py` and `expense.py`, keeps the ownership FK semantics consistent, and matches the established precedent. Flagged as a deviation; confirmed at the planning review.

**2. `pgvector>=0.3,<0.4` added to `backend/requirements.txt`.**
The `pgvector/pgvector:pg16` Docker image already ships with the Postgres extension, but the Python package that provides the `Vector` SQLAlchemy column type was not previously installed. This is a dependency addition (not a new required environment variable) — `EMBEDDING_DIMENSION` is a code constant, not a `Settings` field. The addition is scoped to the database section of `backend/requirements.txt`.

---

## Issues Found and Fixed in QA

1. **Ruff I001 (import ordering) in 10 files** — resolved with `ruff check --fix`. All new model files had `from sqlalchemy import Enum as SAEnum` placed after other `sqlalchemy` imports; isort reordered them.
2. **F401 unused imports in `policy.py`** (`uuid`, `ForeignKey`, `UUID`) — removed by `ruff --fix` after the `policies` model was refactored to use AuthorshipMixin/TenantMixin which already carry those types.
3. **E501 line too long in `test_p3_models.py`** — inline dict extracted to a local variable to bring the assertion under the 100-character limit.
4. **mypy `arg-type` on `len(fetched.embedding)`** — pgvector's `Vector` return type is `object` in mypy's view; suppressed with `# type: ignore[arg-type]` (the runtime type is a list-like with `__len__`).
5. **mypy `arg-type` on `float(fetched.confidence)`** — `Numeric(4,3)` maps to `object` in the ORM type system; cast via `str()` first (`float(str(fetched.confidence))`).
6. **Test teardown FK violation** — `test_conversation_and_messages_round_trip` did not delete its `Conversation` row before the module-scoped `seed_base` fixture teardown attempted to delete the shared `User`, triggering a FK violation on `fk_conversations_user_id_users`. Fixed by adding explicit delete of the conversation and its messages inside the test body.

---

## Validation Evidence

| Check | Result |
|---|---|
| `alembic upgrade head` (0003 → 0004) | Clean |
| `alembic downgrade -1` then `upgrade head` | Clean, repeatable |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 74 files already formatted |
| `mypy backend config` | Success: no issues found in 49 source files |
| `pytest` (159 tests, live Postgres) | 159 passed |
| `GET /health` after uvicorn boot | 200 OK |
| Wrong-dimension vector insert | Rejected (`StatementError`) |
| All 11 new tables present in DB | Confirmed |
| `policy_rules` has no `organization_id` | Confirmed |
| `approvals` has no `created_by`/`updated_by` | Confirmed |
| Five created_at-only tables have no `updated_at` | Confirmed |
