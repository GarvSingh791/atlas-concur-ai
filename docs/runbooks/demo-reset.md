# Demo reset

Last updated: 2026-11-15 (phase 12 follow-up: scheduled reset; phase 11, public-demo security)

## What it does

`python -m database.seeds.reset_demo_data` puts the shared demo organisation
("Atlas Demo Corp") back to the seed baseline. Visitors share the demo accounts,
so over time they leave junk data, change seeded records, archive rules and add
users. The reset clears all of that. Run it by hand (this page), or turn on the
backend's own **scheduled reset** (see [Scheduled reset](#scheduled-reset-in-process)).
The schedule is OFF by default.

An apply runs in **one database transaction**. Either all of it happens or none of it:

1. It deletes the demo org's trips (with their segments), expense reports, expenses,
   receipts, approvals (with their steps), card transactions, calendar events, AI
   conversations (with their messages), idempotency keys and notification read
   markers.
2. It restores the five seeded accounts to their seed values: name, role,
   department, manager, active status, grade and password (`SEED_DEFAULT_PASSWORD`).
   A missing account is re-created. Then it deletes every other user in the demo org,
   which are the users visitors created.
3. It restores the seeded policy rules to their seed definition and status. A deleted
   one is re-created. Then it deletes visitor-authored rules. Each restored rule gets
   a `policy_rule.demo_reset` audit row, so the rule history shows it.
4. It re-runs the idempotent seed steps: the sample trip, card feed, calendar,
   receipts, and reports with their approvals. It leaves the policy corpus and its
   RAG index alone, so no reindex is needed.
5. It writes one `demo.reset` audit row with the counts.

After the commit, it deletes the storage objects of deleted visitor receipts through
the configured storage provider (local, MinIO or R2). This step is best-effort: a
storage failure is reported, but the database reset stays in place. The seed's
`receipts/seed/...` images are never deleted.

**Kept on purpose:** `audit_log` and `ai_decision_logs` are append-only, so the reset
keeps them, including rows about deleted records. When a deleted visitor account was
the actor, the row's `actor_id` becomes NULL through the foreign key's own
`ON DELETE SET NULL`.

## Safeguards

| Safeguard | Effect |
|---|---|
| Dry run is the default | With no `--apply`, the tool runs in a `READ ONLY` transaction and prints the counts per table it **would** delete or restore. It cannot write. |
| `--apply` needs `DEMO_RESET_ENABLED=true` | The flag is read from the process environment, never from a `.env` file. Set it on the one-off command or job, or on the web service **only** when the scheduled reset is turned on (it is that schedule's own safeguard too). |
| `--apply` needs `--org "Atlas Demo Corp"` | The name must match exactly, including case and spaces. Exactly one organisation may have that name. |
| Tenant isolation, check 1 | Before any write, the tool walks every foreign key in the schema. If a row of another organisation references a row the reset would delete, the run is refused. |
| Tenant isolation, check 2 | The tool hashes every other organisation's rows in every table before and after. Any difference rolls the whole reset back. |
| Concurrency | The demo org row is locked for the run, so two resets cannot overlap. |

Exit codes: `0` success (or dry run), `2` refused (nothing written), `1` failed
(rolled back).

## Commands

Dry run first, every time:

```bash
python -m database.seeds.reset_demo_data
```

Then apply:

```bash
DEMO_RESET_ENABLED=true python -m database.seeds.reset_demo_data --apply --org "Atlas Demo Corp"
```

Example dry-run output:

```
Demo reset (dry-run) — organisation 'Atlas Demo Corp'
  WOULD delete     14  trips
  ...
  WOULD delete      3  users (visitor-created)
 WOULD restore      2  seeded accounts
 WOULD restore      1  seeded policy rules
  (the seed's own rows are part of the delete counts; an apply
   re-creates them.  Nothing was written.)
```

## Running it against staging (Render + Neon)

The command needs the **same environment as the backend**: `DATABASE_URL` (Neon),
`JWT_SECRET`, `SEED_DEFAULT_PASSWORD` if the service sets it, and the `STORAGE_*`
variables (R2) so visitor receipt images are deleted too. The backend image already
contains `database/`, `services/` and `config/`.

**Option A: Render Shell** (only on instance types that offer a shell). Open the
`atlas-backend` service, go to **Shell**, and run the two commands above. The shell
already has the service's environment. `DEMO_RESET_ENABLED=true` is set inline on
the apply command only.

**Option B: Render one-off job** (via the Render API, using the service's image
and environment):

```bash
JOBS="https://api.render.com/v1/services/<atlas-backend-service-id>/jobs"
# 1. dry run — read the job log afterwards
curl -X POST "$JOBS" -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"startCommand": "python -m database.seeds.reset_demo_data"}'
# 2. apply
curl -X POST "$JOBS" -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"startCommand": "env DEMO_RESET_ENABLED=true python -m database.seeds.reset_demo_data --apply --org \"Atlas Demo Corp\""}'
```

**Option C: from an operator machine** with a checkout of the deployed commit and
the staging values exported in the shell. They are never written to a file in the
repo:

```bash
export DATABASE_URL=... JWT_SECRET=... STORAGE_PROVIDER=minio STORAGE_ENDPOINT_URL=... \
       STORAGE_BUCKET=atlas-receipts-staging STORAGE_ACCESS_KEY_ID=... STORAGE_SECRET_ACCESS_KEY=...
python -m database.seeds.reset_demo_data
DEMO_RESET_ENABLED=true python -m database.seeds.reset_demo_data --apply --org "Atlas Demo Corp"
```

If the `STORAGE_*` values are missing, the local storage provider is used. Visitor
images then stay in R2, which costs space but is harmless. The database reset is
complete either way.

## Scheduled reset (in-process)

The backend can run the reset itself on a schedule. Render's free plan has no cron
service and no shell, so the scheduler lives inside the web service
(`backend/app/core/demo_reset_scheduler.py`). It calls the same `apply` function as
`--apply`, with the same safeguards and the same single transaction.

### Turning it on (Render dashboard, owner decision)

Add these environment variables to the `atlas-backend` service and redeploy.
`render.yaml` documents them but does **not** set them.

| Variable | Value | Meaning |
|---|---|---|
| `DEMO_MODE` | `true` | Required. The scheduler never runs outside demo mode. |
| `DEMO_RESET_ENABLED` | `true` | Required. The reset tool's own safeguard, read from the process environment. |
| `DEMO_RESET_SCHEDULE` | `interval` | Required. `off` (default) disables the schedule. |
| `DEMO_RESET_INTERVAL_HOURS` | `24` | Optional. Whole hours, at least 1. Default 24. |
| `DEMO_RESET_AT_UTC_HOUR` | e.g. `4` | Optional, 0-23. Resets at the first opportunity after 04:00 UTC, once per interval. Unset = every interval after the last reset. |

It also needs exactly one organisation named `Atlas Demo Corp`. The tool refuses
anything else, and nothing is written. An invalid value (an unknown schedule, an
interval below 1, an hour outside 0-23) **disables** the schedule and logs a
`Scheduled demo reset DISABLED — ...` warning at startup. A missing flag does the
same. The boot never fails because of these variables.

On a correct setup, the startup log shows `Scheduled demo reset ON | every 24h`.

### How it decides

- The scheduler wakes every minute. It reads the **last successful reset** from the
  newest `demo.reset` audit row of the demo organisation. The tool writes that row
  in the reset transaction, so only a committed reset counts. The schedule survives
  restarts, deploys and the free plan's sleep.
- It is due at `last reset + interval`. With `DEMO_RESET_AT_UTC_HOUR`, it is due at
  the first `HH:00 UTC` slot after the last reset (slots are `HH:00 UTC` plus whole
  intervals). With no reset on record, it is due at once.
- After a long sleep with missed slots, it runs **one** catch-up reset on wake, then
  waits for the next slot.
- Several instances: a Postgres advisory lock (`pg_try_advisory_lock`) lets one
  instance reset; the others skip. The due check is repeated under the lock, so an
  instance that gets the lock just after another one finished does nothing.
- A failure or refusal is logged (the exception type or the refusal reason, never a
  secret), counted, and retried no sooner than **15 minutes** later. It never stops
  the app.
- On shutdown, the scheduler is cancelled. A reset in flight gets up to 30 seconds to
  finish. If the process dies first, Postgres rolls the transaction back.
- Under pytest it never starts on its own. Tests start it explicitly.

### Brief inconsistency window

Requests are **not** blocked during a reset. The reset is one transaction, so other
requests see either the old data or the new baseline, never a half-reset state. A
visitor who is working while the reset commits may then see a `404` or `409` for a
record that was just removed. Their session stays valid only if their account still
exists: visitor-created accounts are deleted, and seeded accounts get the seed
password back. Pick an off-peak `DEMO_RESET_AT_UTC_HOUR` to keep this rare.

### What visitors see

`GET /api/v1/public/config` returns:

```json
{
  "demo_mode": true,
  "demo_reset": {
    "enabled": true,
    "interval_hours": 24,
    "next_reset_at": "2026-11-16T04:00:00Z",
    "last_reset_at": "2026-11-15T04:00:31Z"
  }
}
```

The demo banner then adds "Demo data resets daily — next reset ~HH:MM" in the
visitor's local time ("every N hours" for other intervals). With the schedule off,
`demo_reset.enabled` is `false`, the other fields are `null`, and the banner shows
only the general demo notice. There is **no** "Reset now" button anywhere in the app.

### Status for operators

`GET /api/v1/ops/demo-reset` (finance admins only; other roles get `403`) shows:
whether the schedule is on and why not, `next_reset_at`, `last_reset_at`,
`in_progress`, `resets_total`, `failures_total`, `consecutive_failures`,
`last_error`, `retry_after` and `skipped_locked`. The counters are per instance and
reset on restart. `last_result` holds the deleted and restored counts of the last
`demo.reset` audit row. Only a finance admin of the demo organisation sees it; it is
`null` for every other organisation.

### Turning it off

Set `DEMO_RESET_SCHEDULE=off`, or remove `DEMO_RESET_ENABLED`, and redeploy. Every
tick also re-checks `DEMO_MODE` and `DEMO_RESET_ENABLED`, so a closed gate stops the
next reset.

## Alternative: Render cron job (paid plans)

On a paid plan, a Render cron service is an alternative to the in-process schedule.
Use one or the other, not both: the cron job runs the tool directly and does not
consult the schedule or its advisory lock, so both would reset (the org row lock only
keeps the two from overlapping). The in-process schedule does see a cron reset through
its `demo.reset` audit row and waits a full interval after it. To schedule a
nightly reset this way, add a cron service to `render.yaml`. It uses the same image, and the image's ENTRYPOINT runs
migrations first. Do **not** set `SEED_ON_STARTUP` on the cron job.

```yaml
  - type: cron
    name: atlas-demo-reset
    runtime: docker
    repo: https://github.com/GarvSingh791/atlas-concur-ai
    branch: main
    dockerfilePath: ./docker/Dockerfile.backend
    dockerContext: .
    schedule: "0 4 * * *"          # 04:00 UTC daily
    dockerCommand: python -m database.seeds.reset_demo_data --apply --org "Atlas Demo Corp"
    envVars:
      - key: DEMO_RESET_ENABLED
        value: "true"
      - key: DATABASE_URL
        sync: false
      - key: JWT_SECRET
        sync: false
      - key: STORAGE_PROVIDER
        value: minio
      - key: STORAGE_ENDPOINT_URL
        sync: false
      - key: STORAGE_REGION
        value: auto
      - key: STORAGE_BUCKET
        value: atlas-receipts-staging
      - key: STORAGE_ACCESS_KEY_ID
        sync: false
      - key: STORAGE_SECRET_ACCESS_KEY
        sync: false
```

Cron jobs are a paid Render service type. Check the plan before adding one. A failed
run exits non-zero, which Render shows on the job, and leaves the database unchanged.

## Verifying a run

- The output ends with `storage objects deleted N, failed M` (manual run), or the
  log shows `Scheduled demo reset done | rows deleted=... restored=...` (schedule).
- `GET /api/v1/ops/demo-reset` (finance) shows the new `last_reset_at` and
  `consecutive_failures: 0`.
- `GET /api/v1/audit` (finance) shows the `demo.reset` row with the counts.
- `employee@` / `manager@` / `finance@atlas-demo.internal` sign in with the published
  password.
- A second dry run shows `0` for `seeded accounts`, `seeded policy rules`,
  `users (visitor-created)` and `policy_rules (visitor-authored)`. The remaining
  delete counts are the seed's own rows, which an apply would re-create.
