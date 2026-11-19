# P8-T3 — Manual dashboard verification

Render, Vercel, Neon, Cloudflare R2 and GitHub Actions secrets, checked against the staging environment spec.

| | |
|---|---|
| **Date** | 2026-10-29 |
| **Mode** | read-only browser verification |
| **Branch** | `p8-t3-env-audit` |
| **`git log -1 --oneline`** | `d86798a ci: replace unpullable minio/minio image with Chainguard MinIO` |
| **Reference spec** | `render.yaml`, `.github/workflows/ci.yml`, `docs/env_audit_p8_t3.md` (section 5, "Manual dashboard checklist"), `docs/staging_env_map.md` |
| **What was recorded** | Names and non-secret facts only. No secret value, connection string, token, key, access-key id or deploy-hook URL appears in this file. |
| **Masking** | Render service id `srv-...`; Neon project id `<project-id>`, branch id `br-...`, endpoint id `ep-<masked>`; Cloudflare account id `<account_id>`; Vercel team slug `<team-slug>`. |
| **Working tree at start** | `.env.example` and `docs/staging_env_map.md` modified, `docs/env_audit_p8_t3.md` untracked. All three pre-date this run and were not touched. This file is the only addition. |

## 1. Summary

Status meanings: **PASS** = verified from what was seen. **FAIL** = seen, and it contradicts the spec. **BLOCKED** = the page could not be reached or used (login, 2FA, CAPTCHA, site permission). **UNVERIFIABLE** = reachable, but the fact cannot be established from what is on screen. **ACTION OPEN** is not a status: it flags that the Required follow-up in section 4 remains open, and it is shown next to the status without changing it. "STILL PENDING" (4d) is the same fact in the R2 detail, and note-only items such as 1e sit outside the worst-of rule below.

A check's status is the worst result among its own sub-checks (FAIL > BLOCKED > UNVERIFIABLE > PASS), leaving out the items listed in section 3.1 (UNVERIFIABLE BY DESIGN: values the brief itself keeps masked or expects to be unreadable, such as secrets). Items in section 3.2 count when they decide a sub-check (3.2 item 1 is why Check 2 is UNVERIFIABLE) and otherwise only qualify the result they touch (3.2 item 2 the Render and CORS results, 3.2 item 3 the 4d result).

| # | Check | Status | Reason |
|---|---|---|---|
| 1 | Render (`atlas-backend` → Environment) | **PASS** | 23/23 expected names present with no extras or near-miss spellings (two reads agree); 16/16 literals match `render.yaml` (visible characters); auto-deploy Off (single read). |
| 2 | Vercel + CORS | **UNVERIFIABLE** | The variable exists for Production and Preview, but it is Sensitive and showed no reveal control, so its value (and the `/api/v1` suffix) was not readable in this session (Edit not opened); 2b, 2c and 2d pass. |
| 3 | Neon | **PASS** | Backend `/health` ok/staging (liveness only); branch at the repo migration head (0012; observed after the cold-start probe, see 3d) with 3 demo users, `pgcrypto` and `vector`; Render-vs-Neon string equality is UNVERIFIABLE BY DESIGN. |
| 4 | Cloudflare R2 | **PASS** · **ACTION OPEN** | **ACTION OPEN: rotate the R2 token (no rotation evidenced; issued Jun 23, 2026).** Bucket, endpoint pattern and an Object Read & Write token scoped to the bucket are confirmed. |
| 5 | GitHub Actions secrets | **PASS** | Exactly the 3 expected secret names and no others; `ci.yml` uses the hook only via the `secrets` context; no string matching a deploy-hook pattern in tracked or non-ignored untracked files or in any local ref's history (one placeholder). |

## 2. Per-check detail

### Check 1 — Render (service `atlas-backend` → Environment): PASS

Page: `https://dashboard.render.com/web/srv-.../env` (signed in; the page loaded without a login wall). Incidental header facts: Docker, Free instance, "Blueprint managed", status Live (both reads); branch `main` and commit `d86798a` (first read only).

**1a. The 7 dashboard-only variables, by name: PASS.** All seven are present. Values are masked by default (a 12-dot placeholder on every row). Six were left masked and were not inspected; `CORS_ALLOWED_ORIGINS` was revealed under the one permitted exception.

| Variable | Present | Value handling |
|---|---|---|
| `JWT_SECRET` | yes | masked, not inspected |
| `DATABASE_URL` | yes | masked, not inspected |
| `LLM_API_KEY` | yes | masked, not inspected |
| `STORAGE_ENDPOINT_URL` | yes | masked, not inspected |
| `STORAGE_ACCESS_KEY_ID` | yes | masked, not inspected |
| `STORAGE_SECRET_ACCESS_KEY` | yes | masked, not inspected |
| `CORS_ALLOWED_ORIGINS` | yes | revealed (permitted exception): `https://atlas-concur-ai.vercel.app` |

**1b. `OPENROUTER_API_KEY` must not exist: PASS.** Absent. No row name contains `OPENROUTER`; the row that exists is `LLM_API_KEY`, which is the name the app reads. There is also no `RERANK_*` row, so staging runs the in-code default (the local reranker), as the audit checklist expects.

**1c. The 16 literals against `render.yaml`: PASS (16/16).**

| Variable | `render.yaml` | Dashboard | Match |
|---|---|---|---|
| `APP_ENV` | `staging` | `staging` | yes |
| `APP_DEBUG` | `false` | `false` | yes |
| `PORT` | `8000` | `8000` | yes |
| `CORS_ALLOW_CREDENTIALS` | `true` | `true` | yes |
| `LLM_PROVIDER` | `openrouter` | `openrouter` | yes |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | `https://openrouter.ai/api/v1` | yes |
| `LLM_MODEL` | `google/gemini-2.5-flash` | `google/gemini-2.5-flash` | yes |
| `LLM_MODEL_FALLBACK` | `google/gemini-2.5-flash-lite` | `google/gemini-2.5-flash-lite` | yes |
| `LLM_THINKING_BUDGET` | `0` | `0` | yes |
| `EMBEDDINGS_PROVIDER` | `local` | `local` | yes |
| `OCR_PROVIDER` | `local` | `local` | yes |
| `STORAGE_PROVIDER` | `minio` | `minio` | yes |
| `STORAGE_REGION` | `auto` | `auto` | yes |
| `STORAGE_BUCKET` | `atlas-receipts-staging` | `atlas-receipts-staging` | yes |
| `STORAGE_FORCE_PATH_STYLE` | `true` | `true` | yes |
| `SEED_ON_STARTUP` | `false` | `false` | yes |

**1d. Variables outside the 23, typos, extras: none.** 23 rows observed, 23 expected: 0 missing, 0 extra, 0 duplicate names, and no near-miss spelling of any expected name. Other sections on the page: Secret Files (none), Linked Environment Groups (none).

**1e. Auto-deploy (note only): Off (single read).** Settings → Build & Deploy → Auto-Deploy displays `Off`, matching `autoDeploy: false` in `render.yaml`. The deploy hook is managed on that Settings page (per `docs/staging_env_map.md`), so no page text, screenshot or zoom was taken there. Only the Auto-Deploy control was read: it was located with `find`, then confirmed with an accessibility read limited to that one element. Nothing was clicked, and no deploy-hook pattern appears in any text result of that session (the patterns scanned are listed in section 6). The second read skipped this step.

**Evidence quality.** Two independent read sessions with no shared context (same method) produced identical name lists, identical literal values and an identical CORS string, and both match the spec; the first read's `find` also counted 23 key fields. Both readers knew the target names (they needed them for the reveal rules), so the name match indicates the absence of near-miss spellings rather than independent discovery. The value match is the stronger evidence, because neither reader knew the expected values. The page-text tool returned only a promotional banner, so names were read from zoomed screenshots. Two names are cut off by the UI ellipsis at the default width (`STORAGE_FORCE_PATH_STYLE`, `STORAGE_SECRET_ACCESS_KEY`): the first read resolved them by focusing the read-only name field and moving the caret, the second by collapsing the left sidebar to widen the column. Leading or trailing whitespace in a value cannot be seen on screen (section 3.2). Runtime corroboration from outside the dashboard: the service starts only with non-empty `JWT_SECRET` and `DATABASE_URL` (settings validator), `/health` reports env `staging` (`APP_ENV`), and the CORS preflight reflects `CORS_ALLOWED_ORIGINS` (check 2c).

### Check 2 — Vercel (project `atlas-concur-ai`) and the CORS comparison: UNVERIFIABLE

Pages: `https://vercel.com/<team-slug>/atlas-concur-ai` (overview, `/settings/environment-variables`, `/settings/domains`, `/deployments`). During the pre-flight the browser automation refused vercel.com ("Navigation to this domain is not allowed"); the single attempt made later in the run, after site permission was granted, succeeded (signed in, no re-authentication prompt). Two independent read sessions agree on the environment-variable rows (names, environments, Added times), the domain list and the latest deployment's time; those were compared by hand because the script's date parser returned nothing for the Vercel timestamps. Items seen by the first read only are marked "single read".

**2a. `NEXT_PUBLIC_API_BASE_URL` for Production and Preview: exists for both (PASS); value UNVERIFIABLE.**

| Environment | Added (UTC, from tooltip) | Type | Value readable |
|---|---|---|---|
| Production | 2026-06-24 07:59:24 | Sensitive (lock icon, tooltip "Secret") | no |
| Preview | 2026-06-24 07:59:25 | Sensitive (lock icon, tooltip "Secret") | no |

Neither row has an eye/reveal control. The row menu (Edit, Rotate, Copy to Clipboard, View History, Delete) shows "Copy to Clipboard" disabled with the tooltip "Sensitive environment variables cannot be copied" (single read). Edit was deliberately not opened (read-only rule), so what it would show is untested; Vercel's documentation describes Sensitive variables as unreadable after creation (not tested here). Result: no read-only way to see the value was found, so it cannot be confirmed that it ends in `/api/v1` or equals `https://atlas-backend-tsf8.onrender.com/api/v1`.

Supplementary indirect evidence (not a dashboard read; it does not change the status): the 13 script files served by the public Production site at `/login` contain the string `https://atlas-backend-tsf8.onrender.com/api/v1` (2 occurrences) and no other `onrender.com` URL. So the June Production build inlined the expected value. This shows what that build contains, not what the dashboard stores now. The project currently lists a single deployment, which is Production (single read), so no listed build has used the Preview row; the Preview value remains unverified.

**2b. No secret-looking variable behind a `NEXT_PUBLIC_` prefix: PASS.** The complete list of variable names on the project is `NEXT_PUBLIC_API_BASE_URL` twice (the Production and Preview rows). There is no Development or "All Environments" row and no other `NEXT_PUBLIC_*` name. The Shared tab reports "No shared variables linked" (single read).

**2c. Production domain against `CORS_ALLOWED_ORIGINS`: PASS (identical, for the visible characters).**

| Source | String |
|---|---|
| Vercel Production domain (Domains settings and overview card) | `https://atlas-concur-ai.vercel.app` |
| Render `CORS_ALLOWED_ORIGINS` (revealed in check 1) | `https://atlas-concur-ai.vercel.app` |

Compared as transcribed: identical; as read visually, a single origin with `https` scheme, no trailing slash, no path, no comma list and no wildcard. The Domains page shows only the bare host `atlas-concur-ai.vercel.app` (status "Valid Configuration", badge Production); the `https://` scheme comes from the link target. It is the project's only domain (no custom, redirect or branch domains).

Supplementary runtime evidence (an OPTIONS preflight sent to the staging backend; no body, no credentials, no configuration change). Origin `https://atlas-concur-ai.vercel.app` gets HTTP 200 with `access-control-allow-origin: https://atlas-concur-ai.vercel.app` and `access-control-allow-credentials: true`. A foreign origin gets HTTP 400 and no `access-control-allow-origin` header. So the effective, parsed origin list contains this origin and no wildcard (a foreign origin is refused); that the stored string is a single origin rests on the visual read in check 1. This test cannot show leading or trailing whitespace, a trailing comma or an empty element in the stored string, because the app strips every entry and drops blanks (`cors_origins_list` in `config/settings/base.py`); such padding would be harmless at runtime. A trailing comma or empty element would be visible, and neither reader saw one; leading or trailing whitespace is invisible on screen and is not excluded (section 3.2).

**2d. Latest Production deployment is newer than the variable: PASS.**

| Event | UTC | PDT (as displayed) |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` added, Production row | 2026-06-24 07:59:24 | 12:59:24 AM |
| `NEXT_PUBLIC_API_BASE_URL` added, Preview row | 2026-06-24 07:59:25 | 12:59:25 AM |
| Latest Production deployment created (status Ready) | 2026-06-24 08:00:25 | 01:00:25 AM |

The deployment was created 61 s after the Production row, so the build started after the variable existed. Both rows read "Added" rather than "Updated" (View History was not opened), so neither row shows an edit after that time. Preview: no build is listed, so the ordering check does not apply to the Preview row (single read; the 60 s gap to the Preview row is not meaningful).

**Observations outside the five checks (single read unless stated).** The project lists exactly one deployment, whose source is a CLI deploy (not Git), and its Production Checklist reads 1/5 with "Git not connected". The second read also noted that the overview's Active Branches card lists `main` with a Preview link; that was not reconciled with "Git not connected" (it may be metadata attached to the CLI deployment). See follow-up 3.

### Check 3 — Neon: PASS (with Render-vs-Neon equality UNVERIFIABLE BY DESIGN)

Page: `https://console.neon.tech/app/projects/<project-id>/branches/br-...`. Project `atlas-concur-ai`, Free plan, one branch (`production`, Default, the branch in the URL), one database, one compute (Primary, Active).

**3a. Non-secret connection facts.**

| Fact | Observed |
|---|---|
| Region | AWS US East 1 (N. Virginia) |
| Endpoint | id masked `ep-<masked>`; pooled host form `ep-<masked>-pooler.c-3.us-east-1.aws.neon.tech`, direct host form `ep-<masked>.c-3.us-east-1.aws.neon.tech` |
| Database | `neondb` |
| Role | `neondb_owner` (the only role listed) |
| sslmode | `require`. The console's pooled and direct connection strings both end with `sslmode=require` and `channel_binding=require`. The password was shown only as asterisks and was never revealed. |

**3b. Database evidence (six SELECT statements, SQL Editor on the branch above): PASS.**

```sql
select table_name from information_schema.tables where table_schema = 'public' order by table_name
select extname, extversion from pg_extension order by extname
select column_name from information_schema.columns where table_schema = 'public' and table_name = 'users' order by ordinal_position
select email from users where email in ('employee@atlas-demo.internal','manager@atlas-demo.internal','finance@atlas-demo.internal') order by email
select 'users' as t, count(*) as n from users union all select 'organizations', count(*) from organizations union all select 'policies', count(*) from policies union all select 'policy_embeddings', count(*) from policy_embeddings union all select 'policy_rules', count(*) from policy_rules
select version_num from alembic_version
```

- **Demo seed:** `employee@atlas-demo.internal`, `manager@atlas-demo.internal` and `finance@atlas-demo.internal` all exist (3 of 3). The `users` table has 12 columns including a password-hash column, which was never selected.
- **Extensions:** `pgcrypto` 1.4, `plpgsql` 1.0, `vector` 0.8.1. Both required extensions are installed.
- **Row counts:** `users` 5, `organizations` 1, `policies` 1, `policy_embeddings` 8, `policy_rules` 8. No table name contains "chunk"; the policy chunk/embedding table is `policy_embeddings`.
- **Schema:** 23 public tables (`ai_decision_logs`, `alembic_version`, `approval_steps`, `approvals`, `audit_log`, `calendar_events`, `card_transactions`, `conversation_messages`, `conversations`, `expense_reports`, `expenses`, `idempotency_keys`, `organizations`, `permissions`, `policies`, `policy_embeddings`, `policy_rules`, `receipts`, `role_permissions`, `roles`, `trip_segments`, `trips`, `users`).
- **Migration state:** `alembic_version` = `0012_hnsw_ef_construction_200`. That is the newest migration in `database/migrations/versions/` and no migration descends from it, so the branch is at the repo head.

**3c. Backend health (liveness only): PASS.** `https://atlas-backend-tsf8.onrender.com/health` returned `{"status":"ok","app":"Atlas","env":"staging","version":"0.1.0"}`. The first request hit the free-tier cold start (Render's "SERVICE WAKING UP" page, whose start-up log spans roughly 26 s); a re-request about 55 s after the first one returned the JSON above. The handler never touches the database.

**3d. Render's `DATABASE_URL` against this branch: UNVERIFIABLE BY DESIGN.** The value is masked and stays masked, so no string comparison was attempted. The indirect evidence is consistent with it but does not prove it: the container entrypoint runs `alembic upgrade head` under `set -e` before starting uvicorn, so a live container implies the migration succeeded against its own `DATABASE_URL`; and this branch is at the repo head with the demo seed. A different migrated database would also satisfy that, so treat it as consistent-with, not proof. Ordering caveat: the cold start in 3c ran that entrypoint earlier in this same session, before the Neon session read the migration version; if Render's `DATABASE_URL` does point at this branch, that start may itself have applied a pending migration, so "at head" is an observation made after the probe.

### Check 4 — Cloudflare R2: PASS · ACTION OPEN (token rotation)

Page: `https://dash.cloudflare.com/<account_id>/r2/overview` and the Manage API Tokens page (signed in; no login or CAPTCHA wall). No click of any kind was made on these pages.

**4a. Bucket: PASS.** Exactly one bucket is listed, `atlas-receipts-staging` (2 objects, 2.78 kB); no second page.

**4b. Endpoint pattern: PASS (Render-side match UNVERIFIABLE BY DESIGN, section 3.1).** The Account Details card shows an S3 API endpoint of the form `https://<account_id>.r2.cloudflarestorage.com` (default jurisdiction); its host label equals the Account ID on the same card. Whether Render's `STORAGE_ENDPOINT_URL` equals this cannot be seen, because that value stays masked.

**4c. Active API tokens: PASS.** One Account API token and no User API tokens.

| Token name | Applied to | Permission | Issued on | Status |
|---|---|---|---|---|
| `atlas-staging-backend` | `atlas-receipts-staging` | Object Read & Write | Jun 23, 2026 (about 4 months / 128 days before 2026-10-29; date only, no time of day) | Active until Jun 23, 2027 (one year after the issue date) |

The list has no last-used, modified or rolled column. The token's detail page was not opened: the name is not a link, and the only row control is an Actions menu, which was not opened (such menus hold Edit / Roll / Delete-type actions). Whether the secret currently in Render matches this token is UNVERIFIABLE BY DESIGN (section 3.1; the provider shows the secret only at creation, per its documentation, not re-observed here).

**4d. Rotation: STILL PENDING (no rotation evidenced).** The task brief's rule is that rotation is still pending unless the token was created after 2026-06-24; this token was issued on Jun 23, 2026. That date coincides with the P8-T2 session (its phase log is dated 2026-06-23 and records that the token was pasted into a transcript, without giving a date). Caveat: the list cannot distinguish the original creation from a later roll, so this is "no rotation evidenced" rather than a proof that none happened. The Cloudflare account Audit Log (not opened) may list token create/roll events with dates; whether it records R2 token rolls is untested. See follow-ups 1 and 4.

### Check 5 — GitHub Actions secrets: PASS

Page: `https://github.com/GarvSingh791/atlas-concur-ai/settings/secrets/actions` (signed in; no sudo-mode prompt). No click of any kind was made on these pages.

**5a. Expected names: PASS.** Repository secrets lists exactly three names, each "last updated 4 months ago": `RENDER_DEPLOY_HOOK_URL`, `STAGING_BACKEND_URL`, `STAGING_FRONTEND_URL`. Environment secrets: the repository has one environment, `main - atlas-backend`, which has no secrets and no variables (deployment branches and tags: no restriction). GitHub does not display secret values (none was shown on the page).

**5b. Other secrets: none.** No other repository secret, no environment secret, and the page has no Organization secrets section.

**5c. Local checks: PASS.**

- `.github/workflows/ci.yml` mentions `RENDER_DEPLOY_HOOK_URL` twice: line 385 (a comment that names the secret) and line 402, where it is referenced exactly as `${{ secrets.RENDER_DEPLOY_HOOK_URL }}` in the "Trigger Render backend deploy" step. The only `secrets.*` references in the file are that one, `STAGING_BACKEND_URL` (lines 416 and 460) and `STAGING_FRONTEND_URL` (line 444), matching the three dashboard names; line 384 holds only a `secrets.NAME` placeholder inside a comment.
- `git grep -nE` with the task's two deploy-hook patterns (the Render API deploy path, and the deploy-then-query-key form): 1 hit in the working tree (`docs/staging_env_map.md:344`), 1 hit in committed `HEAD` (`docs/staging_env_map.md:279`), and the same single hit when untracked files are included. That line is a format description made of angle-bracket placeholders (service-id and deploy-key), not a URL; an assertion on the exact placeholder text matched both lines, and 0 hits were anything other than the placeholder. No string matching a deploy-hook pattern exists in any tracked file other than that placeholder.
- Beyond the brief, history was checked too: across all 118 commits reachable from any local ref, exactly one commit (the P8-T4 CI/CD commit) ever added or removed a line matching those patterns, and that line is the same placeholder. Ignored local files such as `.env` and `frontend/.env.local` were deliberately not opened.

## 3. Items that could not be verified

### 3.1 UNVERIFIABLE BY DESIGN

These cannot be confirmed from a read-only dashboard session. They are excluded from every status.

1. **Masked Render values:** `JWT_SECRET` (including its strength), `DATABASE_URL` (format and `sslmode`), `LLM_API_KEY`, `STORAGE_ENDPOINT_URL`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`. Presence by name only: "masked, not inspected".
2. **Render `DATABASE_URL` against the Neon connection string:** direct string equality. Only the indirect evidence in check 3 exists.
3. **Render `STORAGE_ENDPOINT_URL` against the R2 account endpoint.**
4. **R2 token secret:** whether the key id and secret currently in Render belong to the active token `atlas-staging-backend`. The provider shows the secret only at creation (per its documentation; not re-observed here).
5. **GitHub secret values:** that `RENDER_DEPLOY_HOOK_URL` is the current hook for the service, and the contents of `STAGING_BACKEND_URL` and `STAGING_FRONTEND_URL`. (The strongest indirect signal is the latest `deploy-staging` run; see follow-up 5.)

### 3.2 Other items that could not be established (not by the task's design)

These are limits of this session's method or of reading a screen, not of the task's design. Item 1 is the reason Check 2 is UNVERIFIABLE; items 2 and 3 qualify the result they touch.

1. **Vercel `NEXT_PUBLIC_API_BASE_URL` value:** no read-only way to see it was found (Sensitive variable; Edit not opened; check 2a). Production is supported by the public-bundle evidence; Preview is unverified (no listed build has used it; single read). It stays counted even if Edit would also withhold the value, because the brief expected this public variable to be readable.
2. **Leading or trailing whitespace in stored values:** in any revealed Render literal, including `CORS_ALLOWED_ORIGINS`. The runtime preflight cannot show it either (the app strips it). The preflight also cannot show a trailing comma or empty element in `CORS_ALLOWED_ORIGINS` (the app drops them), but none was visible on screen (both readers).
3. **Whether the R2 "Issued on" date is the original creation or a later roll:** the list has no modified column and the detail page was not opened.

## 4. Required follow-up actions

**Required**

1. **Rotate the Cloudflare R2 API token (still open; not done here).** `atlas-staging-backend` was issued on Jun 23, 2026 and no rotation is evidenced. Per `docs/env_audit_p8_t3.md` (F-13 and section 5): Cloudflare → R2 → Manage API tokens → regenerate → update `STORAGE_ACCESS_KEY_ID` and `STORAGE_SECRET_ACCESS_KEY` in Render together → confirm a receipt upload still works → revoke the old token. Done when the token list shows an Issued on date after 2026-06-24 and no unintended active token, a receipt upload works, and check 4 is re-run.

**Recommended**

2. **Make Vercel's `NEXT_PUBLIC_API_BASE_URL` auditable.** Both rows are Sensitive, and Vercel is expected to withhold such values after creation, so the dashboard is not expected to show it. The value is inlined into public JavaScript, so the Sensitive flag adds no secrecy. If you want the dashboard to be checkable, re-create it as a plain variable for Production and Preview, redeploy (it is inlined at build time), then re-run check 2a.
3. **Reconcile the Vercel deploy path with the CI comment and the runbook.** The dashboard shows the Vercel project as not Git-connected (Production Checklist 1/5) with a single CLI-sourced deployment from Jun 24, 2026 (first Vercel read only; the second read also saw an Active Branches card listing `main`, not reconciled), so a push to `main` is not expected to redeploy the frontend; confirm in the project's Git settings. The `deploy-staging` step in `ci.yml` is labelled "Vercel auto-deploys on push" and checks the frontend only for HTTP 200/307, and the Vercel runbook in `docs/staging_env_map.md` describes an "import the repo" setup, which does not match a project with Git not connected. The live frontend is the Jun 24 build while the backend runs `main` at `d86798a` (the Render commit is a first-read observation). By committer date, 3 commits touch `frontend/` after that build (baseline: the last commit before it; 15 files differ), but only one, `6bfaae6` (2026-07-02, 13 files, the R6 trust-surface UI), changes application code; the other two touch only `frontend/vercel.json` and `frontend/.gitignore` (Vercel-CLI setup files, probably already in the upload). Those 13 files post-date the build, so the live frontend cannot include them. Either connect the repository or correct the comment and the runbook, and redeploy if the frontend should match `main`.
4. **Check the Cloudflare account Audit Log for token events.** Look for create/roll events for `atlas-staging-backend`; it may settle whether the token was ever rolled (check 4d). Whether it records R2 token rolls is untested.
5. **Check the latest `deploy-staging` run on `main` in GitHub Actions.** A green run (deploy hook returned 2xx; health poll, frontend check and the 3-role login smoke test passed) is the strongest indirect signal that the three secret values are current and that the demo seed password was left at its default. Render showing the newest commit on `main` in the local clone (`origin/main`, as last fetched) live with auto-deploy Off also implies a deploy happened since that commit (first Render read only). The run's timing relative to the cold-start probe also bears on the ordering caveat in 3d.
6. **Owner-only confirmations (they need access to the values).** The items in section 3.1 that only someone holding the values can check: `JWT_SECRET` is a 64-hex-character random value (audit F-09); Render's `DATABASE_URL` equals the Neon string and carries `sslmode=require`; `LLM_API_KEY` is the live OpenRouter key; `STORAGE_ENDPOINT_URL` equals the R2 endpoint; the key id and secret in Render belong to the active R2 token (a successful receipt upload after rotation is the practical test); the three GitHub secret values are current (follow-up 5 is the indirect signal).
7. **Reconcile the documented severity of the R2 rotation.** `docs/staging_env_map.md` still calls it "optional, non-blocking" and "ROTATE AFTER DEMO", while `docs/env_audit_p8_t3.md` (F-13) rates it High and required. Also cross-link this report from `docs/env_audit_p8_t3.md`, whose header and section 5 still say nothing was verified against a live dashboard.

No action is needed for: Render auto-deploy (Off, as intended), the 16 Render literals (no visible drift), the Neon schema and seed, or the three GitHub secret names.

## 5. Deviations from the read-only rules, and side effects

No configuration, secret or deployment setting was changed in any dashboard, and nothing was saved by hand. The only records created were Neon's six automatic History entries (item 5), and waking the sleeping backend ran its start-up entrypoint (item 6). The following incidental interactions and side effects did occur.

1. **Secret rows.** There is no evidence that any secret row's value was revealed (the sessions' own reports and the click-summary audit agree; screenshots cannot be machine-checked). Only the 16 non-secret Render literals and `CORS_ALLOWED_ORIGINS` were revealed, one row at a time through that row's own eye control and re-hidden, and the page was reloaded afterwards. No reveal-all control was used. Two reveal batches in the first read hit a screenshot timeout; the rows were confirmed masked again and the reveals redone.
2. **Vercel click-to-copy of a variable name (twice, one per Vercel read).** Each Vercel session clicked the variable-name text of one row, expecting it to expand the row. That text is a click-to-copy control; a "Copied!" toast appeared. Only the non-secret name `NEXT_PUBLIC_API_BASE_URL` can have been copied, no value was involved and nothing changed in Vercel, but the local clipboard contents were probably replaced by that name. The second read also clicked a blank spot on a variable row (no effect).
3. **Vercel row menus, tab and filter (first read only).** The "…" menu of both `NEXT_PUBLIC_API_BASE_URL` rows was opened to see whether a reveal option exists, read, and closed with Escape without choosing an item. The Shared tab was opened and an "Environment: Production" filter chip applied on the deployments list (both view-only).
4. **Render UI-only controls.** "Show more" (expands the list), the left-navigation collapse toggle (collapsed and re-expanded), and focusing read-only name fields to move the caret with End and arrow keys (no typing) to read two ellipsis-truncated names. One of those fields is the name field of the secret row `STORAGE_SECRET_ACCESS_KEY`; the value field and eye control of that row were not touched. The second read also reported clicking a key label box once and pressing End, and wheel-scrolling horizontally over one key box, without recording which row; the first read also pressed End and ctrl+End and clicked a blank page margin. All of these were focus or scroll only: no typing, and no value field or eye control reported.
5. **Neon.** The Connect dialog was opened; its Role and Database dropdowns were opened and closed without selecting anything; the "Connection pooling" switch was toggled off and back on (the switch changes the displayed string; no save control was used) before the dialog was closed with its X. The six SELECT statements were typed into the SQL Editor and run from the keyboard; Neon's editor automatically recorded six auto-titled entries in its History panel (nothing was saved by hand) and the editor was left empty on an unsaved "Untitled" tab. The password-hash column was never selected.
6. **Cold start.** The requested `/health` check woke the free-tier Render instance from sleep, which ran the container entrypoint (including `alembic upgrade head`) as part of that start; see the ordering caveat in 3d.
7. **Supplementary network requests (outside the five checks; none changes configuration).** One CORS preflight pair (an allowed and a foreign origin) to the staging backend, and plain GETs of the public Production site's pages and script files. A request to a sleeping instance starts it (see item 6).
8. **Tabs.** A new tab group was created for the run. The seven browser sessions each opened one tab, and the first blank tab was used for the pre-flight and the health check. All tabs were left open as instructed.

## 6. Method and evidence quality

Seven read-only browser sessions ran strictly one at a time (they share one Chrome), and none saw another's result. The two Render sessions were given the allow-list and secret-row names but not the expected values; the Neon session was given the three expected demo emails in its SQL; Neon, R2 and GitHub were each read once. Returned strings were compared to `render.yaml` and the audit spec by code; the Vercel timestamp comparison was done by hand.

| Session | Target | Tool calls | Outcome |
|---|---|---|---|
| render-A | Render environment + Settings | 75 | complete |
| neon | Neon console + SQL Editor | 49 | complete |
| r2 | Cloudflare R2 | 12 | complete |
| github | GitHub Actions secrets | 10 | complete |
| vercel-A | Vercel project | 36 | complete |
| render-B | Render environment (second read) | 47 | complete, matches render-A |
| vercel-B | Vercel project (second read) | 34 | complete, matches vercel-A on the compared fields |

- **Audit of the sessions' own conduct.** Every call of all seven sessions was categorised from tool names and action summaries (page content and screenshots were not printed). Tools used: navigation, screenshot, zoom, scroll, hover, click, key, wait, page-text, find, accessibility-read and tab tools. Typing happened only in the Neon SQL Editor, and every typed statement was a SELECT. No script execution, form input, network or console reading, or file upload occurred. Every Render click summary that names a variable names one of the 17 allow-listed rows, and no click summary names a secret row by its full name; the contact with a secret row identified from click summaries is the focusing of a truncated name field, disclosed in section 5 item 4, and the second read also reported two name-box interactions without recording the row (name fields only; no value field or eye control reported). Both rest on the sessions' own reports. Neon's page-text reads all happened after the SQL Editor was opened.
- **Secret-shaped strings.** The text results of all seven sessions were scanned for connection-string schemes, the Render deploy-hook path and the query-key form, API-key prefixes, GitHub and AWS tokens, JWTs, Bearer tokens, long hex and long tokens (40+ characters). The only hits of the long-hex and long-token patterns were the non-secret Cloudflare account id and a hyphenated Vercel deployment hostname. Screenshots cannot be scanned mechanically; no session reported an unmasked secret on screen. This report file was scanned for the same patterns and for the real values and fragments of every masked identifier: 0 hits.
- **Limits.** Both Render reads and both Vercel reads use the same automated method, so they can share blind spots; the match of values to the spec is the stronger evidence, because the two Render readers did not know the expected values. Render names were read visually (see check 1); leading or trailing whitespace in values is invisible (section 3.2); Neon's branch was confirmed by the id in the URL path and by it being the only branch; R2 dates come from a date-only list column.

## 7. Closure (2026-11-05)

Appended after remediation. Sections 1–6 above are the dated 2026-10-29 evidence and are unchanged. The re-reads below were done read-only in the browser on 2026-11-04 and 2026-11-05; identifiers are masked as above.

**Check 2a, re-read: PASS.** On 2026-11-04 the two Sensitive rows were deleted and `NEXT_PUBLIC_API_BASE_URL` was re-created as one **Config** variable for Production and Preview, typed from the repo spec while Git was still disconnected (so no build could run in the gap). The dashboard now reveals the value `https://atlas-backend-tsf8.onrender.com/api/v1`: ends exactly in `/api/v1`, no trailing slash. Revealing it needed one emailed verification code, entered by the owner.

**Check 4, re-read: ACTION CLOSED.** The token list shows exactly one Account API token, `atlas-staging-backend-2026-11`: Object Read & Write, scoped to `atlas-receipts-staging`, issued Nov 4, 2026 (after the 2026-06-24 cutoff), active until Nov 4, 2027. The leaked `atlas-staging-backend` is gone. Rotation used create-swap-delete: the new key pair was swapped into Render in one edit, a receipt upload succeeded, the old token was revoked at 17:19 PDT, and the bucket's third object (875 kB, uploaded at 17:3x PDT) was written after the revoke, so nothing still depends on the old token. Before the rotation the bucket held only the two P8-T2 objects (23–24 Jun) and the 30-day metrics showed no unexplained writes.

**Audit log findings.** The classic Cloudflare audit log retained events back to June 2026. From 2026-06-01 to 2026-11-04 it shows: Jun 23 bucket create and original token create; Jul 23 a system-created notification policy (actor Cloudflare, not a token event); Nov 4 token create and token revoke by the owner. There is **no roll or update of the old token and no unrecognised actor**. Seeing the create and revoke events confirms the audit log records R2 token lifecycle events, which closes the "untested" caveat in 4d and follow-up 4.

**Disposition of the section 4 follow-ups**

| # | Follow-up | Disposition |
|---|---|---|
| 1 | Rotate the R2 token (required) | Done 2026-11-04 (above). |
| 2 | Make `NEXT_PUBLIC_API_BASE_URL` auditable | Done 2026-11-04: re-created as Config; 2a now PASS. |
| 3 | Reconcile the Vercel deploy path with the CI comment and runbook | Done: Git connected 2026-11-04 (Vercel for GitHub, this repo only; Root Directory `frontend`; production branch `main`); Deployment Checks on `backend`, `frontend`, `eval-db`, `e2e`, `docker-build`; GitHub ruleset `protect-main` requires a PR and the same five checks; `ci.yml` comment and `docs/staging_env_map.md` corrected. |
| 4 | Check the Cloudflare audit log | Done (above). |
| 5 | Check the latest `deploy-staging` run on `main` | Post-merge: verified with the first Git-sourced deployment of this change. |
| 6 | Owner-only confirmations (section 3.1 values) | Owner-confirmed; not inspectable read-only. |
| 7 | Reconcile the documented R2 severity | Done: `docs/staging_env_map.md` and `docs/env_audit_p8_t3.md` (F-13 resolved) updated. |

**After the merge.** The post-merge checks cannot be in this commit because they happen after it: Vercel holding and then promoting the merge deployment, the `/login` bundle containing only the expected backend URL, the R6 trust-surface UI being live, the three demo-role logins, and preview behaviour under single-origin CORS (expected to be UI-only). They are recorded as the opening "P8-T3 closure confirmation" entry of the P8-T4 phase log, which is P8-T4's entry probe.

### 7.1 Owner confirmations (2026-11-07)

Outcomes only; no value or fragment was viewed by the verifier or recorded here.

| Value | Outcome |
|---|---|
| `STORAGE_ENDPOINT_URL` equals the R2 endpoint | Proven by the A3 upload (2026-11-04). |
| R2 key pair belongs to the active token | Proven by the A5 upload after the old token was deleted (2026-11-04). |
| Three GitHub secret values current | Proven by CI run #108 on `e66e84d` (2026-11-05): deploy hook 2xx, health, frontend, three-role login all green. |
| `LLM_API_KEY` is the live OpenRouter key | Rotated by the owner on 2026-11-07 (old key had appeared in local shell history; that line was removed). |
| `JWT_SECRET` is 64 random hex characters (F-09) | Rotated by the owner on 2026-11-07 (`openssl rand -hex 32`); service restarted healthy, auth path answering. |
| `DATABASE_URL` points at the Neon branch, TLS | Confirmed indirectly: Render's startup log host matches the Neon `production` compute endpoint (the project's only branch), and Neon accepts only TLS connections. The literal `sslmode` suffix and database name were not viewed. |

With these, every item in section 3.1 is resolved or owner-confirmed, and the P8-T3 closure runbook is complete.
