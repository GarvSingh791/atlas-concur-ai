# Phase 12 follow-up: scheduled demo reset

Branch `fu/autoreset`, based on `main` at `962e71a` (alembic head
`0023_entry_release_lineage`, unchanged; this item adds no migration).

## What

The demo reset tool (`database/seeds/reset_demo_data.py`, phase 11) put
"Atlas Demo Corp" back to the seed baseline, but only when someone ran it by hand.
Render's free plan has no cron service and no shell. A GitHub-Actions trigger
would need a secret reset endpoint. This item adds a self-contained scheduler
inside the backend. It is **off by default** and nothing in `render.yaml` turns it on.

| Piece | Where |
|---|---|
| Settings `DEMO_RESET_SCHEDULE` (`off` / `interval`), `DEMO_RESET_INTERVAL_HOURS` (24), `DEMO_RESET_AT_UTC_HOUR` (unset) | `config/settings/base.py` |
| Scheduler: config parsing, due-time maths, advisory lock, audit read, asyncio loop | `backend/app/core/demo_reset_scheduler.py` |
| Started in the FastAPI lifespan, stopped on shutdown | `backend/app/main_entry/app.py` |
| Shared safeguard `apply_refusal(org_name)` (wraps the tool's own `_check_apply_allowed`) | `database/seeds/reset_demo_data.py` |
| `GET /public/config` gains `demo_reset: {enabled, interval_hours, next_reset_at, last_reset_at}` | `backend/app/api/public_config.py` |
| `GET /ops/demo-reset` (finance admins only, read-only) | `backend/app/api/ops.py` |
| Banner line "Demo data resets daily — next reset ~HH:MM" (local time) | `frontend/components/atlas/demo-banner.tsx`, `frontend/lib/query/public-config-hooks.ts` |
| Runbook, env docs | `docs/runbooks/demo-reset.md`, `.env.example`, `render.yaml` (comments only), `docs/staging_env_map.md`, `README.md` (two table rows) |

## Why

Visitors share the demo accounts and leave junk behind. A manual reset depends on
someone remembering it. The schedule lets the owner turn on a daily reset on the
free plan with three dashboard variables.

## Decisions

1. **Gates, all required.** `DEMO_RESET_SCHEDULE=interval`, `DEMO_MODE=true`, and
   `DEMO_RESET_ENABLED=true` in the process environment. The last one is checked by
   the tool's own function (`apply_refusal` calls `_check_apply_allowed`), so the
   scheduler cannot drift from the tool or weaken it. `apply` runs the check again
   itself. The organisation must be named exactly "Atlas Demo Corp". The scheduler
   always passes that constant, and the tool's `_resolve_demo_org` refuses anything
   else (proven by a test that renames the org to `"Atlas Demo Corp "`).
   `DEMO_RESET_ENABLED` stays a process-environment flag, not a Settings field, as in
   phase 11. A `.env` file can therefore never turn the reset on.
2. **Invalid values disable, never crash.** The three new settings are raw strings.
   `resolve_config` parses them at startup. An unknown schedule, an interval outside
   1-8784 hours or an hour outside 0-23 logs `Scheduled demo reset DISABLED — ...`
   with variable names only (never the value), and the app boots normally.
3. **Schedule state lives in the audit log.** "Due" is computed from the newest
   `demo.reset` audit row of the organisation named exactly "Atlas Demo Corp".
   Verified: the tool writes that row in the reset's own transaction (step 5 of
   `apply`), so it exists only for a committed reset. Restarts, deploys and the free
   plan's sleep therefore lose nothing. Another tenant's lookalike `demo.reset` row is
   ignored (join on the org name and `entity_id = org id`).
4. **Due-time maths.** No reset on record: due now. Interval only: `last + interval`.
   With `DEMO_RESET_AT_UTC_HOUR=H`: slots are `H:00 UTC` on 1970-01-01 plus whole
   intervals, and the reset is due at the first slot strictly after the last reset.
   A long sleep over many slots gives ONE catch-up reset, because the next due time
   comes from the new last reset. A manual reset also moves the schedule.
5. **Cross-instance safety.** `pg_try_advisory_lock(0x41544C4153445231)` on a
   dedicated AUTOCOMMIT connection around decide + reset. The loser skips the tick.
   The due check is repeated under the lock. Without that, an instance that wins the
   lock just after another finished would reset a second time. The lock is always
   released, and on an unlock error the connection is invalidated, never returned to
   the pool holding the lock.
6. **The reset runs in a worker thread** (`asyncio.to_thread`) through the tool's
   `apply`, unchanged: one transaction, both tenant-isolation proofs, best-effort
   storage cleanup after commit.
7. **Failures.** A refusal (`ResetRefused`) or an exception is counted
   (`failures_total`, `consecutive_failures`), logged with the exception TYPE or the
   refusal reason only (no DSN or message text), and retried no sooner than 15
   minutes later. `tick()` never raises, and the loop survives a tick that does.
   Every tick re-checks `DEMO_MODE` and the env flag, so the gates fail closed at run
   time too.
8. **Shutdown.** The loop task is cancelled. The tick in flight is `shield`ed, so the
   cancel never interrupts a reset. `stop()` then waits up to 30 s for it. If the
   process dies first, Postgres rolls the transaction back.
9. **Never under pytest by default.** The lifespan starts the loop only when
   `running_under_pytest()` is false. The repo `conftest.py` also defaults
   `DEMO_RESET_SCHEDULE=off`. Tests build schedulers with injected clock, lock, audit
   read and reset, and call `tick()` / `start()` explicitly. DB tests use their own
   scratch database `<test db>_demo_autoreset`, never the shared test DB, for any reset.
10. **Visitor transparency without new risk.** `/public/config` exposes only the
    on/off state, the interval and two timestamps. With the schedule off it reveals
    nothing, not even a configured interval. The banner keeps its phase-11 text and
    adds the reset line only when `demo_reset.enabled` is true.
11. **Ops endpoint and tenant scoping.** `GET /ops/demo-reset` is deny-by-default
    (finance admin, else 403; no token gives 401). The schedule state is
    deployment-wide, like `/ops/rate-limits`. The last reset's counts (`last_result`)
    come from the `demo.reset` audit row and are returned only when the caller's own
    organisation is the demo org. `restored_user_ids` is never returned. There is
    no "Reset now".
12. **Requests are not blocked.** The reset is transactional, so readers see the old
    or the new state. The runbook documents the brief window: a visitor working
    during the commit may get a 404 / 409 on a record that just vanished, and
    visitor-created accounts lose their session.

## Behaviour changes to existing tests (on purpose)

- `backend/tests/test_ps_demo_protection_db.py::test_public_config_reports_demo_mode_without_a_token`:
  the exact JSON now includes the default-off `demo_reset` block.
- `backend/tests/api/endpoints.py` / `test_contract_matrix.py`: the registry gains
  `GET /ops/demo-reset` (finance only); the live-route count goes from 68 to 69.

## Limits

- The counters in `/ops/demo-reset` (`resets_total`, `failures_total`,
  `skipped_locked`, `last_error`) are per process and start from zero after a
  restart. The persistent facts are the audit rows. A failed attempt is not audited:
  it changes nothing, and logging it is enough.
- The retry backoff is per instance. Two instances that both keep failing each retry
  every 15 minutes, and the advisory lock still allows one reset at a time.
- On Render's free plan, the service sleeps when idle. A reset happens only while the
  service is awake: on the first tick after a wake, if one is due. The banner's "next
  reset" is the earliest time, not a promise.
- `next_reset_at` is `null` until the first tick has read the audit log, which happens
  a few seconds after startup.
- The app clock decides "due" and Postgres `now()` stamps the audit row. Clock skew
  between the two shifts the schedule by that skew only.
- A Render cron job (paid plans) running the CLI does not take the advisory lock. Use
  one or the other (runbook).

## How to verify

```bash
R=<local-runner-dir>/fu/run_autoreset.sh
$R -q backend/tests/test_fu_demo_autoreset.py          # hermetic: maths, config, lock, backoff, lifecycle, public config
$R -q backend/tests/test_fu_demo_autoreset_db.py       # real tool, real advisory lock, audit, lifespan, ops endpoint
cd frontend && npx vitest run __tests__/demo           # banner line
```

Mutation proofs: each guard was reverted, its test went red, and the guard was
restored. The guards: pytest auto-start guard, advisory lock, re-read under the lock,
lock + re-read with two schedulers, retry backoff, per-tick gate, at-hour slots,
catch-up-once (due from the last success), invalid interval, invalid hour, the tool's
env check in config, the DEMO_MODE gate, audit org-name scoping, no secrets in logs,
the disabled public view, ops finance-only, ops tenant scoping, ops hiding user ids,
the in-progress flag, stop cancelling the loop (the test hangs without it), and in
the frontend the enabled check and the rendered line.

Review fix: the `action == "demo.reset"` filter in `read_last_reset` (scheduler) and
in `_last_reset_result` (ops endpoint) is now pinned. The test-data cleanup tool
writes an org-level audit row for the same org (`ops.test_data_cleanup`,
`entity_type="organization"`, `entity_id=<org id>`). Without the filter, a cleanup
run on Atlas Demo Corp would count as a reset and push the next scheduled reset
back by a full interval. Two new DB tests write exactly that row for the demo org.
One checks that `read_last_reset` stays `None` and a tick still resets. The other
checks that the demo org's finance admin sees `last_result: null`. Removing the
action filter in either place makes the matching test fail. The reviewer's exact
mutation (dropping the action and entity_id filters) also fails.

Review fix: the lifespan wiring is now pinned. Before this, removing
`demo_reset.start()` or `await demo_reset.stop()` from `app.py` left every test
green, because the only lifespan test checked that the loop does NOT start under
pytest. Two new DB tests patch `running_under_pytest` in `app.py` to return False,
replace `tick` with a recorder (no database is touched) and wrap `stop` with a spy.
`test_lifespan_starts_the_scheduler_and_stops_it_on_shutdown` opens every gate,
enters `TestClient(create_app())` and checks that the scheduler is running and has
ticked. After the client exits, it checks that `stop` was awaited once, the task is
cleared and the loop task was cancelled. `test_lifespan_does_not_start_a_disabled_scheduler`
sets `DEMO_RESET_SCHEDULE=off` and checks that nothing starts or ticks. Mutations,
each red and then restored: start replaced by `pass`, the stop line deleted, and the
`config.enabled` condition dropped.

Manual check on staging, once the owner turns it on: set `DEMO_MODE=true`,
`DEMO_RESET_ENABLED=true` and `DEMO_RESET_SCHEDULE=interval` (optionally
`DEMO_RESET_AT_UTC_HOUR=4`), then redeploy. The log shows `Scheduled demo reset ON`.
`GET /api/v1/public/config` shows `demo_reset.enabled: true`. As finance@,
`GET /api/v1/ops/demo-reset` shows `last_reset_at`, and the audit log has a new
`demo.reset` row.
