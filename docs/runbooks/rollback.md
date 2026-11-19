# Atlas Staging — Rollback Runbook

Last updated: 2026-11-07 (P8-T4 rewrite + G6 drill; replaces the 2026-06-24 version)

How to put staging back on a known-good release. Live targets:

| Service | URL | Reports its commit at |
|---|---|---|
| Backend (Render, `atlas-backend`, free tier) | `https://atlas-backend-tsf8.onrender.com` | `GET /health` → `"commit"` |
| Frontend (Vercel, `atlas-concur-ai`, Hobby) | `https://atlas-concur-ai.vercel.app` | `GET /api/version` → `"commit"` |
| Database (Neon Postgres + pgvector, branch `production`) | — | `alembic_version` table |

Every deploy normally comes from `main` through CI (`deploy-staging` in
`.github/workflows/ci.yml`). CI deploys the backend pinned to the merged commit and
waits until `/health` reports it, and waits until `/api/version` reports the newest
commit that changed `frontend/` (Vercel skips builds for commits that don't). A rollback is the one time a human moves staging off `main`'s head, which is
why the last step is always to make `main` match what is live again.

```bash
# What is live right now?
curl -s https://atlas-backend-tsf8.onrender.com/health | jq -r .commit
curl -s https://atlas-concur-ai.vercel.app/api/version   | jq -r .commit
```

Render's free instance sleeps after ~15 min idle; the first request can take two to three minutes (measured 100–152 s).
A slow first answer is not a reason to roll back.

---

## 1. Decide

```
Is the problem only in the browser UI (blank page, broken screen), with /health fine?
  └─ yes → Frontend rollback (section 2)

Is the backend failing (/health non-200, logins fail, 500s) after a deploy?
  ├─ Did that release add a migration (new file in database/migrations/versions/)?
  │    ├─ no  → Backend rollback (section 3)
  │    └─ yes → Do NOT use Render rollback: the older build cannot boot on the
  │             newer schema (section 3, step 3). Ship a forward fix through a PR
  │             that keeps the migration file. Expand-then-contract (section 5) is
  │             what lets the fixed code run on the migrated schema.
  └─ Is data wrong or lost (not just code)? → Data restore (section 4)

Is it a config problem (expired R2 token, rotated key not saved)?
  └─ Not a rollback. Fix the Render env var (docs/staging_env_map.md), save and deploy.
```

Never run `alembic downgrade` against staging by hand. `downgrade()` drops columns
and tables with their data, and Render's free tier has no Shell to run it from
anyway. The safety net for data is Neon's restore (section 4).

---

## 2. Frontend rollback (Vercel Instant Rollback)

Time: ~1 minute. No rebuild.

1. Vercel → **atlas-concur-ai** → **Deployments**.
2. Open the current Production deployment's **⋯** menu → **Instant Rollback**
   (or the project's **Instant Rollback** button). On the Hobby plan you can roll
   back **only to the immediately previous production deployment**. To go further
   back, merge a `git revert` PR (section 6) so Vercel builds the old code as a new
   production deployment. In an emergency, **Redeploy** an older deployment from
   its ⋯ menu (a fresh build of that commit); `/api/version` then shows that old
   commit until the revert merges.
3. Confirm: `curl -s https://atlas-concur-ai.vercel.app/api/version` shows the
   previous commit.

**Trap — read before you finish.** After an Instant Rollback, Vercel turns off
automatic promotion of new production deployments until you undo the rollback (section 6, step 1).
While it is off, every merge that changes `frontend/` builds but stays unpromoted,
and CI's `deploy-staging` fails waiting for that commit. Merges that don't touch
`frontend/` still pass, so CI will not remind you. Finish with section 6.

---

## 3. Backend rollback (Render)

Time: ~1–2 minutes. No rebuild: Render reuses the earlier build artifact.

1. Render → **atlas-backend** → **Deploys** (or **Events**).
2. On the last good deploy, click **Rollback** and confirm. How many earlier builds
   can be rolled back to depends on the plan's retention.
3. The rolled-back container still runs the entrypoint, so `alembic upgrade head`
   runs again. That is a no-op when the database is at the code's newest revision.
   If the bad release added a migration, the database is **ahead** of the older
   code: Alembic stops with "Can't locate revision", the container exits, and
   Render keeps the current release. So Render rollback only works for releases
   without a migration; for the others, ship a forward fix (section 1).
4. Confirm: `curl -s https://atlas-backend-tsf8.onrender.com/health` returns
   `"status":"ok"` and the previous commit, then a demo login works:
   ```bash
   curl -s -X POST https://atlas-backend-tsf8.onrender.com/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"employee@atlas-demo.internal","password":"change-me-seed-password"}' \
     | jq 'has("access_token")'
   ```

A rollback pins the service to that deploy; the next deploy hook (the next merge)
deploys `main`'s head again. That is why section 6 matters.

---

## 4. Data restore (Neon)

Only for data damage (bad writes, a destructive migration), never for code.

1. If the restore point is **before a migration** that is now live, first put the
   backend on a release that predates that migration (section 3), or the running
   code will expect tables or columns the restored database no longer has.
2. Neon → project **atlas-concur-ai** → **Branches** → `production` →
   **Restore**, and pick a timestamp before the damage. It must be inside the
   project's restore window (history retention; check it on that page before
   relying on it).
3. Restoring replaces the branch's data in place, so `DATABASE_URL` does not change.
4. Run the checks in section 7. If you restored past a migration, the revert in
   section 6 must remove that migration (or replace it with an expand-only
   version), or the next deploy's entrypoint will apply it again.

---

## 5. The rule that makes code rollback safe: expand, then contract

A release may **add** columns, tables or indexes. It must not **drop or rename**
anything the previous release still reads. Removals ship in a later release, once
no live code uses them.

Why it matters here: Render runs the migration in the **new** container while the
**old** release is still serving traffic, and only swaps once the new one is
healthy. Expand-only migrations keep the old release working during that window,
and let a code-only forward fix run on the migrated schema. They do **not** make a
Render rollback possible after a migration (section 3, step 3); for that case the
plan is always a forward fix. A migration that breaks this rule should say so in
its PR description.

Migrations run forward-only on release: the backend entrypoint runs
`alembic upgrade head` under `set -e`, so a failed migration stops the new container
and Render keeps serving the old one. CI's `backend` job also fails any change that
leaves Alembic with more than one head.

---

## 6. Always finish here

1. **Undo the rollback on Vercel** if you rolled the frontend back. The project
   overview shows a banner, "To undo the rollback promote to production…", with a
   **Manage** button. **Manage** opens **Manage Rollback**, which asks which
   deployment to promote; promoting is what turns automatic promotion back on.
   Pick a known-good deployment, never the bad one: normally the one you rolled
   back to, or the revert PR's production build once it is Ready. (The list
   preselects the rolled-back deployment; that is only correct in a drill.)
2. **`git revert` the bad change on `main`** through a normal PR. When it merges,
   CI redeploys and waits for the revert commit on both services, so `main` and
   staging agree again (the frontend wait applies only if the revert changes
   `frontend/`; otherwise check `/api/version` by hand, section 7). If you skip
   this, the next unrelated merge redeploys the bad code. If the bad change
   included a migration and the database stays migrated, keep the migration file
   in the revert (revert only the code): deleting it would leave the database on
   a revision the code no longer knows, and the next deploy could not boot. The
   one exception is a section 4 restore past that migration (see there).
3. Write the incident into the current phase log: what failed, which section you
   used, the time from decision to restored, and the follow-up fix.

---

## 7. Checks after any rollback or restore

- [ ] `/health` → `"status":"ok"` and the expected `"commit"` (after section 6:
      `main`'s head)
- [ ] `/api/version` → the expected `"commit"` (after section 6: the newest
      commit on `main` that changed `frontend/`,
      `git log -1 --format=%H origin/main -- frontend/`)
- [ ] `employee@`, `manager@`, `finance@atlas-demo.internal` log in
- [ ] CORS preflight from `https://atlas-concur-ai.vercel.app` → 200 (see the
      `deploy-staging` job for the exact command)
- [ ] Vercel shows no active rollback (automatic promotion is on)
- [ ] `main` matches what is live (the revert PR is merged and CI is green)

---

## Drill log

| Date | Release rolled back | Backend: decision → restored | Frontend: decision → restored | Notes |
|---|---|---|---|---|
| 2026-11-07 | `d468dda` (P8-T4) → `06b8ba6`, then rolled forward | 77 s (Render Rollback); roll-forward 51 s | 32 s (Instant Rollback); Undo via Manage Rollback → promote `d468dda` ~6 s | Planned P8-T4 G6 drill. No migration in `d468dda`, so Render rollback was valid. Old release verified by `/health` without `commit` and `/api/version` 404. Section 7 checks passed after roll-forward. |
