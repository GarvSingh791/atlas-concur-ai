# Phase 9 — Product gaps: app shell (gaps 3 and 7)

Track: `pg/shell` · base `2fbe114` (alembic head `0015_canonical_policy_figures`) ·
migration `0020_notification_reads`.

## What was built

| Gap | Before | After |
|---|---|---|
| 3A Notifications | Top-bar bell "Presentational Only", permanent red dot, no handler | Real bell: server unread count badge (only when > 0), popover feed, mark read / mark all read, links to the subject |
| 3B Global search | "Search Atlas…" input with no handler | Command-style search over trips, reports, entries, receipts, approvals and policy rules, scoped like the list endpoints |
| 3C Users admin | `/users` "Coming soon" page in the finance nav; backend had only GET | Finance-admin user administration: list/search/filter, add, edit, deactivate/reactivate with governance safeguards |
| 7 Dead OCR option | `OCR_PROVIDER=anthropic` offered; adapter always raised "not yet implemented" | Option, stub adapter and factory branch removed; a stale value fails at settings validation with a clear message |

### Backend

* `GET /api/v1/notifications` (Page envelope, newest first, capped at 50),
  `GET /notifications/unread-count`, `POST /notifications/read-all`,
  `POST /notifications/{id}/read` — `backend/app/modules/notifications/`.
* `GET /api/v1/search?q=&limit=` — `backend/app/modules/search/`.
* `POST /users`, `PATCH /users/{id}`, `POST /users/{id}/deactivate`,
  `POST /users/{id}/reactivate`; `GET /users?q=` (name/email substring);
  `UserResponse.manager_name` — `backend/app/modules/identity/{admin,password_policy}.py`.
* `backend/app/core/text_search.py` — LIKE-escaped, parameter-bound ILIKE helper.
* Migration `0020_notification_reads` + model `database/models/notification_read.py`;
  the same revision adds the unique expression index `uq_users_email_lower` on
  `users (lower(email))` (see "Email is globally unique" below).
* `config/settings/base.py` — `OCR_PROVIDER: Literal["local","openrouter"]` + a
  before-validator that rejects `anthropic` explicitly.

### Frontend

* `components/atlas/notification-bell.tsx`, `components/atlas/global-search.tsx`
  (mounted by `top-bar.tsx`; the two presentational blocks were replaced).
* `components/users/{users-screen,user-dialogs}.tsx`, `app/(app)/users/page.tsx`.
* Hooks: `lib/query/{notification,search,user-admin}-hooks.ts`; types in a NEW
  file `lib/types/shell.ts` (no shared type module edited);
  `lib/shell/{links,relative-time}.ts`; `lib/hooks/use-debounced-value.ts`.
* Deep links so links land on the subject: `/expenses?report=<id>` opens the
  report detail (read-only banner for non-owners — unchanged ReportDetail
  logic), `/approvals?approval=<id>` preselects the approval, `/trips?trip=<id>`
  expands and scrolls to the trip. Each is a few lines in the existing screen.

## Why / decisions

### Notifications are derived, not stored

There is no event bus, and adding one would mean touching every workflow
transition (hot files owned by other tracks). The feed is computed per request
from state that already exists:

| Recipient | Kind | Source |
|---|---|---|
| approver (`approval.read`) | `approval_pending` | pending approvals whose `risk_summary.routed_to_approver_id` is the caller, inside the caller's `scope_query` read scope, never their own submission |
| finance admin | `approval_escalated` | pending approvals with `risk_summary.route == "escalate"`, changed in the last 30 days, not routed to them, not their own |
| owner | `approved` / `rejected` / `returned` | approval steps (approve, auto_approve, reject, request_changes) on the caller's own submissions in the last 30 days, re-checked against the live subject owner (fail closed on a divergent row) |
| owner | `reimbursed` / `completed` | own reports with status `reimbursed`, own trips with status `completed`, `updated_at` in the last 30 days (no endpoint sets these statuses in this base; they appear whenever data holds them) |

* **Deterministic ids**: `decision:<step id>`, `report_reimbursed:<id>`,
  `trip_completed:<id>`, `approval_pending:<approval id>:<step count>`,
  `approval_escalated:<approval id>:<step count>`. The step count in approval
  keys means a returned-then-resubmitted approval notifies again (new id).
* **Read state** is the only persisted part: `notification_reads(user_id,
  notification_key, read_at)`, unique `(user_id, notification_key)`, FK to
  `users` with `ON DELETE CASCADE`, index `(user_id, read_at)`, key length check
  1..200. Marking is `INSERT … ON CONFLICT DO NOTHING` (idempotent).
  `POST /{id}/read` only accepts an id in the caller's CURRENT feed (404
  otherwise), so nobody can mark or probe another user's items.
  `read-all` also prunes the caller's read rows older than 30 days whose key is
  no longer in the feed (so the table stays bounded without ever resurrecting a
  still-visible item, e.g. a long-pending approval, as unread).
* A table was chosen over `users.preferences` JSONB: per-row uniqueness,
  cascade on user delete and no read-modify-write races on a shared JSON blob.
* **Not audit-logged**: marking a notification read is personal UI state, not a
  business state change; auditing it would flood the compliance log. Every
  business mutation in this track (all user-admin changes) IS audited.
* Cost: ≤ 5 indexed, LIMIT-50 queries per request; the badge polls
  `/unread-count` every 60 s and on window focus.

### Search

* Every group goes through the same `scope_query` the list endpoints use;
  entries are joined to their parent report and scoped through the report;
  approvals only for `approval.read`; policy rules from the org's active policy
  (everyone with `policy.read` sees ACTIVE rules; `policy.manage` also sees
  drafts/archived). Rule titles are derived (`draft_rule_title`), so rules are
  matched in Python over at most 500 rows of the active policy.
* `q` is 2..100 characters (422 `QUERY_TOO_SHORT` / `QUERY_TOO_LONG`); per group
  default 5, hard max 10, `truncated` when more exist.
* `%`, `_` and `\` are escaped and the term is a bound parameter.
* **No new index**: a leading-wildcard ILIKE cannot use a btree and `pg_trgm` is
  not installed (and deliberately not required); every query is already narrowed
  by the tenant / owner indexes and capped.
* **Links**: the server returns a canonical `link` and, where the caller can read
  the subject's approval, a `fallback_link`. The client keeps the first one the
  session may open (`resolveLink` uses the nav's own `canAccessPath`), so
  finance (no `/expenses` page) lands on the approval, a manager opening a
  teammate's report lands on the read-only report view, and an item with no
  reachable page is shown but not clickable (e.g. policy rules for employees and
  managers — the policy page is `policy.manage`-gated). A teammate's trip links
  to its approval (My Trips lists only the caller's own trips).
* Receipts have no stored filename (the storage key is `<org>/<uuid>`), so
  receipts are matched on the OCR-extracted vendor only.

### Users admin

* Gate: `users.read` permission AND the `finance_admin` role (both must hold). No
  new permission key: a key would need an RBAC data migration on every live
  database before the endpoints worked there, and other tracks edit the same
  catalog.
* Safeguards (each has a test that fails when the guard is removed):
  last active finance admin can never be deactivated or demoted (checked under a
  `FOR UPDATE` lock on the org's active finance admins, and BEFORE the self rule);
  no self-deactivate / self-demote; no self-manager; no manager cycles (walks the
  chain); manager must be an active manager / finance admin of the same org;
  users with active direct reports cannot be deactivated or made employees
  (their reports' approvals would route to someone who cannot approve);
  email unique across ALL organizations, case-insensitive (stored lower-cased);
  org-scoped (another org's user is a 404); unknown body fields rejected.
* Audit rows: `user.created` (identity snapshot), `user.updated` (changed fields
  only, before/after), `user.deactivated`, `user.reactivated`. The password and
  hash never appear in audit, logs or responses (tested, incl. caplog).
* Password rules (none existed): 12..72 bytes (bcrypt limit), at least three of
  lowercase / uppercase / digit / symbol, must not contain the email local part.
  Hashed with the existing bcrypt `hash_password`.
* **Token revocation**: `get_current_user` and `/auth/refresh` already reload the
  user and reject a non-active status on every request, so deactivation kills
  existing access and refresh tokens immediately — no gap to fix; now covered by
  a test that fails if the status check is removed.
* UI: refetch-only (no optimistic updates); the server's error-envelope message
  is shown inline in the dialog; the admin's own row cannot be deactivated.

### Email is globally unique (review fix)

Adversarial review found that `POST /users` only checked email uniqueness inside
the admin's org, while `login()` resolves a user by email across ALL orgs with an
unordered `.first()`. A finance admin in org A could create org B's email; which
row login then picked depended on physical row order, so org B's user could be
locked out (or the email+attacker-password could open a session). Before this
track no endpoint created users, so the duplicate could not arise in practice.

Decision: make an email map to exactly one account globally (option (a) of the
review) rather than an org-qualified login, which would change the login API,
the frontend and every demo/E2E flow.

* `_ensure_email_free` compares `lower(email)` across every organization and
  answers 409 `EMAIL_TAKEN` with the same generic message as the same-org case —
  nothing about the other tenant (org, name) is returned.
* Migration 0020 adds `uq_users_email_lower` (unique on `lower(email)`; also
  declared on the model, so the drift check stays empty). It closes the race
  between the pre-check and the insert: `create_user` inserts inside a SAVEPOINT
  and maps a violation of exactly that index to the same 409 `EMAIL_TAKEN`; any
  other integrity error propagates unchanged.
* Populated-DB safety: before creating the index the migration counts
  case-insensitive duplicate emails and, if there are any, FAILS CLOSED with an
  operator message (count only, no email — no PII in logs) instead of guessing
  which account to keep. No row is modified. Staging only holds the seeded
  `@atlas-demo.internal` users (one org), so no duplicates are expected there.
  Downgrade drops the index symmetrically.
* Tests (each fails when its guard is removed — proven by mutation):
  `test_email_taken_in_another_org_is_refused_without_leaking_the_tenant`
  (replaces `test_same_email_in_another_org_is_allowed`, which asserted 201 —
  the intended behaviour changed), `test_other_org_user_can_still_log_in_after_a_takeover_attempt`
  (incl. the row-update that used to flip the lookup), `test_database_enforces_one_account_per_email_across_orgs`,
  `test_create_maps_a_racing_duplicate_to_email_taken`, and
  `backend/tests/test_pg_email_unique_migration_db.py` (migration refuses
  duplicates without the email in its output, succeeds once resolved, downgrades).

### Login matches email case-insensitively; validation logs carry no values (review fix 2)

Second review round found two defects in the add-user flow:

* **Mixed-case emails could not sign in.** `POST /users` stores the email
  lower-cased but `login()` compared it exactly, so `Jane.Doe@Example.test`
  (as the admin typed it, and as the success toast invites) got 401. `login()`
  now matches `lower(email) == email.strip().lower()` with `.one_or_none()`;
  `uq_users_email_lower` guarantees at most one row and serves the lookup
  (and the migration refuses to create it while case-duplicates exist, so the
  lookup can never be ambiguous). Legacy mixed-case stored emails still resolve
  in any casing. Error behaviour is unchanged (same 401 for unknown email, wrong
  password or inactive user).
* **The initial password leaked into the WARNING log on a 422.** The global
  `validation_exception_handler` logged `exc.errors()`, whose `input` is the
  whole request body for a missing field (and the raw value for a too-long
  password). It now logs only `type` / `loc` / `msg` (drops `input` and `ctx`),
  which also covers the pre-existing `/auth/login` body. The client response was
  already the generic "Request validation failed." envelope.
* Tests (red before the fix, green after):
  `test_admin_created_mixed_case_user_logs_in_with_any_casing` (typed casing,
  lower-case, upper-case with whitespace), `test_legacy_mixed_case_stored_email_still_logs_in`,
  `test_case_insensitive_login_still_rejects_wrong_password`, and
  `test_validation_error_never_logs_or_echoes_the_password` (POST /users with
  `role` missing, an unknown field, a >1024-char password, a wrong type; POST
  /auth/login with email missing / wrong type — asserts the password is in
  neither `caplog.text` nor the response, and that the handler did log).

### Gap 7

`OCR_PROVIDER=anthropic` is rejected by a `mode="before"` validator with
"OCR_PROVIDER=anthropic is no longer supported (the adapter was never
implemented); use 'local' or 'openrouter'" — startup fails fast instead of every
receipt upload failing. `.env.example` and `docs/env_audit_p8_t3.md` updated;
`render.yaml` already used `openrouter`. `LLM_PROVIDER=anthropic` is untouched.

## Limits / known gaps

* No forced password change on first login: the codebase has no change-password
  flow; the admin shares the initial password out of band.
* Deactivating a finance admin does not re-route pending approvals already
  routed to them (finance's queue is org-wide, so they stay decidable by others).
* Notifications are pull-based (60 s poll + focus); no push / websocket.
* Search uses substring ILIKE (no ranking, no fuzzy match).
* `test_contract_matrix` pins the live-route count (now 50); other tracks adding
  routes will need to re-pin it at integration.

## How to verify

```bash
# backend (isolated DB runner)
run_shell.sh -q backend/tests/test_pg_users_admin_db.py backend/tests/test_pg_notifications_db.py \
  backend/tests/test_pg_search_db.py backend/tests/test_pg_text_search.py services/tests/test_ocr_provider.py
run_shell.sh -q backend/tests/api backend/tests/test_migration_chain.py \
  backend/tests/test_pg_email_unique_migration_db.py
# frontend
cd frontend && npx vitest run __tests__/shell __tests__/c7/route-gate.test.tsx
```

Manual: sign in as manager@ → bell shows a count only when an approval is routed
to you; open it, click an item → lands on `/approvals?approval=…`, the badge drops.
Type `/` then "lis" → grouped results; Enter on a teammate's report opens it with
the read-only banner. As finance@ → `/users`: add a user (weak password → inline
reason), try to deactivate yourself (disabled) or demote the last finance admin
(409 inline), deactivate a user → their open session gets 401 on the next call.
