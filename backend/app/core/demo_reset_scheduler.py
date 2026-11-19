"""Scheduled demo reset (phase 12 follow-up) — a self-contained in-process scheduler.

Render's free plan has no cron service and no shell, so the shared public demo
("Atlas Demo Corp") is reset by the backend itself.  OFF unless EVERY gate is open:

* ``DEMO_RESET_SCHEDULE=interval`` (default ``off``);
* ``DEMO_MODE=true``;
* the reset tool's own safeguard: ``DEMO_RESET_ENABLED=true`` in the PROCESS
  environment (``database.seeds.reset_demo_data.apply_refusal`` — the same check
  ``apply`` runs again itself), and the organisation named exactly
  ``DEMO_ORG_NAME`` (the tool resolves it and refuses anything else).

Invalid values (an unknown schedule, an interval below 1 hour, an hour outside
0-23) DISABLE the schedule with a startup warning; they never fail the boot.

Design
------
* An asyncio task (started in the FastAPI lifespan, cancelled on shutdown) wakes
  every ``TICK_SECONDS``.  Each tick runs in a worker thread (sync SQLAlchemy).
* "Due" is decided from the LAST SUCCESSFUL RESET: the newest ``demo.reset``
  audit row of the demo organisation (the tool writes it in the reset's own
  transaction, so it exists only for a committed reset).  Schedule state
  therefore survives restarts, deploys and Render's free-plan sleep; on wake
  after any number of missed slots ONE catch-up reset runs (the next due time
  is computed from the new last reset, which is "now").
* Cross-instance safety: a Postgres session advisory lock
  (``pg_try_advisory_lock``) around decide + reset; the loser skips the tick.
  The due check is re-read UNDER the lock, so an instance that wins the lock
  just after another one finished sees the fresh audit row and does nothing.
* The reset itself is the tool's ``apply`` (one transaction, tenant-isolation
  proofs, rollback on any error).  A failure is logged (exception type only),
  counted, and retried no sooner than ``RETRY_BACKOFF``; it never crashes the app.
* While a reset runs, ``in_progress`` is true (GET /ops/demo-reset, public
  config).  Requests are not blocked: the reset is transactional, so readers see
  the old or the new state; a request that races the reset may 404 / 409 on a
  row that just went away (documented in docs/runbooks/demo-reset.md).

The scheduler is never started implicitly under pytest (``running_under_pytest``);
tests construct it and call :meth:`DemoResetScheduler.tick` / ``start`` explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import sqlalchemy as sa

from backend.app.core.config import get_settings

logger = logging.getLogger("atlas.demo_reset.scheduler")

UTC = timezone.utc

TICK_SECONDS = 60.0
RETRY_BACKOFF = timedelta(minutes=15)
MAX_INTERVAL_HOURS = 24 * 366
SHUTDOWN_GRACE_SECONDS = 30.0
RESET_ACTION = "demo.reset"
# Fixed advisory-lock key for the demo reset ("ATLASDR1" as a signed bigint).
ADVISORY_LOCK_KEY = 0x41544C4153445231
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TickOutcome = Literal[
    "disabled", "busy", "gate_closed", "backoff", "not_due", "locked", "reset", "refused", "failed"
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    interval_hours: int = 24
    at_utc_hour: int | None = None
    # Why the schedule is off (variable NAMES only — never a value).
    reason: str = ""

    @property
    def interval(self) -> timedelta:
        return timedelta(hours=self.interval_hours)


def _parse_int(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def demo_org_name() -> str:
    from database.seeds.seed_demo_grades import DEMO_ORG_NAME  # noqa: PLC0415

    return DEMO_ORG_NAME


def gate_refusal() -> str | None:
    """Why a scheduled reset may not run right now (None = every gate is open).
    DEMO_MODE, then the reset tool's OWN environment / organisation-name check."""
    if not get_settings().DEMO_MODE:
        return "DEMO_MODE is not true"
    from database.seeds.reset_demo_data import apply_refusal  # noqa: PLC0415

    return apply_refusal(demo_org_name())


def resolve_config(settings: Any | None = None, *, warn: bool = True) -> ScheduleConfig:
    """Parse the DEMO_RESET_* settings.  Anything invalid -> disabled + warning."""
    s = settings if settings is not None else get_settings()
    schedule = str(s.DEMO_RESET_SCHEDULE or "off").strip().lower()
    if schedule in ("", "off"):
        return ScheduleConfig(enabled=False, reason="DEMO_RESET_SCHEDULE is off")

    problems: list[str] = []
    if schedule != "interval":
        problems.append("DEMO_RESET_SCHEDULE must be 'off' or 'interval'")
    interval = _parse_int(s.DEMO_RESET_INTERVAL_HOURS)
    if interval is None or not 1 <= interval <= MAX_INTERVAL_HOURS:
        problems.append(f"DEMO_RESET_INTERVAL_HOURS must be a whole number 1-{MAX_INTERVAL_HOURS}")
    at_hour: int | None = None
    raw_at = s.DEMO_RESET_AT_UTC_HOUR
    if raw_at is not None and str(raw_at).strip() != "":
        at_hour = _parse_int(raw_at)
        if at_hour is None or not 0 <= at_hour <= 23:
            problems.append("DEMO_RESET_AT_UTC_HOUR must be a whole number 0-23")
    if problems:
        reason = "invalid configuration: " + "; ".join(problems)
        if warn:
            logger.warning("Scheduled demo reset DISABLED — %s", reason)
        return ScheduleConfig(enabled=False, reason=reason)

    assert interval is not None
    refusal = gate_refusal()
    if refusal is not None:
        if warn:
            logger.warning("Scheduled demo reset DISABLED — %s", refusal)
        return ScheduleConfig(
            enabled=False, interval_hours=interval, at_utc_hour=at_hour, reason=refusal
        )
    if warn:
        logger.info(
            "Scheduled demo reset ON | every %dh%s",
            interval,
            "" if at_hour is None else f" from {at_hour:02d}:00 UTC",
        )
    return ScheduleConfig(enabled=True, interval_hours=interval, at_utc_hour=at_hour)


# ---------------------------------------------------------------------------
# Due-time maths (pure; injected clock)
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def due_at(last_reset: datetime | None, config: ScheduleConfig, now: datetime) -> datetime:
    """When the next reset becomes due (``<= now`` means due now).

    * No reset on record: due now.
    * Interval only: ``last_reset + interval``.
    * With ``at_utc_hour``: slots are ``HH:00 UTC`` on 1970-01-01 plus whole
      intervals; due at the first slot strictly after the last reset.  However
      many slots were missed, one reset moves "last" past all of them (catch-up
      runs ONCE).
    """
    if last_reset is None:
        return _utc(now)
    last = _utc(last_reset)
    if config.at_utc_hour is None:
        return last + config.interval
    anchor = _EPOCH + timedelta(hours=config.at_utc_hour)
    slots_passed = (last - anchor) // config.interval
    return anchor + (slots_passed + 1) * config.interval


def is_due(now: datetime, last_reset: datetime | None, config: ScheduleConfig) -> bool:
    return _utc(now) >= due_at(last_reset, config, now)


def next_reset_at(
    now: datetime,
    last_reset: datetime | None,
    config: ScheduleConfig,
    retry_after: datetime | None = None,
) -> datetime:
    """The expected time of the next reset attempt (never in the past)."""
    current = _utc(now)
    expected = max(due_at(last_reset, config, current), current)
    if retry_after is not None:
        expected = max(expected, _utc(retry_after))
    return expected


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else _utc(dt).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Database defaults (advisory lock, last reset from the audit log, the reset)
# ---------------------------------------------------------------------------


@contextmanager
def pg_advisory_lock(engine: sa.Engine, key: int = ADVISORY_LOCK_KEY) -> Iterator[bool]:
    """Try the session advisory lock on a dedicated AUTOCOMMIT connection; yield
    whether it was acquired.  Always released (or the connection discarded)."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(
            conn.execute(sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        )
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        except Exception:  # noqa: BLE001 — never return a lock-holding connection
            conn.invalidate()
        finally:
            conn.close()


def read_last_reset(session_factory: Callable[[], Any]) -> datetime | None:
    """Newest ``demo.reset`` audit row of the organisation named exactly the demo
    org (written only by a COMMITTED reset)."""
    from database.models.audit_log import AuditLog  # noqa: PLC0415
    from database.models.identity import Organization  # noqa: PLC0415

    with session_factory() as session:
        value = session.scalar(
            sa.select(sa.func.max(AuditLog.created_at))
            .join(Organization, Organization.id == AuditLog.organization_id)
            .where(
                Organization.name == demo_org_name(),
                AuditLog.action == RESET_ACTION,
                AuditLog.entity_type == "organization",
                AuditLog.entity_id == Organization.id,
            )
        )
    return None if value is None else _utc(value)


def _default_reset() -> Any:
    from database.seeds.reset_demo_data import apply  # noqa: PLC0415

    return apply(demo_org_name())


def running_under_pytest() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


@dataclass
class ResetStatus:
    in_progress: bool = False
    last_reset_at: datetime | None = None  # from the audit log (cached per tick)
    last_checked_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None  # this process
    last_failure_at: datetime | None = None
    last_error: str | None = None  # exception TYPE or the refusal reason
    gate_reason: str | None = None
    resets_total: int = 0
    failures_total: int = 0
    consecutive_failures: int = 0
    skipped_locked: int = 0
    last_result: dict[str, Any] = field(default_factory=dict)


class DemoResetScheduler:
    def __init__(
        self,
        config: ScheduleConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        reset_fn: Callable[[], Any] | None = None,
        read_last: Callable[[], datetime | None] | None = None,
        lock: Callable[[], AbstractContextManager[bool]] | None = None,
        gate: Callable[[], str | None] | None = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reset_fn = reset_fn or _default_reset
        self._read_last = read_last or self._default_read_last
        self._lock = lock or self._default_lock
        self._gate = gate or gate_refusal
        self.tick_seconds = tick_seconds
        self.status = ResetStatus()
        self._tick_mutex = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._inflight: asyncio.Future[TickOutcome] | None = None

    # -- defaults --------------------------------------------------------------
    @staticmethod
    def _default_read_last() -> datetime | None:
        from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

        return read_last_reset(get_sessionmaker())

    @staticmethod
    def _default_lock() -> AbstractContextManager[bool]:
        from backend.app.core.database import get_engine  # noqa: PLC0415

        return pg_advisory_lock(get_engine())

    # -- one tick (sync; runs in a worker thread) -------------------------------
    def retry_after(self) -> datetime | None:
        s = self.status
        if s.consecutive_failures and s.last_failure_at is not None:
            return s.last_failure_at + RETRY_BACKOFF
        return None

    def _fail(self, now: datetime, error: str) -> None:
        s = self.status
        s.failures_total += 1
        s.consecutive_failures += 1
        s.last_failure_at = now
        s.last_error = error

    def tick(self) -> TickOutcome:
        """Decide and (when due) reset once.  Never raises."""
        if not self.config.enabled:
            return "disabled"
        if not self._tick_mutex.acquire(blocking=False):
            return "busy"
        try:
            return self._tick(self._clock())
        except Exception as exc:  # noqa: BLE001 — the scheduler never crashes the app
            self._fail(self._clock(), type(exc).__name__)
            logger.warning("Scheduled demo reset tick failed (%s).", type(exc).__name__)
            return "failed"
        finally:
            self._tick_mutex.release()

    def _tick(self, now: datetime) -> TickOutcome:
        s = self.status
        s.gate_reason = self._gate()
        if s.gate_reason is not None:
            return "gate_closed"
        s.last_reset_at = self._read_last()
        s.last_checked_at = now
        retry_after = self.retry_after()
        if retry_after is not None and now < retry_after:
            return "backoff"
        if not is_due(now, s.last_reset_at, self.config):
            return "not_due"
        with self._lock() as acquired:
            if not acquired:
                s.skipped_locked += 1
                return "locked"
            # Re-read UNDER the lock: another instance may have just reset.
            s.last_reset_at = self._read_last()
            if not is_due(now, s.last_reset_at, self.config):
                return "not_due"
            return self._run_reset(now)

    def _run_reset(self, now: datetime) -> TickOutcome:
        from database.seeds.reset_demo_data import ResetRefused  # noqa: PLC0415

        s = self.status
        s.in_progress = True
        s.last_attempt_at = now
        try:
            report = self._reset_fn()
        except ResetRefused as exc:
            self._fail(now, f"refused: {exc}")
            logger.warning("Scheduled demo reset REFUSED — %s. Nothing was written.", exc)
            return "refused"
        except Exception as exc:  # noqa: BLE001 — logged by type only (no secrets)
            self._fail(now, type(exc).__name__)
            logger.warning("Scheduled demo reset FAILED (%s) — rolled back.", type(exc).__name__)
            return "failed"
        finally:
            s.in_progress = False
        s.resets_total += 1
        s.consecutive_failures = 0
        s.last_error = None
        s.last_success_at = now
        deleted = dict(getattr(report, "deleted", {}) or {})
        restored = dict(getattr(report, "restored", {}) or {})
        s.last_result = {
            "deleted": deleted,
            "restored": restored,
            "storage_deleted": int(getattr(report, "storage_deleted", 0) or 0),
            "storage_failed": int(getattr(report, "storage_failed", 0) or 0),
        }
        try:
            s.last_reset_at = self._read_last() or now
        except Exception:  # noqa: BLE001 — the reset itself succeeded
            s.last_reset_at = now
        logger.info(
            "Scheduled demo reset done | rows deleted=%d restored=%d",
            sum(deleted.values()),
            sum(restored.values()),
        )
        return "reset"

    # -- asyncio lifecycle ------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task[None]:
        """Start the background loop (explicit; the lifespan calls it)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="atlas-demo-reset-scheduler"
            )
        return self._task

    async def _loop(self) -> None:
        while True:
            try:
                self._inflight = asyncio.ensure_future(asyncio.to_thread(self.tick))
                # shield: a shutdown cancels the loop, never the reset mid-flight
                await asyncio.shield(self._inflight)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                logger.warning("Scheduled demo reset loop error (%s).", type(exc).__name__)
            await asyncio.sleep(self.tick_seconds)

    async def stop(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Cancel the loop; give a reset in flight up to ``grace_seconds`` to finish
        (it is one transaction: if the process dies first, Postgres rolls it back)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            done, _ = await asyncio.wait({inflight}, timeout=grace_seconds)
            if not done:
                logger.warning("Shutdown: a demo reset was still running (it is transactional).")

    # -- views --------------------------------------------------------------------
    def public_view(self) -> dict[str, Any]:
        """GET /public/config ``demo_reset`` — non-secret schedule facts only."""
        if not self.config.enabled:
            return {
                "enabled": False,
                "interval_hours": None,
                "next_reset_at": None,
                "last_reset_at": None,
            }
        s = self.status
        nxt = None
        if s.last_checked_at is not None:
            nxt = next_reset_at(self._clock(), s.last_reset_at, self.config, self.retry_after())
        return {
            "enabled": True,
            "interval_hours": self.config.interval_hours,
            "next_reset_at": _iso(nxt),
            "last_reset_at": _iso(s.last_reset_at),
        }

    def status_view(self) -> dict[str, Any]:
        """GET /ops/demo-reset — deployment-wide schedule state (no tenant data)."""
        s = self.status
        view = self.public_view()
        return {
            **view,
            "schedule": "interval" if self.config.enabled else "off",
            "at_utc_hour": self.config.at_utc_hour if self.config.enabled else None,
            "disabled_reason": None if self.config.enabled else self.config.reason,
            "gate_reason": s.gate_reason,
            "running": self.running,
            "in_progress": s.in_progress,
            "last_checked_at": _iso(s.last_checked_at),
            "last_attempt_at": _iso(s.last_attempt_at),
            "last_success_at": _iso(s.last_success_at),
            "last_failure_at": _iso(s.last_failure_at),
            "last_error": s.last_error,
            "retry_after": _iso(self.retry_after()),
            "resets_total": s.resets_total,
            "failures_total": s.failures_total,
            "consecutive_failures": s.consecutive_failures,
            "skipped_locked": s.skipped_locked,
        }


def disabled_public_view() -> dict[str, Any]:
    return DemoResetScheduler(ScheduleConfig(enabled=False)).public_view()
