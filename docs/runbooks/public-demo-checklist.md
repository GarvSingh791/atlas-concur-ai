# Public demo — go-live checklist (phase 11)

This is the owner's checklist for exposing the staging demo publicly. It covers
all of the phase 11 "public-demo security" work. **[OWNER]** marks a step only
you can do, because it needs a dashboard or production access. **[REPO]** marks
work that is in the code once the phase 11 branches are merged.

Phase 11 was built on parallel branches that are merged afterwards. Items 1–3 and
7 come from the other branches. Check each one against that branch's phase log
in `docs/phase_logs/phase_11_*.md`. This file describes them only at the level
of "what must be true". Items 4–6 are covered in detail in
`phase_11_public_demo_security_ops.md`.

## 1. Rate limits (login, user creation, the API) — other branch
- [x] [REPO] The merged branch's phase log describes the limits and their tests
      (`phase_11_public_demo_security_ratelimit.md`, `phase_11_rate_limit_hardening.md`).
- [x] [OWNER] After deploying, repeated wrong-password logins from one client are
      throttled, and a normal login still works. *Verified on live staging
      2026-11-17: the 6th wrong attempt for one email from one network returned
      429 with `Retry-After`; the three demo logins work (the deploy smoke runs them).*
- [x] [OWNER] The limits are configured for staging. They use the code defaults
      (nothing is overridden in `render.yaml`); `env_audit.py` reports 0 errors.
- [x] [OWNER] `TRUSTED_PROXY_COUNT=1` is correct for Render. *Verified on live
      staging 2026-11-17 (see the phase 11 log): left-hand `X-Forwarded-For` values
      do not reset a budget, and the same email from a second network (a GitHub
      Actions runner) was not throttled while the first network was, so visitors
      do not share one bucket.*

## 2. Upload limits (size and type) — other branch
- [x] [REPO] Oversized and wrong-type receipt uploads are rejected, with tests.
- [x] [OWNER] On staging, a normal receipt (PNG or JPEG) still uploads and OCRs.
      *Verified on live staging 2026-11-17 with the two sample receipts in
      `docs/assets/sample-receipts/`.*

## 3. Shared demo accounts (reset / read-only finance) — other branch
- [x] [REPO] Demo mode behaviour as described in that branch's phase log
      (`DEMO_MODE=true` on staging; the five seeded accounts and seeded rules are protected).
- [x] [OWNER] Decide the demo password. **Decision (2026-11-17): keep the
      published default** (`change-me-seed-password`) on staging. The accounts hold
      only synthetic data, the demo organisation is reset daily, and recruiters
      need to sign in without asking. Production would refuse it at startup. If
      this ever changes, follow "Demo account password" in `key-rotation.md` and
      set the `STAGING_DEMO_PASSWORD` GitHub secret.

## 4. CI hygiene (no credentials in artifacts or logs) — done in this branch
- [x] [REPO] The E2E lane uses the artifact policy's list reporter, with no
      HTML report, traces, screenshots or video, and uploads no Playwright
      artifact. It runs with a masked throwaway password for each run.
- [x] [REPO] The eval artifact is an explicit file allow-list kept for 3 days.
- [x] [REPO] The deploy smoke never prints login bodies. It reads the demo
      password from `STAGING_DEMO_PASSWORD`, falling back to the published
      default with a warning.
- [x] [REPO] `backend/tests/test_ci_workflow_hygiene.py` fails CI if any of
      this regresses.
- [x] [OWNER] GitHub → Actions → Artifacts: delete every old
      `playwright-report` artifact (22 deleted), then rotate `JWT_SECRET`
      (see `key-rotation.md`). Done by the owner; the secret was rotated after the artifact cleanup.
- [x] [OWNER] ~~Add the `STAGING_DEMO_PASSWORD` repository secret.~~ Not needed:
      the demo password is the published default and the deploy smoke falls back
      to it (decision in item 3). Set the secret only if the password is rotated.
- [ ] [OWNER] Optional: add `secret-scan` to the Vercel Deployment Checks.

## 5. Old test data on staging — tool in this branch
- [x] [OWNER] Follow `docs/runbooks/staging-test-data-cleanup.md`. Done by
      2026-11-15: backup branch, dry-run, apply (46 test reports and 13 trips and
      their data removed), post-checks; the two backup branches were deleted at the
      owner's request afterwards. The boot-step form (`TEST_DATA_CLEANUP`) is off.

## 6. Key rotation recheck — tools in this branch
- [x] [REPO] `scripts/ops/secret_scan.py`, which scans the tree and full history,
      found 0 findings and runs in CI as the blocking `secret-scan` job.
- [x] [REPO] `scripts/ops/env_audit.py` found 0 errors and runs in the same job.
- [x] [REPO] Production refuses to start with a weak or placeholder `JWT_SECRET`,
      the published seed password, `APP_DEBUG=true` or a `*` CORS origin.
      Staging logs a warning instead.
- [ ] [OWNER] Run the recheck in `key-rotation.md` §3: review the dashboard
      keys, delete unused ones and record the dates. *Open: the OpenRouter
      workspace still holds an unlimited key idle for 2+ months and a few old
      keys; see item 7 of the owner list for the spend caps.*
- [ ] [OWNER] Replace or remove the dead (401) LLM / OCR / embeddings keys in
      your local `.env`.

## 7. README and LICENSE — done (documentation pass, 2026-11-15)
- [x] [REPO] `README.md` is current for phases 9–11 and `LICENSE` (MIT) is
      present. MIT is a default choice; swap it if you prefer another licence.
- [x] [OWNER] Check that the README's demo login section matches the password
      decision in item 3. *Checked 2026-11-17.*

## Go-live gate
- [ ] All [OWNER] boxes above are ticked.
- [ ] The CI run on `main` is green, including `secret-scan`, and deploy-staging
      verified the commit, the 3-role login and CORS.
- [ ] The post-rotation smoke test in `key-rotation.md` §2 passes.
