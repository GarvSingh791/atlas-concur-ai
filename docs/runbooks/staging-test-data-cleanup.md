# Runbook — remove old test data from staging

Staging (Neon) still holds records from earlier acceptance rounds — C7 / C7R /
UAT / QA / E2E — next to the seeded demo data. Examples: the report `8002eafc…`
titled **"C7"** ($999, submitted by the demo manager), `E2E …` reports, trips
and approvals, `QA run …` reports, and `qa.*@…` users.
`database/seeds/cleanup_test_data.py` removes them by conservative rules. The
module docstring has the full rules.

**Owner action.** The tool was built and tested without access to staging. Every
step below is yours to run.

## What the tool will and will not do

| It deletes | It never touches |
|---|---|
| Reports whose title starts with `E2E`, `QA`, `QA run`, `C7`, `C7R`, `UAT`, `UAT2`… or `Playwright`, with their entries, approvals (+ steps), non-seed receipts, idempotency keys and notification read-markers | The seeded demo users, reports, trips and receipts (`receipts/seed/…`) |
| Trips whose purpose starts with one of those markers, with their approvals | Anything created at or before the seed baseline (the first seeded demo user) |
| Records owned by `qa.*`, `qa-*`, `e2e*` or `playwright*` accounts | Trips still linked from a report it keeps |
| Ids you list in an `--ids` file (`report:<uuid>`, `trip:<uuid>`, `user:<uuid>`), with the same protections | Other organisations, and audit rows (the audit log is append-only) |
| — | Users: test accounts are **deactivated**, never deleted |

Writing needs **both** `--apply` and `CLEANUP_TEST_DATA_ENABLED=true`. Without them
the tool runs a dry-run and writes nothing. `--org "Atlas Demo Corp"` is always
required. If the organisation has no seeded demo users, the tool refuses to run.

## Fastest route: run it as a boot step on Render (no connection string needed)

Render's free plan has no shell, and the tool needs the database URL. The backend
entrypoint can run it for you on the next start, inside the service, so the URL
never leaves Render. It is controlled by environment variables (all optional):

| Variable | Meaning |
|---|---|
| `TEST_DATA_CLEANUP` | `off` (default) \| `dry-run` (list what WOULD be removed in the log, write nothing) \| `apply` (remove it). Anything else is treated as `off` with a warning. |
| `CLEANUP_TEST_DATA_ENABLED` | Must also be `true` for `apply`; the tool refuses otherwise. |
| `TEST_DATA_CLEANUP_ORG` | Organisation name, default `Atlas Demo Corp`. |

1. Take a Neon backup branch (step 0.1 below).
2. In Render, set `TEST_DATA_CLEANUP=dry-run`, save and redeploy, then read the
   log lines after the elapsed-trip sweep: the report lists every record it would
   remove (ids and titles only). Check it contains no seeded demo record.
3. If the list is right, set `TEST_DATA_CLEANUP=apply` **and**
   `CLEANUP_TEST_DATA_ENABLED=true`, redeploy once, and read the result in the log.
4. Set `TEST_DATA_CLEANUP` back to `off` and remove `CLEANUP_TEST_DATA_ENABLED`.

The step is non-fatal: a failure logs a warning and the app still starts.

## 0. Before you start (mandatory)

1. **Back up first: create a Neon branch.** In the Neon console, open the project
   and go to **Branches → Create branch**. Use the parent `main`, name it
   `pre-cleanup-YYYYMMDD`, and choose "current data". With the CLI, run
   `neonctl branches create --name pre-cleanup-YYYYMMDD --parent main`. The
   branch is your restore point. Keep it for at least 7 days.
2. Use the **merged** commit that contains `database/seeds/cleanup_test_data.py`.
   Either check it out locally or build the backend image from it.
3. Copy the connection strings for both the backup branch and `main` from
   Neon (**Connection details**). Never paste them into a terminal that is being
   recorded or into a chat.

## 1. Pre-cleanup checklist (read-only)

Run these in the Neon SQL editor against `main` and keep the numbers.

```sql
-- Totals for the demo organisation.
SELECT
  (SELECT count(*) FROM expense_reports r JOIN organizations o ON o.id = r.organization_id
     WHERE o.name = 'Atlas Demo Corp') AS reports,
  (SELECT count(*) FROM trips t JOIN organizations o ON o.id = t.organization_id
     WHERE o.name = 'Atlas Demo Corp') AS trips,
  (SELECT count(*) FROM users u JOIN organizations o ON o.id = u.organization_id
     WHERE o.name = 'Atlas Demo Corp' AND u.status = 'active') AS active_users;

-- The seeded reports that must still be there afterwards (expect 6 rows).
SELECT r.id, r.title FROM expense_reports r JOIN organizations o ON o.id = r.organization_id
WHERE o.name = 'Atlas Demo Corp' AND r.title IN (
  'Q2 Summit Expenses', 'June Miscellaneous', 'Client Visit Expenses',
  'Conference Expenses (Rejected)', 'Office Supplies', 'Q3 Conference & Travel');
```

- [ ] Neon branch `pre-cleanup-YYYYMMDD` exists.
- [ ] The three demo logins (employee, manager, finance) work on the frontend.
- [ ] Totals and the 6 seeded reports noted.

## 2. Run it: dry-run on the backup branch, then on main

Render's free plan has **no Shell**, so run the tool out-of-band. Use either the
backend image (option A) or a local checkout (option B). `DATABASE_URL` is read
from your shell environment and is never echoed. The tool does not use
`JWT_SECRET`, but `Settings` requires one, so pass any random 32+ character value.

### Option A — the backend image

```bash
# Build the image from the merged commit (or reuse the CI-built tag).
docker build -f docker/Dockerfile.backend -t atlas-backend:cleanup .

read -rs DATABASE_URL && export DATABASE_URL        # paste the BRANCH string, Enter
export JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# Dry-run. --entrypoint python skips the image entrypoint (alembic upgrade).
docker run --rm -e DATABASE_URL -e JWT_SECRET -e STORAGE_PROVIDER=local \
  --entrypoint python atlas-backend:cleanup \
  -m database.seeds.cleanup_test_data --org "Atlas Demo Corp"
```

### Option B — a local checkout

```bash
source .venv/bin/activate
read -rs DATABASE_URL && export DATABASE_URL        # paste the BRANCH string, Enter
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
STORAGE_PROVIDER=local python -m database.seeds.cleanup_test_data --org "Atlas Demo Corp"
```

(If a paid Render plan gives you a Shell, run the option B `python -m …` lines
there. The service already has `DATABASE_URL` and `JWT_SECRET` set.)

**Read the dry-run.** Each line shows a record kind, id, title or purpose, and the
reason. Lines starting with `skip` explain why a record is kept. Check that:

- [ ] every listed report or trip really is test data. The "C7" report `8002eafc…`
      should be in the list;
- [ ] no seeded title (the 6 above) appears outside a `skip` line;
- [ ] the counts are plausible.

To keep a listed record, stop. To add a leftover that no rule matches, put it in
an ids file, for example `report:8002eafc-…` with one per line, and add
`--ids ids.txt`. Explicit ids are still protected by every rule.

**Apply on the backup branch first:**

```bash
# Option A
docker run --rm -e DATABASE_URL -e JWT_SECRET -e STORAGE_PROVIDER=local \
  -e CLEANUP_TEST_DATA_ENABLED=true --entrypoint python atlas-backend:cleanup \
  -m database.seeds.cleanup_test_data --org "Atlas Demo Corp" --apply
# Option B
CLEANUP_TEST_DATA_ENABLED=true STORAGE_PROVIDER=local \
  python -m database.seeds.cleanup_test_data --org "Atlas Demo Corp" --apply
```

Run the post-checks (section 3) against the branch. If they pass, repeat the
dry-run and apply with `DATABASE_URL` set to **main**.

Receipt images: with `STORAGE_PROVIDER=local` the R2 objects of the deleted
receipts are left in place. They are unreferenced and harmless. To delete them
too, run the apply with the R2 settings instead: `STORAGE_PROVIDER=minio`,
`STORAGE_ENDPOINT_URL`, `STORAGE_REGION=auto`, `STORAGE_BUCKET=atlas-receipts-staging`
and the two `STORAGE_*_KEY` values, all read with `read -rs`. Storage deletion is
best effort. A failure is counted in `storage_failed` and never fails the run.

Exit codes: `0` ok · `1` a group failed (it was rolled back and the others were
applied; re-run to retry) · `2` refused (no confirmation, wrong `--org`, no seed
baseline, bad ids file).

## 3. Post-cleanup checklist

- [ ] The dry-run again prints `plan: 0 report(s), 0 trip(s), 0 user(s) to deactivate`.
- [ ] The 6 seeded reports from section 1 are all still there.
- [ ] Report and trip totals dropped by exactly the applied counts.
- [ ] The audit row exists:
      ```sql
      SELECT created_at, after_state FROM audit_log
      WHERE action = 'ops.test_data_cleanup' ORDER BY created_at DESC LIMIT 1;
      ```
- [ ] The three demo logins work. The employee dashboard shows the seeded
      reports, and the manager's approvals queue no longer shows "C7" or E2E items.
- [ ] Unset the variables: `unset DATABASE_URL JWT_SECRET`, then close the terminal.
- [ ] After 7 days without problems, delete the `pre-cleanup-YYYYMMDD` branch.

## Rollback

Neon console → Branches → `pre-cleanup-YYYYMMDD` → **Restore** `main` from
it. You can also point Render's `DATABASE_URL` at the branch and then redeploy.
The restore also discards any demo activity since the backup, which is
acceptable for a demo.
