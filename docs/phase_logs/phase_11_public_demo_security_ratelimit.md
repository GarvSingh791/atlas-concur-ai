# Phase 11 — Public-demo security: rate limiting

Branch `ps/ratelimit` (base `product-gaps` @ 749469f). No migration (the limiter
is in-process; no table was needed).

## What

A small, dependency-free request limiter for the public demo, plus a hard bound
on the bcrypt work one login attempt can cause.

| Piece | Where |
|---|---|
| Rate specs (`"60/hour"`) + parser, validated at startup | `config/settings/rates.py`, `config/settings/base.py` |
| Store, policies, middleware, login guard | `backend/app/core/rate_limit.py` |
| `retry_after_seconds` + protocol headers in the uniform envelope | `backend/app/core/errors.py` (`AppError.headers` / `.extra`, `ErrorCode.RATE_LIMITED`) |
| Wiring (limiter on `app.state`, innermost middleware, CORS exposes the headers) | `backend/app/main_entry/app.py` |
| Per-(IP, email) window + failed-login backoff around login | `backend/app/modules/identity/router.py` (`guard_login`) |
| No-org login candidate cap (K = 5) | `backend/app/modules/identity/service.py` |
| `GET /api/v1/ops/rate-limits` (finance_admin, read-only) | `backend/app/api/ops.py` |
| Frontend 429 handling | `frontend/lib/api/rate-limit.ts`, `lib/api/client.ts`, `lib/api/stream.ts`, `components/ui/rate-limit-toaster.tsx`, login page |
| Config docs | `.env.example`, `render.yaml` (`TRUSTED_PROXY_COUNT=1`), `docs/staging_env_map.md`, CI E2E env |

### Policies (code defaults; every one is an env var)

Every `/api/v1` request is classified into exactly ONE policy (first match):

| Policy | Routes | Counters (identity) | Default |
|---|---|---|---|
| `auth_login` | `POST /auth/login` | per IP (middleware) | `RATE_LIMIT_LOGIN_PER_IP=30/minute` |
| ... in the route | | per (IP, email hash) | `RATE_LIMIT_LOGIN_PER_IP_EMAIL=10/minute` |
| ... in the route | | failed-login backoff per email hash | after `LOGIN_BACKOFF_THRESHOLD=5` failures in `LOGIN_BACKOFF_WINDOW_SECONDS=900`: wait `30 s × 2^(n-5)`, cap `900 s`; success resets |
| `auth_refresh` | `POST /auth/refresh` | per IP | `120/minute` |
| `user_admin` | `POST /users`, `PATCH /users/{id}`, `POST /users/{id}/deactivate|reactivate` | per admin (else IP) | `30/minute` + `300/day` |
| `ai` | `POST /conversations/{id}/messages` (assistant + planner + policy chat), `POST /policy/query`, `POST /policy/evaluate`, `POST /policies/{id}/rules` (rule authoring) | per user burst + per user daily + per ORG daily | `60/hour` + `300/day` + `2000/day` |
| `receipt_upload` | `POST /receipts` (OCR + gap-fill LLM + storage) | per user | `10/minute` + `100/day` |
| `default` | every other `/api/v1` request | per user (valid access token) else per IP | `300/minute` |

Exempt: `/health`, `/docs`, every non-`/api/v1` path (the Next.js frontend is a
different host anyway) and CORS preflights (`OPTIONS`, answered by the CORS
middleware before the limiter).

### Behaviour

* Over the limit: `429`, uniform envelope
  `{"error": {"code": "RATE_LIMITED", "message": "Too many requests. Please wait N seconds and try again.", "correlation_id": ..., "retry_after_seconds": N}}`,
  `Retry-After: N`, `X-RateLimit-Limit/Remaining/Reset`.
* Allowed: `X-RateLimit-Limit`, `-Remaining`, `-Reset` (seconds to the window
  boundary) for the most constrained counter of the matched policy.
* Every throttle: one WARNING line
  `rate limit exceeded | policy=… rule=… identity=<kind>:<hmac> retry_after=…`
  and +1 in the per-policy counter shown by `GET /ops/rate-limits`.
* Frontend: the API client turns a 429 into an `ApiError` with
  `retryAfterSeconds` and a friendly message and broadcasts it; the root
  layout's `RateLimitToaster` shows a live countdown toast on ANY screen
  (assistant panel, planner, receipt upload, users admin, ...); the login page
  shows an inline countdown and keeps "Sign in" disabled until it ends. The SSE
  transport handles 429 the same way. Nothing retries a 429: TanStack Query
  only retries network errors / 502-504 and mutations never retry.

## Why / key decisions

* **In-process, no dependency.** Render runs ONE uvicorn worker on ONE free
  instance (`--workers 1`, `plan: free`), so per-process counters are the
  global counters. The store sits behind the 5-method `RateLimitStore`
  protocol (`consume`, `backoff_remaining`, `record_failure`, `clear_failures`,
  `size`) so a Redis / Postgres store can replace it without touching policies.
* **Sliding-window counter** (current + previous bucket, weighted) for minute /
  hour windows: O(1) memory per key, no burst at window edges. **Fixed UTC day**
  for `/day` quotas so "daily" means a calendar day and resets at 00:00 UTC.
* **All-or-nothing consumption**: a request denied by one counter (e.g. the
  hourly burst) does not spend the others (e.g. the daily quota).
* **Bounded memory**: `RATE_LIMIT_MAX_KEYS` (50 000) entries across counters and
  backoff rows; expired entries are swept every 1 024 operations and whenever
  the cap is hit, then least-recently-used entries are dropped. Losing an LRU
  entry under a key-spraying flood can only under-count, never block anyone.
* **No raw identities kept or logged**: every key is
  `HMAC-SHA256(per-process random secret, kind:value)` truncated to 96 bits.
  `/ops/rate-limits` returns limits and counts only.
* **Client IP from the trusted chain only**: `TRUSTED_PROXY_COUNT=N` reads the
  Nth `X-Forwarded-For` entry from the RIGHT (the one the outermost trusted
  proxy appended); anything further left is client-written and never read; a
  short or malformed header falls back to the socket peer. Default 0 (ignore
  the header); `render.yaml` sets 1. uvicorn's own proxy-header rewriting is not
  relied on (it only trusts `127.0.0.1` by default, so the socket peer is
  Render's proxy).
* **User identity = a verified access token's `sub`** (signature + expiry +
  type checked with the app's own `decode_token`, no DB read). An invalid
  token counts as anonymous, so forged tokens cannot mint fresh budgets. Auth
  endpoints are always keyed by IP, so a token cannot buy more login attempts.
* **Login: no account-existence oracle.** The backoff and the (IP, email)
  window are keyed by the email hash and checked BEFORE the database / bcrypt;
  failures are recorded for unknown and existing emails alike; the 429 body is
  the same. Proven by `test_throttled_login_is_identical_for_existing_and_unknown_accounts`.
* **Bounded bcrypt work for no-org login.** `NO_ORG_LOGIN_CANDIDATES` 3 → 5 and
  is now a cap: a no-code attempt ALWAYS runs exactly 5 verifies (real hashes
  padded with the dummy hash); when MORE than 5 active accounts share the email
  it fails closed with the same generic 401 after 5 dummy verifies (no real
  hash is touched) and those users must give their organisation code. With
  ≤ 5 accounts every one is still a candidate, so the integration fix holds: a
  victim whose password matches exactly one account still logs in despite
  attacker-created duplicates (up to the cap). Combined with the login windows
  this bounds bcrypt work to 5 × (30/min per IP).
* **Tests run with the limiter OFF** via the environment
  (`RATE_LIMIT_ENABLED=false` in the repo `conftest.py` and the CI E2E job);
  the code default stays ON. The rate-limit tests install their own enabled
  limiter with tiny limits and a fake clock on `app.state`.
* **One policy per request.** The stricter route policies replace the safety
  net rather than stacking with it (simpler accounting; each is stricter).
* **Fail fast on bad config**: an unparseable rate string stops startup — a
  security limit silently falling back to "unlimited" would be worse.

## Limits (honest)

* **Single instance only.** Two workers or two instances would each allow the
  full limit (N× the budget). Before scaling out, implement `RateLimitStore` on
  Redis / Postgres. Counters also reset on every deploy / restart (Render free
  instances spin down when idle) — acceptable for a demo, not for billing.
* **`TRUSTED_PROXY_COUNT=1` is an assumption about Render's edge** (Render's
  proxy appends the caller to `X-Forwarded-For`). It was not verified against
  live staging from this branch (no network access by rule). Verify once after
  deploy: trip the per-IP login limit from one network, then confirm a second
  network can still log in, and that adding a fake left-hand
  `X-Forwarded-For` value from the first network does not reset its budget. If Render ever puts another appending proxy in
  front, raise it to 2; too high lets clients choose their identity.
* **Shared IPs** (office NAT, mobile carriers) share the per-IP login /
  anonymous budgets. Authenticated traffic is per user, so this mainly affects
  login (30/min per IP).
* **Backoff can be triggered by anyone who knows an email** (5 wrong passwords
  → that email waits 30 s, doubling to 15 min). Superseded: the integration
  review re-keyed the hard block per (address, email), and the rate-limit
  hardening follow-up (`phase_11_rate_limit_hardening.md`) dropped the org-code
  value from that key and added a per-email distributed guard that steps up
  FAILING addresses only — nobody can lock a victim's correct password out.
* **More than 5 same-email accounts need the organisation code.** A determined
  attacker with admin rights in 5 organisations can force a victim's no-code
  login to fail (they still log in with their organisation code). Accepted per
  the requirement; documented in the login service docstring.
* **Approval / submit / other workflow writes** that run the orchestrator
  (`POST /approvals`, report submit) are under the 300/min safety net only:
  they are state-machine gated (one decision per report) and the approval
  agent does not call the LLM.
* The countdown toast replaces any toast already showing (the app's toaster
  shows one toast at a time).

## How to verify

```bash
R=/path/to/run_ratelimit.sh   # isolated DB, providers local
$R -q backend/tests/test_ps_rate_limit.py            # hermetic: core, identity, middleware (56)
$R -q backend/tests/test_ps_rate_limit_api_db.py     # real app end to end, every policy (13)
$R -q backend/tests/test_kl_org_scoped_login_db.py   # login cap, timing parity, victim path (32)
cd frontend && npx vitest run __tests__/rate-limit   # 429 client / stream / retry / login / toast (9)
```

Mutation proofs (each guard reverted → its test goes red, then restored):
leftmost `X-Forwarded-For` instead of the trusted entry; non-atomic
consumption; no capacity bound; no expired-first sweep; fixed day treated as
sliding; `/health` not exempt; raw IP used as the key (log test); bearer user
ignored; org quota dropped; code default `RATE_LIMIT_ENABLED=False`; login
failures not recorded; success not resetting; per-(IP, email) window skipped;
backoff check skipped; login candidate cap removed; ops role check removed;
auth policy keyed by user; `retry_after_seconds` dropped from the envelope;
frontend: 429 branch removed from the client / SSE transport, 429 made
retryable, login button not disabled, cooldown submit not blocked, toast not
closed.

Manual (local): `RATE_LIMIT_LOGIN_PER_IP=2/minute`, start the API, POST
`/api/v1/auth/login` three times → third is 429 with `Retry-After`; as a
finance admin `GET /api/v1/ops/rate-limits` shows `throttled.auth_login: 1`.
