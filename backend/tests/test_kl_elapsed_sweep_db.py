"""Phase 10 known limit — the elapsed-trip sweep is operator-controlled.

The sweep (``database/seeds/complete_elapsed_trips.py``, a boot ``repair``)
used to mutate live staging data on first boot with no preview.  Now:

* ``ELAPSED_TRIP_SWEEP`` = ``on`` (default) | ``dry-run`` | ``off``; any other
  value is treated as ``off`` with a warning;
* ``dry-run`` logs exactly which trips WOULD be completed — id, end date and
  owner id only (no names, no emails) — and writes nothing (no status change,
  no audit row);
* ``ELAPSED_TRIP_SWEEP_MAX`` (default 500) caps one run, with a log line when
  capped (oldest end date first; the rest go on the next boot);
* ``ELAPSED_TRIP_GRACE_DAYS`` (default 1): a trip is completed once its end
  date is more than that many days in the past;
* audit rows (``trip.completed``, actor NULL, ``trigger: "sweep"``) exist only
  for trips actually completed.

Each test runs inside the rolled-back ``db_session`` transaction, so only its own
trips exist.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from config.settings import Settings
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.seeds import complete_elapsed_trips as sweep
from database.seeds.complete_elapsed_trips import complete_elapsed

pytestmark = pytest.mark.db

TODAY = date(2026, 11, 14)


def _trip(db: Session, traveler: User, *, ended_days_ago: int) -> Trip:
    end = TODAY - timedelta(days=ended_days_ago)
    return f.make_trip(
        db,
        traveler=traveler,
        status=TripStatus.approved,
        start_date=end - timedelta(days=2),
        end_date=end,
    )


def _factory(db: Session) -> Any:
    return lambda: contextlib.nullcontext(db)


def _status(db: Session, trip: Trip) -> TripStatus:
    db.expire_all()
    stored = db.get(Trip, trip.id)
    assert stored is not None
    return stored.status


def _sweep_audits(db: Session) -> list[AuditLog]:
    return list(db.scalars(sa.select(AuditLog).where(AuditLog.action == "trip.completed")).all())


def test_dry_run_writes_nothing_and_logs_only_ids_dates_and_owner_ids(
    db_session: Session, users_a: dict[str, User], caplog: pytest.LogCaptureFixture
) -> None:
    employee = users_a["employee"]
    old = _trip(db_session, employee, ended_days_ago=10)
    older = _trip(db_session, employee, ended_days_ago=30)
    recent = _trip(db_session, employee, ended_days_ago=0)

    with caplog.at_level(logging.INFO, logger=sweep.__name__):
        result = complete_elapsed(
            _factory(db_session), today=TODAY, mode="dry-run", max_trips=500, grace_days=1
        )

    assert result == {"completed": 0, "skipped": 0, "failed": 0, "would_complete": 2, "capped": 0}
    for trip in (old, older, recent):
        assert _status(db_session, trip) == TripStatus.approved
    assert _sweep_audits(db_session) == []

    text = caplog.text
    for trip in (older, old):
        assert f"trip {trip.id} end_date {trip.end_date} owner {employee.id}" in text
    assert str(recent.id) not in text
    # No PII: neither the traveler's email nor name, nor the destination.
    assert employee.email not in text
    assert employee.name not in text
    assert "SFO" not in text
    # Oldest first.
    assert text.index(str(older.id)) < text.index(str(old.id))


def test_on_completes_and_audits_only_what_it_completed(
    db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    old = _trip(db_session, employee, ended_days_ago=10)
    recent = _trip(db_session, employee, ended_days_ago=1)

    result = complete_elapsed(
        _factory(db_session), today=TODAY, mode="on", max_trips=500, grace_days=1
    )
    assert result == {"completed": 1, "skipped": 0, "failed": 0, "would_complete": 0, "capped": 0}
    assert _status(db_session, old) == TripStatus.completed
    assert _status(db_session, recent) == TripStatus.approved
    (row,) = _sweep_audits(db_session)
    assert row.entity_id == old.id and row.actor_id is None
    assert (row.after_state or {})["trigger"] == "sweep"


@pytest.mark.parametrize("mode", ["dry-run", "on"])
def test_the_cap_limits_one_run_and_says_so(
    db_session: Session,
    users_a: dict[str, User],
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    employee = users_a["employee"]
    trips = [_trip(db_session, employee, ended_days_ago=days) for days in (40, 30, 20)]

    with caplog.at_level(logging.INFO, logger=sweep.__name__):
        result = complete_elapsed(
            _factory(db_session), today=TODAY, mode=mode, max_trips=2, grace_days=1
        )

    assert result["capped"] == 1
    assert "capped at 2" in caplog.text
    if mode == "on":
        assert result["completed"] == 2
        # Oldest end date first; the newest waits for the next run.
        assert [_status(db_session, t) for t in trips] == [
            TripStatus.completed,
            TripStatus.completed,
            TripStatus.approved,
        ]
        again = complete_elapsed(
            _factory(db_session), today=TODAY, mode="on", max_trips=2, grace_days=1
        )
        assert again["completed"] == 1 and again["capped"] == 0
        assert _status(db_session, trips[2]) == TripStatus.completed
    else:
        assert result["would_complete"] == 2
        assert str(trips[2].id) not in caplog.text
        assert _sweep_audits(db_session) == []


@pytest.mark.parametrize(
    ("grace", "expected"),
    # Trips ended 0, 1, 2 and 4 days ago: completed once MORE than `grace` days past.
    [(0, [False, True, True, True]), (1, [False, False, True, True]), (3, [False] * 3 + [True])],
)
def test_grace_days(
    db_session: Session, users_a: dict[str, User], grace: int, expected: list[bool]
) -> None:
    employee = users_a["employee"]
    trips = [_trip(db_session, employee, ended_days_ago=d) for d in (0, 1, 2, 4)]
    complete_elapsed(_factory(db_session), today=TODAY, mode="on", max_trips=500, grace_days=grace)
    assert [_status(db_session, t) == TripStatus.completed for t in trips] == expected


@pytest.mark.parametrize("mode", ["off", "OFF", "yes", "dryrun"])
def test_off_or_invalid_mode_does_nothing(
    db_session: Session,
    users_a: dict[str, User],
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    trip = _trip(db_session, users_a["employee"], ended_days_ago=10)
    with caplog.at_level(logging.INFO, logger=sweep.__name__):
        result = complete_elapsed(
            _factory(db_session), today=TODAY, mode=mode, max_trips=500, grace_days=1
        )
    assert result == {"completed": 0, "skipped": 0, "failed": 0, "would_complete": 0, "capped": 0}
    assert _status(db_session, trip) == TripStatus.approved
    assert _sweep_audits(db_session) == []
    invalid = mode.lower() != "off"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert bool(warned) is invalid, caplog.text


@pytest.mark.parametrize(("max_trips", "grace"), [(0, 1), (-1, 1), (500, -1)])
def test_an_invalid_cap_or_grace_turns_the_sweep_off(
    db_session: Session,
    users_a: dict[str, User],
    caplog: pytest.LogCaptureFixture,
    max_trips: int,
    grace: int,
) -> None:
    trip = _trip(db_session, users_a["employee"], ended_days_ago=10)
    with caplog.at_level(logging.WARNING, logger=sweep.__name__):
        result = complete_elapsed(
            _factory(db_session), today=TODAY, mode="on", max_trips=max_trips, grace_days=grace
        )
    assert result["completed"] == 0 and result["would_complete"] == 0
    assert _status(db_session, trip) == TripStatus.approved
    assert "WARNING" in caplog.text


# ---------------------------------------------------------------------------
# Settings: defaults, and a bad value never crashes the app's settings load
# ---------------------------------------------------------------------------


def _settings(**env: str) -> Settings:
    return Settings(JWT_SECRET="x" * 32, DATABASE_URL="postgresql://u:p@h/d", **env)  # type: ignore[arg-type]


def test_settings_defaults() -> None:
    cfg = sweep.resolve_config(_settings())
    assert (cfg.mode, cfg.max_trips, cfg.grace_days) == ("on", 500, 1)


@pytest.mark.parametrize(
    ("env", "mode"),
    [
        ({"ELAPSED_TRIP_SWEEP": " Dry-Run "}, "dry-run"),
        ({"ELAPSED_TRIP_SWEEP": ""}, "on"),
        ({"ELAPSED_TRIP_SWEEP": "maybe"}, "off"),
        ({"ELAPSED_TRIP_SWEEP_MAX": "lots"}, "off"),
        ({"ELAPSED_TRIP_SWEEP_MAX": "0"}, "off"),
        ({"ELAPSED_TRIP_GRACE_DAYS": "-2"}, "off"),
        ({"ELAPSED_TRIP_GRACE_DAYS": "a week"}, "off"),
        ({"ELAPSED_TRIP_SWEEP_MAX": "25", "ELAPSED_TRIP_GRACE_DAYS": "3"}, "on"),
    ],
)
def test_settings_values_resolve_safely(env: dict[str, str], mode: str) -> None:
    cfg = sweep.resolve_config(_settings(**env))
    assert cfg.mode == mode
    if env.get("ELAPSED_TRIP_SWEEP_MAX") == "25":
        assert (cfg.max_trips, cfg.grace_days) == (25, 3)
