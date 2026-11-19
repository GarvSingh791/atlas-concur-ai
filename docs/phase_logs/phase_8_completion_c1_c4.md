# Phase 8 / Completion C1–C4 — Deterministic categorize, receipt image access, correlation id, audit trail
**Date:** 2026-11-08
**Author:** Garv Singh Saini
**Status:** COMPLETE (code) — C1, C2 built; C3 closed with no migration (column
  already exists since 0007, propagation gap fixed); C4 backend verified as already
  conforming, tests and the finance audit page added. One branch,
  `p8-completion-c1-c4`, one commit per item.

## Scope

The four code items left after P8-T4. Each was diagnosed read-only first. The two
"decide, then build" items (C3, C4) were decided against the private design docs,
which say:

- **C3:** the `ai_decision_logs` table in the schema design has no `correlation_id`
  field.
- **C4:** the API design lists `GET /audit`, finance-scoped and filterable.

## C1 — Deterministic handler for `expense_categorize_simple`

**Diagnosis.** The routed task type never ran. The backend always sent
`signals={}`, so every expense turn was classified `expense_reasoning` and logged
`route=llm`. And no deterministic handler was registered for the simple type, so a
turn routed there would have returned `needs_review` without doing anything.

**Built.**
- `ai_agents/agents/expense_agent/rule_categorizer.py` (pure, no LLM import).
  - **Matching:** whole-word tokens plus bigrams. Signal tiers, highest first:
    receipt `expense_type` (0.95), MCC range (0.95), known brand (0.90), generic
    keyword (0.80). Two categories hitting at once is a conflict (0.50); no
    match is `other` (0.0).
  - **Ambiguous tokens:** words such as `united`, `delta`, `ritz`, `inn`, `cab`
    and `train` only match inside a phrase ("united airlines", "train ticket").
- `ai_agents/agents/expense_agent/categorize_handler.py`, registered for the
  task type in `register_expense`.
  - **At or above 0.80:** deterministic answer, zero provider calls, decision log
    `route=deterministic`.
  - **Below 0.80:** one LLM call, logged `route=llm` with `model_provider`.
  - **LLM fails** (provider error, no provider, unparseable or out-of-enum
    answer): `needs_review`, the rules' best guess, `degraded=True` (NFR-R1).
  - **Logging:** `input_summary` holds booleans only, never merchant text.
- `backend/app/modules/ai/service.derive_task_signals`: sets `expense_simple`
  only when an expense conversation's payload carries `intent: "categorize"`.
  This is an explicit caller intent, not a heuristic over the message text.
- `DeterministicHandler` docstring: deterministic-first handlers may escalate
  internally and log their own route.

**QA finding (not changed).** `categorize.py::_keyword_category`, used by
`ExpenseAgent` and the receipt auto-draft, matches substrings ("dinner" contains
"inn", "repair" contains "air"). C1 leaves it untouched, to keep existing outputs
stable; the new rule module does not inherit the bug. Follow-up candidate.

## C2 — Receipt image serving (NFR-SEC4)

**Built.**
- **Access.** `GET /receipts/{id}` is gated by `expense.read.own` (held by all
  three roles) and narrowed by `scope_query` on `Receipt.uploaded_by`. The
  uploader (always the owner of the receipt's report), their direct manager and
  `finance_admin` get the receipt. Everyone else gets 404, and nothing is signed
  for them.
- **The link.** After scoping, the response carries `image_access_url`,
  `image_access_expires_at` and `image_access_expires_in`.
  - Lifetime: hard-capped at 300 s, whatever the setting.
  - Not served: non-http(s) URLs. That includes the local provider's `file://`,
    which a browser can't load and which would expose a server path.
  - Signing errors: any failure degrades to null fields. The log line carries only
    the receipt id and the exception type, never the URL.
- **`receipt_id` on entries.** `ExpenseEntryResponse.receipt_id` holds the earliest
  linked receipt. It is filled by one batched, tenant-guarded query for the report
  entries list, `PATCH /expenses/{id}` and the approval detail.
- **Frontend.** `ReceiptImage` with `useReceipt`.
  - Caching: `staleTime` 0, no retry on 4xx.
  - Refresh: refetches 30 s before the link expires, counted from when the response
    arrived, so a skewed browser clock doesn't matter.
  - Load failure: refetches once on an `<img>` error and re-arms only after a
    successful load, so it can't loop.
  - Where it shows: `EntryLineage` (report and approval views) shows the image.
    `ExtractionReview` prefers the server image and falls back to the local blob
    preview.
- **What stayed the same.** POST and PATCH are still uploader-only. The receipt
  pipeline's decision log stores the object key, never a signed URL (checked).

**Decision.** Managers see a direct report's receipts with the same reach as
`scope_query` gives for expense reports, which includes receipts on drafts. The
design describes manager visibility in terms of submitted items. Narrowing it is a
follow-up decision, applied consistently to reports and receipts together.

**Known limits.**
- In local and e2e runs (`STORAGE_PROVIDER=local`) the link is always null, so the
  uploader sees the blob preview and other roles see "Preview unavailable".
- Docker Compose MinIO links (`http://minio:9000`) can't be reached from a browser.
- Staging (R2, public https endpoint) is the environment where the image shows.

## C3 — `correlation_id` on `ai_decision_logs`

**Decision: no migration 0013, no schema change.** The design doc omits the column,
but the repo already has it: model field plus index, added by migration
`0007_ai_decision_log_correlation_id` (Phase 8 preflight remediation). The decision
log writer already persists `context.correlation_id` and logs it as `corr=`, and the
root log filter adds the correlation id to every log record. So the plan's two
branches didn't apply as written; the real gap was propagation.

**Gap and fix.** Only one of seven call sites (the conversation route) passed the
id; receipt upload/correct, approvals and the three policy routes stored NULL.
`build_agent_context` (backend) now defaults to the request ContextVar,
normalises `""` to `None` (NULL outside a request), and caps the caller-controlled
value at 128 characters. One change fixes all seven call sites. `ai_agents` still
never imports `backend`.

**Superseded notes.** These lines are out of date as of this log:
- `phase_8_p8_t3_env_variables.md` and `phase_8_p8_t4_cicd_pipeline.md` say
  "correlation_id absent on ai_decision_logs".
- The same carry-forward lists say "GET /receipts/{id} and GET /audit not
  implemented".

## C4 — `GET /audit`

**Verified, not rebuilt.** The endpoint (`backend/app/modules/audit/`, mounted at
`/api/v1/audit`) already met every acceptance item:
- **Access:** `audit.read` is granted only to `finance_admin`, and the system has
  no separate admin role.
- **Org scoping:** applied before any filter.
- **Read-only:** a single GET route.
- **Shared helpers:** pagination, allow-listed filters (`action`, `entity_type`,
  `entity_id`, `actor_id`) and sort (`created_at`), with unknown keys rejected as
  400.
- **Staging:** finance gets 200 and employee gets 403.

No backend production code changed.

**Added.**
- **Hermetic tests:**
  - manager 403
  - org predicate present in the WHERE clause
  - POST/PATCH/DELETE return 405
  - `actor_id` filter applied
  - bad sort direction returns 400
  - `-created_at` parsed as descending

  The older filter tests also checked the whole SQL text, where the SELECT list
  always names the column, so they could never fail. All are now checked against
  the WHERE clause only, and each was confirmed by deliberately breaking the code.
- **DB tests** (`test_audit_api_db.py`): cross-org rows excluded (total and ids),
  row-level `?action=` filtering, another org's `entity_id` returns 0.
- **Collection-contract test** for `/audit` in `test_pagination.py`.
- **Frontend:** the "Coming soon" placeholder at `/audit` is replaced with the
  finance audit trail.
  - Hook: `useAuditLog`.
  - Filters: action, entity type.
  - Table: time, actor or "System", action, entity, entity id, with pagination.
  - States: loading, empty and error.
  - E2e: the finance-flow spec now asserts the real page.

## Verification

| Check | Result |
|---|---|
| `ruff check .`, `ruff format --check .`, `mypy backend config evals` | clean (153 files) |
| `pytest` as CI runs it (no DB) | 913 passed, 783 skipped, 0 failed |
| Full DB suite (isolated `atlas_test` DB) vs pre-change baseline | 1656 passed, 24 failed, 3 errors — the 27 failing ids are identical to the baseline; 0 new |
| Frontend `lint`, `format:check`, `type-check` | clean |
| `vitest` | 40 files, 236 tests passed |
| `next build` | compiled; `/audit` built |
| Each item: two adversarial reviews (correctness; security/RBAC) | all real findings fixed (details below) |

**Review findings fixed.**
- **C1:**
  - Brand words that are also ordinary words ("United Way", "Delta Dental",
    "Ritz Crackers") gave confident wrong answers.
  - "train" matched the verb.
  - `has_mcc` was set for unusable codes.
  - A failed chat-message append could break the decision-log write; it now runs
    in a savepoint.
- **C2:**
  - Three frontend tests passed before the query had finished.
  - Link refresh depended on the browser clock.
  - Unwrapped SDK errors could return a 500.
  - The "URL never logged" test was reading a disabled logger.
- **C4:** the filter and org-scope tests could never fail (see C4 above), and sort
  checks couldn't tell `-created_at` from the default.

**Rejected:** an upload file-type allow-list. It isn't a C2 defect: the signed link
is served from the storage domain and only shown in `<img>`. Follow-up candidate.

**Not run here:** Playwright (CI's `e2e` job runs it on the PR).

## Incident during the build

A test run without the isolated-DB setting fell back to the local `.env`, and the
test fixture (which drops and recreates the `public` schema) wiped the local dev
database `atlas_db`. Staging and Neon were not touched. Recovery: `alembic upgrade
head` + `python -m database.seeds.seed_all`. Any hand-entered local dev rows were
lost.

**Lesson.** DB-backed test runs must always point `DATABASE_URL` at a throwaway
database. The test fixture should probably refuse a database whose name doesn't
look like a test database; that guard is a follow-up candidate.
