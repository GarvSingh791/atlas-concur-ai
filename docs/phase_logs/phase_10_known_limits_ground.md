# Phase 10 — Known limits: unconfirmed ground lines (worst-case check)

Branch `kl/ground` (base `product-gaps` @ `b044343`, alembic head unchanged
`0020_notification_reads` — no migration).

## What

Known limit closed: *"Ground lines with no evidence: a ground-transport line with no
receipt, card entry or trusted trip link now always goes to a human approver. Expect more
manual reviews for typed-only rides."*

Policy review round 5 (`phase_9_product_gaps_policy.md`) made a ground line that no TRUSTED
evidence identifies (receipt OCR words, card merchant; a segment alone never verifies)
**unconfirmed**: every sub-type is a candidate, so GRND-01 (rail class), GRND-03 (airport
parking) and GRND-05 (tips) all reach it, and the first two always answer "could not
verify" with an approver check. That closed a bypass (typing "Uber" over an Amtrak
receipt) but sent every $14 typed "Uber" to a manager.

Now, for an unconfirmed line, Atlas computes the **worst case across every candidate
sub-type** (`ai_agents/tools/ground_worst_case.py`, called once at the end of
`PolicyEvalTool._evaluate_all`, so presubmit, the approval agent's fresh re-evaluation,
the receipt router and `POST /policy/evaluate` all agree):

| Check | Rule |
|---|---|
| Amount — receipt floor | every RECEIPT-REQ verdict on the line says no receipt is needed: the amount, converted to USD by the engine's FX helper, is **below** `threshold_usd` ($25 seeded; RECEIPT-REQ needs a receipt *at* $25.00). No receipt rule in the set, an always-required category, an unknown amount or an unsupported currency → not cleared. |
| Amount — every other limit | no other verdict on the line fails (an authored "rideshare cap $20" reaches unconfirmed lines; at $20.00 it passes, at $20.01 it fails and nothing is cleared) and none lacks an FX conversion. |
| Shape — GRND-01 | no word (typed or trusted) or segment names rail, and no ticket class is named (a premium class named anywhere is still evaluated as today → violation). |
| Shape — GRND-03 | the line does not itself say it is airport parking (parking named **and** an airport named / flight segment linked), no premium-lot word (valet, short-term, hourly …), no stated day count over the limit. "Taxi to the airport" is not parking. |
| GRND-05 | never cleared — its could-not-verify only comes from a tip the words or receipt mention, which is a real fact to check. |
| Evidence destroyed | a receipt vendor the claimant overwrote with no OCR snapshot (`ground_named` None) is never cleared. |

A rule that passes both is **cleared**: its verdict becomes plainly in policy (no
`could_not_verify`, no `approver_check`, marker `ground_worst_case_cleared`). When nothing
else on the line still needs an approver, the first cleared verdict carries a single
low-key `policy_advisory` (never escalating): *"Ground transport, category unverified: no
receipt, card or trip segment names what kind of ride this is, but at $14.00 it is below
the $25.00 receipt threshold and within every ground-transport limit whatever kind it is
(GRND-01, GRND-03), so it does not need an approver check. Add a receipt or card match
that names the kind of ride to confirm the type."* A rule that fails either keeps its
could-not-verify + approver check, and its reason now says why it was not cleared
("… because the entry's words or its trip segment say it is rail", "… at $25.00 it is not
below the $25.00 receipt threshold", "… the line is over another limit (GRND-02)") plus the
same hint (`observed.ground_at_risk`).

Entry-time hint: both outcomes carry `observed.confirm_ground_type = true`. The frontend
readiness normalizer reads it (`ReadinessFlag.confirmGroundType`), the suggested fix
becomes "Add a receipt or card match that names the kind of ride to confirm the type.",
and the entry editor shows that line under the justification (`entry-ground-type-hint`).
The finance-facing GRND-01 / GRND-03 summaries (`entry_rule_semantics.describe_kind`) state
the clearing condition; GRND-05's does not change.

Review-load metric: `ground_worst_case.review_load(items, rules)` →
`ReviewLoad(lines, escalated_before, escalated_after)` — "before" is each applicable rule
evaluated alone (a one-rule evaluation has no receipt floor, so it is exactly the round-5
verdict), "after" the real all-rules evaluation; a line escalates when any verdict fails,
needs an approver, is a could-not-verify with an approver check, or lacks an FX conversion.
On a realistic mix of 20 typed-only rides with the seeded policy: **20 → 8** (the 12 small,
plain rides — Uber/Lyft/taxi/bus/subway under $25, EUR 11, JPY 1500 — no longer escalate;
$25.00, $48, $96, "incl. tip", "Amtrak", "Uber to the train", "Hotel valet" and GBP 24 still
do).

## Why (the argument for the floor)

The seeded kind rules have no money cap: a First-class ticket or a 9-day airport stay can,
in principle, cost anything. So "within every cap" alone would clear nothing. The bound
used is the policy's own evidence floor: below the RECEIPT-REQ threshold the policy accepts
the claim **with no document at all**, so it cannot consistently demand a document proving
what kind of ride it was; and whatever the line really is, no more than that receipt-free
amount is at stake. Above the floor (or whenever another limit fails) nothing changes.

## Decisions

* **Where:** one post-pass at the end of `PolicyEvalTool._evaluate_all` (pairs of rule +
  verdict), not in `evaluate_kind`: the floor and the other limits are other rules'
  verdicts, already FX-converted by `evaluate_with_fx`. Hot-file edits are small:
  `policy_eval.py` (+1 import, the pass, one `EvalItem.ground_named` field),
  `entry_context.ground_fields` (+1 key), `ground_transport.TrustedClassification` (+`named`).
* **Words only widen scrutiny:** `ground_named` = every family named by any typed word,
  trusted word or the segment *before* the unconfirmed widening. A named family keeps its
  rule at risk; more words can only add families / premium-lot / class / tip words, so
  adding words never clears a line that fewer words kept (tested).
* **Never more lenient than the claimant's own claim:** a typed "Amtrak" keeps GRND-01 (as a
  verified Amtrak line with no class does), so a typed-only line is never treated better
  than the same line verified as the kind its words name.
* **Verified and contradicting lines untouched:** the pass returns the same verdict
  objects for anything that is not `ground_transport.unconfirmed` (tested: all-rules
  evaluation == per-rule evaluation for verified / contradicting / non-ground lines).
* **Fail closed:** no receipt rule in the evaluated set (e.g. single-rule probes, authoring
  `evaluate_candidate`) → nothing cleared, so every existing single-rule test and the rule
  summaries' probes see the round-5 verdicts.
* **One advisory, not one per rule:** only the first cleared verdict carries the note; when
  another verdict still escalates (e.g. GRND-05 "incl. tip"), no note is added and the
  cleared rules simply drop out of the approver's list.
* No migration, no seed change: the floor is read from the active RECEIPT-REQ rule.

## Limits

* Accepted residual risk: a claimant may type "Uber" for a ride that is really a First-class
  train ticket or long airport parking, without a receipt, **below the receipt threshold**
  (< $25 by default) and get no approver check — the same exposure the policy already
  accepts for any receipt-free claim. A receipt / card that names the kind still verifies
  the line and gets the exact rule, as before.
* The floor follows the org's RECEIPT-REQ threshold; an org that raises it raises the
  clearing amount too (stated in the GRND summaries).
* The metric helper counts policy verdicts only; a report-level flag (missing receipt,
  duplicate, re-filed claim) can still escalate a line the helper counts as clear.
* `test_pg_lifecycle_db.py::test_lines_released_after_request_changes_are_not_punished_as_rejected`
  failed once in one of three full DB runs and passed in isolation and in the next full run;
  it files meals / generic lines, not ground transport (order / timing flake, not caused by
  this change).

## How to verify

```
R=…/run_ground.sh
$R -q ai_agents/tests/test_kl_ground_worst_case.py backend/tests/test_kl_ground_worst_case_db.py
$R -q ai_agents/tests/test_pg2_review_fixes_r5.py backend/tests/test_pg2_review_fixes_r5_db.py  # round-5 security tests, unchanged
$R -q -m 'not db' --ignore=evals/manual ; $R -q -m db ; $R -q backend/tests/eval -m db   # 48 passed
cd frontend && npx vitest run __tests__/expenses/kl-ground-type-hint.test.tsx
```

Tests: `ai_agents/tests/test_kl_ground_worst_case.py` (boundaries $24.98 / $24.99 / $25.00 /
$25.01, authored cap $20.00 / $20.01, strictest of two caps $15.00 / $15.01, EUR / JPY / GBP /
unsupported currency, rail / tip / premium lot / airport parking / class / claimed days
words, ambiguous words, rail segment, flight segment, overwritten vendor, receipt naming
nothing, verified / contradicting / non-ground lines unchanged, no receipt rule, always-
required category, unknown amount, the 20-ride metric, finance summaries);
`backend/tests/test_kl_ground_worst_case_db.py` (presubmit advisory + hint, typed Amtrak flag
with hint, approval routing: $14 Uber → `auto_approve`, $14 Amtrak → `escalate`, receipt
naming nothing $25.00 → `escalate` / $24.99 → `auto_approve`);
`frontend/__tests__/expenses/kl-ground-type-hint.test.tsx`. No existing test was changed.

Guards proven red-when-removed (reverted, test red, restored): the whole pass (25 tests
red), the unconfirmed gate (1), the receipt floor (9), other failing limits (1), rail named
(7), class named (1), airport parking named (4), premium lot (4), evidence destroyed (1),
unknown amount (1); frontend: the normalizer's `=== true` (3), the suggested fix (1), the
editor hint (1). A redundant unsupported-currency branch in the floor was removed (fx_policy
already reports `receipt_required: true` for it).

## Integration update

See `phase_10_known_limits_integration.md`: clearing is now bounded by a total per report (`ClearingBudget`), GRND-03 stays at risk on any airport-linked line, and the 20-ride metric is 20 -> 9.
