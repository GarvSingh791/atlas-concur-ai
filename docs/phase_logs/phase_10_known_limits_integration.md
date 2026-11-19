# Phase 10 — Known limits: integration

Branch `kl/integ`, base `product-gaps` at `b044343` (alembic head was
`0020_notification_reads`). Four reviewed branches merged with `git merge --no-ff`,
then the open review findings were fixed on the merged tree.

## Merge order and conflicts

| # | Merge commit | Branch | Migration | Conflicts |
|---|---|---|---|---|
| 1 | `7bf2580` | `kl/grade` (AIR-04 traveller grades) | `0021_traveller_grades` | none |
| 2 | `4859669` | `kl/identity` (organisation-aware login) | `0022_org_scoped_email` | 5 files, below |
| 3 | `a9c0c60` | `kl/ground` (worst-case clearing of unconfirmed ground lines) | none | none (`policy_eval.py` auto-merged) |
| 4 | `b90e344` | `kl/refile` (re-file lineage, elapsed-trip sweep) | `0023_entry_release_lineage` | none (`approval_agent/evaluate.py` auto-merged) |

Conflict resolutions in merge 2 (both behaviours kept, no guard weakened):

* `database/models/identity.py` — `User.__table_args__` keeps the per-organisation
  `uq_users_org_email_lower (organization_id, lower(email))` index from 0022 (the
  global `uq_users_email_lower` from 0020 is gone, as 0022 drops it) **and** the
  `grade_on_ladder` CHECK from 0021.
* `backend/tests/api/test_contract_matrix.py` — the registry is 64 + `GET /grades` +
  `GET /organization` = **66** routes.
* `frontend/components/users/users-screen.tsx` — imports `useAdminUsers`, `useGrades`
  and `useOrganizationLoginCode`; the screen shows both the Grade column / field and
  the organisation login code.
* `frontend/lib/query/user-admin-hooks.ts` — both query keys (`grades`,
  `organization`) and both hooks.
* `frontend/__tests__/shell/users-screen.test.tsx` — the hook mock provides both
  `useGrades` and `useOrganizationLoginCode`.

Auto-merged hot files checked by hand: `policy_eval.py` (grade's
`EvalItem.traveller_grade` / AIR-04 note and ground's `EvalItem.ground_named` / relax
post-pass do not interact), `approval_agent/evaluate.py` (grade's owner-grade lookup
in `_fresh_trip_flags`, refile's lineage in `_refiled_entry_flags`), `seed_all.py`
(grade's `_seed_demo_grades` and identity's demo-org scoping are both org-scoped),
identity `admin.py` / `router.py` / `schemas.py` (grade fields and org-scoped email
check side by side), corpus units (grade's 219 units; no other branch touched the
corpus).

## Final migration chain (`fe4e5f0`)

```
0019_spend_caps_mileage -> 0020_notification_reads -> 0021_traveller_grades
  -> 0022_org_scoped_email -> 0023_entry_release_lineage (head)
```

Only `down_revision` lines and the "Revises" docstrings changed. The 0022 test derives
its "before" revision from the module, the 0021 test still pins 0020 (its parent did
not change); `test_migration_0023_entry_release_lineage` now pins `0022_org_scoped_email`.

Verified:

* `alembic heads` → one head, `0023_entry_release_lineage`.
* `test_migration_chain` (clean upgrade, zero autogenerate drift, downgrade base,
  re-upgrade) plus the 0020 / 0021 / 0022 / 0023 migration tests: 47 passed.
* On the isolated DB `atlas_test_c1` with existing rows: `downgrade -3` to 0020,
  inserted two orgs whose names collide (`Acme Corp`, `Acme Corp!`), a one-character
  org name (`X`), a mixed-case email and an expense line, then `upgrade head`:
  slugs `acme-corp` / `acme-corp-2` / `x-org`, no grade guessed, the three lineage
  columns NULL, `ck_users_grade_on_ladder` refuses `EVP`, the per-org lower(email)
  index refuses a same-org case duplicate and allows a cross-org one. `downgrade -3`
  with a cross-org duplicate present refused (count only) and rolled back to head;
  after removing it `downgrade -3` and `upgrade head` both ran cleanly. Test rows
  removed afterwards.

## Items closed on the merged tree

### Re-file lineage — two blocking review findings (`1ec46a6`)

Root cause: `_withdrawn_report_refs` marked a withdrawn report `wide` (amount ±2 % /
date ±3 d / currency, strong `refiled_returned_claim` / `refiled_rejected_claim`)
whenever **any** `expense.deleted` row named it, with no time or review test, and
then matched **every** line of that report wide.

Fix:

* The return snapshot (`approval/service._subject_snapshot`) now lists each line's
  `entry_id` — the record of what the reviewer saw.
* A deletion (from the report, or of a line released from it) counts only when the
  deleted line is on a human return snapshot of that report. Only that line's
  content is matched wide (`_WithdrawnRef.wide_entries`: its snapshot entry, its
  withdrawal entry, its deletion audit content), never the whole report.
* Legacy snapshots without entry ids: a deletion counts only when it is not older
  than the report's first submission (database clock on both sides: `min(approvals.created_at)`
  vs the deletion's `audit_log.created_at`, written by separate transactions; a tie
  counts, fail safe); then the whole report matches wide, as before.
* Deletions before the first submission never count. Lineage and receipt matches keep
  the strong flag; legacy rows without categories keep the wide match; the narrow
  `possible_refile` match is unchanged.

Tests (`backend/tests/test_kl_refile_lineage_db.py`, failing first — 4 red on the
merged code, 20 green after):
`test_a_line_deleted_before_the_first_submission_never_widens_the_match` (the exact
repro: 24.50 Cafe Alpha + 5.00 typo deleted before the first submit, submit,
request_changes, withdraw; a 24.50 Bistro Beta meals line on the same date — and a
5.00 line on the typo's date — get no flag; a 710.00 line keeps the report above the
auto-approval limit so a reviewer can return it),
`test_a_reviewed_line_deleted_after_the_return_and_retyped_identically_stays_strong`,
`test_a_reviewed_deletion_widens_only_the_deleted_lines_content`,
`test_a_line_added_after_the_return_and_deleted_was_never_reviewed`,
`test_without_snapshot_entry_ids_only_deletions_after_the_first_submission_count`
(−1 minute: no flag; +1 minute: strong).
Mutations (red, restored): reviewed-line check removed (2 red), snapshot ids ignored
(3), whole report widened (1), legacy time test removed (1), `wide_entries` ignored (4).

### (a) Identity: no-org login DoS (`c897d43`)

`kl/identity` verified the password against the K = 3 **newest** active accounts only,
so admins of K other organisations could create newer duplicates of a victim's
address and push the victim out (no-code login broken). Now every active account with
the email is verified (no short-circuit), padded with the dummy hash up to K (zero
matches still run the dummy verify); success only when exactly one hash verifies,
otherwise the generic 401. With an organisation code: unchanged (one verify).

Tests (`backend/tests/test_kl_org_scoped_login_db.py`):
`test_every_active_account_is_a_candidate_however_many_share_an_email` (failing first),
`test_victim_logs_in_despite_an_attacker_org_duplicate`,
`test_without_an_org_a_password_matching_two_accounts_fails_closed` (same password →
generic 401), `test_verify_count_never_depends_on_how_many_accounts_share_the_email`
(K verifies for 0 / 1 / 2 accounts, one per account beyond K, one with a code),
`test_a_deactivated_duplicate_with_the_same_password_is_ignored`,
`test_with_an_org_each_user_reaches_only_their_own_org` (unchanged).
Mutations: K cap restored (2 red), dummy padding removed (1), several hits accepted
(3), active filter dropped (1, the new deactivated-duplicate test), short-circuit
verify (3). Intent changed on purpose:
`test_newest_accounts_are_the_candidates_when_more_than_k_share_an_email` pinned the
old overflow limit and is replaced by the every-account test.

### (b) Ground worst-case clearing (`7db1575`)

1. **Total per report.** A `ClearingBudget` is shared by all items of one
   `PolicyEvalTool.evaluate_many` call (one report, in report order — the path
   presubmit and the approval agent use). An unconfirmed ground line is cleared only
   while the cleared total including it stays below its RECEIPT-REQ threshold; later
   lines keep their could-not-verify + approver check ("together with the $X of
   unconfirmed ground lines already cleared on this report it reaches the $25.00
   receipt threshold") and the hint. Only a line that ends up not escalating uses the
   budget. The per-report total also bounds every trip day (a day's lines are a
   subset of the report's).
2. **GRND-03.** `_shape_risk` keeps airport parking at risk whenever
   `item.airport_parking` is True (an airport named or a flight segment linked —
   `entry_context.ground_fields`), with or without a parking word.

Tests: `ai_agents/tests/test_kl_ground_worst_case.py::TestAirportLinkKeepsGrnd03` (3),
`::TestClearedTotalPerReport` (5), DB
`backend/tests/test_kl_ground_worst_case_db.py::TestClearedTotalPerReport::test_two_small_typed_rides_on_one_report_escalate`
(6 red on the merged code). Mutations: no total bound (3 red), escalating lines consume
the budget (1), parking word required again (5), `evaluate_many` without the shared
budget (3). Intent changed on purpose: "Uber to the airport" now escalates
(`test_a_ride_to_the_airport_keeps_grnd03_at_risk`) and the 20-ride review-load metric
moves from 20 → 8 to **20 → 9** ("Taxi, Airport hotel to office" escalates).

### (c) Product-gaps security fixes still hold

Re-proved on the merged tree by mutation (each red, restored):

* typed words never narrow scrutiny — typed words counted as trusted: 39 red
  (`test_kl_ground_worst_case`, `test_pg2_review_fixes_r5`); `ground_named` narrowed
  to one family: 4 red (`test_more_typed_words_never_clear_what_fewer_words_kept` etc.);
* a segment alone never verifies — `classify_with_trust` verifying on the segment:
  `test_pg_integ_policy_items.py::TestSegmentNeverVerifiesAlone::test_segment_type_alone_is_not_verified`
  red;
* the owner's grade comes from the DB, never the requester — approval agent using
  `context.user_id` instead of `trip.traveler_id`: `test_kl_grade_db` red
  (also `test_expense_line_uses_the_owners_grade_not_the_requesters`,
  `test_what_if_context_can_never_claim_a_grade`, green).

### Integration breakage fixed (`a2e190c`)

`test_pg_lifecycle_db` built trip dates from `date.today()` (local) while the app uses
the UTC date (`travel.service._today`, `complete_elapsed_trips`). Between local and UTC
midnight `test_trip_cannot_be_completed_before_it_ends_or_unless_approved` and
`test_elapsed_trip_sweep_is_idempotent_and_audited` failed deterministically (first full
DB run, 17:03 MST). The file now uses the app's UTC date.

`test_lines_released_after_request_changes_are_not_punished_as_rejected` (the flake
seen once on `kl/ground`) did not fail here: two full DB runs and 12 consecutive runs of
`test_pg_lifecycle_db` + `test_kl_refile_lineage_db` were green. On the merged tree it is
matched by lineage (no content or clock comparison). Its original cause was not
reproduced, so it is not claimed fixed; the UTC/local date mismatch above is the only
time-dependent defect found in that file.

## Gates (merged tree, tip of this log's commit)

| Gate | Result |
|---|---|
| hermetic `-m 'not db' --ignore=evals/manual` | 3454 passed, 4 skipped |
| full DB `-m db` | 1742 passed, 8 skipped |
| eval-db `backend/tests/eval -m db` | 48 passed; recall@10 0.819, answerable coverage 1.000000, unanswerable abstention 0.019231 (unchanged) |
| ruff check / ruff format --check | clean / 557 files formatted |
| mypy backend config evals | no issues (264 files) |
| frontend vitest / type-check / lint / format:check | 821 passed (113 files) / clean / clean / clean |

The DB suite rewrites the local sections of `ai_agents/rag/abstention_threshold.json`
and `rerank_calibration.json`; those changes were discarded, never committed.

## Residual risks (for the public-demo security phase)

* **No rate limiting** on `/auth/login` or `POST /users`. A no-code login now costs
  max(K, N) bcrypt verifies for N same-email accounts (uncapped on purpose: any cap
  re-opens the push-out attack); N is bounded only by needing one admin per
  organisation. The response time above K reveals "more than K accounts share this
  email" to a caller timing it. Rate limiting, alerting on `user.created` followed by
  a no-code login for that address, and verified invitations remain to do.
* No email verification, no password reset / invite flow; login code not editable in
  the UI (identity log).
* Ground: the clearing bound is per report; rides split across many reports are each
  bounded by the threshold (the same exposure the policy accepts for receipt-free
  claims). Single-line evaluations (`POST /policy/evaluate` with one item, the review
  load metric) judge the line alone.
* Re-file: a never-reviewed line re-typed with a new vendor is not caught (it was
  never in front of a reviewer); legacy snapshots without entry ids still match the
  whole report wide after a post-submission deletion (over-flag only).
* Grades: staging users have no grade until a finance admin sets one; only AIR-04 is
  rule-enforced; no SVP approval tier.
* Elapsed-trip sweep: no undo (runbook covers the manual path); staging's
  `ELAPSED_TRIP_SWEEP` value is the owner's decision.
* Not verified here: browser / Docker / live staging (out of scope for this round —
  no docker, network or browser runs); the Alembic upgrade was exercised on the local
  isolated DB only, not on Neon.
