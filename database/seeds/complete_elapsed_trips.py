"""Mark approved trips that ended more than a day ago as completed (product gap 8).

``approved -> completed`` is a legal trip transition, and the traveler (or
finance) can do it by hand once a trip has ended (POST /trips/{id}/complete).
Trips nobody closes would stay "approved" forever, so this sweep completes every
APPROVED trip whose ``end_date`` is more than ``ELAPSED_TRIP_GRACE_DAYS`` (default
one) days in the past (UTC) — the grace leaves the traveler time to do it
themselves.

Each trip is completed in its own transaction: re-read ``FOR UPDATE`` (skipped
if no longer approved / no longer elapsed), version CAS, one ``trip.completed``
audit row with ``actor_id`` NULL (system) and ``trigger: "sweep"``.  Nothing else
is touched (no approvals, no segments, no other statuses).

Operator control (phase 10 — it runs on every boot against live data):
  * ``ELAPSED_TRIP_SWEEP`` = ``on`` (default; also unset / empty) | ``dry-run``
    (log which trips WOULD be completed — trip id, end date and owner id only,
    no PII — and write nothing) | ``off``.  Any other value is treated as
    ``off`` with a warning, and so is an unusable cap or grace.
  * ``ELAPSED_TRIP_SWEEP_MAX`` (default 500, >= 1): at most that many trips per
    run, oldest end date first, with a log line when capped; the rest are
    handled on the next boot.
  * ``ELAPSED_TRIP_GRACE_DAYS`` (default 1, >= 0).
  Runbook: docs/runbooks/elapsed-trip-sweep.md.

Idempotent: a re-run with nothing elapsed is a no-op.  No LLM / network calls.

Run: ``python -m database.seeds.complete_elapsed_trips`` (exit 0 also when
nothing needed completing; exit 1 when any trip failed).  Wired into
``docker/entrypoints/backend-entrypoint.sh`` as a non-fatal ``repair`` step.
"""

from __future__ import annotations

import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402

from backend.app.core.audit import record_audit  # noqa: E402
from backend.app.core.concurrency import guarded_version_update  # noqa: E402
from backend.app.core.database import get_sessionmaker  # noqa: E402
from database.models.trip import Trip, TripStatus  # noqa: E402

logger = logging.getLogger(__name__)

GRACE_DAYS = 1
MAX_TRIPS = 500
MODE_ON, MODE_DRY_RUN, MODE_OFF = "on", "dry-run", "off"
_MODES = (MODE_ON, MODE_DRY_RUN, MODE_OFF)


@dataclass(frozen=True)
class SweepConfig:
    mode: str
    max_trips: int
    grace_days: int


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def resolve_config(
    settings: Any = None,
    *,
    mode: str | None = None,
    max_trips: int | str | None = None,
    grace_days: int | str | None = None,
) -> SweepConfig:
    """The effective sweep settings; anything unusable turns the sweep OFF (warned).

    Explicit arguments win over *settings* (``config.settings``; loaded only when
    an argument is missing).  Empty / unset mode = ``on``.  Fail safe: an unknown
    mode, a cap below 1 or a negative grace never mutates data.
    """
    if settings is None and (mode is None or max_trips is None or grace_days is None):
        from config.settings import get_settings

        settings = get_settings()
    raw_mode = mode if mode is not None else settings.ELAPSED_TRIP_SWEEP
    raw_max = max_trips if max_trips is not None else settings.ELAPSED_TRIP_SWEEP_MAX
    raw_grace = grace_days if grace_days is not None else settings.ELAPSED_TRIP_GRACE_DAYS
    cap, grace = _as_int(raw_max), _as_int(raw_grace)
    resolved = str(raw_mode or "").strip().lower() or MODE_ON
    if resolved not in _MODES:
        logger.warning(
            "ELAPSED_TRIP_SWEEP=%r is not one of on / dry-run / off: the elapsed-trip "
            "sweep is OFF.",
            str(raw_mode)[:20],
        )
        resolved = MODE_OFF
    if cap is None or cap < 1:
        logger.warning(
            "ELAPSED_TRIP_SWEEP_MAX=%r is not a whole number >= 1: the elapsed-trip sweep is OFF.",
            str(raw_max)[:20],
        )
        resolved, cap = MODE_OFF, MAX_TRIPS
    if grace is None or grace < 0:
        logger.warning(
            "ELAPSED_TRIP_GRACE_DAYS=%r is not a whole number >= 0: the elapsed-trip sweep "
            "is OFF.",
            str(raw_grace)[:20],
        )
        resolved, grace = MODE_OFF, GRACE_DAYS
    return SweepConfig(mode=resolved, max_trips=cap, grace_days=grace)


def _cutoff(today: date | None, grace_days: int = GRACE_DAYS) -> date:
    """Trips whose end_date is BEFORE this date are completed (end < today - grace)."""
    base = today if today is not None else datetime.now(timezone.utc).date()
    return base - timedelta(days=grace_days)


def _elapsed(
    session: sa.orm.Session, cutoff: date, limit: int
) -> list[tuple[uuid.UUID, Any, uuid.UUID]]:
    """(id, end_date, traveler id) of up to *limit* elapsed approved trips, oldest first."""
    return [
        (tid, end, owner)
        for tid, end, owner in session.execute(
            sa.select(Trip.id, Trip.end_date, Trip.traveler_id)
            .where(Trip.status == TripStatus.approved, Trip.end_date < cutoff)
            .order_by(Trip.end_date, Trip.id)
            .limit(limit)
        ).all()
    ]


def _complete_one(session: sa.orm.Session, trip_id: uuid.UUID, cutoff: date) -> bool:
    trip = session.scalars(sa.select(Trip).where(Trip.id == trip_id).with_for_update()).first()
    if trip is None or trip.status != TripStatus.approved:
        return False
    end: Any = trip.end_date
    if not isinstance(end, date) or end >= cutoff:
        return False
    before = {"status": trip.status.value, "version": trip.version, "end_date": str(end)}
    guarded_version_update(
        session,
        Trip,
        entity_id=trip.id,
        expected_version=trip.version,
        values={"status": TripStatus.completed, "updated_by": None},
    )
    session.refresh(trip)
    record_audit(
        session,
        organization_id=trip.organization_id,
        action="trip.completed",
        entity_type="trip",
        entity_id=trip.id,
        actor_id=None,
        before_state=before,
        after_state={
            "status": trip.status.value,
            "version": trip.version,
            "end_date": str(end),
            "trigger": "sweep",
        },
    )
    return True


def complete_elapsed(
    session_factory: Any = None,
    *,
    today: date | None = None,
    mode: str | None = None,
    max_trips: int | str | None = None,
    grace_days: int | str | None = None,
) -> dict[str, int]:
    """Complete (or, in dry-run, list) elapsed approved trips.

    Returns {"completed", "skipped", "failed", "would_complete", "capped"}
    (``capped`` 1 when more elapsed trips remain than this run handled).
    """
    config = resolve_config(mode=mode, max_trips=max_trips, grace_days=grace_days)
    result = {"completed": 0, "skipped": 0, "failed": 0, "would_complete": 0, "capped": 0}
    if config.mode == MODE_OFF:
        logger.info("Elapsed-trip sweep is off: nothing checked, nothing written.")
        return result
    if session_factory is None:
        session_factory = get_sessionmaker()
    cutoff = _cutoff(today, config.grace_days)
    with session_factory() as session:
        targets = _elapsed(session, cutoff, config.max_trips + 1)
    if len(targets) > config.max_trips:
        targets = targets[: config.max_trips]
        result["capped"] = 1
        logger.warning(
            "Elapsed-trip sweep capped at %d trips (ELAPSED_TRIP_SWEEP_MAX): more elapsed "
            "trips remain and are handled on the next run.",
            config.max_trips,
        )
    if config.mode == MODE_DRY_RUN:
        for trip_id, end, owner_id in targets:
            # Ids and the date only — never names, emails or destinations.
            logger.info(
                "DRY RUN would complete trip %s end_date %s owner %s", trip_id, end, owner_id
            )
        result["would_complete"] = len(targets)
        logger.info(
            "Elapsed-trip sweep DRY RUN (grace %d day(s)): %d trip(s) would be completed; "
            "nothing was written.",
            config.grace_days,
            len(targets),
        )
        return result
    for trip_id, _end, _owner in targets:
        with session_factory() as session:
            try:
                if _complete_one(session, trip_id, cutoff):
                    session.commit()
                    result["completed"] += 1
                    logger.info("Completed elapsed trip %s", trip_id)
                else:
                    session.rollback()
                    result["skipped"] += 1
            except Exception:
                session.rollback()
                result["failed"] += 1
                logger.exception("Could not complete trip %s", trip_id)
    logger.info(
        "Elapsed-trip sweep: %d completed, %d skipped, %d failed.",
        result["completed"],
        result["skipped"],
        result["failed"],
    )
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        result = complete_elapsed()
    except Exception:
        logger.exception("Elapsed-trip sweep FAILED.")
        return 1
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
