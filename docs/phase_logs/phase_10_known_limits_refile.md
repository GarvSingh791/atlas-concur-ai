# Phase 10: Known limits, re-filing lineage and the elapsed-trip sweep

Branch `kl/refile`, base `product-gaps` at `b044343` (alembic head
`0020_notification_reads`). This branch fixes two of the known limits listed after
phase 9:

* **(A) Over-flagging on re-filing.** Lines re-filed from a withdrawn report were
  matched on amount, date and currency only, so an unrelated line could be sent to
  a human with `refiled_returned_claim` / `refiled_rejected_claim`.
* **(B) The migration changes data.** The elapsed-trip sweep marked approved
  trips that had already ended as completed on the first boot of staging, with no
  preview.

| Commit | Part |
|---|---|
| `e036e48` | (A) re-filed lines matched by lineage, not by amount and date |
| `86c10c9` | (B) operator control of the elapsed-trip sweep |
| (this log) | phase log, plus keeping the agent's line probe in its pre-phase-10 column order |

## (A) Precise re-file lineage

### What

* **Migration `0023_entry_release_lineage`** (`down_revision 0020_notification_reads`)
  adds three nullable, FK-less columns on `expenses`:
  `released_from_report_id`, `released_from_approval_id` and `released_at`. These
  are catalog-only adds: no rewrite, no default, no row touched. The downgrade drops
  exactly these three columns. There is no FK on purpose: the lineage is an audit
  reference that must outlive its target, and an FK would add a validation scan of
  `expenses`.
* **`move_entries`** (`expense/service.py`, `_release_lineage`) stamps each line
  that leaves a **withdrawn** report, in the move's own transaction. The
  `expense.moved` audit row carries a `released_from` fragment (report id,
  approval id, time). If a line already has a lineage, the first one is kept, so a
  second release can't rewrite where a reviewer first saw the line. Ordinary moves
  between drafts record nothing.
* **Frozen content.** The return snapshot (`approval/service._subject_snapshot`) and
  the withdrawal audit row (`lifecycle/service.withdraw_report`) now also freeze
  each line's `category`, `vendor` and `description`.
* **Approval agent** (`ai_agents/agents/approval_agent/evaluate.py`,
  `_refiled_entry_flags`). For each line on the submitted report, the first hit
  wins:
  1. **Lineage.** The line carries `released_from_report_id`. Strong flag
     (`refiled_returned_claim`, or `refiled_rejected_claim` when that report's
     history holds a reject), `match: "lineage"`. This holds whatever the owner
     edited after the release.
  2. **Receipt evidence.** The line is backed by a receipt listed in a withdrawn
     report's return snapshot (`receipt_ids`). Strong flag, `match: "receipt"`.
  3. **Wide content match, unchanged.** Amount ±max($1, 2%), date ±3 days and the
     same currency, checked against the owner's **rejected** reports and the frozen
     returns of subjects that were **not** withdrawn. Strong flag.
  4. **Wide content match against withdrawn reports that must stay wide.** Strong
     flag, `match: "content"`.
  5. **Narrow content match for re-typed lines.** The wide match, plus the same
     category, plus the same normalised vendor (or the description when there is
     no vendor). Normalised means lower case, punctuation dropped and spaces
     collapsed. This gives a new advisory flag, **`possible_refile`**, naming the
     withdrawn report.
* **`possible_refile` routing.** It still sets `is_policy_compliant = false`, so
  the report goes to a human and never auto-approves. It is not counted in
  `blocking_flag_count` and doesn't set `subject_in_policy = false`.
* **Catalogs.** `possible_refile` is in the finance exception catalog ("Possible
  re-filed claim", `approval/filters.py`) and in the approvals UI type labels
  (`frontend/lib/approvals/risk-summary.ts`).

### Why

Two different $24.50 lunches on the same day used to collide. The second one went
to a human as a "re-filed withdrawn return" even though it had nothing to do with
the return. A line that really was moved out of the withdrawn report is now
identified exactly, by its lineage. The remaining fuzzy path covers only lines
typed from scratch, and it needs a matching category and vendor.

### Decisions

* **Fail safe for legacy data.** A withdrawn report frozen before this change has
  snapshot or withdrawal entries without a `category` key. A pre-0023 move left no
  lineage, so these reports keep the old wide, strong match. A report that
  received a new snapshot after 0023 but whose return happened earlier is still
  matched wide. This means temporary over-flagging during the transition, never
  under-flagging.
* **Delete-and-retype can't launder.** A withdrawn report also stays wide when
  either of these happened:
  * a line it released (an `entry_id` on its withdrawal row) was later deleted,
    anywhere;
  * any line was ever deleted from the report (for example from the returned
    draft before withdrawing it, which UAT2 allows). Such a line is in no
    withdrawal row and carries no lineage, so this is the only thing that catches
    a re-typed copy of it.

  Detection uses the append-only `expense.deleted` audit rows. There is
  deliberately **no time test** ("deleted after the first return"): the return's
  `decided_at` is the app clock and the audit `created_at` the database clock, so
  the comparison broke under clock skew and inside one transaction (review
  finding). Without it, a line deleted before any review also makes the report
  wide; that only over-flags (escalates to a human), never blocks.
* **Lineage keeps the first origin.** A line released a second time (rejected
  report A, then draft B returned and withdrawn) keeps A as its origin, so the
  flag stays `refiled_rejected_claim` naming A, and both `expense.moved` rows
  name A.
* **A reject anywhere on the path wins (review fix).** Keep-first alone lost a
  LATER rejection: a line released from returned A, filed on B, rejected on B,
  withdrawn and moved to C was flagged only "sent back for changes" naming A
  (before phase 10 it was `refiled_rejected_claim` naming B). The agent now also
  loads every withdrawn report whose `expense_report.withdrawn` snapshot lists
  the line (JSONB containment on `after_state.entries[].entry_id`, owner- and
  org-scoped, independent of the 200-most-recent window) and, when the origin
  was not itself rejected, names the first rejecting report on the line's path
  (kind, title and `related_subject_id`). The row still keeps the first origin;
  a returned-only path still names the origin.
* **Editing can't launder.** The lineage columns are not on `ExpenseUpdate`. An
  unknown body field is ignored, and the test sends `released_from_report_id: null`
  to prove it. No code path clears the lineage.
* **Scope.** Only references from **withdrawn** reports changed. Rejected reports,
  open returns and trips keep the existing content match, because nothing can be
  moved out of them.
* **Agent query column order.** The agent's line query keeps its old column order
  (amount first). The hermetic fake sessions route statements by prefix, so they
  needed no change.
* Filing is never blocked, and the fresh policy evaluation still runs.

### Tests (`backend/tests/test_kl_refile_lineage_db.py`, 14; `test_migration_0023_entry_release_lineage.py`, 2)

The DB tests cover these cases:
* lineage plus its audit fragment, and no lineage on an ordinary move;
* two different lunches no longer collide;
* the same vendor in another category doesn't collide;
* a re-typed copy (case, punctuation and a $0.40 / one-day nudge) gives
  `possible_refile`, which is advisory and escalated;
* a moved line edited beyond recognition is still flagged strongly;
* only the moved line is flagged, not a neighbour with the same amount and date;
* a line released from a rejected report gets `refiled_rejected_claim`;
* the reviewed-receipt path;
* delete-and-retype stays strong;
* a legacy withdrawal keeps the wide match;
* deleting a line from the returned draft, withdrawing, then re-typing it with a
  new vendor stays strong (`match == "content"`) — review fix;
* a second release keeps the first lineage (A), its audit rows name A and the
  flag is `refiled_rejected_claim` naming A — review fix;
* returned A → rejected B → C gives `refiled_rejected_claim` naming B — review
  fix (red before it: `refiled_returned_claim` naming A);
* the same with the recent-withdrawn window forced to 0
  (`_WITHDRAWN_REF_LIMIT`), so B is found only through its withdrawal snapshot —
  review fix.

The migration tests cover the ids and the upgrade, downgrade and re-upgrade on a
populated scratch DB.

Mutations, each run once and then reverted. Every one turned at least one test red:
* no stamp;
* no lineage check;
* no receipt path;
* no deletion taint;
* no "deleted from this report" taint (review M11 and the whole `from_report`
  clause removed: the returned-draft delete test turns red);
* lineage overwritten on a second release (review M9: the keep-first test turns
  red);
* the later-reject preference removed in the lineage branch (both
  later-rejection tests red);
* the withdrawal-snapshot lookup removed (the window-0 test red);
* no legacy wide match;
* no category check;
* no vendor check;
* `possible_refile` made blocking;
* no audit fragment;
* the catalog entry removed (`test_pg_integ_flag_catalog` red).

A vitest checks the UI label (`risk-summary.test.ts`).

**Updated test, an intended change:**
`test_edit_in_place_after_reject_cannot_launder_the_original_claim` compared a
whole snapshot entry dict. Entries now carry category, vendor and description, so
the test compares the frozen amount, currency and date, which are as strict as
before.

## (B) Elapsed-trip sweep under operator control

### What

* **`ELAPSED_TRIP_SWEEP`**: `on` (default; also when unset or empty), `dry-run` or
  `off`. Case and surrounding spaces are ignored. Any other value is treated as
  `off`, with a warning.
  * In `dry-run`, the sweep logs `DRY RUN would complete trip <id> end_date <date>
    owner <traveler id>` for each trip, plus a summary line. It writes nothing:
    no status change and no audit row.
* **`ELAPSED_TRIP_SWEEP_MAX`** (default 500). Each run handles at most this many
  trips, oldest end date first. When more remain, it logs
  `Elapsed-trip sweep capped at N trips`. The rest are handled on the next boot.
* **`ELAPSED_TRIP_GRACE_DAYS`** (default 1). A trip is completed once its end date
  is more than this many days past. 1 is the old behaviour.
* **`complete_elapsed(...)`** accepts `mode`, `max_trips` and `grace_days`. When
  they are missing it reads them from settings. It returns `completed`, `skipped`,
  `failed`, `would_complete` and `capped`. `resolve_config` turns an unknown mode,
  a cap below 1 or a negative grace into `off`, with a warning.
* **Entrypoint.** `elapsed_trip_sweep()` serves both seed modes:
  * `off`: skips the step and says so;
  * an invalid value: prints a WARNING and skips;
  * `on` or `dry-run`: runs the module as the same non-fatal `repair`, and the
    module checks the value again.
* **Settings and docs.**
  * The settings are in `config/settings/base.py` and are kept as raw text, so a
    bad value never stops the app from starting.
  * `.env.example` documents them.
  * `render.yaml` describes the options only in comments. The staging value is
    not changed; the project owner decides it.
  * The runbook is `docs/runbooks/elapsed-trip-sweep.md`. It covers the settings,
    a staging preview (set dry-run, deploy, read the log, then set on) and a manual
    run.

### Tests

`backend/tests/test_kl_elapsed_sweep_db.py` (23) covers:
* dry-run writes nothing and logs only the id, end date and owner id (no email,
  name or destination), oldest first;
* `on` audits only what it completed;
* the cap in both modes (oldest first, a log line, the rest on the next run);
* grace 0, 1 and 3;
* off and invalid modes (a warning only for an invalid one);
* an invalid cap or grace turns the sweep off;
* settings defaults and safe resolution.

`backend/tests/test_entrypoint_repairs.py` (+21, 32 in total) runs both seed modes
with these values: unset, empty, `on`, `dry-run`, `DRY-RUN`, `off`, `OFF`,
`" off "`, and the invalid `yes`, `true` and `dryrun`. It also checks that
`SEED_ON_STARTUP=false` never runs the sweep.

Mutations, each run once and then reverted. Every one turned tests red:
* dry-run applies;
* off is ignored;
* an invalid mode is applied;
* no truncation at the cap;
* no capped flag;
* no grace;
* an invalid cap is applied;
* the destination is logged instead of the owner id;
* the entrypoint runs the step on `off` or on an invalid value;
* no lower-casing in the entrypoint.

**Updated test, an intended change:** `test_elapsed_trip_sweep_is_idempotent_and_audited`
compared the exact result dict. It now includes `would_complete: 0` and `capped: 0`.

## Limits / known gaps

* **A re-typed line with a different vendor is not caught** when it doesn't share
  a receipt with the withdrawn report and the report wasn't tainted by a deletion.
  This is the accepted price of no longer over-flagging. The original line still
  exists, either on the closed withdrawn report or moved with lineage. Pre-submit
  duplicate and receipt-reuse checks are unchanged.
* **Lines edited between the return and the withdrawal, then re-typed from scratch
  with the original values.** The narrow match compares against both the return
  snapshot and the withdrawal row, so this is caught only when the category and
  vendor also match.
* **A second release keeps the first lineage.** The flag names the first withdrawn
  report unless a later report on the line's path rejected it, in which case it
  names that rejecting report. When several reports on the path were only
  returned, only the origin is named.
* **Trips are unchanged**, because there is no line movement for trips. Withdrawn
  trips are still matched by destination and dates.
* **Transition.** Withdrawn reports whose snapshots predate this change stay on
  the wide match. Over-flagging on those reports fades out as they age past the
  reference window: the 200 most recently withdrawn reports per owner, plus any
  report named by a lineage.
* **Migration chain.** `0023` chains onto `0020`. The other phase-10 branches
  own `0021` and `0022`, so the integration has to re-chain `down_revision` (one
  line, and `test_migration_0023_entry_release_lineage` reads it).
* **The sweep has no undo.** The runbook explains the manual path through the audit
  rows (they hold the previous status and version).

## Gate results (branch tip)

| Gate | Result |
|---|---|
| hermetic (`-m 'not db' --ignore=evals/manual`) | 3397 passed, 4 skipped |
| full DB (`-m db`) | 1656 passed, 8 skipped |
| eval-db (`backend/tests/eval -m db`) | 48 passed |
| ruff check / ruff format --check | clean / 546 files formatted |
| mypy backend config evals | no issues (258 files) |
| frontend vitest / type-check / lint / format:check | 806 passed (112 files) / clean / clean / clean |

## How to verify

```bash
R=<local-runner-dir>/kl/run_refile.sh
$R -q backend/tests/test_kl_refile_lineage_db.py backend/tests/test_migration_0023_entry_release_lineage.py
$R -q backend/tests/test_kl_elapsed_sweep_db.py backend/tests/test_entrypoint_repairs.py
$R -q -m 'not db' --ignore=evals/manual .      # hermetic
$R -q -m db                                     # full DB
$R -q backend/tests/eval -m db                  # eval-db: exactly 48 passed
ruff check . ; ruff format --check . ; mypy backend config evals
cd frontend && npx vitest run && npm run -s type-check && npm run -s lint && npm run -s format:check
```

To preview the sweep by hand on a database:
`ELAPSED_TRIP_SWEEP=dry-run python -m database.seeds.complete_elapsed_trips`.

## Integration update

See `phase_10_known_limits_integration.md`: the deletion taint now counts only a line a reviewer saw (entry ids on the return snapshot; legacy snapshots: deletions not older than the first submission) and widens only that line's content, and `0023` now chains onto `0022`.
