# Phase 10 — Known limits: organisation-aware login (identity)

Branch `kl/identity`, base `product-gaps` at `b044343`. Migration
`0022_org_scoped_email` (`down_revision = 0020_notification_reads`; the integrator
chains it after `0021`).

## The limit being closed

> Cross-organisation email clash: the Add-user check blocks an email that already
> exists in ANY organisation, so an admin could still claim another organisation's
> address first. Fixing it properly needs organisation-aware login.

Product gap 3 made `users.email` unique across all organisations (index
`uq_users_email_lower`, migration 0020) because login resolved a user by email
alone. That left two problems:

* **Squatting.** A finance admin in org A could create `someone@orgb.com` first.
  Org B then got `EMAIL_TAKEN` and the real person could not get an account.
* **Enumeration.** The 409 told any finance admin "this address has an account
  somewhere".

## What was built

### Backend

* `organizations.slug` — the organisation's **login code**: stable,
  human-typable, not secret, lower-case `[a-z0-9]` separated by single hyphens,
  2–63 chars (DB check `ck_organizations_slug_format`), unique
  (`uq_organizations_slug`), NOT NULL.
  * Application-created orgs get `<name stem>-<6 hex>` (model default), so two
    tenants with the same name never collide. Raw SQL inserts get a server
    default `org-<10 hex>`.
  * Rules live in `database/models/identity.py` (`org_slug_base`,
    `new_org_slug`, `normalise_org_slug`, `RESERVED_ORG_SLUGS`, `ORG_SLUG_RE`).
  * The demo org is seeded with `atlas-demo-corp`, the same value 0022 derives
    from its name on an existing database (staging).
* **Email is unique per organisation, case-insensitive.** The unique index is
  `uq_users_org_email_lower (organization_id, lower(email))`. The original
  case-sensitive `uq_users_organization_id_email` stays. The global
  `uq_users_email_lower` is dropped.
* **Login** (`POST /auth/login`) takes `{email, password, organization?}`:
  * organization given → the account is looked up inside that org only
    (case-insensitive code, trimmed);
  * omitted (or blank) → the password is verified against a **fixed number
    K = 3** (`NO_ORG_LOGIN_CANDIDATES`) of the **newest active** accounts with
    that email, padded with the dummy hash, and the login succeeds only when
    **exactly one** of them matches. With one account this is today's behaviour,
    so the demo works unchanged. If none or several match, the login fails
    closed (review fix — see "Review fixes" below);
  * every failure (unknown email or code, wrong org, wrong password, inactive
    account, ambiguous password) gets the **same generic 401**. The number of
    bcrypt verifies depends only on whether a code was sent (K without, 1 with),
    never on how many accounts exist. The ambiguous case logs only a count,
    never the email.
* **Add user** (`_ensure_email_free`) checks the admin's **own organisation
  only**. An address that exists elsewhere is simply created, and nothing about
  the other tenant is read or returned. The same-org clash is still the uniform
  409 `EMAIL_TAKEN`, with the committed `user.create_refused` audit row (masked
  email plus keyed digest). The racing-insert path maps **either** per-org email
  constraint to the same 409.
* `GET /organization` (finance admin only, read-only) returns
  `{id, name, login_code}` for the caller's own organisation. It is in the
  contract registry (now 65 routes).
* Demo seeds (`seed_all.py`, `reindex_policy_corpus.py`) look up demo users
  **inside the demo org**. Before, a same-email account in another org could be
  picked by `.first()` (password resync) or crash `.one()`.

### Frontend

* Login page: a small **"Signing in to a specific organisation?"** toggle reveals
  an optional **Organisation** field (login code, with helper text). The field is
  sent only when it is open and filled. The code is **remembered per device** in
  `localStorage` (`atlas_login_org`, `lib/auth/org-preference.ts`, guarded
  against unavailable storage). It is saved only after a **successful** login and
  forgotten when someone signs in without it. Email + password alone still works.
* Users admin: finance admins see **"Organisation login code: `…`"** under the
  page title (read-only), with a hint about when users need it. It is fetched
  only for finance admins (`useOrganizationLoginCode(isAdmin)`).

## Key decisions

* **The slug is the login identifier**, not the org UUID or a domain. It is
  typable, stable and safe to share. Domain-based routing would need verified
  domains, which we don't have.
* **No-org login is decided by the password, over a constant K candidates.**
  It only looks at **active** accounts, so a deactivated duplicate never counts.
  The answer and the work done never depend on whether the email exists in
  another org (see "Review fixes").
* **Timing class preserved.** Every no-code attempt runs exactly K bcrypt
  verifies, every attempt with a code exactly one. This is tested by counting
  calls on 7 no-code paths (0, 1, 2 and more-than-K accounts, success and
  failure) and 5 code paths.
* **Tokens stay org-bound.** Access and refresh tokens carry `org`.
  `get_current_user` and `/auth/refresh` reload the user by `(id, org)`, so a
  token whose org claim doesn't match the user's org is refused. This was
  already true and is now covered by a test that fails if the org filter is
  dropped from refresh.
* **Login code exposure.** It is not a secret. It is shown only to finance admins
  in the UI and API (deny-by-default). Employees learn it from their admin.
* **Slug NOT NULL with two defaults** (ORM name-based, server random), so the
  ~40 places that build `Organization(...)` (tests, seeds, other branches) keep
  working unchanged and every org always has a code (fail closed: no "org
  without a code").
* **Migration 0022 is populated-DB safe.** The column is added nullable,
  backfilled only for rows still NULL (deterministic: name stem, reserved words
  get `-org`, empty names become `org-unnamed`, collisions get `-2`, `-3`, … in
  `created_at, id` order), then set NOT NULL with constraints. It refuses (count
  only, no email) if same-org case-duplicates exist, which can't happen after
  0020. The per-org index is created before the global one is dropped. No user
  row is touched.
* **The downgrade restores the global index only when that is safe.** If any
  email now exists in two orgs it **fails closed** (count only) rather than
  deleting someone's account. Once resolved, it restores 0020's schema exactly.
  The migration keeps a frozen copy of the slug rules; a test asserts it matches
  the model helper.
* Hot shared files were kept small: `service.py` (login body and one function),
  `admin.py` (one predicate and the constraint set), seeds (scoping only).

## Tests changed on purpose (intent changed)

| Test | Was | Now |
|---|---|---|
| `test_pg_users_admin_db::test_email_taken_in_another_org_is_refused_without_leaking_the_tenant` | foreign-org email → 409 | renamed `…_is_allowed_and_leaks_nothing`: 201, no tenant data, 2 rows |
| `test_pg_users_admin_db::test_other_org_user_can_still_log_in_after_a_takeover_attempt` | takeover refused, victim logs in by email | takeover row exists; victim logs in **with org code**, squatter's password refused, no-org login fails closed |
| `test_pg_users_admin_db::test_database_enforces_one_account_per_email_across_orgs` | cross-org dup → IntegrityError | renamed `…_within_an_org`: cross-org ok, same-org case-dup refused |
| `test_pg_users_admin_db::test_create_maps_a_racing_duplicate_to_email_taken` | raced a foreign email | races an own-org email |
| `test_pg_integ_identity_db::test_login_fails_closed_when_an_email_matches_two_accounts` | dropped the 0020 index to fake legacy data | the duplicate is now legal; same assertions |
| `test_pg_integ_identity_db::test_email_taken_is_identical_for_own_org_and_foreign_org` | own vs foreign 409 identical | renamed: own-org case variants identical, foreign → 201 |
| `test_pg_integ_identity_db` audit/race tests | used a foreign email | use own-org emails, same assertions |
| `test_pg_email_unique_migration_db` | upgraded to `head` | pins `0020_notification_reads` (head no longer has that index) |
| `api/test_contract_matrix` | 64 routes | 65 (+`GET /organization`) |
| `frontend/__tests__/shell/users-screen.test.tsx` | hook mock | mock also provides `useOrganizationLoginCode` |

## New tests

* `backend/tests/test_kl_org_scoped_login_db.py` (28 after the review fixes):
  two orgs with one email (both exist; no-org login → the password picks the one
  matching account, the same password on both → generic 401; with org → each reaches only
  its own org, case/space-insensitive code, token `org` claims checked, `/me`
  identity checked); wrong or unknown org → generic 401; single account still
  logs in without an org; an inactive twin doesn't cause ambiguity; deactivated
  users refused on both paths; **constant verify count (K without a code, 1
  with) on 12 paths**; create-then-login probe is not an oracle; newest-K overflow;
  ambiguous log carries no email; cross-org create → 201 with no leak and no
  refusal audit, and the victim still logs in with their code; same-org
  duplicate → uniform 409; racing duplicate (exact-case and legacy mixed-case
  stored, so each constraint is exercised); forged foreign-org access and refresh
  tokens → 401; refresh keeps the login's org; `GET /organization` (finance only,
  read-only, own org only); seed password resync ignores another org's
  same-email user; slug defaults, uniqueness and format check.
* `backend/tests/test_kl_migration_0022_org_scoped_email_db.py` (3): populated
  scratch DB → deterministic slugs (collisions, reserved, empty, unicode, long,
  one-character stems),
  users untouched, index swap, cross-org dup allowed and same-org refused,
  downgrade refused while a twin exists (no email in output), downgrade succeeds
  once resolved and restores the global index and removes the column,
  re-upgrade yields identical slugs; pure `assign_slugs`
  determinism/idempotency; migration rules equal model rules.
* Frontend: `login-page.test.tsx` (+4: collapsed and not sent by default,
  sent + remembered on success, remembered code pre-fills and hiding it stops
  sending and forgets it, failed login doesn't remember);
  `users-screen.test.tsx` (+2: code shown read-only to finance; never fetched or
  shown for other roles).

### Mutation proofs (revert guard → red → restore)

Ambiguous → first row; org predicate → `true`; active filter removed; dummy
verify removed; org predicate removed from `_ensure_email_free`; per-org lower
index (and, separately, the case-sensitive constraint) removed from the race
mapping; org filter removed from refresh; `GET /organization` gated by
`get_current_user` only; slug input not lower-cased; downgrade guard removed;
collision suffixing removed; reserved-word handling changed in the model; seed
resync org filter removed. Frontend: organisation never sent; always sent (even
empty); not remembered on success; users screen fetching the code for every
role. All went red. One equivalent mutant survived: dropping the `orgOpen`
check when building the request, because hiding the field already clears its
value.

## Limits that remain

* **No email verification.** An admin can still create an account for an
  address they don't own **inside their own organisation**. It can't affect
  another org's users any more.
* **No password reset or invite flow.** Initial passwords are admin-set. A user
  who forgets theirs needs their admin.
* **The login code is not changeable from the UI** (no rename endpoint); an
  operator can update `organizations.slug` (constraints enforce format and
  uniqueness). Renaming it breaks remembered codes on devices; the field just
  shows the old value and the user fixes it.
* **No rate limiting** on `/auth/login` or `POST /users` (unchanged).
* **Login without a code is K = 3 times the bcrypt work** of a login with a
  code (about 3 × the configured work factor). Acceptable for the demo; a
  rate limiter is the right companion (security & operations step).
* **More than K active accounts on one email:** only the K newest are tried
  without a code, so an older account beyond K needs its code. This reveals
  nothing to a prober: their own freshly created account is always the newest
  and always a candidate. It could only "tell" an admin something if at least K
  other organisations created the same address *after* the admin's probe, and
  even then only that the probe now needs its code. Pinned by
  `test_newest_accounts_are_the_candidates_when_more_than_k_share_an_email`.
* **Two accounts with the same email and the same password** fail closed
  without a code (they must use their code). An admin can only observe this
  for a password they already guessed, which is the same as guessing it
  directly at the login form.
* **No rate limiting or alerting** on `/auth/login` (online password guessing
  is the baseline threat, unchanged; a no-code guess is tried against up to K
  same-email accounts). Follow-ups for the public-demo security step: rate
  limit `/auth/login` and `POST /users`, alert on `user.created` followed by a
  no-code login for that address, verified invitations.

## Review fixes

**Create-then-login oracle (blocking, fixed in code).** The first version kept
"exactly one active account must match" for no-code login. An org-A finance
admin could create email E in their own org (201 even when E exists elsewhere)
with a password of their choice, then sign in as E with no code: 401 meant "E is
active in another organisation", 200 meant it wasn't. The same squat also
forced the rightful org-B user to need their code. The earlier wording here
("doesn't reveal that the email exists elsewhere") and the integration log's
"enumeration — CLOSED" were therefore wrong at the time.

Fix (option b of the review): constant-K verification (described above). The
admin's probe now logs in into org A whether or not E exists elsewhere, and the
org-B user still logs in with email + password only, because the password
picks their account.

Tests: `test_admin_probe_account_logs_in_whether_or_not_the_email_exists_elsewhere`
(the reviewer's reproduction: `{taken: 401, fresh: 200}` before, both 200 into
org A after; victim 200 into org B),
`test_without_an_org_the_password_picks_the_one_matching_account`,
`test_without_an_org_a_password_matching_two_accounts_fails_closed`,
`test_verify_count_never_depends_on_how_many_accounts_share_the_email`
(replaces `test_every_failure_path_runs_exactly_one_password_verify`),
`test_newest_accounts_are_the_candidates_when_more_than_k_share_an_email`.
Intent changed on purpose: `test_without_an_org_an_ambiguous_email_fails_closed_for_both`
became the two "password picks / same password fails closed" tests, and
`test_pg_users_admin_db::test_other_org_user_can_still_log_in_after_a_takeover_attempt`
now expects the victim's no-code login to succeed into org B (was 401).
`test_pg_integ_identity_db::test_login_fails_closed_when_an_email_matches_two_accounts`
is unchanged and still passes (both rows share the factory password).

Mutation proofs (each went red, then restored): the old "exactly one active
account" rule; dummy padding removed (two variants); oldest-first instead of
newest-first candidates; several matching hashes accepted; active filter
dropped (caught by `test_inactive_accounts_never_take_a_candidate_slot`, added because the first run showed the later status check masks it).

**Migration aborts on a one-character org name (blocking, fixed in code).**
Two reviewers found that an existing organisation whose name reduces to a
single `[a-z0-9]` character (`"X"`, `"Q"`, `"7"`, `"A!"`, `"é1"`) got a
1-char slug from the 0022 backfill, which the same migration's CHECK
(`char_length(slug) BETWEEN 2 AND 63`) then rejected, aborting the whole
upgrade (`CheckViolation` on `ck_organizations_slug_format`). Application
inserts were never affected (`new_org_slug` always appends `-<6 hex>`).

Fix: both copies of the stem rule (`slug_base` in the migration,
`org_slug_base` in the model) now append `-org` to a stem shorter than 2
characters, e.g. `"X"` → `x-org`, `"é1"` → `1-org`. Deterministic, matches the
format, and can't produce a reserved word. Tests: `("Q", "q-org")`,
`("X", "x-org")`, `("é1", "1-org")`, `("7", "7-org")` added to the populated
upgrade test; the parity test now covers one-character names and asserts the
model stem is valid on its own. Mutation proofs: removing the migration guard
→ the upgrade test hits the reviewers' `CheckViolation` and the parity test
fails (2 red); removing the model guard → the parity test fails (1 red);
restored → 3 passed.

## How to verify

```bash
R=<local-runner-dir>/kl/run_identity.sh
$R -q backend/tests/test_kl_org_scoped_login_db.py backend/tests/test_kl_migration_0022_org_scoped_email_db.py
$R -q backend/tests/test_pg_users_admin_db.py backend/tests/test_pg_integ_identity_db.py backend/tests/test_pg_email_unique_migration_db.py backend/tests/test_migration_chain.py
cd frontend && npx vitest run __tests__/auth/login-page.test.tsx __tests__/shell/users-screen.test.tsx
```

Manual: as `finance@atlas-demo.internal`, open **Users**; the code reads
`atlas-demo-corp`. Sign out, then sign in with email + password only (works).
Sign in again with "Signing in to a specific organisation?" → `atlas-demo-corp`
(works, and is remembered). Use a wrong code: you get the same "Invalid
credentials." as a wrong password.

## Integration update

See `phase_10_known_limits_integration.md`: no-org login now verifies every active same-email account (dummy-padded up to K), so the K-newest overflow limit below no longer applies, and `0022` now chains onto `0021`.
