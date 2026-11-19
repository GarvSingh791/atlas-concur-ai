#!/bin/sh
# Atlas — backend container entrypoint.
#
# Makes the image self-sufficient so a clean `docker compose up` reproduces a
# working stack (the live volume was previously migrated/seeded out-of-band):
#
#   * alembic upgrade head — brings the schema to head. Idempotent: a no-op when
#                            already at head, so safe on every container start.
#   * SEED_ON_STARTUP=true — runs the idempotent demo seed (ON CONFLICT DO
#                            NOTHING / .first() guards), seeding the demo org and
#                            users (employee@/manager@/finance@atlas-demo.internal),
#                            then refreshes DRAFT reports' pre-submit flags.
#   * SEED_ON_STARTUP=policy_corpus — reindex only the policy RAG corpus
#                            (database/seeds/reindex_policy_corpus.py), then top up
#                            the demo corporate-card feed so smart-match stays
#                            demonstrable (database/seeds/topup_demo_card_feed.py,
#                            C7-22), then open approvals for any submission that
#                            predates submit-opens-approval
#                            (database/seeds/backfill_submitted_approvals.py,
#                            C7R), then route pending approvals that name an
#                            approver grade (SVP / VP flag) and were never routed
#                            by it (database/seeds/backfill_grade_routing.py,
#                            follow-up grades), then re-run the pre-submit policy check of
#                            every DRAFT report so stored flags follow the
#                            current policy figures
#                            (database/seeds/refresh_draft_presubmit_flags.py,
#                            Issue 6), then mark approved trips that ended more
#                            than a day ago as completed
#                            (database/seeds/complete_elapsed_trips.py, product
#                            gap 8). All are safe on a live database such as
#                            staging Neon.
#   * exec "$@"            — replace the shell with the CMD so SIGTERM reaches
#                            uvicorn AND the docker-compose.dev.yml `command:`
#                            override (uvicorn --reload) is honoured.
#
# The pgcrypto + vector extensions the schema needs (but the migrations do not
# create themselves) are provisioned by the postgres service on first init via
# docker/postgres/init/00-extensions.sql. Runs as the non-root `atlas` user;
# WORKDIR is /app where alembic.ini lives (script_location=database/migrations).
set -e

# Repairs of DERIVED or PENDING state (opening approvals for stranded
# submissions, recomputing draft reports' pre-submit flags, completing approved
# trips that have ended) never stop the
# container: they report per-row failures (exit 1) and the app is correct without
# them, whereas one bad legacy row would otherwise block every future deploy.
# The rows are named in the log above the warning, and the step re-runs on every
# boot. Migrations, the demo seed, the corpus reindex and the card-feed top-up stay
# fatal (set -e).
repair() {
  label="$1"
  shift
  if ! "$@"; then
    echo "[atlas-entrypoint] WARNING: ${label} reported failures (see above); continuing startup — the step re-runs on the next boot."
  fi
}

# Elapsed-trip sweep, operator-controlled (phase 10): ELAPSED_TRIP_SWEEP =
#   on      (default; also when unset or empty) complete elapsed approved trips;
#   dry-run log which trips WOULD be completed (id, end date, owner id) and
#           write nothing — preview it on a live database before turning it on;
#   off     skip the step.
# Any other value is treated as off, with a warning. The module re-checks the
# value itself (and ELAPSED_TRIP_SWEEP_MAX / ELAPSED_TRIP_GRACE_DAYS); see
# docs/runbooks/elapsed-trip-sweep.md.
elapsed_trip_sweep() {
  sweep_mode=$(printf '%s' "${ELAPSED_TRIP_SWEEP:-on}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  case "${sweep_mode:-on}" in
    on|dry-run)
      echo "[atlas-entrypoint] $1 — elapsed-trip sweep (ELAPSED_TRIP_SWEEP=${sweep_mode:-on})..."
      repair "elapsed-trip completion" python -m database.seeds.complete_elapsed_trips
      ;;
    off)
      echo "[atlas-entrypoint] $1 — ELAPSED_TRIP_SWEEP=off: skipping the elapsed-trip sweep."
      ;;
    *)
      echo "[atlas-entrypoint] WARNING: ELAPSED_TRIP_SWEEP='$(printf '%.20s' "${ELAPSED_TRIP_SWEEP}")' is not on / dry-run / off — treated as off; the elapsed-trip sweep is skipped."
      ;;
  esac
}

# Staging test-data cleanup as an operator-controlled boot step (Render's free plan
# has no shell, and the tool needs the database URL, which stays inside the
# service). TEST_DATA_CLEANUP = off (default; also unset / empty) | dry-run (lists
# what WOULD be removed in the log, writes nothing) | apply (removes it; the tool
# ALSO requires CLEANUP_TEST_DATA_ENABLED=true and refuses otherwise). Any other
# value is treated as off with a warning. The organisation defaults to the demo
# organisation (TEST_DATA_CLEANUP_ORG). Non-fatal like every repair; set it back
# to off after one run. Runbook: docs/runbooks/staging-test-data-cleanup.md.
test_data_cleanup() {
  cleanup_mode=$(printf '%s' "${TEST_DATA_CLEANUP:-off}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  case "${cleanup_mode:-off}" in
    off)
      ;;
    dry-run)
      echo "[atlas-entrypoint] $1 — test-data cleanup DRY RUN (TEST_DATA_CLEANUP=dry-run)..."
      repair "test-data cleanup (dry-run)" python -m database.seeds.cleanup_test_data --org "${TEST_DATA_CLEANUP_ORG:-Atlas Demo Corp}"
      ;;
    apply)
      echo "[atlas-entrypoint] $1 — test-data cleanup APPLY (TEST_DATA_CLEANUP=apply; needs CLEANUP_TEST_DATA_ENABLED=true)..."
      repair "test-data cleanup (apply)" python -m database.seeds.cleanup_test_data --org "${TEST_DATA_CLEANUP_ORG:-Atlas Demo Corp}" --apply
      ;;
    *)
      echo "[atlas-entrypoint] WARNING: TEST_DATA_CLEANUP='$(printf '%.20s' "${TEST_DATA_CLEANUP}")' is not off / dry-run / apply — treated as off; no cleanup runs."
      ;;
  esac
}

echo "[atlas-entrypoint] Applying database migrations (alembic upgrade head)..."
alembic upgrade head

if [ "${SEED_ON_STARTUP}" = "true" ]; then
  echo "[atlas-entrypoint] SEED_ON_STARTUP=true — seeding demo data (idempotent)..."
  python -m database.seeds.seed_all
  # Existing local databases: recompute DRAFT reports' stored pre-submit flags so
  # they follow the current policy figures (Issue 6; idempotent).
  echo "[atlas-entrypoint] SEED_ON_STARTUP=true — refreshing draft reports' pre-submit flags..."
  repair "draft pre-submit refresh" python -m database.seeds.refresh_draft_presubmit_flags
  elapsed_trip_sweep "SEED_ON_STARTUP=true"
elif [ "${SEED_ON_STARTUP}" = "policy_corpus" ]; then
  # Live databases (staging): reindex ONLY the policy RAG corpus, then ensure
  # one UNMATCHED demo card transaction per demo receipt template (creates rows
  # only for templates smart-match has consumed). No users, passwords, trips,
  # reports or receipts are touched beyond: approvals OPENED for submissions that
  # have none (backfill), DRAFT reports' derived pre_submit_flags snapshots
  # recomputed, and APPROVED trips that ended over a day ago marked completed
  # (audited, actor = system; ELAPSED_TRIP_SWEEP=dry-run previews, off skips). The reindex or top-up failing stops this container
  # (set -e), so the previous deploy keeps serving; the repairs only warn (see repair()).
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — reindexing the policy corpus..."
  python -m database.seeds.reindex_policy_corpus
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — topping up the demo card feed..."
  python -m database.seeds.topup_demo_card_feed
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — opening approvals for stranded submissions..."
  repair "approval backfill" python -m database.seeds.backfill_submitted_approvals
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — routing pending approvals that name an approver grade..."
  repair "grade-routing backfill" python -m database.seeds.backfill_grade_routing
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — refreshing draft reports' pre-submit flags..."
  repair "draft pre-submit refresh" python -m database.seeds.refresh_draft_presubmit_flags
  echo "[atlas-entrypoint] SEED_ON_STARTUP=policy_corpus — setting the demo accounts' traveller grades (only where none is recorded)..."
  repair "demo grades" python -m database.seeds.seed_demo_grades
  elapsed_trip_sweep "SEED_ON_STARTUP=policy_corpus"
  test_data_cleanup "SEED_ON_STARTUP=policy_corpus"
else
  echo "[atlas-entrypoint] SEED_ON_STARTUP='${SEED_ON_STARTUP:-unset}' — skipping seed."
fi

echo "[atlas-entrypoint] Handing off to: $*"
exec "$@"
