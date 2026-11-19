# Phase 8 / Completion C7 — Fixes for the 27 staging acceptance defects
**Date:** 2026-11-09
**Author:** Garv Singh Saini
**Status:** CODE COMPLETE, awaiting merge and the staging re-acceptance run.
Branch `c7-fixes`, five commits, one per defect group.

## Scope

The C7 staging acceptance run reproduced 27 defects: 8 high, 13 medium and 6 low.
All 27 are addressed here, together with the three test-suite defects T1–T3.
C7-10 is fixed by query-time lexical relevance plus a live-safe reindex of
the staging policy corpus that runs on deploy (see "Staging policy index").

Each group went through three stages:
1. Implement, with regression tests shown to fail on the old code (revert plugins
   or in-memory mutants).
2. Two independent adversarial reviews: bypass/correctness, and
   regression/test strength.
3. A fix round.

A final review over the whole branch then looked for bypasses spanning groups,
and found and closed four.

## Defect → commit map

| Defects | Commit | Summary |
|---|---|---|
| C7-01, C7-02, C7-03, C7-13 | `C7-01/02/03/13` | Owner-only writes (403 when the record is readable but not owned, 404 when unreadable). Trips can't be self-approved through PATCH. Nobody can decide or trigger an approval on their own subject. Approvals only on submitted subjects (400 `INVALID_SUBJECT_STATE`). Unrouted escalations fall back to finance_admin. A decision now moves the trip's status. |
| C7-05, C7-15, C7-14, C7-24 | `C7-05/15/14/24` | Fresh deterministic policy evaluation at approval time. Auto-approval requires `is_policy_compliant == True`. Receipt and card evidence take precedence over claimant-typed segments. Pre-submit uses real segments and falls back to the entry's own fields. Manager dashboard scoped like `GET /approvals`. 405 returns `METHOD_NOT_ALLOWED` and keeps the `Allow` header. |
| C7-16, C7-17, C7-18, C7-19, C7-21, C7-22 | `C7-16/17/18/19/21/22` | Receipt RBAC and validated corrections. A correction syncs to its draft entry. Subtotal/total ambiguity guard. Transportation maps to ground_transport. Smart-match for inbox receipts releases the inbox draft entry, so a charge can't be claimed twice. The AI agent's `trip_id` is owner-scoped. |
| C7-04, C7-06, C7-09, C7-10, C7-11, C7-12, C7-23 | `C7-04/06/09/10/11/12/23` | Provisional cited answer for needs_review. Assistant replies persisted. Query-time lexical relevance. `/policy/evaluate` contract. Rule authoring returns the rule it created, or 422 with no audit row. No empty drafts. |
| C7-07, C7-08, C7-20, C7-25, C7-26, C7-27, T1–T3 | `C7-07/08/20/25/26/27 + T1–T3` | needs_review answers rendered with a caution label. Date-only values parsed as local dates. Auto-approved lane filters. Role gate on routes. Stale refresh token cleared. E2E fixes: real receipt image, assertions on the PATCH response, the trip test submits before triggering. |

## Key decisions

- **Errors for writes on others' records:** 403 when the caller can read the
  record but doesn't own it; 404 when the caller can't read it.
- **Approval on an unsubmitted subject** returns 400, as requested. Illegal
  state moves keep returning 409.
- **Cosine citation floor kept at 0.70.** On the 197-query eval set, every lower
  floor admits out-of-corpus questions as answered: 0.65 lets through 5 of 52,
  and 0.60 lets through 19 of 52. Instead, staging shows needs_review answers as
  provisional text, limited to sentences that carry a verified citation and pass
  the entailment check.
- **Fail closed at approval time.** These now escalate to a human: unpriced trip
  segments, non-USD amounts, evaluation errors, user-edited receipt amounts, and
  receipts that don't cover the claim.

## Verification

| Check | Result |
|---|---|
| `ruff check .`, `ruff format --check .`, `mypy backend config evals` | clean (162 files) |
| Hermetic pytest, as CI runs it | 1207 passed, 928 skipped, 0 failed |
| Full DB suite on an isolated DB | the failing set is identical to the 27-id baseline; 0 new failures |
| eval-db | 48 passed. recall@10 0.819, answerable_coverage 1.0, unanswerable_abstention 0.019231, faithfulness_proxy 1.0 (all unchanged) |
| Frontend: vitest | 50 files, 300 tests passed |
| Frontend: tsc, lint, prettier, `next build` | clean |
| Playwright, CI configuration run locally on a fresh migrated and seeded DB | 24/24 passed |

## Staging policy index (C7-10 / C7-06)

Staging still served the legacy 8-clause stub index with no `chunk_tsv`; it was
seeded before the R0-T4 corpus migration. Running the full `seed_all` there would
also resync demo passwords and add demo trips, reports and receipt images, so it
was not used.

- **New, narrow reindex:** `database/seeds/reindex_policy_corpus.py` runs only
  three steps, in one transaction:
  1. `_seed_corpus` adds the Helios document rows if they are missing.
  2. `_embed_corpus` chunks and embeds them and rebuilds `chunk_tsv`.
  3. `_retire_stub_embeddings` deletes the stub's embeddings.

  The stub's deterministic rules stay. Users, trips, reports, receipts and
  approvals are not touched. It is idempotent, and with
  `RAG_CONTEXTUAL_PREFIXES` off it makes no LLM calls.
- **How it runs:** the entrypoint has a new mode,
  `SEED_ON_STARTUP=policy_corpus`, and `render.yaml` sets it. The reindex runs
  on every boot. If it fails, the new container stops and the previous deploy
  keeps serving.
- **Verified:**
  - `test_c7_10_reindex_policy_corpus.py` builds a staging-like DB (8 stub rows,
    no corpus). After the reindex: 0 stub rows, corpus fully indexed with
    `chunk_tsv`, identical user, trip, report, expense, approval and receipt
    counts, and the same password hashes. A second run changes nothing.
  - The real entrypoint in this mode indexed 208 chunks.
- **Contaminated C7 test data:** report `8002eafc` and the other records created
  through the C7 bypasses remain on staging and were left untouched.

## Known limits carried forward

- ~~**P12 for expense reports:** an approval decision still doesn't set the
  report's status.~~ Fixed in C7R (see `phase_8_completion_c7r_fixes.md`):
  approve / reject / request changes now move the report to approved /
  rejected / draft, and a resubmission re-queues and re-evaluates the approval.
- **Splitting a claim across reports** each under $500 remains a limit of the
  policy design.
- **Manual airfare and lodging entries** with no receipt, card transaction or
  approved trip segment now always escalate.
