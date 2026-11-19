# 0007 — Optimistic concurrency (version CAS) and idempotency keys

- **Status:** Accepted
- **Date:** 2026-06-12 (phases P2-T7 and P3-T4, migration 0006); extended to approvals, lifecycle actions, receipt upload and mileage later
- **Decider:** Garv Singh Saini

## Context

Two failure modes matter in an approval workflow:

1. **Lost updates.** An employee edits a report while a manager decides on it, or two tabs submit the same report. Without a check, the last writer silently wins, and an approver can approve content that changed after they read it.
2. **Duplicate writes on retry.** Mobile networks drop responses. A client that retries "create report", "upload receipt" or "add mileage" must not create two rows; a duplicate expense line is money paid twice.

The design called for both (NFR-R2, NFR-R3). The question was how to implement them without holding database locks across user think-time.

## Decision

**Optimistic concurrency with a version compare-and-swap.**

- `trips`, `expense_reports` and `approvals` carry `version INTEGER` (`VersionMixin`).
- Every status transition and guarded update goes through `backend/app/core/concurrency.py::guarded_version_update`: one `UPDATE … SET …, version = version + 1 WHERE id = :id AND version = :expected RETURNING version`. No row returned means someone else changed it first: `409 Conflict`.
- Clients send the version they last read in the `If-Match` header (`if_match_version` dependency) on expense-report, entry, approval-decision, receipt-attach and lifecycle endpoints. Without the header the server uses the version it just loaded, which still catches a concurrent writer between read and update. Trip transitions use the loaded version.
- Where a decision must be made against stable data, the subject row is also locked for the short decision step (for example `decide_approval` loads the subject `FOR UPDATE` before applying the outcome).

**Idempotency keys for creates and uploads.**

- Clients may send `Idempotency-Key`. The server stores `(organization_id, scope, idempotency_key) → entity_id` in `idempotency_keys` (migration 0006), with a unique constraint across the three columns.
- The key row is written in the **same transaction** as the entity and its audit row. A retry finds the key and returns the original entity (replay), with no second write.
- Two concurrent requests with the same key: the second insert hits the unique constraint (PostgreSQL `23505`) and becomes a 409; the client's next retry takes the replay path.
- Keys are organisation-scoped; a key first used by another user never replays, or discloses, that user's record.
- Scopes in use: `expense_report.create`, `receipt.upload`, `conversation.message`, `expense.add_mileage`. Adding mileage **requires** either `If-Match` or an idempotency key (428 without either), so a double submit cannot add the same trip twice.

## Alternatives considered

| Option | Why not |
|---|---|
| Pessimistic locks held while a user edits | Locks would span human think-time and HTTP requests; impossible with a stateless API and risky with a pooled database. |
| Last-writer-wins with `updated_at` | Silent data loss; an approver could approve content that changed after they read it. |
| ETag hashes of the full representation | Works, but integer versions are simpler to reason about, cheap to compare in SQL, and readable in audit snapshots. |
| Deduplicate by content (same amount, date, vendor) | Legitimate repeated expenses exist (two taxi rides at the same price). Content matching is used only as an advisory duplicate flag, never to drop a write. |

## Consequences

**Positive**

- No lost updates on the workflow-bearing records, and no long-held locks.
- Safe retries for the operations most likely to be retried, including the expensive receipt upload (OCR is not re-run on a replay).
- Both mechanisms are plain SQL and easy to test without timing tricks.

**Negative / trade-offs**

- Clients must handle 409 by reloading and retrying; the frontend sends `If-Match` from the version it last read.
- `idempotency_keys.entity_id` has no foreign key (it is polymorphic), so integrity relies on the application. Keys do not expire on their own; the demo reset clears the demo organisation's keys.
- Expense entries themselves have no version column; edits to entries are guarded through the parent report's version.

## Evidence

- Code: [`backend/app/core/concurrency.py`](../../backend/app/core/concurrency.py), [`backend/app/core/idempotency.py`](../../backend/app/core/idempotency.py), [`database/models/idempotency_key.py`](../../database/models/idempotency_key.py), migration `database/migrations/versions/0006_integrity_rules.py`.
- Tests: `backend/tests/unit/test_unit_concurrency_idempotency.py`, `backend/tests/api/test_idempotency.py`, `backend/tests/unit/test_unit_approval_determinism.py::test_decide_stale_version_raises_conflict`, `backend/tests/test_c7r_18_patch_draft_race_db.py`.
- Phase logs: [`P2-T7_expenses_reports_crud.md`](../phase_logs/P2-T7_expenses_reports_crud.md), [`P3-T4_integrity_rules.md`](../phase_logs/P3-T4_integrity_rules.md).
