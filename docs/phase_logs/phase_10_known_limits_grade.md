# Phase 10 — Known limits: AIR-04 traveller grades

Branch `kl/grade` (base `product-gaps` @ `b044343`). Migration `0021_traveller_grades`
(down `0020_notification_reads`).

## What

AIR-04 — *"SVPs and above may book business class on flights > 6 hours"* — could only be
granted by an approver override, because Atlas held no grade for anyone (stage 2 limit in
`phase_9_product_gaps_policy.md`). It is now decided from the traveller's **grade of
record**:

| Piece | Where |
| --- | --- |
| Grade ladder (single source: codes, titles, short forms, rank, clause-stated entitlements) | `config/constants/grades.py` |
| `users.grade TEXT NULL` + `ck_users_grade_on_ladder` | `database/models/identity.py`, `database/migrations/versions/0021_traveller_grades.py` |
| Admin API: grade on create / edit / list / detail, ladder-validated, audited, no self-change (409 `SELF_GRADE_CHANGE`); `GET /grades` | `backend/app/modules/identity/{schemas,admin,router}.py` |
| The OWNER's grade, read from the DB (org-scoped, fail closed) | `ai_agents/agents/traveller_grade.py` (`owner_grade`) |
| AIR-04 as data (`role_exception_status`) + the finding by grade (`role_cabin_exception`) | `ai_agents/tools/process_rules.py` |
| AIR-CABIN verdict: entitled grade -> in policy with an AIR-04 note; otherwise the violation names why AIR-04 did not apply | `ai_agents/tools/policy_eval.py` (`EvalItem.traveller_grade`, `_role_exception_note`) |
| Wiring: trip approval (segment verdicts + AIR-04 flag), expense lines, planner (verdict + notices) | `approval_agent/evaluate.py`, `process_checks.py` (`trip_flags`, `option_notices`), `expense_agent/presubmit.py` (`build_entry_eval_item`), `travel_agent/ranking.py` |
| What-if `POST /policy/evaluate` can never claim a grade | `backend/app/modules/policy/router.py` (`_ALLOWED_EVAL_CONTEXT_KEYS`) |
| Rule validation: `role_exception_role` must be a ladder code; policy-page sentence updated | `ai_agents/agents/process_rule_semantics.py` |
| RAG corpus: policy DEF-09 (ladder), DEF-10 (entitlements, grade of record); FAQ "Traveler Grades" Q51–Q59 — all rendered from the ladder + `figures.py` | `database/seeds/corpus/{grades,policy,faq}.py` |
| Demo grades (seed only, only where none is recorded); corpus document text refreshed on reindex | `database/seeds/seed_all.py` (`DEMO_GRADES`, `_seed_demo_grades`, `_seed_corpus`) |
| Users admin UI: Grade column, grade select on Add / Edit, own grade read-only | `frontend/components/users/*`, `lib/query/user-admin-hooks.ts` (`useGrades`), `lib/types/{identity,shell}.ts` |
| Fix found on the way: the LLM-narrative step dropped every option's process notices (PTA-02 / AIR-06 / AIR-04) and the AIR-06 baseline | `ai_agents/agents/travel_agent/narrative.py` (`dataclasses.replace`) |

### The ladder (most senior first)

| Code | Title | Short | Rank | Entitlements (clause-stated) |
| --- | --- | --- | --- | --- |
| `CSUITE` | Chief Officer | C-suite | 80 | AIR-04, AIR-07, LOD-05, NR-01 |
| `SVP` | Senior Vice President | SVP | 70 | AIR-04, AIR-07, LOD-05, NR-01 |
| `VP` | Vice President | VP | 60 | — |
| `DIRECTOR` | Director | Director | 50 | — |
| `MANAGER` | Manager | Manager | 40 | — |
| `SENIOR_IC` | Senior Individual Contributor | Senior IC | 30 | — |
| `IC` | Individual Contributor | IC | 20 | — |
| `INTERN` | Intern | Intern | 10 | — |

Every entitlement comes from a TEP-FY26 clause that says "SVPs and above" / "SVPs+":
AIR-04 (business on flights > 6 h), AIR-07 (early-boarding and lounge fees), LOD-05
(suites / upgrades), NR-01 (lounge memberships). The corpus states **no** per-grade hotel
cap, per-diem multiplier or rail entitlement, so none exist (a test asserts DEF-10 names
none). Only AIR-04 is enforced by the rule engine; the other three are answerable policy
text that an approver applies.

### AIR-04 behaviour

Applies only to a **business** seat on a flight **longer than 6 h** that the AIR-CABIN
tiers do not already permit (6 h < d < 10 h). Elsewhere nothing changes (reason text is
byte-identical to before).

| Owner's grade of record | AIR-CABIN verdict | AIR-04 flag (trip approval / planner notice) |
| --- | --- | --- |
| C-suite, SVP | **in policy** — "permitted … by AIR-04: the traveler's grade of record (Senior Vice President) is SVP or above" | none |
| VP … Intern | violation (VP override) + "AIR-04 … does not apply: the traveler's grade of record is Vice President" | `approval_required`, approver = the AIR-CABIN override approver (VP) |
| none recorded (NULL) | violation + "AIR-04 … could not be applied: no grade is recorded" | `approval_required`, approver SVP, `could_not_verify: traveller_grade` |
| rule threshold not on the ladder (legacy row) | violation, "threshold … not on the grade ladder" | `approval_required` by that role |

First class is never granted (AIR-04 names business; AIR-03 "first class is never
reimbursable"). Flights ≤ 6 h stay economy for every grade (AIR-01).

## Why / key decisions

* **One ladder, three consumers.** `config/constants/grades.py` is imported by the model
  (CHECK text), the identity API (validation), the rule engine (rank compare) and the
  corpus renderer. Migration 0021 holds a FROZEN copy of the codes; a test fails if they
  drift, so a ladder change forces a new migration.
* **Rank compare against the rule's `role_exception_role`.** The rule stays
  finance-editable data ("SVP"); the ladder says what "and above" means. A finance edit to
  "VP" moves the threshold; a non-code is refused at activation and fails closed if a
  legacy row holds one.
* **Grade comes from the OWNER's user row, never a request.** Trip approval and AIR-04
  flags use `trip.traveler_id`; expense lines use the report owner passed to
  `build_entry_eval_item`; the planner uses `context.user_id` (the planner only books for
  the requester — `traveler_id=context.user_id`). The approver's / finance admin's grade
  never counts (tested with a C-suite evaluator on an IC's trip). `traveller_grade` is
  removed from the what-if keys of POST /policy/evaluate. `owner_grade` is org-scoped and
  any lookup failure means unknown.
* **Unknown = fail closed.** NULL, a title instead of a code, another org's user, a lookup
  error: never entitled; an approver confirms.
* **Junior grade vs unknown grade name different approvers.** For a known junior grade
  AIR-04 simply does not apply, so the flag names the AIR-CABIN override approver (VP);
  for an unknown grade it names the SVP level that must confirm the grade (as before —
  there is still no SVP approval tier; the line manager receives it with the level named).
* **SoD: an admin can never change their OWN grade** (409 `SELF_GRADE_CHANGE`; re-sending
  the unchanged value is a no-op). Another finance admin must do it. The UI disables the
  field on the admin's own row and never sends it. Every grade change is in the
  `user.updated` audit diff; `user.created` carries it too.
* **Migration safety.** Nullable add without default (catalog-only), CHECK added
  `NOT VALID` then validated (all NULL — passes), symmetric downgrade (drop constraint,
  drop column). **No grade is guessed for any real user.** Only the demo seed backfills
  its own five demo accounts, and only where no grade is recorded:
  manager@ = SVP (AIR-04 demonstrable), manager2@ = VP (the boundary), finance@ = Director,
  employee2@ = Senior IC, employee@ = IC.
* **Corpus.** DEF-09 / DEF-10 sit in Section 2 (Definitions & Roles); FAQ Q51–Q59 form a
  "Traveler Grades" section. All text is rendered from the ladder and `figures.py`
  (`SVP_BUSINESS_MIN_HOURS`, cabin hour bands). Unit counts 208 -> 219 (policy 100, faq
  59). The corpus spells "traveler" (US), so the new prose does too.
* **Retrieval regression held.** The first wording displaced three gold units at rank 9–10
  (HOW-07 on "What cabin can I book for a long flight…", NR-01 on the minibar query,
  REF-05 pushed out of the dense top-50 by FAQ-Q56). The FAQ wording was tuned until a
  per-query diff (old vs new corpus, same harness) showed **no gold unit leaving or entering
  any top-10**. eval-db: 48 passed; recall@10 0.819, coverage 1.0, abstention 0.019231
  (identical), per-doctype floors held (howto 0.962, reference 0.667).
* **Calibration artefacts not re-committed.** The DB suite's fit tests rewrite the LOCAL
  sections of `ai_agents/rag/abstention_threshold.json` / `rerank_calibration.json`; on the
  new corpus they move slightly and differ between runs (e.g. local `computed_tau`
  0.220735 vs 0.218258), so the committed values are left as they were. Local is not
  promoted (cosine fallback) and no gate reads those local numbers; the production
  (novita) sections are fitted live and untouched.
* **Reindex picks it up.** `_embed_corpus` re-chunks from the units (the new 11 units are
  embedded on the next `reindex_policy_corpus` boot); `_seed_corpus` now also refreshes a
  corpus row's stale `document_text` in place (system-owned rows, no duplicate).

## Limits (honest)

* **Staging / live users have no grade until a finance admin sets one** (by design). On
  staging, set manager@ to SVP on the Users page to demo AIR-04 (the demo seed does not
  run there; the reindex does).
* Only AIR-04 is rule-enforced. AIR-07 lounge / early-boarding fees, LOD-05 suites and
  NR-01 lounge memberships are in the corpus and on `GET /grades` but no rule checks them.
* There is still no SVP approval tier: an unknown-grade AIR-04 flag names "SVP" but routes
  to the usual approver (line manager / finance fallback).
* AIR-03's "< 8 hours before a critical commitment, with VP approval" branch is still not
  modelled (unchanged).
* The planner's stock inventory has 6–10 h business options only on synthesized routes
  (JFK–LHR 7.0 h, ORD–LHR 8.2 h on non-fixture dates); SFO–LHR is 10.5 h (AIR-03 already).
* Seeded demo grades are written without an audit row (like every seeded demo row).
* `GET /grades` is gated on `users.read` (finance admins); employees do not see the ladder
  in the UI (they can ask Policy Q&A, which now answers from DEF-09 / FAQ).

## How to verify

```
R=…/kl/run_grade.sh
$R -q ai_agents/tests/test_kl_grade_air04.py          # hermetic: every grade boundary, unknown, hours, first class, notices, what-if, validation, narrative
$R -q backend/tests/test_kl_grade_ladder.py           # hermetic: ladder == migration CHECK == model == rule == corpus (DEF-09/10, FAQ)
$R -q backend/tests/test_kl_grade_db.py               # migration 0021 up/down on populated users, admin API + SoD, owner-not-requester (trip / expense / planner), seeds, RAG
$R -q -m 'not db' --ignore=evals/manual ; $R -q -m db ; $R -q backend/tests/eval -m db   # 48 passed
cd frontend && npx vitest run __tests__/shell/users-screen.test.tsx
```

Guards proven red-when-removed (temporarily mutated, test failed, restored): owner grade at
trip approval (segment verdicts) and in `trip_flags`, owner grade on expense lines, planner
grade, admin self-grade refusal (API + UI), grade validated on the ladder, what-if cannot
claim a grade, unknown grade never entitled, the SVP rank boundary, the cabin verdict
clearing only when entitled, the AIR-04 finding suppressed only when entitled, rule role
must be a ladder code, `owner_grade` org scope, demo seed fills only NULL grades, corpus
text refresh, migration ladder == app ladder, the migration's CHECK, and the narrative
keeping the notices.
