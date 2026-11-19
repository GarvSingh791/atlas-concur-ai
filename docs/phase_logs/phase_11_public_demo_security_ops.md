# Phase 11 — Public-demo security: ops (CI token leak, staging cleanup, key rotation)

Branch `ps/ops`, base `product-gaps` at `749469f` (alembic head
`0023_entry_release_lineage`; no migration added here). Deployed staging is still
`main` `2fbe114`. Nothing in this branch reached staging: the build environment had no access
to staging, Render, Vercel, Neon or GitHub. The deliverables are tools,
guards, tests and runbooks. Running them on staging is the owner's job
(`docs/runbooks/public-demo-checklist.md`).

## A. CI token leak

**What was wrong.** The `e2e` job ran `npx playwright test … --reporter=list,html`.
CLI flags override `frontend/playwright.artifact-policy.ts`, so the HTML reporter
was on. The job then uploaded `frontend/playwright-report/` with no retention
limit. That report records every request verbatim, including:
- the login POST body, carrying the seed password, which is also the staging
  demo password;
- the bearer and refresh tokens.

Other leaks:
- `identity_seed.py` printed the seed password into the CI and Render logs.
- The post-deploy smoke test hard-coded the demo password in `ci.yml`.
- On failure, the smoke test printed the first 300 characters of the login
  response body. A 200 response without an `access_token` would have printed
  live tokens.

**What changed**
- `.github/workflows/ci.yml`
  - `e2e`:
    - Playwright runs with no `--reporter` / `--trace` flags, so the policy
      applies: list reporter, with traces, screenshots and video off.
    - The `playwright-report` upload is removed. No Playwright artifact is
      uploaded at all.
    - The step deletes `.auth`, `playwright-report`, `test-results` and
      `e2e/.artifacts` before it exits.
    - A new first step mints `SEED_DEFAULT_PASSWORD=ci-<48 hex>` from
      `openssl rand`, `::add-mask::`s it and exports it through `GITHUB_ENV`. The
      seed hashes it, and `frontend/e2e/helpers.ts` now reads
      `process.env.SEED_DEFAULT_PASSWORD`, falling back to the dev default
      locally. The CI E2E lane therefore never uses the published demo password.
  - `eval-db`: the artifact is an explicit list of the three report files, with
    `retention-days: 3`.
  - `deploy-staging` smoke:
    - The password comes from the new `STAGING_DEMO_PASSWORD` secret through
      `env:`. If the secret is unset, the smoke shows a `::warning::` and uses
      the published default read from `.env.example`. The literal is not in the
      workflow.
    - The password is masked.
    - The JSON body is built by python from env vars, and the response body goes
      to a temp file.
    - Only `token`, or the error-envelope `error.code`, is ever printed, next to
      the HTTP status.
- `database/seeds/identity_seed.py` no longer prints the password.
- `backend/tests/test_ci_workflow_hygiene.py` parses every workflow and fails on
  any of these:
  - an `html`/`blob` reporter, `--trace` other than off, or video;
  - an `E2E_HTML_REPORT`, `E2E_TRACE` or `E2E_ALLOW_SENSITIVE_ARTIFACTS` env
    switch;
  - a hard-coded `SEED_DEFAULT_PASSWORD` in `env:`;
  - an `upload-artifact` without `retention-days` ≤ 3, or one that uploads a
    directory, a glob or a credential-bearing path (`.auth`, storage state,
    `.env`, Playwright report / test-results / trace, `.zip`, compose or log
    files);
  - `${{ secrets.* }}` inlined into a script;
  - an `echo`/`printf` of a secret env var;
  - a step calling `/auth/login` that prints `$BODY`, `$RESP` or `$TOKEN*`;
  - the literal published seed password;
  - a Playwright job without a masked, per-run `SEED_DEFAULT_PASSWORD`.

  It also asserts that the helper reads the env var. Each rule is proven by a
  synthetic bad workflow.
- `requirements-dev.txt` now pins `pyyaml`. It was already installed through
  `uvicorn[standard]`.

**History check.** `git log --all --name-only` shows no committed
`playwright-report`, `test-results`, `blob-report`, `.auth` or `.env` files, and
none exist in the working tree. Artifacts already uploaded by earlier CI runs
live in GitHub, not git. The owner must delete them and rotate `JWT_SECRET`
(public-demo checklist §4).

**Decisions**
- Traces are **off**, not `retain-on-failure`. The policy does not scrub traces,
  so a retained trace would still be a credential dump. The job log is the only
  E2E output.
- There is a per-run random password rather than a fixed CI value. Nothing
  reusable exists to leak.
- The smoke falls back to the published default instead of failing when the
  secret is missing. Failing would break deploys until the owner adds the secret;
  the fallback only warns.

## B. Staging test-data cleanup tool

`database/seeds/cleanup_test_data.py` is run as
`python -m database.seeds.cleanup_test_data --org "Atlas Demo Corp" [--apply] [--ids FILE]`.
The rules are documented in its docstring and in
`docs/runbooks/staging-test-data-cleanup.md`.

**Safety model**
- Dry-run is the default. It prints counts, ids and titles or purposes, with no
  emails or names.
- Writing needs both `--apply` and `CLEANUP_TEST_DATA_ENABLED=true`.
- `--org` is required and must match exactly one organisation. Every query is
  scoped to its id.
- The tool refuses when the seeded demo users (the baseline) are absent.

**Matching rules**
- Report titles or trip purposes that start with `E2E`, `QA`, `QA run`, `C7`,
  `C7R`, `UAT<n>` or `Playwright`, as a whole word and case-insensitive.
  `Qatar`, `C70` and `UATX` do not match.
- Records owned by `qa.`/`qa-`/`qa_`/`qa+`, `e2e…` or `playwright…` accounts.
  Seeded demo users never count as test accounts.
- An `--ids` file.

**Protections.** These apply to explicit ids as well.
- Anything created at or before the baseline (the earliest seeded demo user).
- Seeded report titles and trip purposes owned by seeded users. These are
  mirrored from `seed_all.py`, and a test pins the mirror to the seed sources.
- Trips still linked from a kept report, by `trip_id` or an entry's segment, so
  that no kept row is changed by an FK `SET NULL`.
- Seeded receipts (`receipts/seed/…`).

**Deletion.** Each report, trip or user group runs in its own transaction, in
FK-safe order:
1. notification read-markers and idempotency keys;
2. approvals (steps go by cascade);
3. non-seed receipts;
4. entries;
5. the record.

A failed group rolls back alone and is reported, and the tool exits 1. Its
counters and storage keys are merged only after it commits, so a rolled-back
group never loses its receipt images. Storage deletion is best effort and runs
after the commits. A single `ops.test_data_cleanup` audit row (actor system)
records the counts and ids. Audit rows are never deleted. A re-run is a no-op.

**Decisions**
- **Test users are deactivated, never deleted.** Deleting a user would
  `SET NULL` `audit_log.actor_id` and so rewrite the immutable audit trail. Their
  receipts, card transactions, calendar events and conversations are deleted.
- **Demo card transactions matched to a deleted test entry are kept.** The FK sets
  their `expense_id` to NULL, and `matched` is left as it was. The boot top-up
  (`topup_demo_card_feed`) keeps the feed usable either way.
- **The baseline constants live in the tool, not in `seed_all.py`.** The seeds
  are a shared hot file, and the drift test covers the risk.
- **There is no "production host" heuristic.** The two confirmations, the
  required `--org` and the baseline check are the guard, as the brief allows.

## C. Key-rotation recheck tooling

- **`scripts/ops/secret_scan.py`** uses only the standard library, because
  `gitleaks` is not installed.
  - **Scope:** tracked files, plus untracked files that are not git-ignored (a
    local `.env` is never read), plus every line added in `git log --all -p`.
  - **What it looks for:** OpenRouter, OpenAI, Anthropic, Google and Hugging Face
    keys; AWS and R2 access keys and secrets; JWTs; Postgres URLs with a
    password; private keys; GitHub, Render, Neon and Vercel tokens; the Render
    deploy hook; and high-entropy values assigned to secret-named variables.
  - **Output:** `file:line`, the pattern, a 4-character masked prefix with the
    length, and a fingerprint. Values are never printed.
  - **Allow-list:** `scripts/ops/secret_scan_allowlist.txt`. A `value:` rule must
    match the **whole** value. `path:` and `fingerprint:` rules are also
    supported.
  - **Result on this repository:** 0 working-tree findings and 0 history
    findings. Before the allow-list there were 61 findings; all of them were
    placeholders (`change-me`, `${VAR}`, `<password>`, single-letter test
    passwords, CI stubs that say "not-a-real-key").
- **`scripts/ops/env_audit.py`** parses `Settings` with `ast` (so it needs no env)
  and compares it with `.env.example`, `render.yaml` and
  `docs/staging_env_map.md`.
  - **ERROR** findings:
    - a secret set as a `render.yaml` literal;
    - a missing required secret, or a missing provider secret for a provider that
      `render.yaml` selects;
    - debug on, a `*` CORS origin or a placeholder literal in `render.yaml`;
    - a `render.yaml` key the app does not read;
    - an unsafe code default.
  - **WARN** findings:
    - documentation drift;
    - staging running on the published demo password (intentional).
  - Result after fixing the drift it found: 0 errors and 1 intentional warning.
    The drift was `POLICY_QA_CRITIC_TEMPERATURE`, which was added to
    `.env.example`, plus 6 later fields, which were added to the env map as
    table D2.
- **CI:** a new `secret-scan` job (full-history checkout) runs both tools and is
  **blocking**, because the repository was clean when the job was added.
  `deploy-staging` now needs it.
- **Startup guard** (`config/settings/base.py::deployment_secrets_are_safe`)
  - With `APP_ENV=production`, startup is **refused** (`get_settings` exits 1)
    when any of these hold:
    - `JWT_SECRET` is under 32 characters, contains a placeholder marker
      (`change-me`, `your-`, `not-a-real`, `do-not-use`, `example`, `dummy` …), or
      has fewer than 10 distinct characters;
    - `SEED_DEFAULT_PASSWORD` is the published one;
    - `APP_DEBUG=true`;
    - `CORS_ALLOWED_ORIGINS` contains `*`.
  - `staging` logs one warning per finding.
  - `development` is unchecked.
  - Messages name the variable, never the value.
  - **Decision:** staging only warns. Its demo accounts use the published
    password on purpose, and a refusal would take the demo down. Production has
    no deployment yet, so the extra debug and CORS refusals cannot break
    anything live.
- **Runbooks:**
  - `docs/runbooks/key-rotation.md`: per secret, where it lives, the order of
    steps, verification without values, rollback, the smoke test, the quarterly
    recheck and artifact cleanup;
  - `docs/runbooks/public-demo-checklist.md`: the owner's go-live list for the
    whole round, with [OWNER] / [REPO] marks;
  - `docs/runbooks/staging-test-data-cleanup.md`.

## Limits

- Nothing was run against staging. The cleanup, the deletion of old
  `playwright-report` artifacts, the `STAGING_DEMO_PASSWORD` secret, the
  dashboard key review and the dead local `.env` keys are owner actions.
- Rotation itself cannot be checked from here. The scanner proves that no secret
  is in git. It cannot prove that a provider key was rotated.
- The secret scanner is pattern- and entropy-based. A secret with an unusual shape
  in a variable that does not look like a secret can slip through.
- The hygiene test is a static check of workflow text. A future script that leaks
  in a way the rules don't model (for example `set -x` around a secret) would pass.
  `::add-mask::` is the second line of defence.
- The E2E job log still contains test titles and any assertion messages. Those
  could include an access token if a future test prints a response, but the
  token would be useless outside the throwaway CI database.
- The cleanup tool matches by title or owner. A test record with an ordinary
  title owned by a demo user is only removed if it is listed in `--ids`.
- Two stray files are tracked at the repository root (`scan.sh`, a local scan
  helper, and `new_msg.txt`, an old commit-message draft). Neither holds a
  secret. They are left for the owner to delete, because they are outside this
  item's scope.

## How to verify

```bash
R=<local-runner-dir>/ps/run_ops.sh   # isolated DB runner
$R -q backend/tests/test_ci_workflow_hygiene.py backend/tests/test_ps_ops_settings_guard.py \
      backend/tests/test_ps_ops_env_audit.py backend/tests/test_ps_ops_secret_scan.py
$R -q -m db backend/tests/test_ps_ops_cleanup_db.py backend/tests/test_ps_ops_seed_output_db.py
python scripts/ops/secret_scan.py      # 0 tree, 0 history findings
python scripts/ops/env_audit.py        # 0 errors, 1 warning
```

Gates on the final tree:
- hermetic: 3556 passed, 4 skipped;
- DB: 1756 passed, 8 skipped;
- eval-db: 48 passed;
- ruff check and `ruff format --check`: clean;
- mypy: clean;
- frontend: vitest 821 passed in 113 files; type-check, lint and `format:check`
  are clean.

Mutation proofs:
- The hygiene test turns red when any of these happen:
  - `--reporter=list,html` is re-added;
  - `retention-days` is dropped;
  - the `add-mask` is dropped;
  - the login body is printed;
  - the helper password is hard-coded.
- The settings guard turns red when the raise, the seed-password check or the
  placeholder check is removed.
- The env audit turns red when the secret-literal, provider-secret,
  required-secret or unread-key rule is removed.
- The scanner turns red when the allow-list uses `search` instead of a full
  match, when output is unmasked, when a pattern is dropped, or when history
  scanning is off.
- The seed-output test turns red when the old print is restored.
- The cleanup tool turns red when any of these is removed or broken:
  - the baseline check;
  - the seeded-title check;
  - the env confirmation;
  - the linked-trip check;
  - the org scope;
  - seeded-receipt protection;
  - deletion of trip approvals;
  - deactivation instead of deletion;
  - the baseline refusal;
  - merge-on-commit of storage keys.
