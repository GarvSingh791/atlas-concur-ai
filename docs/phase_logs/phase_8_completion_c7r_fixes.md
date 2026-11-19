# Phase 8 / Completion C7R — Re-acceptance fixes

C7R re-ran the staging acceptance on the C7 fixes (23/27 fixed; C7-06/10
partial, C7-22/27 blocked) and fixed the remaining defects plus the issues the
cross-group reviews found. This log records the behaviour a reviewer needs to
know; the code comments carry the detail.

## Policy Q&A (C7-06 / C7-10 residual)

- **Sentence-level support gate** (`ai_agents/rag/support.py`, called by
  `grounding.check_entailment(cited, threshold, question=, retrieved=)`). Each
  cited sentence is checked against the clause text it cites. Named checks, as
  recorded in the decision log's `sentence_support`:
  - `low_support` — content-concept recall below `NLI_ENTAILMENT_THRESHOLD`
    (0.55);
  - `number` — every figure must appear in the cited evidence; a figure from
    the question is accepted only without a comparator and only when it fits
    the cited threshold;
  - `direction` — a cited figure must keep the clause's comparator
    (under / over, ≤ / >), whether it is written before the figure or after it
    ("15% or more", "3 hours or less", "6 hours and up");
  - `unanchored` — the sentence must state something the clause backs beyond
    the user's own words;
  - `polarity` — a positive sentence that matches a negated clause segment as
    well as a positive one fails, and so does a positive sentence that names a
    subject only a negated segment of the cited clause mentions ("Wi-Fi and
    minibar are reimbursable" against LOD-04's "Minibar ... are not"; "first
    class is reimbursable for flights > 10 hours" against AIR-03). Generic
    predicate words (reimbursable, permitted, approval, fee) are not subjects;
  - `contrast` — another retrieved clause matches the sentence at least as well
    as the cited one (mis-citation);
  - `uncited` — no citation, no evidence.
- **Provisional answers** show only the sentences that pass, with only their
  citations, and only when the LLM critic said SUPPORTED. If a dropped sentence
  states a condition (must, need, require*, approval, justification, when,
  before, ...), the whole draft is withheld (`dropped_caveat`).
- **Abstention.** A draft that says the documents do not cover the question
  (the phrase must refer to the source: policy, clauses, documents, context) is
  classified `model_declined` and returned as the silent shape. The UI shows
  readable copy for reason codes and never the raw code.
- **Critic determinism.** `PolicyQaCritic` runs at
  `POLICY_QA_CRITIC_TEMPERATURE` (default 0.0) through
  `LlmProvider.complete_with_options`, and its verdict parser accepts the first
  non-empty line with markdown, quotes or a `Verdict:` label
  (`parse_critic_verdict`). "NOT SUPPORTED" / "UNSUPPORTED" are never read as
  SUPPORTED; an unparseable reply is inconclusive (withheld).
- The keyed live harnesses (`evals/manual/test_rag_corpus_ragas_live.py`,
  `test_rag_latency_profile_live.py`) pass `question=` and the top-K
  `retrieved=` set to `check_entailment`, so they measure the production gate.

## Receipts (C7-18 residual)

- A total is **withheld** (null, never sent to the gap-fill LLM) when it is
  illegible, ambiguous, equals the subtotal without an affirmed legible total,
  or was not read while a subtotal / tax was (`total_unread`).
- An amount the gap-fill LLM **inferred** (`ai_inferred`, confidence 0.60) is
  kept on the receipt as a suggestion (M-A1 measures it) but is never drafted
  into an expense and never counts as receipt evidence for RECEIPT-REQ
  (`receipt_support_issue` rejects `user_edited` and `ai_inferred` amounts).
  The review reason asks the user to *enter* the total, and the UI hides the
  value.
- Entering the total (PATCH amount) creates the deferred draft
  (`linked_expense_sync: "drafted"`). Its verdict uses the same
  `receipt_support_issue` check as approval time, so a typed total reports a
  blocking `missing_receipt` instead of "in policy". The UI shows the draft and
  its verdict and refreshes the expense caches.
- A missing / low-confidence location is a review reason only for lodging (the
  HOTEL-CAP city) or an unknown expense type.
- Concurrent "enter the total" PATCHes serialise on a row lock: one draft.
- The Receipt Inbox a draft is added to is read `FOR UPDATE`, and so is the
  parent report in `PATCH /expenses/{id}`. A submit of the same report waits for
  the add / edit and then submits the report with it; a submit that committed
  first makes the upload open a new inbox (an edit gets 409). An entry can no
  longer commit onto a report that was already submitted or auto-approved.
- A Receipt Inbox report that has been through an approval (returned for
  changes) is never reused for new uploads; the newest unreviewed inbox wins.

## Policy evaluation and approvals

- Every policy flag carries `entry_id` (the expense UUID) when the entry is
  persisted; `entry_ref` stays a readable label. The approval detail attributes
  a flag that has an `entry_id` by that id only (no vendor fallback), so of two
  same-vendor lines only the violating one is marked. `/policy/evaluate` labels are
  unique within the subject, and ordinals never collide with a typed vendor.
- `/policy/evaluate` RECEIPT-REQ wording matches approval time (e.g. "the
  attached receipt (30.00) does not cover the claimed amount (320.0)").
- **Duplicate claims.** Approval time and `/policy/evaluate` raise a blocking
  `possible_duplicate` flag when the owner has another expense with the same
  date, amount and currency and a matching supplier, either on another report
  that was not rejected or on the same report. Suppliers are compared by
  normalized brand key (`same_supplier`, so "United Airlines Inc" equals
  "UNITED"), using both the claimed vendor and the vendor read from each line's
  receipt; a line with no vendor name at all matches (blanking the vendor
  cannot hide a duplicate). If the lookup fails, the flag is raised anyway
  ("the duplicate-claim check could not be completed") — it fails closed. The
  demo card-feed top-up can recreate a consumed swipe, and an auto-approved
  report is never seen by a human, so this flag is what routes a second claim
  for one purchase to an approver.
- **P12.** Approve / reject / request changes set the report status. A
  resubmitted report re-queues its approval, re-runs the deterministic risk
  assessment over the current entries (route forced to `escalate`,
  `reassessed_on_resubmission`, `resubmitted_at`) and routes it to an
  independent approver. If the re-assessment fails, the snapshot says
  `evaluation_failed: true` with `subject_in_policy: null`; the UI shows
  "Not evaluated — human review required".
- **Trips.** `request_changes` on a trip sets it back to `draft` (not
  `pending_approval`) so the traveler can change it. Resubmitting
  (`PATCH /trips/{id}` `{status: "pending_approval"}`) re-queues the same
  approval as `pending` with a fresh, deterministic, escalate-only risk
  assessment (`reassessed_on_resubmission`) routed to an independent approver.
  `GET /trips` and `GET /trips/{id}` carry `approval_status` and, while the
  trip is returned or rejected, `approval_reason`. The frontend now has a
  submit / resubmit action: the planner shows the selected trip's approval
  panel (status, approver's comment, "Submit for approval" / "Resubmit for
  approval"), and `/trips` lists the traveler's trips with the same panel. The
  planner stepper puts an unsubmitted draft on "Selection" and a pending or
  returned trip on "Approval"; saving a selection toasts "Trip saved as a
  draft" (it was "pending approval", which was never true).
- Authored rules have a readable title (`draft_rule_title`, e.g. "Hotel cap —
  Tier 2 cities $275/night") on the API and in the authoring card, which also
  shows the corpus-verified clause (or "No matching clause cited").

## Known limits

- The lexical support gate cannot judge relations that need inference (a wrong
  tier-to-amount mapping using a figure present in the clause); the LLM critic
  remains the semantic gate. Some faithful corrections are withheld (safe
  false negatives).
- Duplicate detection needs the same date, amount and currency. A second claim
  with a different amount or date, or with a supplier name that does not
  normalize to the same brand on either the claim or the receipt, is not
  flagged. Two genuinely separate same-day purchases of the same amount from
  one supplier are flagged (a human decides).
- Card merchant strings padded with a city but no digits still read as a
  supplier conflict and escalate (fail-closed).
- Staging data created during C7 / C7R acceptance is not repaired.
- The calibration fit tests (`evals/calibration/test_fit_*`) rewrite the tracked
  calibration JSON only when a value changed, so a re-run no longer re-escapes
  the notes.
