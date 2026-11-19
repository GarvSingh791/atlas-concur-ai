# Phase 12 follow-ups — account recovery (password reset, invitations, email verification)

Branch `fu/accounts` (off `main@962e71a`). Migration `0024_auth_tokens`.

## What

Before this change there was no way to recover a forgotten password, finance
admins could only create users with a password they chose and passed on
themselves (`POST /users`), nothing checked email addresses, and the app had no
email code at all. This change adds:

| Area | What was built |
|---|---|
| Email provider | `services/email_provider/`: interface, `console` (default), `smtp` (stdlib `smtplib` + `ssl`, STARTTLS or implicit TLS, timeouts, plain-text + HTML), `off`, factory. Settings `EMAIL_PROVIDER`, `EMAIL_FROM`, `SMTP_HOST/PORT/USERNAME/PASSWORD/USE_TLS/TIMEOUT_SECONDS`, `APP_BASE_URL` |
| Schema (0024) | New table `auth_tokens` (one-time tokens, SHA-256 only) + `users.email_verified_at`, `users.password_changed_at` (nullable) |
| Password reset | `POST /auth/forgot-password` (always 202, same body) → email → `POST /auth/reset-password` (204) |
| Password change | `POST /auth/change-password` (signed in; returns a fresh token pair) |
| Session revocation | Every access / refresh token carries `pwv`, a keyed fingerprint of `password_changed_at`; tokens minted before a reset / change / acceptance are refused |
| Invitations | `POST /users/invite` (finance admin), `POST /users/{id}/invite/resend`, `POST /users/{id}/invite/revoke`, `GET /auth/invite/{token}` (peek), `POST /auth/accept-invite` |
| Verification | `POST /auth/send-verification` (signed in), `POST /auth/verify-email`; acceptance and reset also count as verified |
| Throttling | Six new limiter rules (below), all checked before any database read |
| Frontend | Login "Forgot password?" link and notices; `/forgot-password`, `/reset-password`, `/accept-invite`, `/verify-email`; Users admin "Invite user" dialog with a one-time link box, resend / revoke actions, a Verification column; a dismissible "verify your email" banner |

Main files: `backend/app/modules/identity/accounts.py` (all the logic),
`router.py` (endpoints), `schemas.py`, `admin.py` (`build_new_user` is now
shared by `POST /users` and `POST /users/invite`, plus the reactivation guard),
`backend/app/core/security.py` (`pwv`), `backend/app/core/rate_limit.py`,
`backend/app/core/errors.py` (`loggable_path`), `database/models/auth_token.py`,
`database/migrations/versions/0024_auth_tokens.py`.

## Why

The PRD's identity section assumed accounts could be recovered and invited
without an admin knowing anyone's password. Admin-chosen passwords are shared
secrets (an admin can sign in as the user until they change it, and nobody
could change it). Invitations let the user pick their own password. Reset
covers forgotten passwords. Verification shows which addresses really reach
their owner.

## Key decisions

1. **Invited users are real `users` rows**, `status = inactive`, with an
   unusable `password_hash` (`!unusable:` + random hex, never a valid bcrypt
   hash, so `verify_password` is always False). No PG enum was altered and no
   nullable `user_id` was needed. "Awaiting activation" is
   `inactive AND unusable password`. `invite_pending` means an open invitation
   row also exists. Reactivating such an account is refused with
   409 `INVITE_PENDING` (it would create a login with no password); the UI
   offers resend instead.
2. **Tokens.** `secrets.token_urlsafe(32)` (43 chars). Only the SHA-256 hex is
   stored (unique index, CHECK `^[0-9a-f]{64}$`). A lookup is by digest, then a
   `hmac.compare_digest`, then purpose, `used_at IS NULL`, `expires_at > now`,
   and the account must still belong to the token's `organization_id`. Every
   failure gets the same answer: 400 `INVALID_OR_EXPIRED_TOKEN` (the peek
   answers a generic 404). `purpose` is TEXT + CHECK, not an enum. Issuing a
   token voids that user's earlier unused tokens of the same purpose. Using one
   marks it used. Rows are never deleted; they are the record.
   TTLs: reset 30 min, invite 7 days, verify 24 h (settings).
3. **No account-existence oracle on forgot-password.** It always answers 202
   with the same body. The database and hashing work has one fixed shape: one
   candidate SELECT (the email, active, joined to the organisation), K token
   generations (K = 1 with an organisation code, else
   `NO_ORG_LOGIN_CANDIDATES` = 5, as at login), one UPDATE that voids earlier
   tokens, and one `INSERT … SELECT … JOIN (VALUES …)`. The VALUES list is
   padded with random ids that match no user. So an unknown, inactive,
   invited or demo-protected account runs exactly the same statements as a
   real one, and a test counts them. The email is sent by a background task
   after the response. Nothing is audited for a reset request, because an audit
   insert only for real accounts would bring the timing signal back. The
   `auth_tokens` row (time and requester hash) is the record. Without an
   organisation code, each of the K oldest active accounts with that email
   gets its own link, bound to its own organisation (the mailbox owner owns
   them all). With a code, only that organisation's account gets one.
4. **Session revocation by fingerprint, not by `iat`.** JWT `iat` has
   one-second resolution, so "issued before `password_changed_at`" cannot
   tell a token minted in the same second before the change from the
   re-issued one after it. Instead tokens carry
   `pwv = HMAC(JWT_SECRET, user_id:microsecond-epoch)[:32]`, and
   `get_current_user` and `/auth/refresh` compare it with the current value.
   It fails closed: a token without the claim is valid only while
   `password_changed_at IS NULL`, so every token in circulation today stays
   valid until that user's first password change. The fingerprint is
   timezone-independent and keyed, so it does not show when the password
   changed.
5. **Email after the commit.** Services commit and return an `EmailMessage`.
   The router schedules it with `BackgroundTasks`, which runs after the
   response is sent. A refused or rolled-back request never sends anything,
   and SMTP latency never shows in the response time. Delivery failures are
   logged (exception class and masked recipient only) and never reach the
   caller.
6. **Invite links without email.** With `EMAIL_PROVIDER=console|off` (the
   default; staging) nothing is delivered. `POST /users/invite` and `/resend`
   then return `invite_url` once, to the inviting admin only. It is never
   stored, logged or audited, and the response is `Cache-Control: no-store`.
   With `smtp` the field is `null`.
7. **Links only from `APP_BASE_URL`**, validated at startup (absolute
   http(s), no credentials / query / fragment). The app never reads the Host
   or X-Forwarded-Host header to build a link. **Staging and production both
   refuse a non-https value** (review fix: a separate `Settings` validator, not
   one of staging's warn-only secret findings, so staging can never mail
   http/localhost token links). `render.yaml` sets
   `APP_BASE_URL=https://atlas-concur-ai.vercel.app`; the Render blueprint must
   be synced before (or with) the deploy that ships this branch, or staging
   will fail its boot on the `http://localhost:3000` default.
8. **Tokens never in a query string on a mutating endpoint.** Reset, accept and
   verify read the token from the JSON body only. The one read that takes it in
   the path is the invite peek (`GET /auth/invite/{token}`, as specified). Every
   logged path goes through `loggable_path()` (error handlers and the body-limit
   middleware), which writes `/auth/invite/[redacted]`. The frontend pages
   send `Referrer-Policy: no-referrer` (auth layout metadata).
9. **DEMO_MODE protected accounts** (`backend/app/core/demo.py` provenance:
   seed email + demo org name). They never get a reset email (same 202).
   A reset token for one is refused. Change-password and send-verification
   answer 403 `DEMO_ACCOUNT_PROTECTED` (audited, through
   `guard_protected_account`). `GET /me` now reports `demo_protected`, so the
   banner hides itself.
10. **Invitations reuse every `POST /users` validation** (`build_new_user`):
    email shape, per-org uniqueness with the same audited 409 `EMAIL_TAKEN`
    (an address taken in another organisation is fine; login stays org-aware),
    role, manager (active manager / finance admin of the org), grade on the
    ladder. Resend / revoke work only on an account awaiting activation (else
    409 `NOT_INVITED`), are org-scoped (404 elsewhere) and are refused for
    protected accounts.
11. **Verification needs a click.** The `/verify-email` page posts only when
    the user presses the button, so a mail scanner that opens the link first
    cannot use up the token. Unverified users keep full access; the banner
    only reminds them.
12. **Rate limits**, all through the existing limiter, before any database read:

    | Rule | Default | Keyed by |
    |---|---|---|
    | `RATE_LIMIT_FORGOT_PER_IP_EMAIL` | 5/hour | IP + email hash (in the route, like the login guard) |
    | `RATE_LIMIT_FORGOT_PER_IP` | 20/hour | IP (middleware) |
    | `RATE_LIMIT_ACCOUNT_LINK_PER_IP` | 10/minute | IP: reset, accept, verify, change-password share it; a bearer never buys a per-user budget |
    | `RATE_LIMIT_INVITE_PEEK_PER_IP` | 30/minute | IP |
    | `RATE_LIMIT_INVITE_PER_USER` | 30/hour | admin (plus the user-admin minute / day budgets) |
    | `RATE_LIMIT_SEND_VERIFICATION_PER_USER` | 5/hour | user |

    The setting names avoid `TOKEN` / `PASSWORD` on purpose: the phase-11 env
    audit treats such names as secrets (`scripts/ops/env_audit.py`).

## Limits / residual risks

- **Console mode delivers nothing.** Staging runs `console`, so reset and
  verification emails go nowhere. Self-service password reset does not work
  on staging until SMTP is configured. Invitations still work, because the
  admin hands the link over.
- **Timing parity is statement-level, not wall-clock.** A real account's
  INSERT writes a row that a padded slot does not, and the per-statement
  latency is not hidden. The response no longer depends on email delivery,
  and the statement count and token work are identical, but a precise
  network-side timing attacker could still measure the insert's cost. The
  rate limits bound how many probes they can make.
- **No audit row for a reset request** (decision 3). The token row is the record.
- **Peek path in proxy logs.** `GET /auth/invite/{token}` is redacted in app
  logs, but Render's edge request log records paths. The token is single-use
  and its holder is the invitee. Moving the peek to a POST body would close
  this.
- **Old tokens stay valid until the first change.** Tokens minted before this
  release have no `pwv`; they keep working until the user's first password
  change (fail-closed after that), and at most 7 days (refresh lifetime).
- **Email address changes.** Email is not editable, so a verification is
  tied to the address it was sent to. If editing is added later, it must
  clear `email_verified_at` and void open verify tokens.
- **Change-password UI** is not built (the endpoint and tests exist). The
  spec's frontend list did not include it.
- **Per-process limiter** (phase-11 limitation): the new rules are per
  instance like the others.

## How to verify

```
# backend (isolated DB runner)
run_accounts.sh -q backend/tests/test_fu_accounts_db.py backend/tests/test_fu_accounts_ratelimit_db.py
run_accounts.sh -q backend/tests/test_fu_accounts_unit.py services/tests/test_email_provider.py
run_accounts.sh -q backend/tests/test_migration_0024_auth_tokens.py backend/tests/test_migration_chain.py
run_accounts.sh -q backend/tests/api/test_contract_matrix.py      # 78 routes
# frontend
cd frontend && npx vitest run __tests__/accounts
```

Mutation proofs: each guard was reverted on its own and its test went red, then
the guard was restored and the test went green: 27 backend guards and 8
frontend guards.

Manual, local (`EMAIL_PROVIDER=console`): sign in as a finance admin → Users →
Invite user → copy the link → open it in a private window → choose a password →
sign in. Forgot password: the page always shows the same confirmation, and the
backend log shows `email not delivered: provider=console purpose=reset
to=e***@a***.internal`.

## Operator notes

**Configure SMTP on Render.** In the dashboard, set `EMAIL_PROVIDER=smtp`,
`EMAIL_FROM="Atlas <no-reply@your-domain>"`, `SMTP_HOST`, `SMTP_PORT` (587
STARTTLS or 465 TLS), `SMTP_USERNAME`, and `SMTP_PASSWORD` (a secret: dashboard
only, never in `render.yaml`). Keep `SMTP_USE_TLS=true`. The sending domain
needs SPF / DKIM for the provider (SES, Postmark, Mailgun, and others) or
messages will land in spam. If `smtp` is set without a host or sender, the app
refuses to start. A delivery failure logs
`email delivery failed: provider=smtp purpose=… to=a***@d***.com error=<Class>`,
and the user still sees the normal answer.

**Console mode delivers nothing.** It is the default, safe for demos. Each
message logs one masked line. Self-service reset and verification do not work;
invitations do, because the admin copies the one-time link from the dialog.

**How invites work without email.** The admin invites → the dialog shows the
link once with a warning → the admin sends it over a trusted channel → the
invitee opens `/accept-invite?token=…`, sees the organisation and the masked
email, chooses a password → the account is active and verified. A lost link
is fixed with Resend (older links stop working). Revoke voids all of them and
keeps the account inactive.

**`APP_BASE_URL`** must be the frontend origin (`render.yaml` sets the Vercel
URL). Change it when the frontend domain changes, or the links will point to
the old domain.

**Threat model.** Covered: enumeration via forgot-password, peek, accept and
throttled answers (identical status, body and work); token theft from the
database (hashes only), logs (never written; paths redacted), audit rows and
error bodies (never included); token reuse, expiry and purpose / org
confusion; sessions surviving a password reset; link poisoning through the
Host header; mail scanners using up verification links; demo accounts
hijacked by a reset. Residual: a mailbox compromise compromises the account
(inherent to email recovery); network-level timing (see Limits); proxy logs of
the peek path; multi-instance rate limits.


## Review fixes (adversarial review round 1)

1. **Login was an invitation oracle (fixed).** A pending or revoked invitee's
   row carries the non-bcrypt `!unusable:<hex>` marker. On the org-code login
   path the row is found regardless of status, and bcrypt rejected the marker
   in microseconds, so its 401 came back ~250 ms faster than for an unknown
   email or a real account. Now `service.login` verifies `DUMMY_HASH` whenever
   the stored hash is unusable and fails regardless of the result, and
   `security.verify_password` itself does one full-cost check against
   `DUMMY_HASH` whenever bcrypt raises on a stored value (defence in depth for
   every caller). Tests: `test_org_code_login_does_full_bcrypt_work_for_an_invited_placeholder`
   (every org-code 401 — invitee, unknown, real — verifies exactly one `$2b$`
   hash; the dummy's own password never signs a placeholder in, even an active
   one) and `test_verify_password_does_real_bcrypt_work_for_a_non_bcrypt_hash`.
   Mutation-proved: dropping the substitution, the `and usable` check or the
   fallback bcrypt call each turns a test red.
2. **APP_BASE_URL https on staging (fixed).** The check moved out of
   `deployment_secret_problems` (warn-only on staging) into its own validator
   `app_base_url_is_https_when_deployed`, which raises for
   `APP_ENV in (staging, production)`. Development keeps `http://localhost:3000`.
   Test: `test_emailed_link_origin_must_be_https_on_staging_and_production`
   (mutation: restricting it to production turns the staging cases red).

## Review fixes (adversarial review round 2)

Two guards had no test, so deleting either one left every test green. The code
was already correct; only the coverage was missing.

3. **A password change voids outstanding reset links (now tested).**
   `change_password` marks every unused `password_reset` token used. Without
   that, a reset link sent before a deliberate change (a leaked link, or one
   read from a shared mailbox) would still work until it expired and could
   take the account back. Test:
   `test_change_password_voids_an_outstanding_reset_link`. It requests a reset,
   changes the password, then tries the captured link. The test expects a
   400 `INVALID_OR_EXPIRED_TOKEN`, `used_at` set on the row, and the changed
   password still working.
   Mutation: removing the `_void_tokens(... password_reset ...)` call in
   `change_password` turns it red.
4. **Verification refuses a deactivated account (now tested).**
   Deactivation does not void auth tokens, so the
   `user.status != active` check in `verify_email` is the only thing that
   stops a link issued while the account was active. Test:
   `test_a_verification_link_dies_with_the_account`. The employee requests
   verification and a finance admin deactivates the account through the API.
   Redeeming the link must return the uniform 400, leave `email_verified_at`
   NULL, write no `user.email_verified` audit row and leave the token unused.
   Mutation: removing the status check turns it red.

## Review fixes round 3

Two guards were untested; the code was already correct, so this round adds
tests and corrects stale docs only.

5. **Accept-invite refuses an account that is no longer a placeholder, or is
   protected.** The guard is
   `if not _is_placeholder(user) or is_protected_account(session, user)`.
   - `test_an_account_activated_outside_the_accept_flow_cannot_be_taken_over_by_its_invite`
     activates an invited user in the database (active, real password hash),
     as a seed or demo-reset restore would. Accept must return the generic 400
     `INVALID_OR_EXPIRED_TOKEN`, leave the hash unchanged and the token unused.
     It also asserts the invite peek returns the generic 404 with
     `INVITE_LINK_MESSAGE`.
   - `test_a_protected_placeholder_cannot_be_activated_by_an_invite` uses
     DEMO_MODE, a placeholder with a protected seed email in the demo
     organisation and an invite token. Accept returns 400 and the account stays
     inactive.
   Mutations (each reverted afterwards, `accounts.py` byte-identical):
   removing the `not _is_placeholder` half turns the first test red; removing
   the `is_protected_account` half turns the second red; removing the
   `_is_placeholder` check in `peek_invite` turns the first test red.
6. **Stale docs.** `.env.example` and `docs/staging_env_map.md` said staging
   only warns about a non-https `APP_BASE_URL`. Since commit 0f89e52 https is
   required on staging and production.
