# Phase 9 — Product gaps: report & trip lifecycle (gaps 1, 4, part of 8)

Track: `pg/lifecycle` · base `2fbe114` (alembic head `0015_canonical_policy_figures`) ·
migration `0016_report_trip_lifecycle`.

## What

| Gap | Problem | Built |
|---|---|---|
| 1 — simulated reimbursement | Nothing moved an expense report `approved → reimbursed` (PRD US-E5); "Avg Cycle Time" (US-F3 AC2) measured time-to-decision, not time-to-payment. | Finance-only `POST /expense-reports/{id}/reimburse` (+ `GET /reimbursements/queue`), new permission `expense.reimburse.org`, columns `reimbursed_at / reimbursed_by_id / reimbursement_reference / reimbursement_amount`; finance dashboard "Ready to reimburse" panel + "Mark reimbursed" on the report detail; owner sees "Reimbursed" + date + reference; "Avg Cycle Time" is now submitted → reimbursed, with a separate "Avg Time to Decision". |
| 4 — abandoned returned report | While an owner had a returned report/trip unresolved, the approval agent escalated every new submission (`_open_returned_subject_flags`), and a report with approval history can't be deleted — one forgotten return ended auto-approval for good. | Owner-only `POST /expense-reports/{id}/withdraw` and `POST /trips/{id}/withdraw` (columns `withdrawn_at / withdrawn_by_id / withdrawal_reason` on both tables). The agent ignores withdrawn subjects; withdrawn reports are read-only, hidden from the default list (`?withdrawn=true|any`), and their lines are released (movable) with reviewed receipts kept for audit. "Withdraw" button + confirm dialog, "Withdrawn" badge and filter in the UI. |
| 8 (part) — trips never completed | `approved → completed` was legal but nothing set it. | `POST /trips/{id}/complete` (traveler or finance, approved + ended only), the PATCH path got the same end-date guard, the idempotent sweep `database/seeds/complete_elapsed_trips.py` (wired as a non-fatal `repair` in both seeding branches of the backend entrypoint), "Mark completed" button, trip status filter incl. Completed / Cancelled / Withdrawn. |

New module `backend/app/modules/lifecycle/` (router / service / schemas) holds the
five endpoints so the hot `expense` / `travel` routers stay untouched; the router is
registered with one line in `backend/app/api/v1.py`.

## Why / key decisions

1. **Permission `expense.reimburse.org`** (catalog `resource.action.scope`, like
   `expense.read.org`), granted to `finance_admin` only by the identity seed AND by
   migration 0016 (`INSERT … ON CONFLICT DO NOTHING`) — staging boots with
   `SEED_ON_STARTUP=policy_corpus`, which never re-runs the identity seed.
2. **Reimburse rules (fail closed):** report must be `approved` AND its approval
   `approved` or `auto_approved` (missing / pending / returned / rejected → 409
   `APPROVAL_NOT_APPROVED`); a second call → 409 `ALREADY_REIMBURSED` with no new
   audit row; the caller's own report → 403 even for finance (SoD); the stored total
   must equal the sum of its lines and be > 0 (else 409
   `REIMBURSEMENT_TOTAL_MISMATCH`); If-Match honoured. Row lock + version CAS +
   one `expense_report.reimbursed` audit row (before/after, `simulated: true`,
   optional finance note) in one transaction. No `ai_decision_log` row: that table
   records AI decisions, and this is a human action.
3. **Reference** `REIMB-<yyyymmdd>-<first 12 hex of the report id>` — deterministic
   per report, unique index `uq_expense_reports_reimbursement_reference`. The note
   lives only in the audit row (no extra column).
4. **Reimbursed is terminal:** the existing draft-only guards already refused
   edit / delete / move / attach / rename; withdraw refuses it; and
   `decide_approval` refuses a reversal of an auto-approval once the report is
   reimbursed (400 `INVALID_SUBJECT_STATE`) — each covered by a test.
5. **Withdrawn = `draft` + `withdrawn_at`** (no new PG enum value: `ALTER TYPE ADD
   VALUE` cannot be used in the same transaction and has no symmetric downgrade).
   Because the status stays `draft`, every draft-only write path now also refuses a
   withdrawn report (`refuse_withdrawn` → 409 `REPORT_WITHDRAWN`): submit, rename,
   entry edit/delete, report delete, receipt attach (both the attach endpoint and
   the upload-to-entry path), receipt delete/correction sync, move INTO it; the
   duplicate-peer refresh, attachable-receipt list and draft-count recommendation
   skip it; `correction_locked_receipt_ids` treats it like a non-draft report.
6. **Returned** = draft whose approval is `changes_requested` or `rejected`
   (rejected-then-reopened). Never-submitted drafts (delete them instead), reports
   under review, approved, reimbursed and finally-rejected reports → 409
   `REPORT_NOT_RETURNED`. The approval and its steps are never touched.
7. **Released lines:** the move endpoint accepts a withdrawn report as SOURCE only
   (the `REPORT_UNDER_REVIEW` refusal is skipped for it). Laundering is handled at
   approval time instead:
   * reject snapshots (0014) still count in `_refiled_rejected_flags`;
   * the withdrawal audit row freezes the lines + approval status, and a withdrawn
     REJECTED report's lines are re-file references too — covers legacy reject
     steps that froze no snapshot;
   * request-changes snapshots of a WITHDRAWN subject are excluded (the spec's
     "must not be punished");
   * reviewed receipts of a released line get the `receipt.retained_for_audit`
     marker while still linked, so they stay uncorrectable (`RECEIPT_LOCKED`),
     undeletable (`RECEIPT_RETAINED` — `delete_receipt` now checks the marker for
     linked receipts too) and are detached-and-kept (never deleted) if the line is
     later deleted (`_discard_entry_evidence` honours the marker). Receipts attached
     after the return (never reviewed) stay free, as before.
8. **Trip withdrawal** reuses the cancel transition (`draft → cancelled`) through a
   dedicated audited endpoint (the ordinary PATCH cancel still refuses a returned
   trip with `SUBJECT_UNDER_REVIEW`); `trips.withdrawn_at` distinguishes it from a
   plain cancellation for the badge, the filter and the agent.
9. **Trip completion:** traveler or `finance_admin` (a manager reader → 403);
   approved only (completed is terminal → 409 `TRIP_NOT_APPROVED`), end date ≤
   today in UTC (409 `TRIP_NOT_ENDED`). The owner PATCH `approved → completed` got
   the same end-date guard so the rule cannot be bypassed — three existing tests
   that completed not-yet-ended trips were updated to use ended trips (intended
   behaviour change). The sweep completes approved trips whose `end_date` is more
   than one day past (grace for the traveler), one transaction per trip, audit row
   `trip.completed` with actor NULL (system) and `trigger: "sweep"`.
10. **Analytics:** `KpiMetric.value` is now nullable and carries an optional
    `description`; "Avg Cycle Time" = avg(reimbursed_at − submitted_at) over
    reimbursed reports, `null` when none (UI "—"); the old computation is kept,
    honestly renamed, as "Avg Time to Decision". The existing finance KPI test was
    updated (a null value is accepted only for the day-based averages).
11. **Contract registry** (`backend/tests/api/endpoints.py`) lists the 5 new routes;
    the live-route count assertion went 41 → 46.
12. **Withdrawn receipt uploads (review fix):** a new receipt uploaded straight onto
    a line (`POST /receipts?expense_id=`) is refused with 409 `REPORT_WITHDRAWN` by
    the router pre-check *before* anything is stored or OCR'd; the locked link step
    (`link_receipt_locked`) re-checks `withdrawn_at` as defence in depth. Each guard
    has its own test (`test_withdrawn_report_is_read_only` "upload to entry" asserts
    no storage write ever happened and no receipt row; `test_link_receipt_locked_
    refuses_a_withdrawn_report_on_its_own` drives the locked step directly).
13. **Report list (review fix):** a reimbursed row shows "Reimbursed" with
    `Paid <date> · REIMB-…` (monospace reference, `break-all` for narrow widths),
    matching the detail notice.

## Limits / known gaps

* No bulk "reimburse selected" (optional in the spec) — one report per action.
* Finance reaches approved reports through the dashboard panel (and the report
  detail if opened); the approvals screen has no reimburse action.
* The approver of a report may also reimburse it (only self-reimbursement is
  blocked); stricter approver ≠ payer SoD was not required.
* Lines left on a withdrawn report still count for the pre-submit duplicate check
  (conservative): re-typing such a line elsewhere is flagged — the intended path is
  to MOVE it. A withdrawn-after-request-changes line that is moved and resubmitted
  auto-approves if it is otherwise in policy (spec decision), so a reviewer's
  judgement-only request can be abandoned; policy violations are still caught by the
  fresh approval-time evaluation.
* Withdrawn reports keep `status = draft` in the database; any future code that
  treats "draft" as editable must also check `withdrawn_at` (`refuse_withdrawn`).
* Trip completion uses the server's UTC date; a traveler in a far-west time zone
  may see the button one day "late" (fail closed).

## How to verify

```
R=<local-runner-dir>/pg/run_lifecycle.sh
$R -q backend/tests/test_pg_lifecycle_db.py backend/tests/test_migration_0016_report_trip_lifecycle.py
$R -q -m 'not db' --ignore=evals/manual      # hermetic
$R -q -m db                                   # DB suite
$R -q backend/tests/eval -m db                # eval-db baseline (48 passed)
cd frontend && npx vitest run __tests__/lifecycle
```

Manually (local stack): as employee submit a > $500 report, as manager request
changes; submit a small report (escalated, `open_returned_report`); withdraw the
returned one (confirm dialog); submit another small report → auto-approved. As
finance, open the dashboard → "Ready to reimburse" → "Mark reimbursed" (dialog shows
amount, title, owner, "no real money moves"); as the employee the report now reads
"Reimbursed" with date and `REIMB-…` reference; the finance KPI "Avg Cycle Time"
shows days (was "—"). Approve a trip whose end date has passed → "Mark completed".

Each important guard was proven by temporarily removing it and watching its test go
red (agent withdrawn filter, request-changes snapshot exclusion, withdrawal-audit
re-file refs, reimburse SoD / approval / idempotency checks, withdrawn write guards
including each of the two receipt-upload guards independently,
withdrawn list default, receipt marker on release / delete / discard, retention lock
for withdrawn reports, PATCH completion end-date guard, analytics null + draft count;
frontend: reimburse SoD, owner-only withdraw, withdrawn read-only, withdrawn filter
param, KPI "—", trip completion date rule, queue own-row action, list reference).
