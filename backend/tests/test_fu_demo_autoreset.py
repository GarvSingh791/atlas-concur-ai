"""Phase 12 follow-up — the scheduled demo reset (backend/app/core/demo_reset_scheduler.py).

Hermetic: every collaborator (clock, last reset, advisory lock, the reset itself,
the gate) is injected, so no database is touched.  The DB-backed half (the real
tool, the real advisory lock, the audit log, the lifespan, the finance endpoint)
is backend/tests/test_fu_demo_autoreset_db.py.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

import backend.app.core.demo_reset_scheduler as drs
from backend.app.core.demo_reset_scheduler import (
    RETRY_BACKOFF,
    UTC,
    DemoResetScheduler,
    ScheduleConfig,
    due_at,
    is_due,
    next_reset_at,
    resolve_config,
)
from config.settings import get_settings
from database.seeds import reset_demo_data as rd

T0 = datetime(2026, 11, 15, 10, 30, tzinfo=UTC)
DAILY = ScheduleConfig(enabled=True, interval_hours=24)
DAILY_AT_4 = ScheduleConfig(enabled=True, interval_hours=24, at_utc_hour=4)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeAudit:
    """Stands in for the demo.reset audit rows: a reset records "now"."""

    def __init__(self, last: datetime | None = None) -> None:
        self.last = last

    def __call__(self) -> datetime | None:
        return self.last


def make(
    config: ScheduleConfig = DAILY,
    *,
    clock: Clock | None = None,
    audit: FakeAudit | None = None,
    reset: Any = None,
    lock: Any = None,
    gate: Any = None,
) -> tuple[DemoResetScheduler, Clock, FakeAudit, list[datetime]]:
    clock = clock or Clock()
    audit = audit or FakeAudit()
    runs: list[datetime] = []

    def _reset() -> Any:
        runs.append(clock())
        audit.last = clock()
        return rd.ResetReport(mode="apply", deleted={"trips": 3}, restored={"x": 1})

    sched = DemoResetScheduler(
        config,
        clock=clock,
        reset_fn=reset or _reset,
        read_last=audit,
        lock=lock or (lambda: nullcontext(True)),
        gate=gate or (lambda: None),
        tick_seconds=0.01,
    )
    return sched, clock, audit, runs


# ---------------------------------------------------------------------------
# Due-time maths (injected clock)
# ---------------------------------------------------------------------------


def test_interval_due_time_is_last_reset_plus_interval() -> None:
    last = T0 - timedelta(hours=23, minutes=59)
    assert due_at(last, DAILY, T0) == last + timedelta(hours=24)
    assert not is_due(T0, last, DAILY)
    assert is_due(T0, T0 - timedelta(hours=24), DAILY)
    twelve = ScheduleConfig(enabled=True, interval_hours=12)
    assert is_due(T0, T0 - timedelta(hours=12), twelve)
    assert not is_due(T0, T0 - timedelta(hours=11), twelve)


def test_no_reset_on_record_is_due_now() -> None:
    assert is_due(T0, None, DAILY)
    assert is_due(T0, None, DAILY_AT_4)
    assert next_reset_at(T0, None, DAILY) == T0


def test_at_hour_runs_at_the_first_opportunity_after_the_slot() -> None:
    last = datetime(2026, 11, 14, 4, 5, tzinfo=UTC)  # yesterday's 04:00 slot ran at 04:05
    assert due_at(last, DAILY_AT_4, T0) == datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
    # A reset at 23:00 (e.g. a manual run) still waits for the next 04:00 slot.
    late = datetime(2026, 11, 14, 23, 0, tzinfo=UTC)
    assert due_at(late, DAILY_AT_4, T0) == datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
    assert not is_due(datetime(2026, 11, 15, 3, 59, tzinfo=UTC), late, DAILY_AT_4)
    assert is_due(datetime(2026, 11, 15, 4, 0, tzinfo=UTC), late, DAILY_AT_4)
    # After this morning's 04:02 reset, the next is tomorrow 04:00 (not +24h = 04:02).
    today = datetime(2026, 11, 15, 4, 2, tzinfo=UTC)
    assert next_reset_at(T0, today, DAILY_AT_4) == datetime(2026, 11, 16, 4, 0, tzinfo=UTC)


def test_at_hour_with_a_six_hour_interval() -> None:
    six = ScheduleConfig(enabled=True, interval_hours=6, at_utc_hour=4)
    last = datetime(2026, 11, 15, 10, 1, tzinfo=UTC)  # the 10:00 slot ran
    assert due_at(last, six, T0) == datetime(2026, 11, 15, 16, 0, tzinfo=UTC)


def test_next_reset_is_never_in_the_past_and_honours_the_retry_backoff() -> None:
    long_ago = T0 - timedelta(days=9)
    assert next_reset_at(T0, long_ago, DAILY) == T0
    retry = T0 + timedelta(minutes=15)
    assert next_reset_at(T0, long_ago, DAILY, retry) == retry


@pytest.mark.parametrize("config", [DAILY, DAILY_AT_4], ids=["interval", "at-hour"])
def test_catch_up_after_a_long_sleep_runs_exactly_once(config: ScheduleConfig) -> None:
    """Render free-plan sleep: 10 days of missed slots -> ONE reset on wake, then
    nothing until the next slot."""
    sched, clock, _audit, runs = make(config, audit=FakeAudit(T0 - timedelta(days=10)))
    outcomes = []
    for _ in range(120):  # two hours of minute ticks after waking
        outcomes.append(sched.tick())
        clock.advance(minutes=1)
    assert runs == [T0]
    assert outcomes[0] == "reset"
    assert set(outcomes[1:]) == {"not_due"}


def test_interval_schedule_runs_once_per_interval_over_three_days() -> None:
    sched, clock, _audit, runs = make(DAILY_AT_4, audit=FakeAudit(T0 - timedelta(hours=1)))
    for _ in range(3 * 24 * 60):
        sched.tick()
        clock.advance(minutes=1)
    assert [r.isoformat() for r in runs] == [
        "2026-11-16T04:00:00+00:00",
        "2026-11-17T04:00:00+00:00",
        "2026-11-18T04:00:00+00:00",
    ]


# ---------------------------------------------------------------------------
# Configuration: disabled unless every flag is set; invalid values disable
# ---------------------------------------------------------------------------


def _settings(**over: Any) -> SimpleNamespace:
    base = {
        "DEMO_RESET_SCHEDULE": "interval",
        "DEMO_RESET_INTERVAL_HOURS": "24",
        "DEMO_RESET_AT_UTC_HOUR": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def all_gates_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    monkeypatch.setenv(rd.ENABLE_ENV, "true")


def test_enabled_only_when_every_flag_is_set(all_gates_open: None) -> None:
    cfg = resolve_config(_settings(DEMO_RESET_AT_UTC_HOUR="4"))
    assert cfg == ScheduleConfig(enabled=True, interval_hours=24, at_utc_hour=4)


def test_code_defaults_are_off() -> None:
    s = get_settings()
    assert s.DEMO_RESET_SCHEDULE == "off"
    assert s.DEMO_RESET_INTERVAL_HOURS == "24"
    assert s.DEMO_RESET_AT_UTC_HOUR is None
    assert resolve_config().enabled is False


@pytest.mark.parametrize(
    ("demo_mode", "env", "schedule"),
    [
        (True, "true", "off"),
        (True, "true", ""),
        (False, "true", "interval"),
        (True, None, "interval"),
        (True, "false", "interval"),
        (True, "1", "interval"),
        (True, "yes", "interval"),
    ],
    ids=["schedule-off", "schedule-empty", "demo-mode-off", "no-env", "env-false", "env-1", "yes"],
)
def test_disabled_unless_every_flag_is_set(
    monkeypatch: pytest.MonkeyPatch, demo_mode: bool, env: str | None, schedule: str
) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", demo_mode)
    if env is None:
        monkeypatch.delenv(rd.ENABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(rd.ENABLE_ENV, env)
    assert resolve_config(_settings(DEMO_RESET_SCHEDULE=schedule)).enabled is False


def test_the_env_gate_is_the_reset_tools_own_check(
    all_gates_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shared safeguard: the scheduler asks the tool (apply_refusal) and never
    re-implements it — a stricter tool check closes the scheduler too."""
    seen: list[str | None] = []

    def strict(org_name: str | None) -> str | None:
        seen.append(org_name)
        return "tool says no"

    monkeypatch.setattr(rd, "apply_refusal", strict)
    cfg = resolve_config(_settings())
    assert cfg.enabled is False and cfg.reason == "tool says no"
    assert seen == ["Atlas Demo Corp"]


@pytest.mark.parametrize(
    "over",
    [
        {"DEMO_RESET_SCHEDULE": "daily"},
        {"DEMO_RESET_SCHEDULE": "cron"},
        {"DEMO_RESET_INTERVAL_HOURS": "0"},
        {"DEMO_RESET_INTERVAL_HOURS": "-3"},
        {"DEMO_RESET_INTERVAL_HOURS": "1.5"},
        {"DEMO_RESET_INTERVAL_HOURS": "abc"},
        {"DEMO_RESET_INTERVAL_HOURS": "99999"},
        {"DEMO_RESET_AT_UTC_HOUR": "24"},
        {"DEMO_RESET_AT_UTC_HOUR": "-1"},
        {"DEMO_RESET_AT_UTC_HOUR": "4am"},
    ],
)
def test_invalid_values_disable_with_a_startup_warning(
    all_gates_open: None, caplog: pytest.LogCaptureFixture, over: dict[str, str]
) -> None:
    with caplog.at_level(logging.WARNING, logger="atlas.demo_reset.scheduler"):
        cfg = resolve_config(_settings(**over))
    assert cfg.enabled is False
    assert "invalid configuration" in cfg.reason
    assert any("DISABLED" in r.getMessage() for r in caplog.records)
    # Names only, never the raw value.
    assert all(str(list(over.values())[0]) not in r.getMessage() for r in caplog.records)


def test_case_and_whitespace_are_tolerated(all_gates_open: None) -> None:
    cfg = resolve_config(
        _settings(
            DEMO_RESET_SCHEDULE=" Interval ",
            DEMO_RESET_INTERVAL_HOURS=" 6 ",
            DEMO_RESET_AT_UTC_HOUR="",
        )
    )
    assert cfg == ScheduleConfig(enabled=True, interval_hours=6, at_utc_hour=None)


def test_a_disabled_scheduler_never_resets() -> None:
    sched, _clock, _audit, runs = make(ScheduleConfig(enabled=False))
    assert sched.tick() == "disabled"
    assert runs == []


def test_a_gate_closed_at_runtime_stops_the_reset() -> None:
    """Fail closed per tick: DEMO_MODE / DEMO_RESET_ENABLED re-checked every time."""
    sched, _clock, _audit, runs = make(gate=lambda: "DEMO_MODE is not true")
    assert sched.tick() == "gate_closed"
    assert runs == [] and sched.status.gate_reason == "DEMO_MODE is not true"


def test_default_gate_checks_demo_mode_then_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", False)
    monkeypatch.setenv(rd.ENABLE_ENV, "true")
    assert drs.gate_refusal() == "DEMO_MODE is not true"
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    assert drs.gate_refusal() is None
    monkeypatch.delenv(rd.ENABLE_ENV)
    assert "DEMO_RESET_ENABLED" in (drs.gate_refusal() or "")


def test_default_reset_names_the_demo_org_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def fake_apply(org: str, **kw: Any) -> str:
        calls.append((org, kw))
        return "report"

    monkeypatch.setattr(rd, "apply", fake_apply)
    assert drs._default_reset() == "report"
    assert calls == [("Atlas Demo Corp", {})]


# ---------------------------------------------------------------------------
# Lock, failures, backoff
# ---------------------------------------------------------------------------


def test_lock_held_elsewhere_skips_the_tick() -> None:
    sched, _clock, _audit, runs = make(lock=lambda: nullcontext(False))
    assert sched.tick() == "locked"
    assert runs == [] and sched.status.skipped_locked == 1


def test_due_is_re_read_under_the_lock() -> None:
    """Another instance finished a reset between our check and our lock: skip."""
    reads = iter([None, T0])  # before the lock: never reset; under it: just now
    sched, _clock, _audit, runs = make(audit=FakeAudit())
    sched._read_last = lambda: next(reads)
    assert sched.tick() == "not_due"
    assert runs == []


def test_failure_is_counted_and_retried_no_sooner_than_15_minutes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[datetime] = []
    clock = Clock()

    def boom() -> Any:
        attempts.append(clock())
        # Built at runtime: no secret-shaped literal for scripts/ops/secret_scan.py.
        raise RuntimeError("postgresql://atlas:" + "s3cr3t" + "-pw@db.internal/atlas")

    sched, _c, _audit, _runs = make(clock=clock, reset=boom)
    with caplog.at_level(logging.INFO):
        for _ in range(40):
            sched.tick()
            clock.advance(minutes=1)
    assert attempts == [T0, T0 + RETRY_BACKOFF, T0 + 2 * RETRY_BACKOFF]
    assert RETRY_BACKOFF == timedelta(minutes=15)
    st = sched.status
    assert st.failures_total == 3 and st.consecutive_failures == 3
    assert st.last_error == "RuntimeError" and st.in_progress is False
    # No secrets in logs or status: the exception TYPE only.
    assert "s3cr3t" not in caplog.text and "s3cr3t" not in str(sched.status_view())


def test_success_after_failures_clears_the_backoff() -> None:
    clock = Clock()
    fail = {"on": True}
    audit = FakeAudit()

    def flaky() -> Any:
        if fail["on"]:
            raise RuntimeError("down")
        audit.last = clock()
        return rd.ResetReport(mode="apply")

    sched, _c, _a, _r = make(clock=clock, audit=audit, reset=flaky)
    assert sched.tick() == "failed"
    fail["on"] = False
    clock.advance(minutes=5)
    assert sched.tick() == "backoff"
    clock.advance(minutes=10)
    assert sched.tick() == "reset"
    assert sched.status.consecutive_failures == 0 and sched.retry_after() is None
    assert sched.status.failures_total == 1 and sched.status.resets_total == 1


def test_a_refusal_is_counted_and_backs_off() -> None:
    def refuse() -> Any:
        raise rd.ResetRefused("expected exactly one organisation named 'Atlas Demo Corp'")

    sched, clock, _audit, _runs = make(reset=refuse)
    assert sched.tick() == "refused"
    assert sched.status.last_error is not None
    assert sched.status.last_error.startswith("refused: expected exactly one")
    clock.advance(minutes=1)
    assert sched.tick() == "backoff"


def test_a_broken_database_never_raises_out_of_tick() -> None:
    def broken() -> datetime | None:
        raise ConnectionError("db down")

    sched, _clock, _audit, runs = make()
    sched._read_last = broken
    assert sched.tick() == "failed"
    assert runs == [] and sched.status.failures_total == 1


def test_in_progress_is_visible_during_the_reset() -> None:
    seen: list[bool] = []
    sched_holder: dict[str, DemoResetScheduler] = {}

    def observe() -> Any:
        seen.append(sched_holder["s"].status_view()["in_progress"])
        return rd.ResetReport(mode="apply")

    sched, _clock, _audit, _runs = make(reset=observe)
    sched_holder["s"] = sched
    assert sched.tick() == "reset"
    assert seen == [True] and sched.status.in_progress is False


def test_concurrent_ticks_in_one_process_do_not_overlap() -> None:
    started, release = threading.Event(), threading.Event()

    def slow() -> Any:
        started.set()
        release.wait(5)
        return rd.ResetReport(mode="apply")

    sched, _clock, _audit, _runs = make(reset=slow)
    t = threading.Thread(target=sched.tick)
    t.start()
    assert started.wait(5)
    assert sched.tick() == "busy"
    release.set()
    t.join(5)


# ---------------------------------------------------------------------------
# asyncio lifecycle
# ---------------------------------------------------------------------------


def test_start_ticks_and_stop_cancels_cleanly() -> None:
    sched, _clock, _audit, runs = make()

    async def scenario() -> asyncio.Task[None]:
        task = sched.start()
        assert sched.start() is task  # idempotent
        for _ in range(100):
            await asyncio.sleep(0.01)
            if runs:
                break
        assert sched.running
        await sched.stop()
        return task

    task = asyncio.run(scenario())
    assert task.done() and task.cancelled()
    assert not sched.running
    assert len(runs) == 1


def test_stop_lets_a_reset_in_flight_finish() -> None:
    finished = threading.Event()

    def slow() -> Any:
        time.sleep(0.3)
        finished.set()
        return rd.ResetReport(mode="apply")

    sched, _clock, _audit, _runs = make(reset=slow)

    async def scenario() -> None:
        sched.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if sched.status.in_progress:
                break
        await sched.stop(grace_seconds=5)

    asyncio.run(scenario())
    assert finished.is_set() and sched.status.resets_total == 1


def test_the_loop_survives_a_tick_that_raises() -> None:
    sched, _clock, _audit, _runs = make()
    calls = {"n": 0}

    def bad_tick() -> Any:
        calls["n"] += 1
        raise RuntimeError("unexpected")

    sched.tick = bad_tick  # type: ignore[method-assign]

    async def scenario() -> None:
        sched.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if calls["n"] >= 3:
                break
        assert sched.running
        await sched.stop()

    asyncio.run(scenario())
    assert calls["n"] >= 3


def test_running_under_pytest_is_detected() -> None:
    assert drs.running_under_pytest() is True


# ---------------------------------------------------------------------------
# GET /public/config — the banner payload
# ---------------------------------------------------------------------------


@contextmanager
def _installed(sched: DemoResetScheduler | None) -> Iterator[TestClient]:
    from backend.app.main_entry.app import app  # noqa: PLC0415

    mp = pytest.MonkeyPatch()
    if sched is None:
        mp.delattr(app.state, "demo_reset_scheduler", raising=False)
    else:
        mp.setattr(app.state, "demo_reset_scheduler", sched, raising=False)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        mp.undo()


DISABLED = {"enabled": False, "interval_hours": None, "next_reset_at": None, "last_reset_at": None}


def test_public_config_reports_reset_disabled_by_default() -> None:
    with _installed(None) as client:
        body = client.get("/api/v1/public/config").json()
    assert body == {"demo_mode": False, "demo_reset": DISABLED, "demo_org_code": None}


def test_public_config_reports_the_schedule_when_enabled() -> None:
    last = datetime(2026, 11, 15, 4, 1, tzinfo=UTC)
    sched, _clock, _audit, _runs = make(DAILY_AT_4, audit=FakeAudit(last))
    assert sched.tick() == "not_due"
    with _installed(sched) as client:
        body = client.get("/api/v1/public/config").json()
    assert body["demo_reset"] == {
        "enabled": True,
        "interval_hours": 24,
        "next_reset_at": "2026-11-16T04:00:00Z",
        "last_reset_at": "2026-11-15T04:01:00Z",
    }


def test_public_config_next_reset_unknown_before_the_first_check() -> None:
    sched, _clock, _audit, _runs = make()
    with _installed(sched) as client:
        body = client.get("/api/v1/public/config").json()
    assert body["demo_reset"]["enabled"] is True
    assert body["demo_reset"]["next_reset_at"] is None


def test_public_config_disabled_scheduler_reveals_nothing() -> None:
    sched = DemoResetScheduler(
        ScheduleConfig(enabled=False, interval_hours=6, reason="DEMO_MODE is not true")
    )
    with _installed(sched) as client:
        body = client.get("/api/v1/public/config").json()
    assert body["demo_reset"] == DISABLED
