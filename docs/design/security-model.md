# Security model

How Atlas protects a public demo that anyone can sign in to with a published password: the threat model, every control that answers it, where each control lives in the code, and the risks that remain. It is for security-minded reviewers and engineers. Each control below was checked against the code at the paths given; the policy for reporting a vulnerability is in [SECURITY.md](../../SECURITY.md).

**Contents:** [Threat model](#threat-model) · [Controls at a glance](#controls-at-a-glance) · [Authentication](#authentication) · [RBAC and record scoping](#rbac-and-record-scoping) · [Tenant isolation](#tenant-isolation) · [Org-aware login](#org-aware-login) · [Segregation of duties](#segregation-of-duties) · [Audit](#audit) · [Account recovery tokens](#account-recovery-tokens) · [Rate limiting](#rate-limiting) · [Upload hardening](#upload-hardening) · [AI-specific controls](#ai-specific-controls) · [Demo mode and reset](#demo-mode-and-reset) · [Configuration and startup guard](#configuration-and-startup-guard) · [Secrets and CI](#secrets-and-ci) · [Residual risks](#residual-risks)

---

## Threat model

**The deployment.** A Next.js frontend on Vercel and one FastAPI container on Render (one uvicorn worker), backed by Neon PostgreSQL, Cloudflare R2 and OpenRouter. The demo organisation, "Atlas Demo Corp", has five seeded accounts whose password is published in the README on purpose. All data is synthetic.

**What is worth protecting.**

| Asset | Why it matters |
|---|---|
| Service secrets (`JWT_SECRET`, `DATABASE_URL`, R2 and LLM keys, the Render deploy hook) | Forging tokens, reading the database, or running up provider bills |
| Provider spend | Every LLM call, vision OCR call and stored object costs money; the demo is open to anyone |
| Availability of the shared demo | One visitor must not be able to break it for the next (lock accounts, archive the policy, fill storage) |
| Tenant and record boundaries | The code supports several organisations; the controls must hold even though the demo uses one |
| Integrity of the approval workflow | A user must not approve or reimburse their own claim, or launder a rejected claim by re-filing it |
| Whatever visitors upload | Visitors may upload real receipt photos despite the synthetic-data notice |

**Who the attacker is.** Mostly an anonymous internet user, or a visitor signed in to a shared demo account. The design assumes they know the published password, can read the source code, and can script requests. It does not try to defend against a compromised hosting provider.

| Threat | Main controls |
|---|---|
| Brute force, credential stuffing, account enumeration | Per-IP and per-(IP, email) login limits, progressive backoff, constant-shape login and recovery paths, one generic 401 |
| Privilege escalation, IDOR, cross-tenant reads | Server-side permission lookup, `scope_query` on every read, owner-only writes, 404 for unreadable records |
| Workflow fraud | Segregation-of-duties checks, frozen review snapshots, re-file lineage, grade-gated approvals |
| Malicious uploads (oversized, decompression bombs, polyglots, hostile PDFs) | Body caps, magic-byte sniffing, header-only size checks, metadata stripping, PDF rasterised in a contained child process, quotas |
| Spend abuse | Per-user, per-user-per-day and per-organisation-per-day AI budgets; receipt upload budgets |
| Demo vandalism | `DEMO_MODE` protection of seeded accounts and rules; scheduled demo reset |
| Prompt injection driving actions | Agents can only take enumerated actions bound to permission keys; approvals never use an LLM |
| Secret leakage through the repo or CI | `.gitignore`, a blocking secret scan of tree and full history, CI hygiene tests, production startup guard |

---

## Controls at a glance

| Control | Code | Tests |
|---|---|---|
| JWT authentication, session revocation | `backend/app/core/security.py` | `backend/tests/test_identity_auth.py`, `backend/tests/test_fu_accounts_db.py` |
| RBAC and record scoping | `backend/app/core/rbac.py`, `config/constants/permissions.py` | `backend/tests/test_rbac.py`, `backend/tests/test_record_scoping.py`, `backend/tests/api/test_rbac_scoping.py`, `backend/tests/test_c7_g1_rbac_write_scope.py` |
| Contract matrix (auth on every route) | `backend/tests/api/endpoints.py` | `backend/tests/api/test_contract_matrix.py` |
| Org-aware login | `backend/app/modules/identity/service.py` | `backend/tests/test_kl_org_scoped_login_db.py` |
| Segregation of duties | `backend/app/modules/approval/service.py`, `lifecycle/service.py`, `ai_agents/agents/approval_agent/evaluate.py` | `backend/tests/test_pg_integ_lifecycle_sod_db.py`, `backend/tests/test_c7r_sod_returned_report_db.py`, `backend/tests/test_kl_refile_lineage_db.py` |
| Approver grades | `backend/app/modules/approval/grade_routing.py` | `backend/tests/test_fu_svp_approval_level_db.py`, `backend/tests/test_fu_svp_routing_unit.py` |
| Audit | `backend/app/core/audit.py` | `backend/tests/test_audit.py`, `backend/tests/test_audit_api_db.py` |
| Account recovery | `backend/app/modules/identity/accounts.py`, `services/email_provider/` | `backend/tests/test_fu_accounts_unit.py`, `backend/tests/test_fu_accounts_db.py`, `backend/tests/test_fu_accounts_ratelimit_db.py` |
| Rate limiting | `backend/app/core/rate_limit.py`, `rate_limit_redis.py` | `backend/tests/test_ps_rate_limit.py`, `backend/tests/test_rl_harden.py`, `backend/tests/test_rl_harden_login_db.py`, `backend/tests/test_rl_store_contract.py`, `backend/tests/test_rl_redis_store.py` |
| Upload hardening | `backend/app/core/body_limit.py`, `backend/app/modules/receipt/upload_guard.py`, `pdf_raster.py`, `pdf_render_worker.py`, `quota.py` | `backend/tests/test_ps_upload_guard.py`, `backend/tests/test_ps_upload_api_db.py`, `backend/tests/test_pdf_receipts.py`, `backend/tests/test_pdf_receipts_api_db.py` |
| Receipt media access | `backend/app/modules/receipt/router.py` | `backend/tests/api/test_receipt_image_access.py` |
| Demo mode and reset | `backend/app/core/demo.py`, `demo_reset_scheduler.py`, `database/seeds/reset_demo_data.py` | `backend/tests/test_ps_demo_protection_db.py`, `backend/tests/test_ps_demo_reset_db.py`, `backend/tests/test_fu_demo_autoreset.py` |
| Startup guard | `config/settings/base.py` | `backend/tests/test_ps_ops_settings_guard.py` |
| Secret scan, env audit, CI hygiene | `scripts/ops/secret_scan.py`, `scripts/ops/env_audit.py`, `.github/workflows/ci.yml` | `backend/tests/test_ps_ops_secret_scan.py`, `backend/tests/test_ps_ops_env_audit.py`, `backend/tests/test_ci_workflow_hygiene.py` |

---

## Authentication

- **Passwords** are hashed with bcrypt (`PASSWORD_HASH_ROUNDS`, default 12). A stored value that is not a bcrypt hash (an invitation placeholder) still costs one full bcrypt check, so a fast failure cannot act as a timing oracle (`security.py::verify_password`).
- **Tokens** are HS256 JWTs: access tokens for 15 minutes, refresh tokens for 7 days. Claims are `sub`, `org`, `type`, `iat`, `exp`, and `pwv` once the password has been changed through the app. A refresh token is refused where an access token is expected.
- **Session revocation.** `pwv` is an HMAC-SHA256 fingerprint of `users.password_changed_at`, keyed with `JWT_SECRET`. A password reset, change or invitation acceptance moves that timestamp, so every token minted earlier stops working (`ensure_token_current`).
- **Every request reloads the user.** `get_current_user` loads the user by `(id, organization_id)` from the token and refuses unknown or inactive users.
- **Client storage.** The frontend keeps the access token in memory and the refresh token in `localStorage` (`frontend/lib/auth/token-store.ts`). This trade-off is listed under [residual risks](#residual-risks).
- **Interactive API docs** (`/docs`, `/redoc`) are only served when `APP_DEBUG=true`, which production refuses.

## RBAC and record scoping

1. **Permission keys** have the form `resource.action.scope` (for example `expense.read.own`, `approval.decide.reports`, `expense.reimburse.org`), catalogued in `config/constants/permissions.py`.
2. **`require_permission(key)`** looks the caller's permissions up in `role_permissions` on every request. A role or permission claim from the client is never trusted.
3. **`scope_query`** is the read choke point. It always applies `organization_id = caller's organisation` first, even for finance admins, then narrows: employee → own rows, manager → own rows plus direct reports (one level), finance admin → the whole organisation. An unknown role raises 403.
4. **Writes are owner-only** for every role (`require_owner`). A manager can read a direct report's report but cannot edit or submit it. Mutation paths load the record through `scope_query` first, so a record the caller cannot read is a 404 (no existence leak) and a readable but not-owned record is a 403.
5. **Filters only narrow.** Optional filters such as "my items only" (`narrow_to_caller`) add predicates after `scope_query`; they cannot widen scope.

`backend/tests/api/test_contract_matrix.py` registers all 80 `/api/v1` endpoints and checks, for each protected one, that a missing or garbage token gets 401 in the standard envelope, never a 500, and that a forbidden role gets 403. Eight endpoints are public by design: login, refresh, forgot-password, reset-password, verify-email, the invitation peek, accept-invite and `GET /public/config`.

## Tenant isolation

Every root business table carries `organization_id`; child tables inherit it through their parent. Isolation is enforced in the application by `scope_query`, by org-scoped lookups in the services, and in retrieval by an organisation-scoped join in the RAG engine. The AI-eval tier probes retrieval with 27 cross-tenant cases and requires zero cross-tenant chunks (`backend/tests/eval/test_eval_db_retrieval.py`). There is **no PostgreSQL row-level security**; see [ADR-0006](../adr/0006-application-level-tenancy.md) for why and what it would take to add it.

## Org-aware login

Email is unique per organisation (case-insensitive, migration 0022), so the same address can exist in two tenants. Login therefore takes an **optional organisation code** (`organizations.slug`; the demo's is `atlas-demo-corp`):

- With a code, the account is looked up in that organisation only.
- Without one, the password is checked against the oldest active accounts with that email, padded with dummy hashes to exactly `NO_ORG_LOGIN_CANDIDATES = 5` bcrypt verifications, and must match exactly one. If more accounts share the email, or the password matches several, the code is required.
- Every failure is the same generic 401 (`AmbiguousCredentialsError` is a subclass of `UnauthorizedError` with the same response), so the response does not reveal whether an email exists or in how many organisations.
- Rate limits and backoff run before any database read.

## Segregation of duties

| Rule | Where | Result |
|---|---|---|
| Nobody opens an approval on their own submission | `approval/service.py` (trigger gate) | 403 |
| Nobody decides an approval on their own submission, checked against the recorded submitter **and** the live subject owner | `approval/service.py::decide_approval` | 403 |
| Nobody reimburses their own report | `lifecycle/service.py::reimburse_report` | 403 |
| A new approval is routed to an independent approver, never the submitter | `approval/lifecycle.py::ensure_independent_approver` | |
| What a reviewer returned or rejected is frozen in `approval_steps.subject_snapshot`; a re-filed returned or rejected claim is flagged `refiled_returned_claim` / `refiled_rejected_claim` and escalates | `ai_agents/agents/approval_agent/evaluate.py` | Withdrawing and re-filing cannot launder a return |
| Lines moved out of a withdrawn report carry their origin (`released_from_report_id`, `released_from_approval_id`, migration 0023), so the re-file check flags exactly those lines | expense service, approval agent | |
| A flag that names an approver grade (SVP, VP) can only be decided by a user of that grade or above; a missing grade never grants an exception | `approval/grade_routing.py::ensure_approver_grade` | 403 `INSUFFICIENT_APPROVER_GRADE` |
| Reject and request-changes require a non-blank reason | `backend/app/core/validation.py` | 422 |
| The approval agent never auto-rejects | `ai_agents/agents/approval_agent/routing.py` | Rejection is always a human step |

## Audit

- **`audit_log`** gets one row per business state change, written by `record_audit` inside the same transaction as the change, so the change and its record commit together or not at all. The actor is NULL for system actions (agent, scheduler, migration).
- **Refused sensitive actions are audited too**, for example an attempt to edit a protected demo account.
- **`ai_decision_logs`** gets one row per AI task, with input, tool and output summaries passed through a minimiser (`ai_agents/decision_log/minimizer.py`) that redacts secret-named keys (password, token, API key, authorization, card number) and masks bearer tokens, email addresses and 16-digit card-like numbers.
- **Logging hygiene:** audit payloads, tokens, passwords, presigned URLs and the database DSN are never logged; the correlation ID ties log lines to requests.
- Finance admins read and export the audit trail (`GET /api/v1/audit`, `/audit/export.csv`); the endpoint is scoped to the caller's organisation.

## Account recovery tokens

Password reset, invitations and email verification (`backend/app/modules/identity/accounts.py`, table `auth_tokens`, migration 0024):

| Property | Implementation |
|---|---|
| Token | 32 random bytes, URL-safe. Only the SHA-256 hex digest is stored (unique, format CHECK). The raw value is never logged, audited or put in an error message. |
| Lifetime | Reset 30 minutes, invitation 7 days, verification 24 hours (`AUTH_RESET_LINK_TTL_MINUTES`, `AUTH_INVITE_LINK_TTL_HOURS`, `AUTH_VERIFY_LINK_TTL_HOURS`) |
| Use | Looked up by digest, compared in constant time, checked for purpose, expiry, single use and that the account still belongs to the token's organisation. Issuing a new token voids earlier unused ones of the same purpose. |
| Failure | Always the same generic answer: `400 INVALID_OR_EXPIRED_TOKEN`, or a generic 404 for the invitation peek |
| No account oracle | `POST /auth/forgot-password` always answers 202 with the same body and runs the same statements whether or not the address exists (a padded `INSERT ... SELECT`). The email is sent after the response, so delivery time is not visible. |
| Links | Built only from `APP_BASE_URL`, never from the request's `Host` header. Staging and production refuse a non-https `APP_BASE_URL` at startup. |
| Sessions | A reset or change sets `password_changed_at`, revoking older tokens (see [Authentication](#authentication)). |
| Email | `EMAIL_PROVIDER` defaults to `console`, which delivers nothing; `smtp` requires `SMTP_HOST` and `EMAIL_FROM` or the app will not start. |
| Demo accounts | Under `DEMO_MODE`, the shared accounts get no reset or verification email, cannot redeem a reset token and cannot change their password. |

## Rate limiting

`backend/app/core/rate_limit.py` classifies every `/api/v1` request into one policy and answers `429 RATE_LIMITED` with `Retry-After` in the standard envelope; allowed responses carry `X-RateLimit-Limit`, `-Remaining` and `-Reset`. `/health` is exempt. It runs before the body-size guard, so a throttled flood is refused before its body is read.

| Policy | Default |
|---|---|
| Login | 30/minute per IP; 10/minute per (IP, email) |
| Failed-login backoff | After 5 failures per (IP, email, "org code given"), 30 s doubling to at most 900 s. Past 50 failures an hour for one email from all addresses, every failing address is locked progressively longer; a correct password from a clean address is never blocked. |
| Refresh | 120/minute per IP |
| Forgot password | 5/hour per (IP, email); 20/hour per IP |
| Reset password, accept invite, verify email, change password | 10/minute per IP; invitation peek 30/minute per IP |
| Sending invitations; sending verification emails | 30/hour per user; 5/hour per user |
| User administration (create, edit, deactivate, reactivate) | 30/minute and 300/day per user |
| AI spend (assistant messages, policy query, policy evaluate, rule authoring) | 60/hour and 300/day per user; 2000/day per organisation |
| Receipt upload | 10/minute and 100/day per user |
| Everything else | 300/minute |

Design points:

- **Identities are HMAC-hashed** with a per-process random key before they are stored or logged; no raw IP, email or token is kept.
- **Bounded memory, fail closed.** Live counters are never evicted to make room. Keys per IP (per IPv6 /64) and per IPv6 /48 are capped; an identity that finds no room is counted in its own address's overflow row or in one of a fixed set of hashed network buckets, never handed a fresh budget and never pooled into one global bucket.
- **Client IP** comes from `X-Forwarded-For` only up to `TRUSTED_PROXY_COUNT` hops. Staging sets it to 1 for Render's single proxy, verified against live traffic on 2026-11-17.
- **Store.** In process by default; `RATE_LIMIT_STORE=redis` shares counters across workers and instances with the same guarantees ([runbook](../runbooks/rate-limit-redis.md)). Staging runs the in-process store.
- **Visibility.** `GET /api/v1/ops/rate-limits` (finance admin) reports the limits and soft signals such as distributed failures against one email.
- Every rate spec is parsed at startup; an invalid one stops the app rather than silently disabling a limit.

## Upload hardening

Receipts are the one place where untrusted files enter the system. Everything below happens **before** a byte is stored or sent to OCR (`backend/app/modules/receipt/router.py::upload_receipt`):

1. **Body caps.** `BodySizeLimitMiddleware` refuses a `Content-Length` over the limit at once and counts streamed bytes for chunked or under-declared bodies: 10 MiB plus multipart overhead for `/api/v1/receipts`, 1 MiB elsewhere (413 `PAYLOAD_TOO_LARGE`). The route re-reads the file with a bounded reader as a second line.
2. **Type from content.** Magic bytes decide the type against an allow-list (PNG, JPEG, WebP, plus PDF). The client's `Content-Type` and file extension may be absent, but if present they must agree with the sniffed type (415). A file that is both an image and carries a PDF header in the first 1024 bytes is an ambiguous polyglot and is refused.
3. **Structural checks without decoding.** Dimensions and pixel count are read from headers (decompression-bomb limits: 40 million pixels, 12,000 px per side). One decode check runs only when its estimated memory fits a budget, one image at a time per process. Empty, truncated or corrupt files are 422.
4. **Sanitising.** Metadata (EXIF including GPS, XMP, text chunks, comments) and any bytes after the image's end marker are dropped losslessly; pixels are not re-encoded. Only a minimal EXIF orientation tag is written back.
5. **PDFs are rasterised, never stored** ([ADR-0008](../adr/0008-pdf-rasterise-only.md)). PDFium (pypdfium2) renders the first 3 pages into one PNG in a **separate child process** (`pdf_render_worker.py`) started as `python -I -B` with an empty environment, its own process group and a temporary working directory. Inside the child: a CPU-seconds limit, no core dumps, no file writes (`RLIMIT_FSIZE = 0`), an address-space budget above what is already mapped, and no new file descriptors (`RLIMIT_NOFILE = 0`, so no socket or file can be opened). The parent enforces a 10-second wall clock, kills the whole process group, and reads the answer over a pipe with a hard size bound. Page count and page size are checked before any page is rendered. One render runs at a time per process; a request waits at most 3 seconds for the slot and otherwise gets a retryable 503. Encrypted, damaged, page-less or oversized-page PDFs and any render failure are a friendly 422. The resulting PNG then goes through steps 2–4 like any upload.
6. **Quota.** Per user 200 files / 200 MiB, per organisation 5,000 files / 2 GiB (`409 UPLOAD_QUOTA_EXCEEDED`).
7. **Storage.** The object key is generated on the server (`<org>/<uuid>.<ext>`, extension from the sniffed type); the client file name is never stored, logged or used in a key. The database stores the key, never a public URL.
8. **Serving.** Images are returned only as signed URLs valid for at most 300 seconds, and only after RBAC and `scope_query` admit the caller. Signing failures degrade to "no link"; the URL is never logged. On staging the R2 credentials are scoped to one bucket.
9. **Receipt locking.** A receipt that a reviewer relied on (part of a submitted or returned report) cannot be corrected (`409 RECEIPT_LOCKED`); reviewed receipts of a withdrawn report are kept for audit.

## AI-specific controls

| Risk | Control | Code |
|---|---|---|
| A prompt-injected instruction triggers a state change | Agents can only take actions from the `AgentAction` enum, each mapped to a permission key and checked against the caller's keys before any state-changing tool call | `ai_agents/guardrails/actions.py` |
| An LLM influences money decisions | The approval agent has no LLM; auto-approval is a fixed deterministic gate (in policy, no blocking flags, no approver grade needed, total ≤ 500 USD) | `ai_agents/agents/approval_agent/routing.py` ([ADR-0004](../adr/0004-llm-free-approval-agent.md)) |
| Ungrounded policy answers | Calibrated abstention before any LLM call; answers must cite retrieved clauses, and each sentence must be supported | `ai_agents/rag/`, `ai_agents/guardrails/citations.py`, `ai_agents/rag/support.py` |
| Invented or wrong inferred fields | Generator → critic loops; rejected inferences are nulled and marked `needs_review` | `ai_agents/guardrails/reflection.py`, `ai_agents/receipt_pipeline/reflection.py` |
| Rule authoring activates a bad rule | LLM drafts are critic-checked and saved as `draft`; a finance admin activates them | `ai_agents/agents/policy_authoring.py` |
| Secrets or PII in AI records | Decision-log minimiser before persistence | `ai_agents/decision_log/minimizer.py` |
| Runaway spend | AI and upload rate budgets per user, per day and per organisation | `backend/app/core/rate_limit.py` |

## Demo mode and reset

**`DEMO_MODE=true`** (`backend/app/core/demo.py`) protects the shared demo from vandalism:

- The five seeded accounts of "Atlas Demo Corp" cannot be edited (name, role, manager, department, grade) or deactivated by anyone, finance admins included. Email, password and organisation have no write endpoint at all. Refusals are `403 DEMO_ACCOUNT_PROTECTED` and audited. Accounts created by visitors stay fully manageable.
- With `DEMO_RESTRICT_DESTRUCTIVE` (which follows `DEMO_MODE` unless set), the seeded policy rules cannot be archived, because archiving is terminal. Seeded rules are identified by **provenance** (the seed's policy, a seed-created clause, and no `policy_rule.drafted` audit row from the API), not by content, so a visitor's lookalike copy stays archivable.
- An account is protected only when its stored email is a seed email **and** its stored organisation is exactly the demo organisation; nothing is taken from the client. The UI lock badges and the demo banner (driven by the unauthenticated `GET /api/v1/public/config`) are hints only.

**Demo reset** restores the demo organisation to the seed baseline ([runbook](../runbooks/demo-reset.md)):

- `python -m database.seeds.reset_demo_data` is a dry run by default. `--apply` also needs `DEMO_RESET_ENABLED=true` and `--org "Atlas Demo Corp"`.
- An apply runs in **one transaction**: it deletes the demo organisation's visitor data (trips, reports, expenses, receipts, approvals, card transactions, calendar events, conversations, idempotency keys, notification read markers) and visitor-created users and rules, restores the seeded accounts and rules, re-runs the idempotent seed steps and writes a `demo.reset` audit row. After the commit it removes deleted receipts' objects from storage, best-effort. `audit_log` and `ai_decision_logs` are kept. A cross-tenant reference refuses the reset.
- An optional in-process schedule (`DEMO_RESET_SCHEDULE=interval`, off by default and not set in `render.yaml`) runs it periodically; the status is at `GET /api/v1/ops/demo-reset`. The public demo runs it daily: it is due at 04:00 UTC, and because the free instance sleeps when idle, a missed slot runs once on the next wake.

## Configuration and startup guard

`config/settings/base.py` validates every setting at startup:

| Check | Development | Staging | Production |
|---|---|---|---|
| `JWT_SECRET` at least 32 characters, not a placeholder (`change-me`, `your-`, …), at least 10 distinct characters | not checked | warning | **refuses to start** |
| `SEED_DEFAULT_PASSWORD` is not the published demo password | not checked | warning (the demo uses it on purpose) | **refuses to start** |
| `APP_DEBUG` is false | not checked | warning | **refuses to start** |
| No `*` in `CORS_ALLOWED_ORIGINS` | not checked | warning | **refuses to start** |
| `APP_BASE_URL` is https | not checked | **refuses to start** | **refuses to start** |
| `EMAIL_PROVIDER=smtp` has `SMTP_HOST` and `EMAIL_FROM` | refuses | refuses | refuses |
| Every rate spec parses | refuses | refuses | refuses |

Messages name the variable, never its value. Other HTTP-level settings: CORS uses an explicit origin list; every response carries `X-Content-Type-Options: nosniff`; errors never include stack traces.

## Secrets and CI

- **Where secrets live.** Runtime secrets live only in the Render and Vercel dashboards (`sync: false` in `render.yaml`). GitHub Actions holds `RENDER_DEPLOY_HOOK_URL`, `STAGING_BACKEND_URL`, `STAGING_FRONTEND_URL` and optionally `STAGING_DEMO_PASSWORD`. `.env` files and the staging hand-off file are git-ignored.
- **Secret scan.** The blocking CI job `secret-scan` runs `scripts/ops/secret_scan.py` over tracked and untracked (not ignored) files **and every line ever added in any commit** (`git log --all -p`). It looks for known credential shapes (OpenRouter, OpenAI, Anthropic, Google, Hugging Face, Novita, AWS/R2, JWTs, Postgres URLs with passwords, private keys, GitHub, Render, Neon and Vercel tokens, the Render hook URL) plus high-entropy values assigned to secret-named variables. It never prints a secret, only a 4-character prefix, length and fingerprint. `scripts/ops/env_audit.py` runs in the same job.
- **CI hygiene.** The E2E lane uses a masked per-run seed password and uploads no Playwright HTML report or trace (those would record the login body and live tokens); artifacts have short retention; the deploy smoke never prints login response bodies. `backend/tests/test_ci_workflow_hygiene.py` parses the workflow and fails if any of this regresses.
- **Deploy integrity.** The Render deploy hook is pinned to the tested commit, and CI waits until `/health` reports that SHA ([ADR-0005](../adr/0005-commit-pinned-deploy-verification.md)).
- **Rotation.** [Key rotation runbook](../runbooks/key-rotation.md): per-secret order, verification without printing values, rollback.

---

## Residual risks

Known and accepted for a portfolio demo. Each is also recorded in the phase logs where it was found.

| Risk | Why it remains | What would close it |
|---|---|---|
| **Shared, public demo accounts.** Anything a visitor enters is visible to the next visitor until the daily reset. | The demo is meant to be explored without sign-up. | Per-visitor sandboxes. Visitors should not upload real personal receipts; uploads are sent to a third-party OCR provider (OpenRouter). |
| **Refresh token in `localStorage`, no Content-Security-Policy.** An XSS on the frontend origin could read it. | Chosen for simplicity; access tokens are short-lived (15 min) and in memory. | httpOnly refresh cookie plus a CSP. |
| **In-process rate limits on staging.** Counters reset on restart or deploy; a second worker or instance would each allow the full budget. | Staging runs one worker on one instance; the Redis store is built and tested but not switched on. | `RATE_LIMIT_STORE=redis`. |
| **Application-level tenancy, no row-level security.** A missed `scope_query` in a new query would not be caught by the database. | One choke point, the contract matrix and cross-tenant tests are judged sufficient at this size ([ADR-0006](../adr/0006-application-level-tenancy.md)). | PostgreSQL RLS keyed on a session variable. |
| **`audit_log` append-only by convention.** The application role could technically update or delete rows. | Single writer, code review. | A trigger or a separate role without UPDATE/DELETE on the table. |
| **PDFium is native code** without seccomp or namespaces. A memory-corruption exploit runs as the app user; it cannot open files or sockets but could, where `ptrace_scope` allows, read the parent's memory. | Containment limits the blast radius; the container is the outer boundary. | A separate uid, a non-dumpable parent, or seccomp. Track pypdfium2 releases. |
| **Recovery timing parity is statement-level, not wall-clock.** A precise network timing attacker could measure the extra insert for a real account. | Rate limits bound the number of probes. | Constant-time padding of the response. |
| **Invitation peek token in proxy logs.** `GET /auth/invite/{token}` is redacted in app logs but appears in Render's edge request log. | Single-use token held by the invitee. | Move the peek to a POST body. |
| **No email on staging.** `EMAIL_PROVIDER=console`, so self-service password reset does not work there; invitations show their link once to the inviting admin. | No SMTP account configured for the demo. | Configure SMTP. |
| **Upload quotas are not serialised.** Concurrent uploads can overshoot a quota by the number in flight. | Bounded by the per-user upload rate limit. | A row lock or counter table. |
| **Distributed guessing of one email** from many addresses is slowed, not blocked. | A global lock would let an attacker lock out a real user. | Accepted design trade-off. |
