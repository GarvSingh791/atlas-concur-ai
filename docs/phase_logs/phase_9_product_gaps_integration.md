# Phase 9 — Product gaps: integration of the four tracks

Branch `pg/integ` · base `2fbe114` (deployed `origin/main`, alembic head
`0015_canonical_policy_figures`). Track logs:
`phase_9_product_gaps_{lifecycle,shell,team,fxcard}.md`.

## Merge order

Each track was merged with `git merge --no-ff` (subject "Integrate product gaps: <track>"):

| # | Track | Merge commit | Conflicts |
|---|---|---|---|
| 1 | `pg/lifecycle` (gaps 1, 4, part of 8) | `706da4c` | none |
| 2 | `pg/shell` (gaps 3, 7) | `e7bacc4` | `api/v1.py`, `tests/api/endpoints.py`, `tests/api/test_contract_matrix.py`, `trips/trip-list.tsx` |
| 3 | `pg/team` (gaps 5, 6, part of 8) | `ac4d045` | `api/v1.py`, `expense/service.py`, `endpoints.py`, `test_contract_matrix.py`, `approvals/approvals-screen.tsx` |
| 4 | `pg/fxcard` (part of gap 8) | `34de248` | `api/v1.py`, `endpoints.py`, `test_contract_matrix.py`, `expenses/expenses-screen.tsx` |

Every other shared file (`approval/service.py`, `policy_eval.py`, `presubmit.py`,
`config/settings/base.py`, `.env.example`, `docker-compose`, the backend entrypoint,
frontend types and hooks) merged without conflicts. The full gates below show the
merged results work together.

## Conflict resolutions (both sides kept every time)

* **`backend/app/api/v1.py`**: imports and `include_router` calls for all of
  lifecycle, notifications, search, team (`/team`) and card. No paths collide.
* **Contract registry** (`endpoints.py` + `test_contract_matrix.py`): every track's
  `EndpointSpec` rows kept (audit CSV export, team ×2, card feed). The pinned
  live-route count is the sum: **41 + 5 lifecycle + 9 shell + 6 team + 1 card = 62**.
* **`expense/service.py` `REPORT_FILTER_FIELDS`**: keeps lifecycle's `withdrawn` and
  team's `*REPORT_EXTENDED_FILTER_FIELDS`. `split_filters` takes the extended
  filters out before `_apply_filters`, so withdrawn reports are still hidden by
  default under every combination of filters.
* **`trips/trip-list.tsx`**: lifecycle's status filter (incl. Completed / Withdrawn)
  plus shell's `?trip=<id>` deep-link highlight on the filtered cards.
* **`approvals/approvals-screen.tsx`**: team's URL-driven filters (`usePathname`,
  `router.replace`) plus shell's `?approval=<id>` preselection. Both read the
  same `useSearchParams()` value.
* **`expenses/expenses-screen.tsx`**: shell and fxcard both added
  `/expenses?report=<id>`. I kept the shell's reactive `useSearchParams` version
  and passed it through fxcard's exported `initialReportId` helper (which trims
  the id and ignores a blank one), so both tracks' tests still check the same behaviour.

## Migration chain (single head)

```
… 0014_approval_step_snapshot
  → 0015_canonical_policy_figures
  → 0016_report_trip_lifecycle      (lifecycle; down_revision 0015, unchanged)
  → 0020_notification_reads         (shell;     down_revision 0015 → 0016)
```

team and fxcard add no migrations. The only edit is the `down_revision` line (and
the docstring "Revises:") of `0020_notification_reads.py`. Ids and file names are
unchanged.

**For the second integration (policy 0017, 0018, 0019):** chain them
`0016 → 0017 → 0018 → 0019`, then change **only** `0020`'s `down_revision` to the
0019 id. `test_pg_email_unique_migration_db.py` now reads its starting revision
from 0020's own `down_revision`, so it follows the chain without edits.
`test_migration_0016_…` still asserts 0016 → 0015, which stays true.

Verified: `alembic heads` returns one head; `test_migration_chain` (full upgrade,
drift check, downgrade base, re-upgrade), `test_migration_0016` and the 0020
email-guard test passed; `upgrade head / downgrade -1 / upgrade head` ran on
`atlas_test_c1`.

## Cross-track breakages fixed

| Commit | Problem when combined | Fix |
|---|---|---|
| `7d32929` | team's exception-type catalog guard test failed: fxcard emits a new `currency_unverified` flag | flag added to the catalog ("Currency not verified"). The guard test was right, so it was not touched |
| `7d32929` | shell deep-link test mocks `next/navigation` without `usePathname`, which team's approvals screen now uses | mocks extended (`usePathname`, filter hooks, filter bar); new case for `?approval=` combined with a filter |
| `d78f547` | lifecycle reimburse compared the stored total with the plain `SUM(lines)`, but fxcard stores a converted total, so **every mixed-currency report would have been refused** | guard recomputes the total with the shared `report_total` rule. Drift still fails closed |
| `e254680` | team view counted withdrawn reports (status `draft` + `withdrawn_at`) as open drafts | excluded |

## Security items closed

### (a) Identity — one account per email, login fails closed (`a716f7e`)
* Verified on shell: `_ensure_email_free` is global and case-insensitive, migration
  0020 adds `uq_users_email_lower` and fails closed on existing duplicates, and
  login is case-insensitive.
* **Hardened:** login used `.one_or_none()`, so a duplicate (legacy data, or a DB
  missing the index) raised `MultipleResultsFound` and returned a 500. Login now
  loads at most 2 rows. Unless exactly one row matches, it runs the dummy hash and
  returns the **same generic 401** as a wrong password. Only the count is logged.
* `EMAIL_TAKEN` is identical for an own-org clash and a foreign-org clash
  (status, code, message), on both the pre-check path and the racing-insert path.
* Each refused attempt writes a **committed** `user.create_refused` audit row in
  the admin's org. It holds the actor, the masked email (`j***@domain`) and an
  HMAC-SHA256 digest keyed by the server secret (the entity id is a uuid5 of that
  digest, so repeated attempts correlate). It never holds the raw address or
  which org owns it. The row is committed before the 409 because `get_db` rolls
  back on errors.
* Tests: `backend/tests/test_pg_integ_identity_db.py` (4). Mutations: login takes
  the first match, so test red; audit not committed, so 2 tests red; race path
  without audit, so test red.

### (b) Lifecycle SoD — withdrawal cannot launder a return (`8af6d65`)
* A line released from a report withdrawn after **request-changes** can still be
  moved and filed. Policy evaluation still runs. The approval agent matches it
  against the withdrawn return (request-changes snapshot plus the withdrawal
  audit row) and escalates with a blocking **`refiled_returned_claim`** flag. The
  flag names the original report (`related_subject_id` + title). Unrelated lines
  still auto-approve. Trips follow the same rule.
* A withdrawn subject with **any** reject in its history stays a rejected
  reference (`refiled_rejected_claim`). This includes a legacy pre-0014 reject
  with no snapshot, followed by reopen, request-changes and withdrawal. This
  closes the lifecycle reviewer's gap.
* Exception catalog: "Re-filed withdrawn return". Approvals UI: whole-trip
  blocking exception.
* **Reconciled test:** `test_lines_released_after_request_changes_are_not_punished`
  checked the track's original spec (auto-approve). That is the laundering the
  security review rejected. It is now
  `…_are_not_punished_as_rejected` and asserts escalation with
  `refiled_returned_claim` and no `refiled_rejected_claim`.
* Tests: `backend/tests/test_pg_integ_lifecycle_sod_db.py` (5): escalated with the
  flag, unrelated lines unaffected, legacy reject then request-changes then
  withdraw stays rejected, rejected-then-withdrawn still flagged, trip re-file.
  Mutations: withdrawn returns dropped as references, so 3 tests red; reject
  history ignored, so the legacy test red; frontend whole-trip set, so a vitest red.

### (c) Budget Posture (`ee415e2`)
Counts `approved + completed` trips. The elapsed-trip sweep no longer makes them
drop out. Tests: `test_pg_integ_analytics_db.py` (2, incl. completing a trip
through the endpoint leaves the KPI unchanged). Mutation: approved only, so both red.

### (d) Notifications (`baf68ea`)
Shell already derived `reimbursed` / `completed` from status. A reimbursement is
now dated by `reimbursed_at` and names its `REIMB-…` reference. Tests drive the
real lifecycle actions (finance reimburse, traveler completion, sweep) and check
that only the owner is notified: `test_pg_integ_notifications_db.py` (2).

## Residual risks / follow-ups

* **Cross-org email squatting — CLOSED** (known limits, migration
  `0022_org_scoped_email`, see `phase_10_known_limits_identity.md`). Email is
  unique per organisation again, and login takes an optional organisation login
  code (`organizations.slug`). An admin in org A creating `someone@orgb.com` no
  longer affects org B: org B can still create its own account, and that user
  signs in with org B's code **or** with email + password alone (without a code
  the password is checked against a constant K = 3 newest active same-email
  accounts and must match exactly one).
* **Account enumeration by an admin — CLOSED (both probes).** The Add-user
  check only looks at the admin's own organisation. The create-then-login
  probe found in review (create E in your own org, then log in as E without a
  code: 401 used to mean "E exists elsewhere") is closed in code by the
  constant-K login: the probe account logs in either way and the verify count
  never depends on what exists. Residual, documented in
  `phase_10_known_limits_identity.md`: more than K active accounts on one email
  (only the K newest are tried without a code), and no rate limiting or
  alerting on `/auth/login` yet.
* **Still open:** no email verification (an admin can create an account for an
  address they don't own inside their own org), no password reset or invite
  flow (initial passwords are admin-set), no rate limiting on `POST /users` or
  `/auth/login`.
* The audit digest is keyed by `JWT_SECRET`. Rotating the secret stops correlating
  old and new attempts (it doesn't affect security).
* `refiled_returned_claim` is content-matched (amount ±max($1, 2%), date ±3
  days, same currency, vendor ignored). Like `refiled_rejected_claim`, it can
  over-flag (an extra human review). It can never block a filing.
* Withdrawn reports keep `status = draft`. Every new "draft" reader must also check
  `withdrawn_at`. The integration audited the new tracks' readers (team, search,
  card, notifications, analytics) and fixed the team count.
* The policy migrations 0017–0019 were merged in round 2 (below).

## Gate results (integrated tree)

Runner: `<local-runner-dir>/pg/run_integ.sh` (isolated DB `atlas_test_c1`, providers local).

| Gate | Result |
|---|---|
| pytest hermetic `-m 'not db' --ignore=evals/manual` | 2749 passed, 4 skipped |
| pytest DB `-m db` | 1454 passed, 8 skipped, 0 failed |
| eval-db `backend/tests/eval -m db` | 48 passed |
| ruff check / ruff format --check | clean / 505 files formatted |
| mypy backend config evals | no issues (240 files) |
| vitest | 99 files, 723 tests passed |
| type-check / lint / format:check | clean |

One earlier DB run showed a single setup ERROR (`relation "organizations" does
not exist` in `test_pg_csv_export`). It happened because I ran a hermetic pytest
in parallel on the same scratch DB. The file passes alone, and the full DB suite
re-run on its own was clean (row above).

---

# Round 2 — the policy tracks (`pg/policy`, `pg/policy2`)

Track logs: `phase_9_product_gaps_policy.md` (stages 1 and 2), `phase_9_product_gaps_policy2.md`.

## Merge order

| # | Track | Merge commit | Conflicts |
|---|---|---|---|
| 5 | `pg/policy` (gap 2 stages 1 + 2, part of gap 8; 12 commits; 0017, 0018) | `b84fabd` | `presubmit.py`, `policy_eval.py`, `expense/service.py`, `test_contract_matrix.py` |
| 6 | `pg/policy2` (gap 2 spend caps, gifts, mileage; 8 commits; 0019) | `297df6b` | `categorize.py`, `presubmit.py`, `policy_authoring.py`, `policy_rule_semantics.py`, `policy_eval.py`, `expense/schemas.py`, `expense/service.py`, `endpoints.py`, `test_contract_matrix.py`, `test_p3_seed.py`, `models/expense.py`, `seeds/policy_content.py`, `entry-editor.tsx`, `report-detail.tsx`, `lib/types/expenses.ts` |

## Conflict resolutions (both behaviours kept, no guard weakened)

* **`policy_eval.py`**: fxcard's `evaluate_with_fx` stays the one wrapper around the
  per-rule evaluators; the entry-rule `kind` dispatch (GRND-01/03/05) and the spend-rule
  dispatch (MEAL-05, ENT-02, ENT-04, MIL-01..03) run inside the converted evaluator, the
  MEAL-04 exclusion verdict beside it. `EvalItem` carries fxcard's `currency`, every
  entry-rule field and the spend facts. Imports keep entry / process / spend modules.
* **`presubmit.py`**: meals items carry both `item_text` (MEAL-04) and the spend facts;
  the trip meal total skips team meals (MEAL-05) **and** still refuses to add amounts in
  different currencies (fxcard); `DraftEntry` carries `receipt_affidavit_at` and
  attendees / disclosure / mileage.
* **`policy_authoring.probe_item`**: keeps the ground sub-type probe and adds the spend
  facts (a meals probe reaches MEAL-05).
* **`policy_rule_semantics.py`**: RECEIPT-REQ allowed keys keep EXP-04's affidavit keys;
  the receipt validator runs the affidavit check; the three new rule types get their own
  validators and describers.
* **`expense/service.update_entry`**: the category-gated policy fields
  (`apply_entry_policy_fields`) apply first, then the affidavit change; the audit
  snapshot carries both sets of fields.
* **Seeds / `test_p3_seed`**: `RULES` (with `SPEND_RULES` appended) + `ENTRY_RULES` +
  `PROCESS_RULES`; the seed test accepts stub, entry, process and spend clause refs and
  requires every corpus ref to be a real TEP-FY26 clause.
* **Contract registry**: every row kept; live routes 62 + rule history + mileage = **64**.
* **Frontend**: the entry editor's form has both the affidavit and the
  attendees / disclosure / mileage fields; `report-detail.tsx` keeps withdraw / reimburse,
  `canSelect` for the selection bar and the Add mileage button; one mileage test literal
  gained `affidavit: false` (the merged `FormState` requires it).

## Final migration chain (single head)

```
… 0014_approval_step_snapshot
  → 0015_canonical_policy_figures
  → 0016_report_trip_lifecycle
  → 0017_entry_level_policy_rules   (down_revision 0015 → 0016)
  → 0018_process_timing_rules       (unchanged: 0017)
  → 0019_spend_caps_mileage         (down_revision 0015 → 0018)
  → 0020_notification_reads         (down_revision 0016 → 0019)
```

Only `down_revision` lines and the docstrings' "Revises:" changed; ids and file names are
the same. Tests that pinned a parent: `test_pg2_rule_semantics` (0017 → 0016) and
`test_migration_0019_…` (0019 → 0018). `test_pg_email_unique_migration_db` follows 0020's
own `down_revision`. Verified: `alembic heads` = one head; `test_migration_chain` (in the
DB suite); on `atlas_test_c1`: `upgrade head → downgrade 0016 → upgrade head`,
`downgrade -1 → upgrade head`, `downgrade base → upgrade head`.
Seeded databases: `test_pg_integ_policy_hardening_db::TestMigrationsOnASeededDatabase` —
a fresh seed + 0017/0018/0019 inserts nothing, rewrites nothing, writes no audit row;
admin-edited (RECEIPT-REQ threshold, MEAL-05 minimum) and archived (GRND-01, EXP-01)
rules stay as they are.

## Cross-track work

**Currency (`5dcac6a`).** `fx_policy` now converts every money figure an item carries —
the receipt total, tip and line items, the line's own claim and the spend facts' amount —
with the line amount's rate, and treats `per_head_cap` and `gift_disclosure` as money
rules; the MEAL-04 exclusion verdict reads the converted figures; EXP-04's $75 ceiling
and the RECEIPT-REQ threshold used by the affidavit count are compared in USD
(`process_rules.amount_in_usd` / `receipt_needed`). Unsupported currency: per-head cap →
could-not-verify + `currency_unverified` (escalates); ENT-04 → judged as an unknown amount
(disclosure required) + `currency_unverified`; EXP-04 → affidavit not accepted. USD is
byte-for-byte unchanged. Tests: `ai_agents/tests/test_pg_integ_currency_rules.py` (27;
EUR 95 gift ≈ $103.08, EUR / JPY at $100, $120 and $75, EUR meal line items, EUR tip).
PTA-05 / AIR-06 already refuse non-USD legs (could not verify) and CARD-03 totals per
currency without a threshold; mileage stays USD-only.

**Exception catalog (`b97862e`).** `approval/filters.py` gains `policy_advisory`,
`late_submission`, `unreconciled_card` and labels for `per_head_cap`, `gift_disclosure`,
`mileage`; the approver's exception list shows each flag's type.
`backend/tests/test_pg_integ_flag_catalog.py` parses every producer module (AST) and
resolves `flag_type` constants, attributes and conditionals — the older literal-only scan
had missed `process_rules.LATE_SUBMISSION` & co. Vitest
`__tests__/approvals/pg-integ-policy-flags.test.tsx`.

## Open items closed (each with a test proven red by a mutation)

| # | Item | Fix | Tests |
|---|---|---|---|
| 1 | EXP-04 blocker | only affidavits standing in for a REQUIRED receipt (threshold / category in USD) use a slot; signing refused (422) where not offered; the editor offers the checkbox only where the server's `missing_receipt` flag says `affidavit_allowed` | `test_pg_integ_policy_hardening_db::TestAffidavitsForRequiredReceiptsOnly` (3), vitest `pg2-process-rules` "is not offered where no receipt is required", "…cannot be used", "reads affidavit_allowed" |
| 2 | EXP-04 gaming | `before_flush` listener withdraws a draft line's signature on a date / amount / currency / vendor / description change or a receipt (un)link; re-signing stamps a fresh time; only a validated receipt on a locked line frees a slot; the approval agent re-counts at submission | `TestSignatureFollowsTheClaim` (8), `TestOnlyAValidatedReceiptOnALockedLineFreesASlot` (3), `TestQuarterRecheckedAtApprovalTime` |
| 3 | EXP-01 clock | completion = min(trusted trip end, earliest expense) | `test_pg2_process_rules_db::TestSubmissionDeadline::test_trip_completion_anchors_a_trip_report`, `…test_lines_long_before_the_trip_anchor_on_their_own_date` (both reconciled: intended change) |
| 4 | PTA-02 departure | planner-selected segment's start, else min(segment start, trip start) | `test_pg_integ_policy_items::TestTrustedDeparture` (5), `test_pg_integ_policy_hardening_db::test_pta02_a_typed_far_future_segment_cannot_lengthen_the_lead_time` |
| 5 | MEAL-04 | (i) tax / tip pro rata over kept and excluded items, un-attributable extras → could-not-verify, honest partial claims untouched; (ii) cooked-with = named drink directly followed by food phrasing, only that span stripped | `test_pg_integ_policy_items::TestAlcoholTaxTipGap` (6), `TestCookedWith` (12); `test_pg2_review_fixes_r3::…test_receipt_total_with_tax_is_the_non_excluded_base` reconciled (the beer's $2 tax now excluded) |
| 6 | Ground transport | NOT already closed: a rail / car segment alone verified a line. Now only the receipt's OCR words or the card merchant verify; the segment corroborates (another family blocks) | `TestSegmentNeverVerifiesAlone` (3) |
| 7 | Docs | policy log: "could not verify" escalates (corrected), unimplemented clause sentences (GRND-01 rail ≤ 4 h, tolls, conference hotels …), PTA-05 two approved trips, PTA-02 measured at the approval-time evaluation | — |
| 8 | Hardening | mileage POST needs If-Match or Idempotency-Key (428 otherwise, replay on the same key); `attendee_count` StrictInt; 0018 downgrade removes seeded process rules nobody touched, keeps admin-authored / edited ones | `test_mileage_needs_if_match_or_an_idempotency_key`, `test_attendee_count_is_a_strict_integer[4]`, `TestMigrationsOnASeededDatabase` (3) |

Mutations (each reverted after): 25 backend (one per guard) and 2 frontend, all red.

Other reconciled tests: `test_pg2_process_rules_db::TestAffidavit::test_over_the_ceiling_stays_blocking`
(signing $75.01 is now 422; a legacy signed line still blocks);
`test_pg2_spend_mileage_db._add` sends a fresh Idempotency-Key when no version is given;
`test_pg_integ_identity_db` compares refusal rows by entity id counts (`210ef50`: the rows
share a `created_at` in one test transaction, so their order was arbitrary — a flake
from round 1 that the bigger suite exposed).

## Residual risks (round 2)

* A signature is withdrawn only on draft lines; a submitted / approved line keeps it by
  design. Attaching ANY receipt to a draft line withdraws the signature (the user's choice,
  like unticking); the line is then judged on that receipt — an unreadable one is the
  existing escalating advisory, one that does not cover the claim blocks.
* Human approval does not re-evaluate; the approval agent's fresh evaluation at
  submission (and on resubmission) is the approval-time re-count.
* Ratio rules (GRND-05) in a foreign currency are converted figure by figure, so an
  exact-boundary case can move by a cent of rounding.
* A segment-only ground line is now UNCONFIRMED: every kind rule reaches it as an
  escalating could-not-verify (more human reviews for lines without receipt / card evidence).
* A drink directly followed by a cooking word that is really a drink ("Bourbon butter"
  cocktail names) is still exempt; vocabulary limits from stage 1 remain.
* 0017's downgrade still keeps GRND rules a seed created (only 0018 was asked for).
* The intermediate round-2 commits were not gated one by one; the gates below ran on
  the final tree.

## Gate results (round 2, final tree)

Runner: `<local-runner-dir>/pg/run_integ.sh` (isolated DB `atlas_test_c1`, providers local),
suites run one at a time.

| Gate | Result |
|---|---|
| pytest hermetic `-m 'not db' --ignore=evals/manual` | 3273 passed, 4 skipped |
| pytest DB `-m db` | 1601 passed, 8 skipped, 0 failed |
| eval-db `backend/tests/eval -m db` | 48 passed |
| ruff check / ruff format --check | clean / 540 files formatted |
| mypy backend config evals | no issues (253 files) |
| vitest | 105 files, 766 tests passed |
| type-check / lint / format:check | clean |

# QA round fixes (browser QA of the integrated build)

Source: manual-style browser QA run (Nov 14, 2026). Every fix has a test shown red
without it; the new tests are `backend/tests/test_qa_round_flag_types_unit.py`,
`backend/tests/api/test_qa_round_fixes.py`, `ai_agents/tests/test_qa_round_rule_titles.py`
and `frontend/__tests__/qa-round/*`.

| # | Defect | Fix |
|---|---|---|
| 1 | CARD-03 flags stored as `rule_type=category_allow`, counted by "Rule: category not allowed" | process-rule flags carry their process kind (`card_reconciliation`, `submission_deadline`, `advance_booking`, `trip_change_reapproval`, `lowest_fare`) with their own labels; legacy stored flags mapped at read time (`effective_rule_type` + a `jsonb_path_exists` SQL twin) — list, counts and CSV agree |
| 2 | Inverted approvals date range → generic "Couldn't load approvals" | filter bar shows an inline alert and sends no request; any 400/422 shows the server's message (`validationErrorMessage`, also on the card feed) |
| 3 | Employee dashboard at 390px clipped status badges | shrinkable `grid-cols-1` / `min-w-0` columns, truncating text, badges `shrink-0 whitespace-nowrap` (both badge primitives) |
| 4 | Department analytics grouped by e-mail prefix | grouped by `users.department` (trimmed, NULL/blank → "Unassigned"); API shape unchanged |
| 5 | Trip Timeline showed completed trips as "Approved" | every trip status labelled accurately |
| 6 | Misleading / duplicated policy rule titles | `draft_rule_title` names every product-gap rule with figures and clause code ("Advance booking — 14 days domestic, 21 days international (PTA-02)", ...); clause code shown in the rule list |
| 7 | Withdrawn report shown as "Changes requested" in the queue / CSV | `subject_withdrawn` + `display_status`; `current_status=withdrawn` filter; CSV Status "withdrawn"; withdrawn rows sort with resolved ones |
| 8 | Small UI items | pills no longer wrap; distinct New-report trip options; affidavit unticks on amount/date/vendor/description change (re-tick re-signs); no pre-submit panel on approved/reimbursed reports; manager dashboard formats subject type, money (2 decimals) and activity text (server summary no longer carries the UUID); team list/drill-down mark Inactive members (`status` on team members); member Reports tab / opened report in the URL |
| 9 | Approvals detail pane: withdrawn report still "Changes requested" (header badge + "This approval is changes requested and can't be changed."); mileage exception cards titled "entry_0"; "In policy: Yes" beside "Blocking flags: 1" | detail response carries `subject_withdrawn` + `display_status` too; the header badge uses it and the Decision note reads "This report was withdrawn by its owner; the approval is closed." (other closed approvals: "This approval was approved / rejected / returned for changes and can't be changed."); an internal `entry_<n>` / id ref is never a card title — the flag is resolved to its entry (entry_id, else evaluation order) and titled vendor → description → "Mileage entry", else "Expense entry" (the agent ref itself is unchanged: the Policy column and reflection match on it); the approval agent now sets `subject_in_policy=false` when SoD review flags (open return / re-filed claim / split trip) add blocking flags. Stored summaries written before this keep their old values. Tests: `frontend/__tests__/qa-round/approval-detail-qa.test.tsx`, `test_withdrawn_subject_is_flagged_on_the_detail`, `test_blocking_review_flag_means_not_in_policy` |

Not changed, by decision:

* No `0021_policy_rule_titles` migration: `policy_rules` has no title column — titles
  are derived from the stored rule on every read, so existing rows (seeded or live)
  pick up the new titles without a data change, and an edited figure shows in the title.
* Rejected-then-withdrawn reports keep showing "Rejected" (the final decision); only a
  withdrawn return (changes_requested) displays as "Withdrawn".
* Not touched (as instructed): the always-present off-screen assistant `role=dialog`,
  and the UNITED $320 receipt vs card transaction match question. Also left: the static
  exception-type counts (they do not follow the other active filters), row action icons
  wrapping, no live amount preview in the Add mileage dialog, the "Match: Auto · Receipt"
  label after a receipt is removed, and the Policy document pane listing only the
  original 8 sections.

## Gate results (QA round)

| Gate | Result |
|---|---|
| pytest hermetic `-m 'not db' --ignore=evals/manual` | 3297 passed, 4 skipped |
| pytest DB `-m db` | 1616 passed, 8 skipped, 0 failed |
| eval-db `backend/tests/eval -m db` | 48 passed |
| ruff check / ruff format --check | clean / 543 files formatted |
| mypy backend config evals | no issues (255 files) |
| vitest | 111 files, 798 tests passed |
| type-check / lint / format:check | clean |
