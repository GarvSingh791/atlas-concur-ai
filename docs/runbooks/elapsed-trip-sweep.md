# Elapsed-trip sweep

Last updated: 2026-11-14 (phase 10, known limits)

## What it does

`database/seeds/complete_elapsed_trips.py` marks **approved** trips whose end
date is more than `ELAPSED_TRIP_GRACE_DAYS` days in the past (UTC) as
**completed**. Each trip is completed in its own transaction and gets one
`trip.completed` audit row (actor NULL = system, `trigger: "sweep"`). It touches
no approvals, segments or other statuses. Budget Posture counts approved and
completed trips alike, so the KPI does not move.

It runs as a non-fatal `repair` step of `docker/entrypoints/backend-entrypoint.sh`
on every boot where `SEED_ON_STARTUP` is `true` or `policy_corpus` (staging uses
`policy_corpus`). A failed trip only logs a warning, and the step runs again on
the next boot.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ELAPSED_TRIP_SWEEP` | `on` | `on`: complete the trips. `dry-run`: log which trips **would** be completed and write nothing. `off`: skip the step. Unset or empty means `on`. Case and surrounding spaces are ignored. Any other value is treated as `off`, with a warning (from the entrypoint at boot, or from the module when run by hand). |
| `ELAPSED_TRIP_SWEEP_MAX` | `500` | At most this many trips per run, oldest end date first. When more remain, the log says `Elapsed-trip sweep capped at N trips`, and the rest are handled on the next boot. A value that is not a whole number of at least 1 turns the sweep off, with a warning. |
| `ELAPSED_TRIP_GRACE_DAYS` | `1` | Days after the end date before the sweep completes a trip. With `1`, a trip that ended yesterday is left for the traveler, and one that ended two days ago is completed. A value that is not a whole number of at least 0 turns the sweep off, with a warning. |

A bad value never stops the app from starting. The settings are read as text,
and only the sweep interprets them.

The dry-run log holds only the trip id, end date and owner (traveler) id:

```
INFO  DRY RUN would complete trip 6f1c… end_date 2026-10-02 owner 9a3e…
INFO  Elapsed-trip sweep DRY RUN (grace 1 day(s)): 12 trip(s) would be completed; nothing was written.
```

## Preview on staging before the first real run

`render.yaml` does not set these variables, so staging runs with the defaults
(`on`). The project owner decides the staging value. To preview first:

1. In the Render dashboard (service `atlas-backend` → Environment), add
   `ELAPSED_TRIP_SWEEP=dry-run`, save, and deploy (if saving does not start a
   deploy, use **Manual Deploy → Deploy latest commit**).
2. When the deploy is live (`GET /health` reports the commit), open the service
   **Logs** and search for `DRY RUN would complete trip` and
   `Elapsed-trip sweep DRY RUN`. Check the list: these trips are the ones that
   will change from approved to completed. Look up any doubtful id in the app
   or in Neon (`SELECT id, status, end_date FROM trips WHERE id = '…'`).
3. If the list is right, set `ELAPSED_TRIP_SWEEP=on`, or delete the variable,
   since `on` is the default. Deploy, then look for `Elapsed-trip sweep: N
   completed, 0 skipped, 0 failed.` in the log.
4. If it is not right, set `ELAPSED_TRIP_SWEEP=off` and deploy. Nothing is
   written, and the step is skipped on every boot until you turn it back on.

To limit a first real run, also set `ELAPSED_TRIP_SWEEP_MAX` to a small number
(for example `20`). Each boot then completes at most that many trips, oldest
first.

## Undoing a completion

The sweep has no undo. The `trip.completed` audit rows (`trigger: "sweep"`)
record each trip's previous status and version. A trip completed by mistake has
to be put back to `approved` by hand in the database. Record that change in the
audit log the same way.

## Run by hand

```bash
ELAPSED_TRIP_SWEEP=dry-run python -m database.seeds.complete_elapsed_trips
```

It exits `0` when nothing failed (including when nothing was elapsed), and `1`
when any trip failed.
