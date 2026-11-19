# Phase 9 — Product gaps: policy enforcement (track `pg/policy`)

Base: `2fbe114` (origin/main, alembic head `0015_canonical_policy_figures`).
Branch: `pg/policy`. Each stage appends its own section; stage 2 follows stage 1.

---

## Stage 1 — entry-level rules, ground-transport semantics, rule history

Product gap 2 (entry-level policy rules) and two items of gap 8 (ground-transport
sub-types, per-rule change history).

### What was built

| Area | Files |
| --- | --- |
| Ground-transport sub-type classifier (deterministic, word-boundary, ambiguous -> broad) | `ai_agents/tools/ground_transport.py` |
| Entry-level evaluators (pure, `Decimal`, fail-safe) | `ai_agents/tools/entry_rules.py` |
| Engine wiring (kind dispatch, exclusion verdicts, LOD-02 / MEAL-02 hooks, sub-type reach) | `ai_agents/tools/policy_eval.py` (localised hooks only) |
| Evidence for the item (OCR line items / tip / parking days, trust boundary, rail class) | `ai_agents/agents/expense_agent/entry_context.py`, `presubmit.py` |
| Flag mapping (`policy_advisory`, "could not verify" -> `not_evaluated`, justifiable marker) | `ai_agents/agents/expense_agent/presubmit.py` |
| Validation + plain-English summaries + authoring narrowing | `ai_agents/agents/entry_rule_semantics.py`, `policy_rule_semantics.py`, `policy_authoring.py` |
| Planner options are "booked through the tool" (LOD-02) | `ai_agents/agents/travel_agent/ranking.py` |
| Seeds (fresh DBs) | `database/seeds/policy_content.py` (`HOTEL_CAP_DEFINITION` / `MEALS_PD_DEFINITION` keys, new `ENTRY_RULES`), `seed_all.py` |
| Live-DB data migration | `database/migrations/versions/0017_entry_level_policy_rules.py` |
| Rule history API | `backend/app/modules/policy/history.py`, `schemas.py`, `router.py` (`GET /api/v1/policy/rules/{rule_id}/history`) |
| Rule history UI (replaces "coming soon") | `frontend/components/policy/rule-history.tsx`, `lib/query/policy-hooks.ts`, `lib/types/policy.ts`, `app/(app)/policy/page.tsx`; `policy_advisory` hint in `lib/expenses/readiness.ts`; labels in `rule-definition.tsx` |

### The clauses, as written (TEP-FY26 corpus, `database/seeds/corpus/policy.py`)

* **LOD-02** "conference hotels and rates exceeding the cap by ≤ 15% are auto-approved when
  booked through the tool; > 15% requires manager approval with justification."
* **MEAL-02** "75% of per diem on departure and return days."
* **MEAL-04** "personal alcohol is not reimbursable under per diem or individual meals;
  alcohol is reimbursable only as part of approved client entertainment … with attendee
  documentation."
* **GRND-01** "Standard/Economy class for trips < 3 hours; First/Business rail permitted for
  trips ≥ 3 hours if cheaper than or comparable to flying." (No advance-booking rule exists in
  the clause — none is invented.)
* **GRND-03** "airport parking reimbursable up to 7 days (economy lot)."
* **GRND-05** "Tolls and reasonable tips (≤ 20%) are reimbursable with the ride."

All figures come from `database/seeds/corpus/figures.py` (`LODGING_CAP_AUTO_EXCEPTION_PCT`,
`PER_DIEM_PARTIAL_PCT`, `RAIL_FIRST_MIN_HOURS`, `AIRPORT_PARKING_MAX_DAYS`, `TIP_MAX_PCT`).

### How a verdict becomes a flag (the UI already renders every type)

| Verdict | Flag type | Severity at submit | At approval |
| --- | --- | --- | --- |
| breach (`in_policy=False`) | `policy_violation` | blocking; advisory once a justification is written when justifiable | always escalates (never auto-approved) |
| in policy through the clause's own allowance (`observed.advisory`) | `policy_advisory` (new) | advisory | does not escalate (the clause auto-approves) |
| rule applies but evidence is missing (`observed.could_not_verify`), reason promises an approver check (MEAL-04 keyword only, GRND-01 class / GRND-03 days / GRND-05 tip or fare unverified; `observed.approver_check`) | `not_evaluated` with `requires_approver` = the rule's approver | advisory ("Couldn't be auto-checked") | **always escalates** (round 4: `requires_approver` keeps it out of auto-approval) |
| rule applies but evidence is missing, no approver check promised (MEAL-02 unverified travel day) | `not_evaluated` | advisory ("Couldn't be auto-checked") | does not escalate — same as the existing `not_evaluated`; its wording promises no approver check |
| in policy | — | — | — |

Every flag carries the clause code: the verdict's `source_clause_ref` is the corpus clause
that decided it (`LOD-02`, `MEAL-02`, `MEAL-04`, `GRND-01/03/05`) even when the rule row
cites the stub section (`HOTEL-CAP`, `MEALS-PD`).

Justifiability: `presubmit.is_justifiable` additionally accepts a verdict whose
`observed["justifiable_exception"]` is true (the entry-level rules whose breach a manager may
accept). The approver ceiling (`manager` only) and the unverified-supplier guard still apply.

### Behaviour tables

**LOD-02 — hotel nightly cap tolerance** (modifier of the HOTEL-CAP `amount_cap`; per-night,
room + tax, LOD-01 tiers; amount taken to the cent, compared exactly
`(rate − cap) × 100 ≤ 15 × cap`)

| Nightly rate vs cap | Booked through the tool? | Result |
| --- | --- | --- |
| ≤ cap | any | in policy, no flag (unchanged) |
| > cap, ≤ 15.00% over | yes | in policy, `policy_advisory` citing LOD-02 ("…is 15.00% over … auto-approved") |
| > cap, ≤ 15.00% over | no | HOTEL-CAP `policy_violation` (manager, justifiable) + sentence explaining LOD-02 applies only to tool bookings |
| > 15.00% over (15.01% and up) | any | the pre-existing HOTEL-CAP violation, byte-identical (LOD-02's "> 15% requires manager approval with justification") |

"Booked through the tool" (`EvalItem.booked_through_tool`, default False — `build_eval_item`
never sets it) is true only for a planner option (`travel_agent.ranking`), a hotel segment the
**planner** booked at trip approval (`presubmit.is_planner_booked_hotel`: the trip carries the
server-set planner `recommendation_context` and the segment's `details.option_id` is one of
its `selected_option_ids`), and a lodging line linked to such a planner-booked **hotel**
segment of the traveler's **human-approved** trip whose receipt evidence matches
(`build_entry_eval_item`; round 4). A hotel typed into POST /trips is never a tool booking. A flight / car / rail segment is never a tool booking, and a deterministic
receipt vendor / card merchant that does not name the segment's hotel supplier (or a receipt
vendor the claimant overwrote with no card merchant confirming it, or a booking with no
supplier to compare) clears it (`build_entry_eval_item`). Claimant text never sets it.
Round 3: on an expense line it additionally needs a receipt / card naming the booked hotel
and the receipt's OCR city matching the segment; no evidence → manager exception.

**MEAL-02 — partial days** (modifier of MEALS-PD `per_diem`)

| Situation | Allowance compared | Result |
| --- | --- | --- |
| meal linked to an approved trip of N covered days (N ≥ 2) | rate × (N − 2) + 75% × rate × 2 against the pooled trip meal total | breach cites MEAL-02 when the total is within the full-rate allowance, else MEALS-PD |
| same-day trip (1 covered day) | 75% × rate (the day is both departure and return, counted once) | as above |
| meal not linked to an approved trip, amount ≤ 75% × rate | — | in policy, no flag |
| meal not linked, 75% × rate < amount ≤ rate | — | in policy + `not_evaluated` ("could not verify whether it falls on a departure or return day", MEAL-02) |
| meal not linked, amount > rate | — | the existing MEALS-PD violation (unchanged) |

Day classification: the trip's covered days are the union of its dated segments' calendar
days (existing `_trip_meal_scope`; overlapping segments count once). Departure = first
covered day, return = last covered day (`entry_rules.travel_day_count`). Multi-segment trips
therefore get exactly two partial days; a gap between segments does not add partial days.

**Exclusions (MEAL-04 alcohol; generic for any rule's `exclusions`)**

| Evidence | Result |
| --- | --- |
| OCR line items name an excluded item (wine, beer, cocktail, … — `entry_rules.EXCLUSION_TERMS`) and the claim includes it (round 3: `excluded_in_claim`) | the excluded amount inside the claim taken out before the per-diem compare + an explicit `policy_violation` citing MEAL-04 with the excluded and reimbursable amounts (manager may accept it as approved client entertainment → justifiable) |
| itemized excluded item the claim already leaves out | in policy, no flag; the per diem compares the full claim |
| no line items, but vendor / description / card merchant names an excluded item | `not_evaluated` ("could not verify the alcohol exclusion … the receipt is not itemized") |
| "root beer", "ginger ale", "non-alcoholic beer", "mocktail" | not excluded |
| an exclusion term not in the vocabulary (admin-added, e.g. "spa") | matched by its own words |

**GRND-01 — rail class** (`kind: rail_class`, `category_allow` / `all`, reaches only lines
classified as rail)

| Class | Journey | Result |
| --- | --- | --- |
| standard / economy (from the trusted rail segment) | any | in policy |
| first / business | < 3 h | `policy_violation` (manager, justifiable) |
| first / business | ≥ 3 h (3.00 h included) | `policy_violation` — permitted only if cheaper than or comparable to flying, which Atlas cannot verify; the justification carries the fare comparison |
| first / business | unknown | `policy_violation` (the 3-hour threshold could not be confirmed) |
| unknown, or "standard" typed by the claimant | any | `not_evaluated` (could not verify the class) — a typed "standard" never clears the rule |

**GRND-03 — airport parking** (`kind: parking_days`, `amount_cap` / `all`, reaches only lines
classified as parking **and shown to be airport parking**)

Airport parking (`EvalItem.airport_parking`, `entry_context.ground_fields`): the words —
entry vendor / description, card merchant, the receipt's deterministic OCR vendor or line
items — say "airport", name an airport park-and-fly operator ("Park 'N Fly", "The Parking
Spot", "WallyPark", "terminal garage" …) or a major-airport IATA code in capitals
(`ground_transport.airport_named`; codes that are also common upper-case words / city
prefixes — SAN, LAS, MIA, AUS … — are left out), **or** the line is linked to a flight
segment. Hotel valet, an hourly city garage, SpotHero near a client … are parking but not
airport parking: GRND-03 does not reach them at all (no flag).

| Days | Result |
| --- | --- |
| ≤ 7 (OCR `parking_days` / `days` READ from the receipt) | in policy |
| ≥ 8 | `policy_violation` with the pro-rata reimbursable share (7 of N days), manager, justifiable |
| valet / premium / short-term / hourly lot named | `policy_violation` (economy lot) |
| only stated in the description, > 7 | used (a claim against interest) → violation |
| only stated in the description, ≤ 7, or unknown | `not_evaluated` (could not verify); when the line is linked to an approved trip the reason names its length as context only ("the linked trip spans 9 days; the receipt does not say how many of them were parked") — the trip length is never used as parking days |

**GRND-05 — tips** (`kind: tip_pct`, `amount_cap` / `all`, reaches only rideshare/taxi lines)

Tip = OCR `tip` / `gratuity`, else the sum of tip line items. Fare = the RECEIPT's OCR total
− tip (round 3; the sum of line items when the tip is a line item; unknown → `not_evaluated`).
Only the tip inside the claim (claim − fare) can breach.
Compared exactly: `tip × 100 ≤ 20 × fare`.

| Situation | Result |
| --- | --- |
| tip ≤ 20.00% of fare | in policy |
| tip > 20% (20.01% …) and the claim holds more tip than 20% of fare | `policy_violation`, reimbursable tip = 20% of fare, manager, justifiable |
| tip > 20% but the claim leaves (the excess of) it out | in policy, no flag |
| no tip itemized, none mentioned | in policy (`tip_identified: false`), no flag |
| "tip" mentioned, no amount | `not_evaluated` |
| fare ≤ 0 | `not_evaluated` |

### Ground-transport semantics (gap 8)

`classify_ground_transport(vendor, description, card merchant, OCR vendor, OCR line items,
segment_type=…)` returns
`rideshare_taxi`, `car_rental`, `rail`, `transit`, `parking`, or the broad `ground_transport`
when nothing is recognised **or** more than one family is named ("Uber to the train" —
fail safe). Longer phrases are consumed first ("light rail" is transit; "Enterprise
Rent-A-Car" is one rental match); never substring matching ("Cabernet" is not a cab).

* The receipt's own words are classified together with the claimant's
  (`entry_context.receipt_texts`: the OCR vendor only with provenance `deterministic`, plus
  the OCR line-item descriptions). A claimant who types "Amtrak" over an Uber receipt gets
  an ambiguous line → broad category → the authored rideshare cap still reaches it (and the
  GRND-01 rail rule does not).
* Rules may carry `ground_subtypes` (only with `applies_to: all` +
  `expense_categories: ["ground_transport"]`; validated).
* An authored sub-type cap ("rideshare cap $50") reaches lines of that sub-type **and lines
  Atlas could not classify** (they may be a rideshare — fail safe); the flag on an
  unclassified line says so and the employee can justify it. It never reaches lines
  classified as another sub-type (trains, parking …).
* The seeded `kind` rules reach lines verified as their sub-type, and unverified lines
  whose words / evidence name it as a candidate (round 3, security review — see below).
  A line that no trusted evidence identifies at all is UNCONFIRMED and every kind rule
  reaches it as a could-not-verify (round 5).
* Authoring: `entry_rule_semantics.narrow_ground_scope` narrows a broad ground-transport cap
  to the sub-types the intent names (same classifier), before validation and the
  faithfulness judge; "ground transport" / "transportation" intents stay broad. The draft
  summary states precisely what is covered and what is not; the title names the sub-type
  ("Rideshare/taxi cap — Standard $75/item").

### Rule history (gap 8)

`GET /api/v1/policy/rules/{rule_id}/history` — finance admins and managers
(`require_role`, deny-by-default: employees 403); the rule must belong to a policy of the
caller's organization (404 otherwise, indistinguishable from a missing rule); audit rows are
additionally filtered on `organization_id`. Each entry: action + readable label, timestamp,
actor name (or `null` = system), whitelisted field changes (`status`, `title`, `rule_type`,
`applies_to`, `source_clause_ref`, and `definition.<key>` per changed definition key) with
before → after, and the system `source` note. Authoring intent, AI decision ids and
activation summaries stored on audit rows are never returned. Read-only.

The policy page renders the history under the selected rule (newest first, empty / error /
retry states); the "Full per-rule change history coming soon" text is gone.

### Migration 0017 (`0017_entry_level_policy_rules`, down 0015)

Data only — no schema or enum change (new rules reuse `amount_cap` / `category_allow` with a
`kind` discriminator). Upgrade:

1. HOTEL-CAP / MEALS-PD rows whose definition still equals the canonical 0015 literal (jsonb
   equality) get the LOD-02 / MEAL-02 / MEAL-04 keys; anything else is left alone and
   reported at WARNING. One `policy_rule.definition_updated` audit row each.
2. GRND-01 / 03 / 05 are inserted (active) into every policy that carries a HOTEL-CAP rule,
   only when that policy has **no** rule citing the clause (any status — an archived or edited
   one is never re-added); one `policy_rule.seeded` audit row each.

Idempotent; fresh databases are a no-op (the seed writes the new values). Downgrade deletes
only inserted rows (`created_by IS NULL`) whose definition still equals the literal (audited
`policy_rule.unseeded`) and reverts the two definitions that still equal the new literal.

### Key decisions

* **Modifiers, not new rows, for LOD-02 / MEAL-02 / exclusions** — they change how the
  existing caps compare; a second row would double-flag the same line.
* **No enum change** — `kind` rides on existing rule types so the migration is pure data and
  needs no non-transactional `ALTER TYPE`.
* **Entry rules cite the corpus clause** (`GRND-01` …) as `source_clause_ref`; the frozen
  8-section stub document has no section for them (two tests that asserted "every rule cites a
  stub section" were updated to allow `ENTRY_RULES`' corpus clauses — intended change).
* **"Could not verify" is advisory at submit but ESCALATES to the approver**
  (`not_evaluated` with `requires_approver` = the rule's approver, round 4): the line can
  be submitted, the report is never auto-approved. (Only MEAL-02's unverified travel day,
  which promises no approver check, stays a plain non-escalating `not_evaluated`.) A real
  breach is always a `policy_violation` (escalates). Where the evidence positively shows a
  premium class / a long stay / an itemized alcohol line, the outcome is a violation, not
  an advisory. *(Corrected in integration round 2: this bullet used to say "advisory and
  non-escalating", which round 4 had already changed.)*
* **> 15% hotel verdicts are byte-identical** to before (tested), so stored snapshots and
  demo texts don't drift.
* **History route** is one path (`/policy/rules/{id}/history`, as specified) to keep the
  contract-matrix surface small; the matrix count moved 41 → 42.

### Tests changed because the intended behaviour changed

* `ai_agents/tests/test_c7_05_15_policy_gates.py::test_over_cap_on_real_segment_still_flags`
  — 310/night on an approved-trip hotel is now LOD-02 auto-approved; the test uses 350/night
  (16.7% over) and a new sibling test asserts the 310/night `policy_advisory`.
* `ai_agents/tests/test_issue5_rule_authoring_failures.py::test_retest_sentence_drafts_the_ground_transport_cap`
  and `backend/tests/test_issue5_rule_authoring_api_db.py::test_retest_sentence_drafts_faithful_ground_transport_cap`
  — "Rideshare trips over $75…" now drafts a rideshare/taxi-only cap (gap 8).
* `backend/tests/test_migration_0015_canonical_policy_figures.py` — the seed now carries the
  0017 keys on top of 0015's literal.
* `backend/tests/test_p3_seed.py::test_policy_rules_consistency`,
  `ai_agents/tests/test_rag_indexer.py::test_all_rule_clause_refs_have_embedding` — allow the
  `ENTRY_RULES` corpus clause refs.
* `backend/tests/api/endpoints.py` / `test_contract_matrix.py` — the new route.
* Review round 1 (own tests of this stage): `TestGrnd03Parking` fixtures now mark the line
  as airport parking (GRND-03 no longer reaches plain parking), and
  `test_ground_rules_on_classified_lines` expects the could-not-verify advisory instead of
  a trip-length violation.

### Limits (honest)

* The live OCR prompt does not yet return `line_items`, `tip` or `parking_days`; those are
  read "where available" (ingested / itemized receipts). Without them exclusions and tips are
  keyword-detected → "could not verify", which (round 4) escalates the report to the
  approver: until OCR returns them, most alcohol / tip mentions reach a human.
* Airport parking is recognised from words / airport codes / a linked flight segment only. A
  parking receipt (verified as parking) that names no airport and is not linked to a flight
  segment is not reached by GRND-03 even if it was in fact at an airport (before gap 2 no rule reached it
  either); the date heuristic "first / last day of a trip with a flight" was rejected
  because a hotel-garage receipt on the checkout day would match it. GRND-05 ignores tolls
  (fare = total − tip).
* When the attach-receipt refetch of the report fails (`refreshed` null) the toast stays the
  neutral "Receipt attached" one; it cannot tell a refresh failure from a rule-level
  advisory without the refreshed report.
* LOD-02's "conference hotels" exception is not modelled.
* Trip-approval re-evaluation still maps rail segments to "other" (hot file
  `approval_agent/evaluate.py` untouched), so GRND-01 runs on expense lines, not on planned
  rail segments.
* MEAL-02 ignores MEAL-06 (provided meals) and pools by trip; a gap between segments is not
  a travel day.
* Seeded rules (fresh DB) have no audit rows, so their history is empty until first changed;
  audit rows written in one transaction share a timestamp (order within it is by id).
* Downgrading 0017 keeps GRND rules the seed created (they carry `created_by`).

### Review fixes (stage 1, round 1)

Adversarial review found four blocking defects; all fixed, each with a test that fails
without the fix (proven by temporarily reverting it):

1. **GRND-03 reached every parking line** (hotel valet, hourly city garage) and cited an
   airport-only clause → it now reaches airport parking only
   (`entry_rules.ground_subtype_reach`; tests `test_plain_parking_is_not_reached`,
   `test_plain_parking_gets_no_grnd03`, `test_parking_on_a_long_trip_is_not_given_the_trip_length`).
2. **The trip length was used as parking days** (a one-night hotel garage on a 9-day trip
   became "9 days … 7 of 9 reimbursable", blocking) → only receipt-read days (or claimed
   days against interest) decide a breach; the trip length is reason context only
   (`EvalItem.trip_days`). `test_ground_rules_on_classified_lines` was updated — intended
   behaviour change: the SFO airport-parking line on a 9-day trip is now a `not_evaluated`
   advisory naming the trip length, not a violation.
3. **The receipt's OCR vendor was ignored by the classifier**, so typing "Amtrak" over an
   Uber receipt escaped an authored rideshare cap → the deterministic OCR vendor and OCR
   line items now feed the classifier (`test_typed_vendor_cannot_hide_a_rideshare_receipt`).
4. **The attach toast read any `not_evaluated` as "the check couldn't be re-run"** and
   checked it before the blocking branch → the toast now checks the real submit gate first
   and keys the re-run message on the report-level note only (`not_evaluated` with no
   entry and no rule — what `expense.service` writes when the refresh fails); rule-level
   "could not verify" advisories name their entry and rule
   (`frontend/__tests__/expenses/pg2-attach-toast.test.ts`).

### Review fixes (stage 1, round 2)

Adversarial review found two more blocking defects; both fixed, each with a test that fails
without the fix (proven by temporarily reverting it):

1. **The GRND-03 summary described the pre-round-1 engine** ("days come from the receipt or
   the linked approved trip", "lines Atlas identifies as parking", "valet … lots are
   exceptions") — the text finance sees on the policy page and in the activation
   confirmation. `entry_rule_semantics.describe_kind` now says: applies only to airport
   parking (an airport named on the receipt or entry, or a line linked to a flight segment);
   hotel valet, city garages and meters are not covered; premium/valet/short-term airport
   lots are exceptions; days come only from the parking receipt (OCR) or a stated count above
   the limit, and the linked trip's length is context, never counted as parking days. Pinned
   by `ai_agents/tests/test_pg2_review_fixes_r2.py::test_grnd03_summary_*` (the second test
   fails if the "linked approved trip" wording returns).
2. **LOD-02 auto-approval was reachable through any trip segment.** `build_eval_item` set
   `booked_through_tool = segment is not None`, so a lodging entry linked to (or PATCHed
   onto) a car / flight / rail segment got the 15% tolerance — a non-escalating advisory
   that could let a small report auto-approve with no manager. Now only a HOTEL segment
   counts (`presubmit._is_hotel_segment`, ORM enum or plain value; fail closed), and
   `build_entry_eval_item` clears it when the receipt / card vendor contradicts the segment's
   hotel supplier (same rule as the flight-supplier contradiction). The LOD-02 summary
   sentence now names that condition. Tests:
   `test_pg2_review_fixes_r2.py::test_lodging_on_a_non_hotel_segment_is_not_a_tool_booking`,
   `test_pg2_entry_rules_db.py::test_lod02_needs_a_hotel_segment[car|flight|rail]` (13% over
   → HOTEL-CAP `policy_violation`, `requires_approver='manager'`, no advisory) and
   `test_lod02_receipt_vendor_contradicting_the_booked_hotel`.

Decision: a hotel segment with **no** supplier is still a tool booking when the line has no
receipt / card vendor at all (nothing contradicts the booking); once a vendor exists, a
blank segment supplier cannot be confirmed and the tolerance is withheld (fail closed).
Limit: a lodging line linked to a non-hotel segment still takes that segment's city / nights
as before gap 2 (pre-existing behaviour, unchanged here); only the tool-booking status changed.

### Review fixes (stage 1, round 3)

Adversarial review found three blocking defects, and the automated security review left
three carry-over items; all fixed, each with a test that fails without the fix (proven by
temporarily reverting it).

1. **Partial claims (MEAL-04, GRND-05) mixed the claimed amount with the receipt's
   amounts.** A claimant may claim part of a receipt (`receipt_total_covers` accepts any
   claim up to the receipt total). Before this fix, the wine was subtracted from a claim
   that already left it out. That hid the MEAL-01 per-diem overage and kept a MEAL-04
   violation that could not be cleared. The tip percentage was also computed on
   claim − tip. Now:
   * `EvalItem.receipt_total_usd` is the receipt's own OCR total (`entry_context.receipt_total`:
     provenance not `user_edited` / `ai_inferred`), never the claim. The receipt router
     paths capture it, together with the receipt's own words, **before** they overwrite
     `amount` / `vendor` with the entry's values (`entry_context.with_receipt_evidence`).
   * MEAL-04: `entry_rules.excluded_in_claim` = `max(0, min(excluded, claim − non_excluded))`.
     Here `non_excluded` is the OCR total minus the excluded lines (this covers tax), or
     else the sum of the non-excluded line items. The per-diem compare
     (`net_of_exclusions`), the pooled trip total (`pooled_total`, which uses the line's own
     claim `EvalItem.line_claim_usd`) and the MEAL-04 verdict all use that figure. When the
     claim holds none of the excluded amount, the verdict is in policy with no flag, and the
     per diem compares the full claim.
   * GRND-05: fare = receipt total − tip. If the tip is a line item and no OCR total was
     read, the sum of the line items is used. If the total is unknown, the result is
     could-not-verify. The claim holds `claim − fare` of the tip, and a violation is raised
     only when that is above the allowed tip.
   * Fail closed: with no line claim, or on a receipt that also backs other lines
     (`EvalItem.receipt_shared`: itemized siblings / a shared receipt), the excluded amount
     (or tip) is taken to be in the claim. Without this, a wine-only or tip-only sibling
     would hide the excluded money.
   Tests: `ai_agents/tests/test_pg2_review_fixes_r3.py` (`TestMeal04PartialClaims` (a)/(b)/(c),
   the reviewers' probes, tax, pooled, shared; `TestGrnd05PartialClaims`) and
   `backend/tests/test_pg2_entry_rules_db.py::test_partial_claims_of_a_receipt`.
2. **History reported unchanged context fields as changes.** `history.field_changes` now
   handles three cases. A creation event (no rule field before) lists every field. A
   removal event (no rule field after) lists every field it removed. On an update, only a
   field present on both sides with different values is listed. Activation and archive rows
   therefore show only `status draft → active`. Tests: `backend/tests/test_pg8_history_changes.py`
   and `test_finance_reads_whitelisted_changes`, which is tightened to exact equality.
3. (Same root cause as 1, MEAL-04 double subtraction; covered by the same fix and tests.)

Security carry-over:

* **Ground sub-type not decided by typed words alone.** `ground_transport.classify_with_trust`
  separates the claimant's words (entry vendor / description) from trusted evidence: the
  deterministic OCR vendor and line items, the card merchant and the trusted segment type.
  A line is *verified* only when trusted evidence names exactly one family and the typed
  words name no other. An unverified line (typed only, disagreeing, or several families)
  works like this:
  every authored sub-type cap reaches it, and every `kind` rule whose sub-type is a
  candidate reaches it. GRND-05 also reaches it whenever the receipt itemizes a tip
  (`entry_rules.ground_subtype_reach`). Typed "Amtrak" over an Uber receipt now gets the
  rideshare cap, the tip rule **and** the rail rule (a could-not-verify on the class).
  Tests: `TestGroundTrust`. `test_typed_vendor_cannot_hide_a_rideshare_receipt` was
  updated because the behaviour changed on purpose: the "Amtrak" variant now also carries
  a GRND-01 `not_evaluated`.
* **LOD-02 needs positive evidence.** In `build_entry_eval_item`, a hotel segment is a tool
  booking only when a receipt / card vendor names the segment's hotel supplier **and** the
  receipt's OCR location (not edited) is the segment's city. With no evidence, or with
  either check failing, the manager exception stays. (Round 3 said trip-approval
  re-evaluation "is unchanged: there the segment *is* the booking" — wrong for hotels typed
  into POST /trips; corrected in round 4, below.) Tests: `test_lod02_needs_the_booked_hotel_and_city_on_the_receipt[no-evidence|
  no-city|other-city|edited-city]`. `test_lod02_trip_hotel_vs_unlinked` and the
  `test_lod02_receipt_vendor_contradicting_the_booked_hotel` fixtures now carry the receipt
  evidence. `test_c7_05_15_policy_gates::test_within_lod02_tolerance_*` now expects the
  manager exception when there is no receipt (intended change).
* **Trip-derived parking days.** These never cleared GRND-03 (round 1). The context figure
  now comes from the trip's own start / end dates (`presubmit._trip_span_days`), not from
  the union of its segments' dates (`test_parking_context_uses_the_trip_dates_not_the_segment_union`).
  Decision: the trip length is not used against the claimant either. A 9-day trip does
  not show that the car was parked for 9 days (the round-1 finding), so the line stays a
  could-not-verify advisory.

The summaries finance sees (`entry_rule_semantics`) now state these conditions: kind and
sub-type reach in terms of confirmed evidence, LOD-02's receipt condition, GRND-05's
fare-from-receipt and tip-in-claim, and MEAL-04's "a claim that already leaves them out is
not flagged". Tests that pinned the old wording were updated: `test_pg2_rule_semantics`,
`test_pg2_review_fixes_r2`, `test_issue5_rule_authoring_failures`, `test_issue5_rule_authoring_api_db`.
The hermetic fixture `_ground` in `test_pg2_entry_rules.py` now marks a sub-typed line as
verified, and the GRND-05 fixtures carry the receipt total.

Limits: tax charged on the alcohol is not separated, because the OCR total minus the
excluded lines counts it as non-excluded. The GRND-05 fare ignores tolls, as before.
(Round 3 did not give a typed-only line the kind rules of families its words do not name;
round 5 reverses that — see below.)

### Review fixes (stage 1, round 4)

1. **LOD-02 at trip approval trusted any hotel segment.** `approval_agent/evaluate._fresh_trip_flags`
   built the segment items with `build_eval_item`, which set `booked_through_tool` for every
   HOTEL segment — including segments the traveler typed into POST /trips (supplier,
   location, amount, `in_policy`, `details` are all client-supplied). A manual Denver hotel at
   $180 for 1 night (12.5% over the $160 Tier 3 cap) auto-approved the trip with a LOD-02
   advisory; before gap 2 it escalated. Fix:
   * `build_eval_item` no longer sets `booked_through_tool` (default False). Only the trusted
     call sites set it: the planner ranking; `_fresh_trip_flags`, via
     `presubmit.is_planner_booked_hotel(segment, trip)`; and `build_entry_eval_item`, only
     when the linked segment is planner-booked **and** the receipt / card hotel + OCR city
     match it.
   * `is_planner_booked_hotel` requires a HOTEL segment of that trip, a trip
     `recommendation_context` dict with `agent_task == "trip_planning"` (written only by the
     planner, `travel_agent.persistence`; POST /trips always writes None and no API lets a
     client set it), and a `details.option_id` listed in its `selected_option_ids`.
     `details` alone proves nothing (client-writable on POST /trips).
   * Decision: the expense path now also needs the planner booking (stricter than the carry-
     over's receipt-only condition), so the trip and expense paths agree on what "booked
     through the travel tool" means. A hand-typed hotel 13% over the cap is a manager
     HOTEL-CAP exception on both.
   Tests: `ai_agents/tests/test_pg2_review_fixes_r4.py::TestTripApprovalLod02` (manual
   segment, with and without a typed `option_id`, → `escalate` + blocking HOTEL-CAP; planner
   trip → LOD-02 advisory + `auto_approve`; planner trip with a foreign option → escalate),
   `test_is_planner_booked_hotel`, and
   `backend/tests/test_pg2_review_fixes_r4_db.py::TestLod02ExpensePathNeedsAPlannerBooking`.
   Tests changed because the behaviour changed on purpose:
   `test_pg2_review_fixes_r2::test_lodging_on_a_hotel_segment_alone_is_not_a_tool_booking`
   (was `..._is_a_tool_booking`, now asserts False) and the LOD-02 summary wording test; the
   positive LOD-02 DB fixtures in `test_pg2_entry_rules_db.py` (`test_lod02_trip_hotel_vs_unlinked`,
   `test_lod02_receipt_vendor_contradicting_the_booked_hotel`,
   `test_lod02_needs_the_booked_hotel_and_city_on_the_receipt`) now make the segment
   planner-booked (`_planner_booked`), so their negatives still isolate the receipt evidence.
2. **"Your approver should check" advisories were auto-approved.** The could-not-verify
   outcomes of MEAL-04 (alcohol by keyword only), GRND-01 (class unverified), GRND-03 (days
   unverified) and GRND-05 (tip or fare unverified) told the claimant an approver would
   check, but they were `not_evaluated` advisories with no approver, so a report ≤ $500
   auto-approved past them — and with the live OCR not yet returning `line_items` / `tip` /
   `parking_days` this is the common path. Fix (escalation, not rewording): those outcomes
   carry `observed["approver_check"]` = the rule's `exception_approver` (default manager;
   `entry_rules.APPROVER_CHECK`), and `presubmit.verdicts_to_flags` copies it onto the
   `not_evaluated` flag's `requires_approver`. The flag stays advisory at submit (the line
   can be submitted), and the approval agent's `requires_approver` signal keeps the report
   out of auto-approval. MEAL-02's unverified travel day promises no approver check and is
   unchanged (`requires_approver` None). The finance-facing summaries now say these lines
   go to the approver and are never auto-approved. `policy_eval.py` (hot file) is untouched.
   Tests: `test_pg2_review_fixes_r4.py::test_approver_check_advisories_require_an_approver`
   (all five could-not-verify paths) and `test_meal02_unverified_travel_day_stays_a_plain_advisory`;
   DB: `test_pg2_review_fixes_r4_db.py::TestApproverCheckAdvisoriesEscalate` runs the approval
   agent (`assess_risk` + `decide_route`) on a $60 "Dinner with wine" (un-itemized receipt)
   and a $30 Uber "incl. tip" report → `escalate`, each with a control report (no wine / no
   tip) that still → `auto_approve`.

Guards proven red-when-removed in round 4: the planner check at trip approval (reverting to
"any hotel segment" fails the manual-segment and foreign-option tests), the planner check
on the expense path, and the approver on could-not-verify flags (7 tests fail, both DB
approval tests included).

### Review fixes (stage 1, round 5)

1. **GRND-01: a "Standard" rail segment overrode a First Class receipt.**
   `entry_context.ground_fields` took the segment's class outright and read the words only
   when the segment had none, so an approved trip planning a standard ticket cleared a
   2-hour journey whose deterministic receipt says "Acela First Class" (segment `details` are
   client-writable on POST /trips, too). Fix: `ground_transport.premium_rail_class` reads a
   premium class named ANYWHERE in the trusted words (OCR vendor / line items, card
   merchant) or the claimant's words (against their interest — even beside a standard word,
   "Coach upgrade to First Class"); a premium class wins over the segment. The segment's
   class is used only when no word names a premium class; a typed "standard" alone still
   never clears GRND-01. Tests: `ai_agents/tests/test_pg2_review_fixes_r5.py::TestRailClassEvidenceWins`
   (reviewer probe included) and `backend/tests/test_pg2_review_fixes_r5_db.py::TestRailReceiptClassBeatsSegment`
   (approved rail segment `travel_class: standard`, 2h, receipt line item "Acela First Class
   seat 2C" → GRND-01 `policy_violation`).
2. **MEAL-04 missed the usual itemized drinks.** `entry_rules.EXCLUSION_TERMS["alcohol"]` held
   generic words only and phrase words were joined by spaces / hyphens, so "Cabernet
   Sauvignon", "Pinot Noir glass", "Heineken" and "Gin & tonic" stayed in the meal total with
   no flag. Fix: grape varietals / wine styles (cabernet, cab sauv, merlot, pinot noir /
   grigio / gris, chardonnay, sauvignon blanc, malbec, riesling, rosé, syrah, shiraz,
   zinfandel), drink styles and spirits (ale, rum, gin, mezcal, soju, highball, old
   fashioned, spritz); and a `LINE_ITEM_TERMS` tier matched on receipt LINE ITEMS only —
   port, sherry, "glass of", pint(s) and the major beer brands (Heineken, Corona, Guinness,
   Stella (Artois), Bud Light, Budweiser, Modelo, Peroni, Asahi, Sapporo). Decision: those are
   line-item-only because as a vendor name they are usually something else ("Corona Cafe",
   "Port City Grill", "Stella's Diner") and a keyword hit on the entry's own words escalates
   the report. A phrase written with "and" also matches "&" ("gin and tonic" ↔ "Gin &
   tonic"). New false-positive guards: soft drinks by the glass / pint ("glass of water /
   orange juice", "pint of milk") and food cooked with a drink — a drink word followed by
   reduction, sauce, vinaigrette, glaze(d), jus, braised, battered, marinated, poached,
   infused, gravy, cake, butter or demi-glace ("beer-battered cod", "red wine braised short
   rib", "port reduction", "rum cake" are food, not alcohol). Tests:
   `TestAlcoholVocabulary` (the four reviewer probes as violations with the excluded amount
   and reimbursable remainder, 29 further drinks, 11 food / soft-drink non-matches, 3 vendor
   names) and the DB `TestAlcoholLineItems` (the four probes through presubmit). Limit: the
   vocabulary is still a list — an unlisted brand or cocktail name ("Aperol" alone, a craft
   beer's name, a house cocktail) on an itemized line is not excluded; a keyword that is
   also the dish ("Sangria chicken") can still match.
3. **Security carry-over (1) — a claimant could make GRND-01 / GRND-03 skip a line.**
   (a) Editing the receipt's OCR vendor (provenance `user_edited`) dropped it from the
   trusted words, so a $180 Amtrak receipt retyped to "Uber" became a typed-only rideshare
   candidate and auto-approved. Fix: `entry_context.receipt_texts(..., grounding_context=)`
   falls back to the frozen OCR snapshot (`receipts.grounding_context.ocr_snapshot.vendor`,
   written once by the pipeline, never by a correction — `receipt_pipeline.evidence`) when the
   vendor was edited; presubmit (`_fields_eval_item`) and the receipt router
   (`with_receipt_evidence`) pass the receipt's `grounding_context`. A row with an edited
   vendor and no snapshot (legacy) has lost its vendor evidence: `vendor_edited=True` →
   `classify_with_trust(evidence_destroyed=True)` → never verified.
   (b) A line with no trusted family at all (typed words only, a receipt vendor that names
   no family, no receipt) skipped every kind rule its words did not name. Fix:
   `classify_with_trust` marks such a line UNCONFIRMED — every sub-type is a candidate
   (`ground_transport.unconfirmed`); `entry_rules.ground_subtype_reach` lets every kind rule
   reach it (GRND-03 included, though it is not shown to be airport parking), and
   `entry_rules.evaluate_kind` prefixes the could-not-verify reason with "Atlas could not
   confirm what kind of ground transport this is …" (`observed.ground_subtype_unconfirmed`).
   GRND-03 on an unconfirmed line that is not shown to be airport parking is a
   could-not-verify `airport_parking` (never a premium-lot violation — a typed hotel valet is
   not an airport-lot breach); GRND-01 has no class → could-not-verify; GRND-05 without any
   tip is in policy (nothing to check). Those could-not-verify outcomes carry
   `approver_check`, so the report escalates (round 4). Lines whose receipt / card names
   their kind, or whose receipt contradicts the typed words (candidates = the named
   families, unchanged), are not affected.
   Decision: per kind rule (each says what it could not verify) rather than one extra
   "sub-type unverified" advisory, so `policy_eval.py` (hot file) stays untouched and every
   flag cites the clause it could not check. Consequence (intended): a ground line with no
   receipt / card naming its kind now always goes to the approver while kind rules are
   active — card-fed rides (merchant "UBER *TRIP") and receipts naming the vendor are
   verified and still auto-approve.
   Tests: `TestUnconfirmedSubtype`, `TestReceiptTextsSnapshot` (hermetic); DB
   `TestGroundLinesNeverAutoApproveUnverified` through `assess_risk` + `decide_route`: receipt
   "UBER" → `auto_approve` (control); reviewer case (b) (OCR snapshot AMTRAK, vendor edited
   to Uber) → GRND-01 flag + `escalate`; edited vendor without snapshot → GRND-01 + GRND-03 +
   `escalate`; reviewer case (c) (no OCR vendor, typed Uber) → GRND-01 / GRND-03
   `not_evaluated` with `requires_approver` manager + `escalate`.
   Tests changed because the behaviour changed on purpose:
   `test_pg2_review_fixes.py::test_plain_parking_gets_no_grnd03` (typed-only hotel / city
   parking now gets a GRND-03 could-not-verify, never a violation; the same line verified as
   parking still gets nothing), `test_pg2_entry_rules_db.py::test_ground_rules_on_classified_lines`
   (the typed-only parking and Lyft lines now also carry GRND-01 could-not-verify flags; the
   rail violation and the GRND-03 trip-context advisory are unchanged) and
   `test_parking_on_a_long_trip_is_not_given_the_trip_length` (GRND-03 reaches those typed-
   only lines as could-not-verify `airport_parking`, still never with trip days and never a
   violation). The finance-facing GRND summaries (`entry_rule_semantics.describe_kind`) state
   the new reach and the rail-class precedence.
4. **Carry-overs (2) and (3) re-verified, already closed:** LOD-02 auto-approval needs a
   planner-booked HOTEL segment plus receipt / card hotel and OCR city matching it (round 4,
   `is_planner_booked_hotel` + `build_entry_eval_item`); GRND-03 never counts trip days as
   parking days and its trip context uses the trip's start / end dates (round 3,
   `_trip_span_days`).

Guards proven red-when-removed in round 5: all new tests fail on the round-4 code (8 of 9
DB, 54 of 63 hermetic); separately, removing the OCR-snapshot fallback, the unconfirmed
widening in `classify_with_trust`, the unconfirmed reach in `ground_subtype_reach`, or the
GRND-03 non-airport could-not-verify branch each turns tests red.

### How to verify

```
R=…/run_policy.sh
$R -q ai_agents/tests/test_pg2_entry_rules.py ai_agents/tests/test_pg2_ground_transport.py \
      ai_agents/tests/test_pg2_rule_semantics.py \
      ai_agents/tests/test_pg2_review_fixes.py ai_agents/tests/test_pg2_review_fixes_r2.py \
      ai_agents/tests/test_pg2_review_fixes_r3.py ai_agents/tests/test_pg2_review_fixes_r4.py \
      ai_agents/tests/test_pg2_review_fixes_r5.py \
      backend/tests/test_pg8_history_changes.py  # hermetic
$R -q backend/tests/test_pg2_entry_rules_db.py backend/tests/test_pg2_review_fixes_r4_db.py \
      backend/tests/test_pg2_review_fixes_r5_db.py  # migration 0017, presubmit, approval, history API
$R -q -m 'not db' --ignore=evals/manual ; $R -q -m db ; $R -q backend/tests/eval -m db   # 48 passed
cd frontend && npx vitest run __tests__/policy/rule-history.test.tsx __tests__/expenses/readiness.test.ts \
      __tests__/expenses/pg2-attach-toast.test.ts
```

Guards proven red-when-removed (temporarily reverted, test failed, restored): sub-type reach
filter, LOD-02 "booked through the tool" condition, the justifiable marker, typed "standard"
never clearing GRND-01, claimed parking days never clearing GRND-03, history org scope, the
history role gate, the history field whitelist, and the migration's never-re-add guard; review
round 1: GRND-03 airport-only reach, trip length never used as parking days, OCR words in the
classifier, and the toast's blocking-first / report-level re-run marker.; review round 2: the
GRND-03 summary wording, LOD-02 hotel-segment-only tool booking, and the hotel-supplier
contradiction; review round 3: excluded-in-claim (MEAL-04), fare from the receipt total and
tip-in-claim (GRND-05), the shared-receipt fail-closed, receipt total captured before the
overwrite, trusted ground classification (verified flag, candidate reach), LOD-02 receipt
hotel + city, trip-date span, and the history update-vs-creation rule.

---

## Stage 2 — process & timing rules (EXP-01, EXP-04, PTA-02, PTA-05, AIR-04, AIR-06, CARD-03)

Product gap 2, stage 2. Same discipline as stage 1: structured rules (seeded + migrated),
pure deterministic evaluators with an injectable reference day, `Decimal` money, exact
cross-multiplied percentages, fail-safe on missing data, every flag citing its clause.

### What was built

| Area | Files |
| --- | --- |
| Pure evaluators (no DB, no clock; `today` / `booked_on` injected) | `ai_agents/tools/process_rules.py` |
| Wiring: load the org's ACTIVE rules (through `PolicyEvalTool._load_active_rules`, the one rule query), gather trusted facts, findings -> flag dicts | `ai_agents/agents/process_checks.py` (`today_utc` is the one clock) |
| Engine: a process rule reaches no line / option (one guard in `rule_applies`) | `ai_agents/tools/policy_eval.py` (3 lines) |
| Pre-submit: EXP-01 + CARD-03 report flags, EXP-04 affidavit on missing-receipt flags | `ai_agents/agents/expense_agent/presubmit.py` (`apply_affidavit`, `apply_affidavit_fields`, `affidavit_fields`), `categorize.DraftEntry.receipt_affidavit_at` |
| Same EXP-04 outcome on POST /policy/evaluate | `ai_agents/agents/policy_eval_handler.py`, `backend/app/modules/policy/service.py` (unit carries the affidavit inputs) |
| Trip approval: PTA-02 / AIR-06 / AIR-04 per flight segment, PTA-05 per trip | `ai_agents/agents/approval_agent/evaluate.py` (one hook in `_assess_trip`, fail-closed) |
| Planner: notices per flight option at selection time, AIR-06 out-of-policy label, AIR-06 baseline recorded on the selection, PTA-05 link + message | `ai_agents/agents/travel_agent/ranking.py`, `agent.py`, `persistence.py`, `reflection.py` (critic keeps the new fields via `dataclasses.replace`) |
| Affidavit write (owner-only PATCH, audited) + read-time EXP-01 / CARD-03 on drafts | `backend/app/modules/expense/process_flags.py`, `schemas.py` (`receipt_affidavit`, `receipt_affidavit_at`), `service.py` (two one-line hooks + audit snapshot key) |
| Model / migration | `database/models/expense.py` (`receipt_affidavit_at`), `database/migrations/versions/0018_process_timing_rules.py` |
| Seeds | `database/seeds/policy_content.py` (`PROCESS_RULES`, EXP-04 keys on RECEIPT-REQ, AIR-04 keys on AIR-CABIN), `seed_all.py` |
| Policy page validation / summaries | `ai_agents/agents/process_rule_semantics.py`, `policy_rule_semantics.py` (dispatch + allowed keys) |
| UI | `frontend/components/expenses/entry-editor.tsx` (affidavit checkbox with the clause text), `components/planner/option-card.tsx` (notices), `lib/expenses/readiness.ts` (hints), `lib/types/expenses.ts`, `lib/types/trips.ts` |

### The clauses, as written (`database/seeds/corpus/policy.py`, figures from `figures.py`)

* **EXP-01** "submit expense reports within 30 days of trip completion; reports > 60 days
  old require VP approval and may be treated as taxable."
* **EXP-04** "a signed missing-receipt affidavit is allowed up to $75 and no more than 3
  times per quarter." (With EXP-02: receipts are required from $25, and always for airfare
  and lodging — the existing RECEIPT-REQ rule.)
* **PTA-02** "domestic flights ≥ 14 days ahead; international ≥ 21 days ahead … Shorter lead
  time requires a documented business reason." (Flights only — the clause names no hotel
  lead time, so none is invented.)
* **PTA-05** "Trip changes must be re-approved if total cost increases > 10%."
* **AIR-04** "SVPs and above may book business class on flights > 6 hours."
* **AIR-06** "travelers must accept fares within $150 (domestic) / $400 (international) of
  the cheapest option meeting routing/timing needs."
* **CARD-03** "Reconcile all card transactions in an expense report within the EXP-01
  deadline." (`CARD_RECONCILE_DEADLINE_DAYS = 30`.)
* **CARD-04** "Lost/stolen cards must be reported … within 24 hours." — not implemented (no
  card-loss events exist in Atlas; see Limits).

### Rules (structured, finance-controlled)

New rows reuse existing rule types with a `kind` discriminator (no enum change); a rule that
is archived on the policy page is simply not checked.

| Clause | Stored as | Definition keys |
| --- | --- | --- |
| EXP-01 | `category_allow` / `all`, kind `submission_deadline` | `deadline_days` 30, `stale_days` 60, `stale_approver` VP |
| PTA-02 | `category_allow` / `flight`, kind `advance_booking` | `domestic_days` 14, `international_days` 21, `exception_approver` manager |
| PTA-05 | `amount_cap` / `all`, kind `trip_change_reapproval` | `max_increase_pct` 10, `exception_approver` manager |
| AIR-06 | `amount_cap` / `flight`, kind `lowest_fare` | `domestic_tolerance_usd` 150, `international_tolerance_usd` 400 |
| CARD-03 | `category_allow` / `all`, kind `card_reconciliation` | `deadline_days` 30 |
| EXP-04 | modifier keys on RECEIPT-REQ | `affidavit_max_usd` 75, `affidavit_max_per_quarter` 3, `affidavit_clause_ref` |
| AIR-04 | modifier keys on AIR-CABIN | `role_exception_class` business, `role_exception_min_hours` 6, `role_exception_role` SVP, `role_exception_clause_ref` |

`PolicyEvalTool.rule_applies` returns False for a process kind, so these rows never produce a
per-line verdict (no `not_evaluated` noise, no double flags); `validate_rule_definition`
enforces the shapes above (wrong rule type / target, unknown keys, bad numbers, stale <
deadline are refused at activation) and `describe_rule` states what the wiring does.

### Behaviour tables

**EXP-01 — submission deadline** (report-level flag, `entry_ref` "Report"; pre-submit,
approval time, and READ time for drafts). *(Integration round 2 changed the anchor: the
clock now starts at min(trusted trip end, earliest expense date) — see
`phase_9_product_gaps_integration.md`; the paragraph below describes stage 2.)*
Age = today − completion day, where completion
= the report's EARLIEST expense date (a recent line cannot restart the clock for older
ones), replaced by the linked trip's end date only when that date is trustworthy for the
lines (review fix, stage 2): the trip is the OWNER's in the organization, it is
`approved` or `completed` (draft / pending / rejected / cancelled trip dates are whatever
the claimant typed), and it has already ended (end ≤ today). Even then a line dated more
than `PRE_TRIP_BOOKING_DAYS` (90) before the trip's start is not part of the trip and
anchors on its own date (basis "an expense outside the trip"): completion = min(trip end,
earliest such line). A claimant therefore cannot stop the clock by linking a future,
draft, cancelled or unrelated trip.

| Age (days) | Flag | Severity | At approval |
| --- | --- | --- | --- |
| ≤ 30 (day 30 included) | — | — | — |
| 31 … 60 (day 60 included) | `late_submission` (EXP-01) | advisory | does not escalate (the clause names no approver for it) |
| ≥ 61 | `approval_required`, `requires_approver` VP, "may be treated as taxable" | advisory (submittable) | always escalates |

Read time: `GET /expense-reports/{id}` of a DRAFT recomputes EXP-01 / CARD-03 for today
and merges them into the response's `pre_submit_flags` (stored EXP-01 / CARD-03 flags are
replaced, counts recomputed, nothing written). A forgotten draft therefore shows its
stale flag without anyone editing it — no cron. Submitted reports keep the flags they
were judged on.

**CARD-03 — card reconciliation** (report-level flag, `entry_ref` "Corporate card").

| Owner's card transactions not linked to any expense (`expense_id IS NULL`) | Flag |
| --- | --- |
| none older than 30 days (day 30 included) | — |
| one or more 31+ days old | `unreconciled_card` advisory: count, totals per currency, oldest date |
| linked to an expense, or another user's | never counted |

**EXP-04 — missing-receipt affidavit** (applied to a BLOCKING `missing_receipt` flag of a
line with NO receipt linked; an attached receipt that does not cover the claim is not a
lost receipt and stays a plain RECEIPT-REQ block).

| Situation | Severity | Clause cited |
| --- | --- | --- |
| not signed | blocking; the reason (RECEIPT-REQ's, unchanged, + one sentence) says the affidavit is possible | RECEIPT-REQ |
| signed, amount > $75.00 ($75.01 up) | blocking ("allowed only up to $75.00") | EXP-04 |
| signed, justification empty | blocking ("add the explanation") | EXP-04 |
| signed, already 3 earlier affidavits in the quarter | blocking ("no more than 3 per quarter") | EXP-04 |
| signed, explained, ≤ $75.00, 1st–3rd of the quarter | **advisory** `missing_receipt` ("affidavit n of 3 for Qn yyyy") — still an escalating flag type, so never auto-approved | EXP-04 |

*(Integration round 2: only an affidavit standing in for a REQUIRED receipt counts; a
signature is withdrawn when the claim or its receipt linkage changes; only a validated
receipt on a locked line frees a slot; signing is refused where not offered — see
`phase_9_product_gaps_integration.md`. The paragraph below describes stage 2.)*
Quarter = calendar quarter of the expense date. Count = the owner's affidavits IN USE (any
report status, own org) for that quarter signed EARLIER — ordered by (signing time, entry
id) — so an affidavit keeps its place and a later one never pushes it out. In use = the line
has no receipt linked (own or shared itemized — the pre-submit evidence lookup) and its
amount is within `affidavit_max_usd`; a line that later got its receipt, or one above the
ceiling (where an affidavit is never accepted), does not use up a slot (stage 2 review
round 2). Signing is
`PATCH /expenses/{id}` `{"receipt_affidavit": true}` (owner-only through the existing
write scope, draft reports only, If-Match); the server stamps `receipt_affidavit_at` (the
first signing time is kept), `false` withdraws it, and the existing `expense.updated` audit
row records `receipt_affidavit_at` before → after. The UI shows the checkbox with the clause
text on a line without a receipt, and keeps a SIGNED affidavit visible after a receipt is
attached ("not used, does not count toward the quarter — untick to withdraw it").

**PTA-02 — advance booking** (per flight; booked_on = the day the trip is submitted (the
approval agent runs at submission) / the day of the planner search)

| Route | Lead (days) | Result |
| --- | --- | --- |
| domestic | ≥ 14 | — |
| domestic | ≤ 13 | `approval_required` (manager): "Booked N days ahead …; PTA-02 requires domestic flights … at least 14 days ahead" + documented business reason |
| international | ≥ 21 / ≤ 20 | — / same, with 21 |
| unknown country | ≥ 21 | — |
| unknown country | 14 … 20 | `not_evaluated` with `requires_approver` manager (enough for domestic, not international) |
| unknown country | ≤ 13 | `approval_required` (short for either) |
| no departure date | — | `not_evaluated` with approver check |

Domestic / international comes from the trip's origin / destination resolved to an airport
of known country (`process_rules.AIRPORT_COUNTRY`: SFO, JFK, ORD = US; LHR = GB). Departure
= the segment's start, else the trip's start date. *(Integration round 2: the segment's
start is trusted only for a planner-selected segment; otherwise the EARLIER of the
segment's and the trip's start dates counts — both are typed on POST /trips.)*

**AIR-06 — lowest logical fare** (per flight; "comparable" = the same search (route and date)
and the same cabin)

| Situation | Result |
| --- | --- |
| fare − cheapest ≤ $150.00 (domestic) / $400.00 (international) | — |
| over by $150.01 (domestic) / $400.01 (international) | `policy_violation` (manager), option shown **Out of Policy** in the planner, trip escalates |
| route of unknown country | the stricter domestic tolerance |
| no search baseline (a flight typed into POST /trips, or a segment whose option the planner did not select for this trip) | `not_evaluated` advisory (no approver) |

The baseline is the planner's: `RankedOption.lowest_fare_usd` is recorded in the trip's
server-written `recommendation_context.selected_options[].lowest_fare_usd` and read back at
approval only when the segment's `option_id` is one the planner selected for THAT trip
(`details` alone is client-writable).

**AIR-04 — SVP business class** (modifier of AIR-CABIN)

| Cabin / duration | Result |
| --- | --- |
| business, > 6 h and not permitted by the AIR-CABIN tiers (6 h < d < 10 h) | `approval_required` with `requires_approver` "SVP" (in addition to the unchanged AIR-CABIN VP-override violation) |
| business ≤ 6 h | AIR-04 does not apply (AIR-CABIN alone) |
| business ≥ 10 h | permitted by AIR-03 / AIR-CABIN — no AIR-04 flag |

Decision (as instructed — no user columns): Atlas records no grade, so AIR-04 is never
satisfied automatically. The only "override" is a human approver approving the trip with
the flag in front of them; the AIR-CABIN violation is not softened. Atlas has no SVP / VP
approval tier, so (exactly like the existing AIR-CABIN VP override) the line manager
receives it with the required level named.

**PTA-05 — re-approval on cost increase**

Decision — which semantics: the existing state machine forbids changing an approved or
under-review trip (`travel.service` has no segment edit endpoint; the planner never changes
a non-draft trip — "owner decision 3"). So PTA-05 is enforced where cost CAN change: a
planner selection for the SAME route (origin + destination) as the conversation's approved
/ pending-approval trip becomes a NEW draft, linked by the server-written
`recommendation_context.changes_trip_id`. Nothing is re-approved silently:

| Where | Result |
| --- | --- |
| selection time | the reply names the change: > 10% → "PTA-05: … rose from $A to $B (n% more) … must be re-approved"; ≤ 10% → "keeps the estimated cost within the re-approval threshold … still needs approval before booking (PTA-01)"; `trip_change.changes_trip_id` / `reapproval_required` in the frame and on the `trip.created` audit row |
| effective estimate (both places) | the new draft holds only the re-selected legs, so the change is measured on the trip AS CHANGED: new estimate + the linked trip's legs of every type NOT re-selected (a selection replaces the leg of its type, as in the UAT2 in-place update). A kept leg unpriced / not in USD → `not_evaluated` (approver checks). Example: $152 economy + $783 hotel approved ($935), only the $759 business flight re-selected → $1,542 (+64.92%) → re-approval |
| approval time | new estimate = max(estimated_cost, Σ segments) + kept legs (above) vs the linked trip's estimate (else Σ its segments); > 10% (exact: (new − old) × 100 > 10 × old; 10.00% within, 10.01% not) → `approval_required` (manager) with `related_subject_id` = the approved trip; never auto-approved |
| link to a trip of another traveler / org | ignored (no baseline) |
| unknown / zero baseline | `not_evaluated` with approver check |

SoD is unchanged (the traveler can never approve; the approval opens through
`open_approval_on_submission` with the usual audit rows); a change of an approved trip also
overlaps it, so the existing `possible_split_trip` flag already routes it to a human — PTA-05
adds the clause and the increase.

### Migration 0018 (`0018_process_timing_rules`, down `0017_entry_level_policy_rules`)

* Schema: `expenses.receipt_affidavit_at timestamptz NULL` (nullable add, no default — no
  table rewrite). Downgrade drops it.
* Data (same pattern as 0017): RECEIPT-REQ / AIR-CABIN rows whose definition still EQUALS
  the canonical seed literal get the EXP-04 / AIR-04 keys (others left alone, WARNING);
  EXP-01, PTA-02, PTA-05, AIR-06, CARD-03 inserted (active) into every policy carrying a
  HOTEL-CAP rule that has no rule citing the clause (any status — an archived one is never
  re-added). One audit row per rewritten / inserted row (actor NULL). Idempotent; fresh DB:
  column only (the seed writes the rules). Downgrade deletes only inserted rows
  (`created_by IS NULL`) still equal to the literal (audited `policy_rule.unseeded`) and
  reverts definitions still equal to the new literal.

### Key decisions

* **Process rules are rows, not hard-coded checks** — finance sees, edits (validated) and
  archives them on the policy page like any other rule; the evaluators read their numbers.
* **They never reach PolicyEvalTool's per-item loop** (one guard) — the report / trip /
  planner wiring runs them where the facts live.
* **Rules are loaded through `PolicyEvalTool._load_active_rules`** — one org-scoped query
  for every rule consumer (hermetic fakes that patch it keep working unchanged).
* **EXP-01 late (31–60) is advisory and non-escalating**; only the stale rule names an
  approver (VP) and escalates — as the clause says. Flags are advisory at submit: the
  claimant cannot "fix" lateness, so blocking would only trap the report.
* **EXP-04 stays a `missing_receipt` flag** (an escalating type): an accepted affidavit is
  submittable but always reaches a human; the unsigned flag keeps RECEIPT-REQ as its clause
  and its original reason (one sentence appended).
* **AIR-06 compares within the same cabin** — the cheapest economy fare is not the baseline
  of a business seat (cabin entitlement is AIR-01..04's job).
* **New flag types** `late_submission` and `unreconciled_card` (the UI renders every type;
  readiness hints added); everything else reuses `approval_required`, `policy_violation`,
  `missing_receipt`, `not_evaluated`.

### Tests changed because the intended behaviour changed

* `ai_agents/tests/test_approval_agent_db.py` — the seed now carries EXP-01 / PTA-02; these
  routing scenarios use fixed July 2026 dates, so an autouse fixture runs the process
  checks as of 2026-06-01 (trip booked 30 days ahead, expenses not yet due). Without it the
  in-policy report is 130 days stale and the trip departed in the past — both now
  (correctly) escalate.
* `backend/tests/test_p3_seed.py::test_policy_rules_consistency`,
  `ai_agents/tests/test_rag_indexer.py::test_all_rule_clause_refs_have_embedding` — allow the
  `PROCESS_RULES` corpus clause refs; the seeded rule count is 15.

### Security carry-over (verified, all closed in stage 1 rounds 3–5 — no change needed)

1. Ground sub-type classification uses trusted evidence (`ground_transport.classify_with_trust`:
   deterministic OCR vendor / line items, card merchant, trusted segment type; an edited OCR
   vendor falls back to the frozen OCR snapshot, a destroyed one is never verified); an
   unverified or ambiguous line gets EVERY candidate kind rule, and a line nothing trusted
   identifies is UNCONFIRMED — every kind rule reaches it as a could-not-verify that escalates
   (`entry_rules.ground_subtype_reach`, `evaluate_kind`).
2. LOD-02 auto-approval only for a planner-booked HOTEL segment
   (`presubmit.is_planner_booked_hotel`) whose receipt / card names the booked hotel with the
   receipt's OCR city matching the segment (`build_entry_eval_item`); otherwise the manager
   exception stays.
3. Trip-derived parking days never clear GRND-03: only receipt-read days decide; the trip
   length (from the trip's start / end dates, `_trip_span_days`, not the segment union) is
   reason context of the could-not-verify advisory (which escalates). Decision kept from
   round 3: the trip length is not used against the claimant either (a 9-day trip does not
   show the car was parked 9 days).

The stage-1 tests pinning these (`TestGroundTrust`, `TestUnconfirmedSubtype`,
`TestReceiptTextsSnapshot`, `TestGroundLinesNeverAutoApproveUnverified`,
`TestTripApprovalLod02`, `test_lod02_needs_the_booked_hotel_and_city_on_the_receipt`,
`test_parking_context_uses_the_trip_dates_not_the_segment_union`) still pass.

### Limits (honest)

* **CARD-04 not implemented** — Atlas has no lost/stolen-card events or reporting timestamps.
* **No finance exceptions list** exists to surface stale reports / overdue card transactions
  in; they appear on the owner's reports (and the approver's view of them) only.
* EXP-01 uses a linked trip's end date only for an approved / completed trip that has
  ended; a line booked within 90 days before an approved trip is treated as part of it (a
  fare booked further ahead, or an unrelated old line, anchors on its own date — stricter
  for the claimant, an approver can see why). CARD-03 counts every unlinked card transaction, including a personal charge
  already repaid under CARD-02 (no repayment record exists).
* EXP-04: attaching a receipt later does not clear the affidavit stamp (the audit trail
  keeps it), but the line no longer counts toward the quarter; the owner can still untick it
  in the editor. Only lines with no receipt at all can use an affidavit.
* PTA-05 effective estimate assumes the change keeps every approved leg whose type was not
  re-selected (the planner has no "drop a leg" action); a traveler who really drops the
  hotel is compared against a higher effective total (stricter, never laxer).
* PTA-02's lead time is measured when the approval agent runs (at submission — the
  approval-time evaluation) or at the planner search; it is never re-measured when a human
  approves days later, so a trip submitted 14 days ahead and approved 10 days ahead passes.
* PTA-02 counts from the submission / search day; a trip approved by a human days later is
  not re-measured. Countries are known only for the airports the inventory serves.
* PTA-05 links only planner changes of the conversation's own trip; a hand-built
  replacement trip (POST /trips) is not linked (the existing split-trip check still sends an
  overlapping one to a human). Approving the change does not cancel the original trip.
* PTA-05: a change within 10% is not re-approved under PTA-05, and the change is a NEW
  trip, so after its (PTA-01) approval the traveler can hold TWO approved trips for the
  same route — the original and the change; nothing cancels the first (only the
  overlapping-trip check sends the second to a human).
* AIR-06 has a baseline only for planner flights; hand-typed flights are "could not verify"
  (advisory, not escalating by itself).
* AIR-04 cannot be granted automatically (no grade data); there is no SVP approval tier.

### How to verify

```
R=…/run_policy.sh
$R -q ai_agents/tests/test_pg2_process_rules.py                   # hermetic: boundaries, reach, semantics
$R -q backend/tests/test_pg2_process_rules_db.py                  # migration 0018, presubmit, approval, read time, planner
$R -q -m 'not db' --ignore=evals/manual ; $R -q -m db ; $R -q backend/tests/eval -m db   # 48 passed
cd frontend && npx vitest run __tests__/expenses/pg2-process-rules.test.tsx
```

Guards proven red-when-removed (temporarily reverted, test failed, restored): process rules
reach no item; stale report → VP approver (escalation); affidavit quarterly count; affidavit
$75 ceiling; affidavit only on a line with no receipt; PTA-05 baseline only from the same
traveler's trip; AIR-06 baseline only from the planner's selection; PTA-02 short notice →
approver (escalation); affidavit count owner-scoped; read-time EXP-01 flag; UI affidavit only
on a line without a receipt.

### Review fixes (stage 2, round 1)

* **EXP-01 could be switched off by linking a future-dated own trip** (draft, cancelled,
  pending or approved — trip dates are client-set on POST /trips): `report_completion`
  used any owned trip's end date, so a 90-day-old line linked to a trip ending next year
  had a negative age, no flag, and auto-approved. Now the trip end anchors only for an
  approved / completed trip that has already ended, and lines dated before the trip window
  (start − 90 days) keep their own date. Tests (`TestSubmissionDeadline`):
  `test_a_future_trip_never_suppresses_the_stale_flag[draft|cancelled|pending_approval|approved]`,
  `test_an_unapproved_past_trip_is_not_an_anchor[…]`,
  `test_lines_long_before_the_trip_anchor_on_their_own_date` (with the in-window pre-booked
  fare control), `test_a_linked_future_trip_still_escalates_a_submitted_stale_report`
  (approval route = `escalate` for draft / cancelled / approved future trips). Each of the
  three conditions (status, ended, window) proven red-when-removed individually.
* **The draft-only read-time guard had no test**: new
  `test_a_submitted_report_keeps_the_flags_it_was_judged_on` — a SUBMITTED report with a
  90-day-old line, an overdue card transaction and a stored snapshot without EXP-01 /
  CARD-03 is returned byte-identical by `GET /expense-reports/{id}`; the same report as a
  draft gets both read-time flags (control). Proven red with the guard removed.
* Security carry-over (ground classification from trusted evidence, LOD-02 only via a
  planner-booked hotel segment with receipt hotel + city match, trip days never clearing
  GRND-03 and taken from the trip's start / end) re-verified in code — still closed, tests
  unchanged.

### Review fixes (stage 2, round 2)

* **EXP-04 counted affidavits no longer in use** (a line that got its receipt after
  signing, a line above $75): a genuine 2nd affidavit was blocked as the "4th of the
  quarter", and the documented way out (untick) was hidden in the UI once a receipt was
  attached. `process_checks.affidavits_before` now counts only lines with no linked receipt
  (same `receipt_ids_by_entry` lookup as pre-submit) within `affidavit_max_usd`
  (`process_rules.affidavit_ceiling`; an unreadable setting keeps every signed affidavit).
  The editor keeps a signed affidavit visible after a receipt is attached so it can be
  withdrawn. Tests: `TestAffidavitsInUse` (the reviewer's exact L1–L4 setup → L4 accepted as
  "affidavit 2 of 3"; 3 signed + receipt on one → a 4th accepted, with the all-in-use
  control blocked), vitest "stays visible to withdraw once a receipt is attached".
* **Fail-closed trip approval had no test**: `TestTripApprovalFailClosed` — an in-policy
  sub-$500 trip 14+ days ahead auto-approves (control); with `process_checks.trip_flags`
  raising, the same kind of trip escalates and the rationale carries "Trip process checks
  could not be completed; human review required."
* **PTA-05 compared only the re-selected legs with the whole approved trip**: new
  `process_checks.effective_change_estimate` (new estimate + approved legs of the types not
  re-selected) used by both `selection_change` (planner; `new_types` = the selected
  segment types) and `trip_flags` (approval). Tests: `TestPta05EffectiveTrip` — $935
  Delta + Marriott trip, only the $759 United business flight re-selected →
  `reapproval_required`, the message names $1,542.00, and the approval's PTA-05 flag has
  `new_estimate` 1542.00; the $158 United economy swap (+0.64%) → no re-approval, no flag.
  Hermetic `TestEffectiveChangeEstimate` (kept leg, all legs replaced, two prior flights,
  unpriced / EUR kept leg → could not verify).
* **Guards without a red-when-removed test** — added: `TestPta05Linking` (a REJECTED /
  CANCELLED / COMPLETED prior trip is not linked; a different-route selection SFO → JFK
  after a submitted ORD → JFK trip is not linked); hermetic `TestAir06InThePlanner` (a
  flight $150.01 above the cheapest economy fare is `in_policy=false`, cites AIR-06, names
  the manager, ranks below every compliant option and survives `TravelPolicyHonestyCritic`;
  the $150.00 option stays in policy); `TestServerStamp` (signing stamps a server time
  between request start and response, re-signing keeps the first time, a client-sent
  `receipt_affidavit_at` is never stored).
* Each guard proven red-when-removed (temporarily reverted, test failed, restored): the
  receipt-linked and over-ceiling filters of the count (individually); the fail-closed
  rationale; the kept-leg sum; the `_CHANGEABLE_STATES` check; the same-route check; the
  AIR-06 breach branch of `_annotate`; the server stamp and keep-first; the editor's
  signed-affidavit visibility.
* Security carry-over re-verified in code — still closed: ground sub-type from trusted
  evidence with every candidate's rules on disagreement (`entry_context.ground_fields` →
  `classify_with_trust`, `TestGroundTrust` / `test_disagreeing_evidence_applies_every_candidate_kind_rule`);
  LOD-02 only via a planner-booked HOTEL segment with receipt / card hotel + OCR city match
  (`presubmit` `is_planner_booked_hotel` + `same_supplier` + `_same_city`); trip days are
  GRND-03 reason context only, from the trip's start / end (`_trip_span_days`,
  `_eval_parking_days` never treats them as parked days).

How to verify the round:

```
$R -q ai_agents/tests/test_pg2_process_review_fixes.py backend/tests/test_pg2_process_review_fixes_db.py
cd frontend && npx vitest run __tests__/expenses/pg2-process-rules.test.tsx
```

---

## Integration round 2 — documentation corrections and clause sentences not implemented

Behaviour changes made at integration (EXP-01 anchor, EXP-04 counting / signing, PTA-02
departure, MEAL-04 pro-rata tax and tip, the cooked-with exemption, ground transport
verification, currency conversion of the new rules) are described in
`phase_9_product_gaps_integration.md` (round 2).

Clause sentences of the implemented clauses that Atlas does NOT check (honest list):

* **GRND-01** "Rail preferred over air where rail ≤ 4 hours (sustainability)" — no check;
  a flight on a route with a short rail alternative is not flagged.  "First/Business rail
  … if cheaper than or comparable to flying" — Atlas has no fare comparison; a premium
  rail line ≥ 3 h is a manager exception carrying the comparison in the justification.
* **GRND-03** "Airport transfers: rideshare/taxi/public transit reimbursable" — permissive,
  no rule needed; only the parking half is a rule.
* **GRND-05** "Tolls … are reimbursable with the ride" — tolls are not separated (fare =
  receipt total − tip).
* **LOD-02** "conference hotels" — not modelled (only the 15% tool-booking tolerance).
* **PTA-02** "Shorter lead time requires a documented business reason" — the approver
  sees the flag; Atlas does not capture the reason as a separate field.
* **MEAL-04** "alcohol is reimbursable only as part of approved client entertainment …
  with attendee documentation" — the manager may accept the violation with a
  justification; Atlas does not check that the entertainment was approved.
* **EXP-01** "may be treated as taxable" — stated in the flag; no tax handling.
* **AIR-06** "meeting routing/timing needs" — comparable = the same planner search and
  cabin; hand-typed flights have no baseline (could-not-verify).
* **CARD-04** — not implemented (no card-loss events).
