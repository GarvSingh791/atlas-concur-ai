# Phase 9 — Product gap 2 (policy2): spend caps, gift disclosure and mileage

Track: `pg/policy2` · base `2fbe114` (alembic head `0015_canonical_policy_figures`) ·
migration `0019_spend_caps_mileage` (down_revision `0015_canonical_policy_figures`;
the integrator re-chains it).

## What

The policy corpus (TEP-FY26) was quoted by the Q&A assistant, but only the 7 stub
rules were enforced. This track adds structured, deterministic, Decimal rules for
**MEAL-05, ENT-02, ENT-04, MIL-01, MIL-02, MIL-03**, the entry data they need, a
**mileage** feature that did not exist, and the UI for it.

| Layer | Change |
|---|---|
| Models / migration 0019 | `expense_category` += `mileage`, `entertainment`, `gift`; `policy_rule_type` += `per_head_cap`, `gift_disclosure`, `mileage`; `expenses` += nullable `attendee_count` (CHECK ≥ 1), `disclosure` JSONB, `mileage` JSONB; six active rules inserted into each org's stub policy (audited, idempotent). |
| Seeds | `database/seeds/spend_rules_content.py` (`SPEND_RULES`, derived from `corpus/figures.py`), appended to `policy_content.RULES` for fresh databases. `figures.MILEAGE_RATE_USD_PER_MILE = "0.70"` (not rendered into corpus prose). |
| Engine | `ai_agents/tools/spend_policy.py` — pure evaluators, `compute_mileage`, applicability. `policy_eval.py` gets one new section (`_eval_spend_rule`, `_receipt_exempt_verdict`) plus a dispatch line, a `rule_applies` hook and `EvalItem.spend`. |
| Pre-submit / approval | `presubmit.build_eval_item` / `build_entry_eval_item` attach the entry's spend facts; `per_head_cap` is justifiable (manager); team meals leave the per-diem day total; approval-time evaluation reuses the same `run_presubmit`. `/policy/evaluate` restores the facts (`restore_spend`). |
| Rule semantics | validators + engine-derived descriptions for the three new rule types (`/policy` page, activation gate, authoring probes). |
| API | `POST /expense-reports/{id}/mileage` (owner, draft, If-Match) — amount computed server-side; `PATCH /expenses/{id}` accepts `attendee_count`, `disclosure`, `mileage` (category-gated); entry responses carry them (approver detail included). |
| Frontend | Entry editor: attendees (meals / entertainment), disclosure (gift / entertainment), trip (mileage; amount read-only, rate + billable miles shown); "Add mileage" dialog on an owner's draft; labels / icons for the new categories; readiness hints for disclosure / mileage blocks. |

## Behaviour tables

Flag mapping (unchanged machinery, `presubmit.verdicts_to_flags`): failing verdict →
`policy_violation` **blocking** (justification softens it only for justifiable rule
types — now incl. `per_head_cap` with a manager approver); in-policy verdict with an
approver → `approval_required` **advisory** that never auto-approves
(`routing.decide_route` requires `requires_approver == False`). Every flag carries
`source_clause_ref` = the clause id and the reason names it.

### MEAL-05 — team meals (`per_head_cap`, applies_to `meals`)
Clause: the most senior employee present pays and expenses, listing all attendees;
per-head dinner caps Tier 1 $90 / Tier 2 $75 / Tier 3 $60.

| Line | Result |
|---|---|
| meals, `attendee_count` null or 1 | rule does not apply (individual meal → MEAL-01 per diem as before) |
| meals, ≥ 2 attendees, amount ≤ cap(tier) × attendees | in policy, but `approval_required` advisory → manager, `observed.could_not_verify = attendees` (the count is unverified — never auto-approved); MEALS-PD **not** applied to a team meal |
| meals, ≥ 2 attendees, but the org has **no active** MEAL-05 (archived, or 0019 skipped it) or its `min_attendees` exceeds the count | MEALS-PD **still applies** and the line stays in the per-diem day total (fail closed — a typed attendee count never leaves a meal without a meal rule) |
| … amount = cap × attendees + $0.01 | policy_violation, blocking, justifiable → manager |
| no verified city | Tier 3 (lowest); a city named in description/vendor that would fit → advisory `pending_verification=city`, still escalates |

### ENT-02 — client entertainment (`per_head_cap`, applies_to `all`, category `entertainment`)
Clause: client meals up to $150/head (Tier 1) / $120 / $100 (Tier 3); larger events require pre-approval.

| Line | Result |
|---|---|
| amount ≤ cap × attendees, attendee count given | in policy, `approval_required` advisory → manager (confirm the attendee list, ENT-01) |
| amount ≤ cap, no attendee count | in policy (no flag — nothing claimed, checked as one person) |
| over | policy_violation, blocking, justifiable → manager ("Larger client events require pre-approval") |
| no attendee count | evaluated as **one** person (fail closed), reason asks for the count |

### ENT-04 — gifts / government officials (`gift_disclosure`, categories `gift`, `entertainment`)
Clause: entertainment of government officials requires Legal/Compliance pre-approval; gifts > $100 require disclosure.

| Line | Result |
|---|---|
| gift $100.00 | no disclosure needed, no flag |
| gift $100.01 without recipient + business purpose + government-official answer | policy_violation, **blocking, not justifiable** — lists the missing fields |
| gift > $100 with a complete disclosure | in policy; disclosure stored on the entry and in the `expense.updated` audit row |
| gift / entertainment with `government_official = true` | approval_required → "Legal/Compliance", never auto-approved (Atlas cannot verify the pre-approval) |
| gift with unknown amount | disclosure required (fail closed) |

### MIL-01 / MIL-02 / MIL-03 — mileage (`mileage`, category `mileage`, one rule per clause)

Amount = billable miles × rate, Decimal, cents ROUND_HALF_UP; billable = one-way
miles (×2 round trip) − commute offset; km → miles ÷ 1.609344, to 0.1 mi.
Rate = the org's active MIL-01 `rate_per_mile` (lowest if several), else
`figures.MILEAGE_RATE_USD_PER_MILE`.

| Check | Line | Result |
|---|---|---|
| MIL-01 rate | amount == billable × rate | in policy, but `approval_required` advisory → manager, `observed.could_not_verify = distance` (Atlas has no route data — never auto-approved) |
| | amount differs (e.g. rate changed since saving) | policy_violation, blocking, not justifiable — "save the trip again"; the entry editor then shows a recompute hint and **Save changes re-sends the unchanged trip** so the server recomputes it |
| | mileage line without trip details | blocking |
| | line currency is not USD (only a legacy / hand-made row can be) | blocking, not justifiable — "a mileage line is always in USD" |
| MIL-02 commute | commute offset recorded (0 allowed) | in policy, but `approval_required` advisory → manager, `observed.could_not_verify = commute_offset` (Atlas has no home / office data) |
| | missing | blocking (the API makes it required, so only legacy / hand-made rows hit this) |
| MIL-03 air cap | ≤ 300.0 mi one way | in policy |
| | 300.1 mi | approval_required → manager: "could not verify against the lowest logical airfare" (or "air impractical — confirm") |
| RECEIPT-REQ | mileage of any amount | receipt **not** required (REF-08 / EXP-03), no missing_receipt flag |

Write-path rules: `POST …/mileage` refuses a sent `amount` that differs (422) and any
unknown field such as `rate_per_mile` (422); PATCH refuses an amount change on a
mileage entry, a category change to / from mileage, a non-USD currency (with or
without a `mileage` trip in the same PATCH), attendees
outside meals / entertainment and a disclosure outside gift / entertainment (all 422);
fields that no longer fit a changed category are cleared (also when a receipt
`expense_type` correction changes the category). A receipt can never be attached
to a mileage entry (`POST /expenses/{id}/receipts` and an upload with `expense_id`
→ 422, REF-08), and a receipt correction never writes amount / currency / category
onto a mileage entry (`linked_expense_sync = not_applicable`). `POST /expense-reports`
refuses `mileage` entries. Receipt / card categorisation never yields the three new
categories (`rule_categorizer._VALID_CATEGORIES`), so OCR paths are untouched.

## Key decisions

- **New rule types** instead of overloading `amount_cap`: the semantics validator
  already rejects meal `amount_cap`s, and a dedicated type keeps the old evaluators
  byte-identical. Spend rules reach ONLY their lines (`spend_rule_applies`), so no
  existing line gains a verdict or a `not_evaluated` note → eval-db unchanged.
- **New categories** `entertainment` and `gift` (no such categories existed; HOW-23/24
  name "Team Meal" and "Gifts & Hospitality"). A team meal is a `meals` line with ≥ 2
  attendees rather than another category.
- **Team meal excludes the per diem — only while a per-head cap covers it** (review
  fix): MEAL-01 is the claimant's own allowance; the host's team dinner is capped per
  head by MEAL-05. The decision is made against the evaluated rule SET
  (`PolicyEvalTool._filter_applicable` + `spend_policy.team_meal_cap_minimum`), not
  per rule: MEALS-PD is dropped for a meals line only when an active `per_head_cap`
  / `meals` rule reaches it (its `min_attendees`). Likewise a team meal leaves the
  trip's per-diem day total (`presubmit._segment_meal_total`) only under the same
  condition. With MEAL-05 archived — or never inserted because the stub policy
  already had an (archived) rule citing MEAL-05, which 0019 deliberately leaves alone
  — every meals line keeps the per diem.
- **Mileage rate figure**: MIL-01 names no number ("prevailing IRS rate, updated
  annually"). Seeded 0.70 USD/mi (IRS 2025 business rate, the last one confirmed when
  this was built); finance updates `rate_per_mile` on the MIL-01 rule each year and
  older lines are then flagged until re-saved.
- **A claimed attendee count always reaches a human** (review fix): it multiplies
  the per-head cap and, for a team meal, replaces the per diem, yet Atlas captures
  no attendee list. An in-policy MEAL-05 line (≥ 2 attendees) or ENT-02 line with a
  count is an `approval_required` advisory to the rule's exception approver, so the
  report escalates instead of auto-approving.
- **Receipts and mileage** (review fix): the attach is refused (option a — REF-08
  means mileage has no receipt) and, as defence in depth for any link that predates
  the guard, `_sync_linked_expense` skips the computed columns of a mileage entry.
- **Mileage always reaches a human** (review fix): distance and commute offset are
  the claimant's word (no route, home or office data), and REF-08 exempts mileage
  from RECEIPT-REQ, so before this fix a mileage-only report ≤ $500 auto-approved
  with no flag at all. An in-policy MIL-01 line now names the manager to confirm the
  route distance and an in-policy MIL-02 line the commute offset (`approval_required`
  advisories, never blocking), so `decide_route` escalates. Two advisories instead of
  one: each cites the clause whose input it cannot verify, and either alone keeps the
  control if the other rule is archived.
- **Re-computing after a rate change** (review fix): the entry editor sends the
  unchanged trip on Save whenever the server attributes a blocking `mileage`
  policy_violation to the entry (`diffEntryChanges(..., { recomputeMileage })`); the
  server recomputes idempotently. Without such a flag an untouched mileage entry still
  saves nothing.
- **MIL-03 is advisory + escalating**, not blocking: Atlas has no airfare quote, so the
  derivable subset is "flag every > 300-mile trip to a human".
- **Government officials** → `approval_required` to "Legal/Compliance": Atlas has no
  such approval tier; the flag guarantees a human sees it (never auto-approved) and
  the approver UI shows the requirement verbatim.
- **Clause citations**: `source_clause_ref` is the TEP-FY26 id itself (MEAL-05 …).
  Three stub-only tests that paired every rule with the frozen stub CLAUSES were
  narrowed to the stub rules, and a new assertion requires every spend rule to cite
  a real TEP-FY26 clause (intent changed, not weakened).
- Migration 0019 follows the 0015 pattern: rows inserted only where the stub policy
  has no rule citing that clause (an admin-edited / archived rule is never touched),
  one audit row per insert, idempotent, no-op on a fresh database. Downgrade refuses
  while any expense uses a new category, deletes the new-type rules with audit rows
  (a customised one is logged at WARNING) and drops the columns; PostgreSQL cannot
  drop enum values, so the six values stay (re-added idempotently).
- **Third review round — a human check must be checkable from the approval screen.**
  (1) Every entries table (the approver's decision panel included) renders the
  stored policy facts under the description (`EntryPolicyDetails`, read-only, DTO
  values only): the trip (from → to, one-way distance + unit, round trip, commute
  offset, air-impractical, billable miles × rate with its clause), the attendee
  count, and the disclosure (recipient + organisation, business purpose, government
  official Yes / No / Not stated). A mileage line without a trip says "Trip details
  missing". Owner screens show the same block (harmless, and the owner sees what the
  approver will see). (2) MIL-01 / MIL-02 / MIL-03 reasons start with
  "Mileage <origin> → <destination>:" and carry `origin` / `destination` in
  `observed` (an unrecorded route is said out loud: "route not recorded"); the
  ENT-04 government-official reason names the recipient (organisation) and business
  purpose. The exception list alone therefore identifies what to confirm. (3) A trip
  PATCH regenerates the description only while it still equals the previously
  generated `<old origin> → <old destination>` and the PATCH sends no description —
  a claimant's own text, or a description in the same PATCH, is never overwritten
  (the change is in the existing `expense.updated` audit row's before/after).
- **Fourth review round — a mileage amount can never be relabelled.** A PATCH that
  carried both a `mileage` trip and `currency: "EUR"` stored the USD-computed amount
  as EUR 154.70 with no blocking flag: the trip branch set `currency = USD` but left
  the client's currency in the update dict, and the generic `setattr` loop in
  `update_entry` then overwrote it. The currency check now runs for every edit of a
  mileage entry (before the trip branch) and pops the field, so a non-USD currency is
  a 422 whether or not a trip is sent, and nothing downstream can write it. Defence
  in depth: `SpendFacts` carries the line's currency (`entry_spend_facts`,
  `restore_spend`) and MIL-01 blocks a mileage line whose currency is not USD, so a
  row relabelled before this fix (or by hand) can never pass pre-submit or approval.
  An unknown currency (`None`, e.g. a rule-authoring probe) is not judged.

## Limits / known gaps

- MIL-03: no airfare comparison (no fare data) — always escalated instead.
- MIL-01: US IRS rate only; "local statutory rate" for non-US is not modelled
  (mileage entries are always USD). No route mapping / distance verification:
  distance and commute offset are the claimant's statement — every mileage line is
  therefore escalated to the manager to confirm them (MIL-01 / MIL-02 advisories); a
  mileage report never auto-approves.
- If an org archives every mileage rule (MIL-01..03), mileage lines are evaluated
  against nothing (REF-08 still exempts them from RECEIPT-REQ) — an explicit admin
  choice, as for any archived rule.
- Attendee names are not captured (ENT-01 names / companies, MEAL-05 "listing all
  attendees"): the count is escalated to the approver instead, who checks the list in
  the description. MEAL-05's "most senior employee pays" and ENT-03 (alcohol
  moderation) are not enforced.
- ENT-04 Legal/Compliance pre-approval is not verifiable; it is escalated.
- ENT-02 / entertainment lines resolve their city tier only from text the claimant
  wrote (unverified → lowest tier + pending verification); a linked trip segment's
  city is not used for entertainment lines.
- `POST /expense-reports` (create with entries) does not accept attendees /
  disclosure; set them with PATCH.
- docs/DATABASE_SCHEMA.md / TECHNICAL_DESIGN.md were not edited (shared docs; left to
  the integrator to avoid cross-track conflicts) — this log is the reference.

## How to verify

```
run_policy2.sh -q -m 'not db' --ignore=evals/manual         # hermetic
run_policy2.sh -q -m db                                     # DB suite
run_policy2.sh -q backend/tests/eval -m db                  # eval-db baseline: 48 passed
run_policy2.sh -q ai_agents/tests/test_pg2_spend_policy.py backend/tests/test_pg2_spend_mileage_db.py backend/tests/test_migration_0019_spend_caps_mileage.py
cd frontend && npx vitest run __tests__/expenses/pg2-spend-mileage.test.tsx
```

Guards proven red→green by temporary removal: REF-08 receipt exemption, team-meal
per-diem exclusion, client-amount refusal on mileage, mileage category refusal on
create, category change into / out of mileage, gift threshold strictness (> vs ≥),
spend-rule reach restriction, manual-only categorisation, `per_head_cap`
justifiability, and (frontend) "never send a mileage amount". Review fixes, same
proof: receipt attach refusal (service + upload pre-check), the mileage skip in the
receipt-correction sync, the category-change field clearing on that path and the
attendee-count escalation (hermetic + DB `decide_route == escalate`). Second review
round, same proof: the rule-set-level per-diem exclusion (hermetic: MEALS-PD-only rule
set keeps MEALS-PD on a 2-attendee $400 meal; DB: archived MEAL-05 keeps the MEALS-PD
flag and the per-diem total), the MIL-01 / MIL-02 "could not verify" advisories (DB: a
290-mile round trip, $406, `decide_route == escalate`) and (frontend) the recompute
re-send on a blocked mileage line.
Third review round, same proof: the trip-edit description regeneration (DB: red until
the PATCH regenerated it), its "claimant text survives" condition (DB: red when the
equality check is removed), the route / recipient in the flag reasons (hermetic) and
(frontend) the approver's decision panel showing trip, attendees and disclosure
(`__tests__/expenses/pg2-approver-policy-details.test.tsx`, red before wiring).
Fourth review round, same proof: the currency check on a trip PATCH (DB: `{mileage,
currency: EUR}` returned 200 / stored EUR until the check moved ahead of the trip
branch; removing it also reds the older no-trip currency test) and the MIL-01
non-USD block (hermetic + DB: a row forced to EUR gets a blocking MIL-01 flag; red
when the check is removed).
