# Runbook — key rotation and the rotation recheck

A checklist for each secret Atlas uses: where it lives, how to rotate it, the
order of steps, how to verify the change without printing the value, and how
to roll back. **Every step is an owner action**: the provider dashboards are not
reachable from the repository or CI.

Rotation history from the project notes:
- 4–7 Nov 2026: the R2, LLM (OpenRouter), JWT and DB keys were rotated.
- Render has its own OpenRouter key.
- The LLM / OCR / embeddings keys in the local `.env` now return 401, so they
  are dead and must be replaced locally if they are needed.
- The Novita key is still valid.

Treat this list as unverified until you complete the recheck in section 3.

## 0. Ground rules

- **Never print a value.** To check that a variable is set, print its length or
  a short hash prefix, never the value:
  ```bash
  python3 -c 'import os,hashlib; v=os.environ["JWT_SECRET"]; print(len(v), hashlib.sha256(v.encode()).hexdigest()[:8])'
  ```
  Compare the hash prefix with the one you noted when you set the value.
- Paste new values with `read -rs NAME && export NAME`. That keeps them out of
  shell history and off the screen.
- Keep the **previous** value in your password manager until the post-rotation
  smoke test passes. That is your rollback. Delete it after 7 days.
- Rotate one secret at a time and run the smoke test (section 2) after each one.
- A secret that ever reached git, a log, an artifact or a chat **must** be
  rotated. Do not allow-list it in `scripts/ops/secret_scan_allowlist.txt`.

## 1. Per-secret checklists

### JWT_SECRET (Render)
- **Where:** Render → `atlas-backend` → Environment (`sync: false`).
- **Rotate:**
  1. Generate a value with `python3 -c 'import secrets; print(secrets.token_hex(32))'`.
     It must be 32+ characters. Production refuses a short or placeholder value
     at startup, and staging logs a warning.
  2. Set it in Render, then **Save, rebuild and deploy**.
- **Effect:** every access and refresh token becomes invalid, and all users must
  sign in again. There is no data loss.
- **Verify:** the backend `/health` endpoint reports the new commit with status
  ok, the 3-role login smoke passes (section 2), and the Render log has no
  `JWT_SECRET` warning.
- **Roll back:** restore the previous value and redeploy.

### Database password (Neon → Render `DATABASE_URL`)
- **Where:** Neon → project → Roles → the app role; Render `DATABASE_URL`; your
  local, git-ignored `.staging_handoff.json`.
- **Order:**
  1. In Neon, **Reset password** for the role and copy the new connection string.
  2. Immediately update `DATABASE_URL` in Render (keep `?sslmode=require`), then
     deploy.
  3. Update `.staging_handoff.json` locally.
  The old password stops working as soon as you reset it, so update Render right
  away. Expect a short outage between the reset and the deploy.
- **Verify:** `/health` is ok, the login smoke passes, and the Render log shows no
  `password authentication failed`.
- **Roll back:** a reset cannot be undone. Reset again and update Render.

### R2 access keys (`STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`)
- **Where:** Cloudflare → R2 → Manage API tokens; Render env.
- **Order:**
  1. Create a **new** token: *Object Read & Write*, scoped to
     `atlas-receipts-staging`.
  2. Set both values in Render and deploy.
  3. Verify.
  4. **Then** revoke the old token.
  Creating the new token before revoking the old one means zero downtime.
- **Verify:** upload a receipt as the employee, open its image (presigned URL),
  and check that the Render log has no `AccessDenied`.
- **Roll back:** until you revoke it, the old token still works. Put its values
  back.

### OpenRouter / LLM key (`LLM_API_KEY`; OCR uses it as well)
- **Where:** openrouter.ai → Keys; Render `LLM_API_KEY`. `OCR_API_KEY` and
  `EMBEDDINGS_API_KEY` are empty on staging. OCR falls back to `LLM_API_KEY`.
- **Order:**
  1. Create a new key with a **credit limit**.
  2. Set it in Render and deploy.
  3. Verify.
  4. Delete the old key.
- **Verify:** ask the assistant a policy question (you should get a grounded
  answer, not an abstention caused by a provider error). Upload a receipt and
  check that OCR fills the fields. The Render log should have no 401 from
  openrouter.
- **Local `.env`:** the local LLM / OCR / embeddings keys already return 401.
  Replace them only if you need live local runs. CI never uses them, because
  every provider is `local` there.
- **Roll back:** put the old key back, if you have not deleted it yet.

### Novita (`RERANK_API_KEY`, local and keyed eval runs only)
- **Where:** novita.ai → Key management; your local `.env` only. It is not used on
  staging, where `RERANK_PROVIDER=local`.
- **Rotate:** create a new key, update `.env`, delete the old key.
- **Verify:** a short keyed rerank run. Respect the 30 requests/minute limit:
  throttle any live eval.

### Render deploy hook (`RENDER_DEPLOY_HOOK_URL`, GitHub secret)
- **Where:** Render → `atlas-backend` → Settings → Deploy Hook; GitHub → repo
  Settings → Secrets and variables → Actions.
- **Order:**
  1. In Render, **Regenerate** the hook. The old URL dies at once.
  2. Update the GitHub secret straight away.
- **Verify:** the next push to `main` shows `Render deploy hook response: HTTP 2xx`
  in the deploy-staging job. CI prints only the status code, never the URL.
- **Roll back:** none. Regenerate again.

### Render API key, Vercel tokens, Neon API key
- **Where:**
  - Render → Account settings → API keys;
  - Vercel → Account → Tokens;
  - Neon → Account → API keys.
  None of these is used by the repository or CI. They exist only if you created
  them for the CLIs.
- **Rotate:** create a new key or token, update your local CLI config, revoke the
  old one. Revoke any token you no longer use.

### GitHub tokens
- **`GITHUB_TOKEN`** is issued per CI run. You cannot rotate it, and nothing needs doing.
- **Personal access tokens:** GitHub → Settings → Developer settings → Tokens.
  Prefer fine-grained tokens scoped to this repository with an expiry. Revoke
  unused tokens.
- **Repository secrets to review:** `RENDER_DEPLOY_HOOK_URL`, `STAGING_BACKEND_URL`,
  `STAGING_FRONTEND_URL` and `STAGING_DEMO_PASSWORD`. The last one is new in
  phase 11. It is the demo password the post-deploy smoke logs in with. When it
  is unset, the smoke falls back to the published default and shows a warning.

### Demo account password (`SEED_DEFAULT_PASSWORD` / `STAGING_DEMO_PASSWORD`)
- The staging demo accounts deliberately use the **published** demo password,
  which is the code default and appears in `.env.example`. That is what makes
  the public demo usable.
- **Production** refuses to start with that value.
- **CI's E2E lane** uses a masked, throwaway password for each run.
- **To change it:**
  1. Run the seed's password resync out-of-band with the new
     `SEED_DEFAULT_PASSWORD` (`seed_all` only rehashes the seeded demo users).
  2. Set `STAGING_DEMO_PASSWORD` in GitHub to the same value.
  3. Update the login hint the demo shows.

## 2. Post-rotation smoke test

Run it after **every** rotation. It needs no secrets on your machine.

1. **Backend health:** `curl -fsS https://atlas-backend-tsf8.onrender.com/health`.
   The status should be ok and `commit` should be the expected SHA.
2. **Logins:** sign in on the frontend as employee, manager and finance. You can
   instead re-run the latest CI run's deploy-staging job, which performs the
   3-role login smoke and the CORS preflight pair.
3. **Receipts:** as the employee, upload a receipt and open its image. This
   exercises R2 and OCR.
4. **Assistant:** ask "What is the hotel nightly cap?" This exercises the LLM
   provider.
5. **Logs:** check the Render log for 401/403 errors from providers and for
   `[Atlas] staging:` warnings.

## 3. The rotation recheck (quarterly, and after any suspected leak)

1. **Repository scan.** Run `python scripts/ops/secret_scan.py`. It scans the
   working tree and the full git history and must report 0 findings. CI runs the
   same scan as the blocking `secret-scan` job.
   - If a real hit appears, rotate that secret (section 1), then purge it from
     history or accept it as revoked.
   - Never allow-list a real value.
2. **Configuration audit.** Run `python scripts/ops/env_audit.py`. It must report
   0 errors: secrets are `sync: false` and there are no unsafe literals or
   defaults.
3. **Dashboards.** For each provider, list the keys and tokens, delete the ones
   that are unused or unknown, and confirm that each key in use was created
   after the last rotation. Check:
   - Render env (names only);
   - GitHub Actions secrets (names and "updated" dates);
   - R2 API tokens;
   - OpenRouter keys and credit limits;
   - Neon roles;
   - Novita keys;
   - Vercel tokens.
4. **Artifacts.** Check GitHub → Actions → Artifacts. There must be no
   `playwright-report` artifact: phase 11 stopped producing it. Older ones may
   still exist, so delete any that remain. They contain tokens and the demo
   password. Rotate `JWT_SECRET` if a report from a staging run ever existed.
5. Record the date and what you rotated in your ops notes (names only).

**Cadence:** quarterly (JWT, R2, LLM, deploy hook), at once after any suspected
exposure, and whenever someone with dashboard access leaves.
