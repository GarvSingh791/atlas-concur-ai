# Phase 12 — Follow-ups: more traveller-grade rules and a real SVP approval level

Branch `fu/grades` (base `main` @ `962e71a`). Migration `0025_grade_entitlement_rules`
(down `0023_entry_release_lineage` in this branch; the accounts track owns
`0024_auth_tokens`, the integrator re-chains 0025 onto it).

## What

Phase 10 decided only AIR-04 from `users.grade`, and every flag that named "SVP" still
went to the usual line manager. Now:

1. **AIR-07, LOD-05, NR-01 are rule-enforced** from the expense OWNER's grade of record.
2. **A real approver-grade level**: a flag that names an approver grade (SVP, VP) routes
   the approval to an approver of that grade and refuses a decision by anyone below it.
3. **Corpus / rules / ladder / migration consistency** is a test.
4. **Approvals UI** shows the required grade, the routed approver and why the viewer
   may or may not decide.

| Piece | Where |
| --- | --- |
| Pure evaluators: detection, FX, three outcomes by grade | `ai_agents/tools/grade_entitlements.py` |
| Process kind `grade_entitlement` (no enum change) | `ai_agents/tools/process_rules.py` |
| Per-line wiring (trusted evidence of the OWNER, owner grade, justification) | `ai_agents/agents/grade_checks.py` |
| Hook into pre-submit / approval-time evaluation (two new functions) | `ai_agents/agents/expense_agent/presubmit.py` (`memo_owner_grade`, `grade_line_flags`) |
| Validator, policy-page description, rule title | `ai_agents/agents/process_rule_semantics.py`, `policy_authoring.py` |
| Seed rules (appended to `PROCESS_RULES`) | `database/seeds/grade_rules_content.py`, `policy_content.py` |
| AIR-07 delay figure (renders AIR-07 and FAQ-Q58 byte-identically) | `database/seeds/corpus/figures.py` (`LOUNGE_DELAY_EXCEPTION_HOURS`) |
| Data migration (insert missing rules + audit, symmetric downgrade) | `database/migrations/versions/0025_grade_entitlement_rules.py` |
| Ladder: entitlements rule-enforced, `approver_grade`, `most_senior` | `config/constants/grades.py` |
| Required grade, routing, scope, enforcement, UI payload | `backend/app/modules/approval/grade_routing.py` |
| Hooks: routing after every escalation / re-queue; decide guard; detail/list fields; notifications | `approval/lifecycle.py`, `approval/service.py`, `approval/schemas.py`, `notifications/service.py` |
| Finance filter / CSV label "Rule: grade entitlement" | `backend/app/modules/approval/filters.py` |
| UI: queue badge, "Approver level" section, disabled buttons + note, 403 toast | `frontend/components/approvals/{approval-queue,decision-panel,decision-actions,grade-requirement}.tsx`, `lib/approvals/grade.ts`, `lib/types/{approvals,api}.ts` |

### The clauses, exactly as written

| Clause | Text (TEP-FY26) | Rule (`entitlement`) |
| --- | --- | --- |
| AIR-07 | "Early-boarding and lounge fees are not reimbursable (except for SVPs+ or delays > 4 hours)." | `airport_extras`, `min_grade` SVP, `delay_exception_hours` 4 |
| LOD-05 | "standard room only; suites/upgrades not reimbursable except SVPs+." | `room_upgrade`, `min_grade` SVP |
| NR-01 | "lounge memberships (except SVPs+)" are not reimbursable | `lounge_membership`, `min_grade` SVP |

DEF-09 / DEF-10 and FAQ Q51–Q59 already state these entitlements from the ladder; the
corpus text is unchanged (hash of every unit identical before / after).

### Behaviour (per expense line)

Detection reads TRUSTED words — the owner's receipt OCR vendor (or its frozen OCR
snapshot) and line items, the owner's card merchant — and the claimant's vendor /
description. A match in either detects the line; typed words can only widen (a typed
"standard room" never hides a receipt's "Junior Suite"). A typed-only match says so in
the reason ("named on the entry, not on a receipt or card").

Whether a line is **at a hotel** (a "Suite" / "Upgrade" line is a room) or **at an
airport** (a bare "Lounge" line is an airport lounge) is the context of the WHOLE entry,
read from trusted evidence (review fix): any receipt word / line item or card merchant
with hotel words (hotel, inn, resort, motel, room, nights, folio, plural "Suites", major
hotel brands) or airline words (airline(s), airways, boarding pass, a few carrier
names), or a linked trip segment of type hotel / flight on the owner's own trip in the
same org. The claimant's category only ADDS context ("lodging" / "airfare"); filing a
Marriott folio with a "Junior Suite Supplement" line under "other" or "meals" still
raises LOD-05. "airport" alone is not airline context (an "Airport Marriott" lobby
lounge is a hotel bar).

| Line | Detected by |
| --- | --- |
| AIR-07 | early-bird / early / priority boarding; lounge brands (Sky Club, United Club, Admirals Club, Centurion, Polaris, Plaza Premium, Priority Pass, airport lounge); "lounge access / pass / day pass / fee"; bare "lounge" only in airport context (see above). A text that also says membership / dues / annual fee is NR-01, not AIR-07. |
| LOD-05 | "room upgrade", "hotel upgrade", "upgraded room" anywhere; "suite upgrade", "upgrade to (a) suite / club / executive / deluxe", the singular room type "suite" or a plain "upgrade" only in hotel context (so "Adobe Creative Suite upgrade" or a conference-room receipt's "AV upgrade" is never a room; a bare "room" / "rooms" is not hotel context — "room night / rate / charge / tax", "guest room", "per night", "folio", a brand are) — never a brand ("Embassy Suites") or an address ("Suite 400", "Suite #12", "Suite No. 4", "Ste #12"). A "Suite" followed by a stay length or date is a room line ("Executive Suite 2 nights", "Suite 1 night", "Suite 3 nts", "Suite 2x", "Junior Suite 11/12-11/14"). |
| NR-01 | a lounge brand / "lounge" together with membership / dues / annual fee / renewal / subscription. |

Amount at stake: the matching trusted receipt line items (the upgrade part of a folio),
else the whole claim; converted to USD with `process_rules.amount_in_usd` (the engine's
FX table); an unsupported currency is said out loud, never treated as USD.

| Owner's grade of record | Flag |
| --- | --- |
| C-suite, SVP | `policy_advisory` (advisory, never escalating) citing the clause: in policy |
| VP … Intern | `policy_violation`, blocking, justifiable (a justification makes it advisory so it can be submitted), `requires_approver` SVP; AIR-07's reason names the delay exception (Atlas has no delay data) |
| none / not on the ladder | `approval_required` by SVP, `could_not_verify: traveller_grade` (never auto-approved) |

Flags carry `rule_type = grade_entitlement` and `observed.process_rule`, so finance
filters / CSV label them "Rule: grade entitlement"; the flag types are the existing
catalogued ones (registry-vs-emitters test unchanged and green).

### The approver-grade level

The approval's **required grade** = the most senior ladder grade any flag names in
`requires_approver` ("manager" is the reporting line, not the Manager grade;
"Legal/Compliance" names no grade). Today that is SVP for AIR-07 / LOD-05 / NR-01 and
AIR-04 with no grade on record, and VP for the AIR-CABIN override (AIR-03 "with VP
approval"), a junior AIR-04 and a stale EXP-01 report.

* **Routing** (`route_by_grade`, run from `ensure_independent_approver` after every
  escalation, fallback approval and re-queue): walk up the submitter's manager chain to
  the first active manager / finance admin whose grade is at or above it; else an
  active finance admin of sufficient grade; else a finance admin with
  `routed_by = fallback_no_approver_of_grade` and a rationale saying no approver of that
  grade exists. Never the submitter (a self-managed user is skipped). A changed route
  writes `approval.routed_by_grade` to the audit log. Stored in `risk_summary`
  (`required_approver_grade`, `grade_routing`, `routed_to_approver_id`) — no schema change.
* **Visibility**: a manager also sees (queue, detail, "waiting on you" notification) an
  approval routed to them BY GRADE (`grade_routing` present) — an SVP above the
  submitter's direct manager. A plain routed id still widens nothing (existing
  notifications invariant kept).
* **Enforcement**: `decide_approval` refuses approve / reject / request changes by a user
  whose grade is below the required grade, or who has no grade, with **403
  `INSUFFICIENT_APPROVER_GRADE`** — after the unchanged SoD check (nobody decides their
  own submission). The requirement is read from the server-written flags and the stored
  grade (most senior wins), so a legacy pending approval with a VP flag is enforced too.
* **UI**: queue badge "Requires SVP"; detail "Approver level" section (grade, clauses,
  routed approver, rationale); buttons disabled with the server's message when
  `viewer_can_decide` is false.

Demo data (seed grades unchanged): employee@ (IC) → manager@ (SVP) can approve an
SVP-required item; employee2@ (Senior IC) → manager2@ (VP) cannot (403), and with no
SVP in that chain and finance@ a Director, it falls back to finance@ with the rationale.

## Key decisions

* **Process kind, not a per-line rule type.** PolicyEvalTool never evaluates these rows
  (`is_process_rule`), so per-line verdicts are unchanged — eval-db stays 48 with the
  same recall / coverage / abstention.
* **Owner, never requester.** The grade is the report owner's `users.grade`, org-scoped,
  looked up once per report; NULL / off-ladder / lookup failure = unknown (fail closed).
* **Junior = justifiable violation, unknown = approval required.** A junior grade is
  simply not covered ("not reimbursable"), but the claimant can still submit with a
  reason (e.g. AIR-07's delay) for an SVP-level approver; an unknown grade is a
  could-not-verify for the same level, mirroring AIR-04.
* **Every decision needs the grade**, as specified. Consequence: with no qualifying
  approver anywhere, the fallback finance admin must first have a qualifying grade
  recorded (by another finance admin — no one changes their own grade).
* **Existing VP flags are now enforced as a VP level.** The policy says "VP approval";
  before this change a line manager of any grade could approve them.
* **AIR-03's "< 8 hours before a critical business commitment, with VP approval"
  branch is not evaluated automatically.** Atlas holds no notion of a *critical*
  commitment (calendar events are simulated grounding data with no criticality, and a
  traveller-typed purpose is not evidence). What is enforceable is the approval half:
  the AIR-CABIN violation for business class under the AIR-03 threshold already names
  the VP override approver, which is now a real VP level — the VP confirms the
  commitment.
* **Corpus untouched.** The FAQ already answers lounge / suite / membership questions
  (Q51–Q59); adding units risked retrieval drift, so only a figure was factored out with
  byte-identical rendering.

## Limits (honest)

* AIR-07's "delays > 4 hours" exception cannot be verified (no flight-delay data): a
  junior grade's lounge fee is a violation the claimant justifies.
* Detection is keyword-based: an unusual wording on both receipt and entry ("VIP room
  surcharge") is not recognised; the approver still sees the line.
* "Suite <n>" with no stay length / date after the number is read as an address
  ("Suite 400"); a room line printed exactly as "Suite 2" (no "nights", no date) is
  therefore missed, and an address range "Suite 400-402" is read as a room line (a
  false positive the approver sees). Hotel / airline context words are a fixed list;
  an unbranded hotel with no hotel word anywhere on its folio, filed outside
  "lodging", is not placed at a hotel.
* A context-free "room upgrade" phrase on a non-hotel receipt (e.g. "Conference room
  upgrade" on a venue invoice) is still read as LOD-05 (a false positive the approver
  sees). An unbranded folio whose only hotel signal is a bare "Room" line (no "nights",
  rate, charge or brand) filed outside "lodging" is not placed at a hotel.
* Staging / live orgs where nobody has a grade recorded: every approval with a VP / SVP
  flag (including the pre-existing AIR-CABIN override, junior AIR-04 and stale EXP-01
  flags) routes to the finance-admin fallback and **cannot be decided until a finance
  admin records grades** on the Users page. Set grades on staging before demoing.
* The demo org has no SVP above manager2@: employee2@'s SVP-required items are only
  decidable after finance records an SVP finance admin or an SVP manager for manager2@.
* A routed-up SVP sees the approval (queue, detail with entries, notification); other
  pages that follow the direct-report scope (e.g. the report page itself, receipt
  images opened outside the approval detail) were not widened.
* Seeded demo data contains no lounge / suite / membership line to show the new flags.
* README's "23 revisions … 0023 head" lines are left for the documentation pass.

## How to verify

```
R=…/fu/run_grades.sh
$R -q ai_agents/tests/test_fu_grade_entitlements.py                 # detection, grades, FX, validation (115)
$R -q backend/tests/test_fu_grade_entitlements_consistency.py backend/tests/test_fu_svp_routing_unit.py
$R -q backend/tests/test_fu_grade_entitlements_db.py backend/tests/test_fu_svp_approval_level_db.py
$R -q -m 'not db' --ignore=evals/manual ; $R -q -m db ; $R -q backend/tests/eval -m db   # 48 passed
cd frontend && npx vitest run __tests__/approvals/fu-grade-approver-level.test.tsx
```

Guards proven red-when-removed (mutated, test red, restored): owner (not requester)
grade, receipt ownership, card merchant evidence, suite address/brand guard, unknown
grade never entitled, membership vs per-visit split, justification softening, migration
insert NOT EXISTS (re-run inserts nothing), engine reach (process rule), decide-time
grade enforcement, routed-by-grade scope and its `grade_routing` key, chain walk,
submitter exclusion, finance-admin-of-grade step, "manager" is not a grade, unknown
approver grade refused, lifecycle hook, notifications scope; UI: disabled buttons,
explanation note, queue badge, specific 403 toast.

Correction (review round): the first version of this log also claimed the migration
DOWNGRADE guards were proven red. They were not — the only downgrade case used a row
that both guards saved, so either one could be removed with every test green. The
review round added the missing tests and re-ran every mutation below.

Review-round guards, each proven red-when-removed on its own:

| Guard | Test that goes red |
| --- | --- |
| 0025 downgrade: `definition = literal` | `test_downgrade_keeps_every_rule_a_person_touched` (an admin-edited LOD-05 with no actor audit survives) |
| 0025 downgrade: no actor audit | same test (a literal AIR-07 an admin acted on survives) |
| 0025 downgrade: `policy_rule.unseeded` audit row | same test (asserts the system audit row of the removed NR-01) |
| `approval_scope` manager tenant filter | `test_routed_scope_never_crosses_tenants` (org-B approval routed to an org-A SVP: not listed, 404 detail, 404 decide, no notification) |
| `_eligible` active status | `test_inactive_svp_in_the_chain_is_skipped`, `test_inactive_only_svp_falls_through_to_the_finance_step` |
| `route_by_grade` pending-only | `test_a_decided_approval_is_never_rerouted` (approved, rejected) |
| `required_grade_of` stored grade (fail closed) | `test_stored_required_grade_survives_rewritten_flags` (VP refused 403) |
| chain walk tenant stop | `test_chain_never_walks_through_another_org` |
| both org checks together | `test_chain_crossing_into_another_org_falls_back_in_tenant` (finance fallback, foreign user 404) |
| `_eligible` org check (defence in depth) | `test_a_foreign_org_user_is_never_eligible` (unit) |
| card merchant org / owner | `test_card_transaction_from_another_org_is_not_evidence`, `test_another_users_card_transaction_is_not_evidence` |
| receipt org | `test_receipt_from_another_org_is_not_evidence` |
| trusted hotel / airline context (no category gate) | `test_recategorised_folio_still_raises_lod05[other/meals]`, `test_recategorised_upgrade_line_still_raises_lod05`, pure `test_*_whatever_the_category` |
| segment context: wired / org / owner | `test_owned_hotel_segment_places_the_line_at_a_hotel`, `test_foreign_or_other_users_segment_adds_no_context` |
| "Suite <n>" whole-number lookahead | `test_suite_room_lines_detected[Junior Suite 11/12-11/14]` |

## Review fixes round 3

Five adversarial-review blockers.

1. **Approvals already pending were never re-routed by grade.** `route_by_grade` only
   runs from `ensure_independent_approver` (submit / escalate / re-queue). A pending
   approval with a pre-existing `requires_approver` SVP (or legacy VP) flag, routed to a
   manager below that grade, got 403 `INSUFFICIENT_APPROVER_GRADE` from that manager and
   404 from the qualified SVP above (`approval_scope` widens only when `risk_summary` has
   a `grade_routing` key). Fix: an idempotent repair,
   `database/seeds/backfill_grade_routing.py` (modelled on
   `backfill_submitted_approvals`): every PENDING approval whose flags name a ladder grade
   and whose `risk_summary` has no `grade_routing` key goes through `route_by_grade`
   (a changed route audits `approval.routed_by_grade`; the repair itself audits
   `approval.grade_routing_backfilled`, actor system). One transaction per approval, exit
   1 on any row failure, a re-run selects nothing (`route_by_grade` writes
   `grade_routing`). Decided approvals and approvals naming no grade are never touched.
   **Boot wiring:** `docker/entrypoints/backend-entrypoint.sh`, `SEED_ON_STARTUP=policy_corpus`
   branch, `repair "grade-routing backfill" ...` right after the approval backfill — a
   failure only warns (`backend/tests/test_entrypoint_repairs.py`: happy-path trace
   updated, the module added to the failing-repair matrix). Not in migration 0025 (no data
   changes there). Staging / live databases get it on the next boot; local `docker compose`
   with `SEED_ON_STARTUP=true` is unchanged.
2. **`_eligible` role guard tested.** An employee-role user with an SVP grade in the chain
   is skipped (routed to the SVP manager above); unit case `_eligible` False for it.
3. **Malformed-rule fail-safe tested.** An active `grade_entitlement` rule with
   `min_grade = 'XYZ'` written straight to the DB yields a `not_evaluated` flag with
   `observed.could_not_verify == 'definition'`; the line is not dropped.
4. **`_fallback_finance` submitter exclusion tested.** Earliest finance admin is the
   submitter, no SVP anywhere: `fallback_no_approver_of_grade`, routed to the other
   finance admin, or to nobody when there is none — never the submitter.
5. This log.

Tests: `backend/tests/test_fu_grade_review3_db.py` (8, DB). Each guard proven
red-when-removed (mutated, test red, restored):

| Guard | Tests that go red |
| --- | --- |
| `_eligible`: `user.role in _DECIDING_ROLES` | `test_employee_role_svp_in_the_chain_is_never_routed_to`, `test_an_employee_role_user_is_not_eligible_whatever_the_grade` |
| `grade_checks` except clause (`ValueError`) | `test_a_malformed_grade_rule_yields_a_not_evaluated_flag` |
| `_fallback_finance`: `User.id != approval.submitter_id` | `test_fallback_finance_never_picks_the_submitter`, `test_fallback_finance_with_only_the_submitter_routes_to_nobody` |
| backfill calls `route_by_grade` | `test_backfill_routes_a_pre_existing_svp_flag_to_the_svp_and_is_idempotent`, `test_backfill_main_exits_1_when_a_row_fails` |
