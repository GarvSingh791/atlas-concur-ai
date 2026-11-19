"""Phase 12 follow-up — the scheduled demo reset against a REAL database.

The scheduler half that needs Postgres runs ONLY against its own throwaway
database (``<test db>_demo_autoreset``), migrated and seeded with the real
``seed_all``; it never touches the shared test database or anything else.

* a tick runs the REAL reset tool, which writes the ``demo.reset`` audit row the
  schedule is computed from; a restarted scheduler reads it back (not due an hour
  later, due a day later);
* the organisation name must be exactly "Atlas Demo Corp" (the tool refuses
  anything else — nothing written), and the env flag is re-checked every tick;
* the Postgres advisory lock: held elsewhere -> skip; two schedulers ticking at
  once -> exactly ONE reset;
* the lifespan never starts the scheduler under pytest, but outside pytest it
  starts an enabled one (never a disabled one) and stops it on shutdown;
* GET /ops/demo-reset is finance-only and shows the last reset's counts only to
  the demo organisation's own finance admin (shared test DB, rolled back).
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import backend.app.core.demo_reset_scheduler as drs
import backend.tests.factories as f
from backend.app.core.audit import record_audit
from backend.app.core.demo_reset_scheduler import (
    ADVISORY_LOCK_KEY,
    DemoResetScheduler,
    ScheduleConfig,
    pg_advisory_lock,
    read_last_reset,
)
from backend.app.core.security import create_access_token
from config.settings import get_settings
from database.models.identity import RoleKey, User
from database.seeds import reset_demo_data as rd
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = "Atlas Demo Corp"
API = "/api/v1"
DAILY = ScheduleConfig(enabled=True, interval_hours=24)


class FakeStorage(StorageProvider):
    def put_object(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        return None

    def get_object(self, key: str) -> bytes:
        return b""

    def delete_object(self, key: str) -> None:
        return None

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"fake://{key}", expires_in=expires_in)


def _url_with_db(raw_url: str, db_name: str) -> str:
    return make_url(raw_url).set(database=db_name).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    raw_url = get_settings().DATABASE_URL
    db_name = f"{make_url(raw_url).database}_demo_autoreset"
    try:
        admin = create_engine(_url_with_db(raw_url, "postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable — skipping scheduled demo reset tests")

    def _drop() -> None:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}"'))

    _drop()
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    url = _url_with_db(raw_url, db_name)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    up = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert up.returncode == 0, up.stderr
    try:
        yield eng
    finally:
        eng.dispose()
        _drop()
        admin.dispose()


@pytest.fixture(scope="module")
def factory(engine: Engine) -> Callable[[], Session]:
    from database.seeds.seed_all import seed  # noqa: PLC0415

    fac = sessionmaker(bind=engine, expire_on_commit=False)
    mp = pytest.MonkeyPatch()
    mp.setenv("STORAGE_SECRET_ACCESS_KEY", "")  # the seed's MinIO upload stays off
    try:
        seed(session_factory=fac)
    finally:
        mp.undo()
    return fac


@pytest.fixture()
def gates_open(monkeypatch: pytest.MonkeyPatch, _schema: Engine | None) -> None:
    # The lifespan tests start the real app, which probes the database: skip them
    # (like every other DB test) when Postgres is unreachable, e.g. the CI lane.
    if _schema is None:
        pytest.skip("Postgres not reachable — skipping DB-backed tests")
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    monkeypatch.setenv(rd.ENABLE_ENV, "true")


def _scheduler(
    engine: Engine,
    factory: Callable[[], Session],
    clock: Callable[[], datetime] | None = None,
    reset_fn: Callable[[], Any] | None = None,
) -> DemoResetScheduler:
    """A scheduler wired to the SCRATCH database only (real tool, real lock, real
    audit read — never the app's default engine)."""
    return DemoResetScheduler(
        DAILY,
        clock=clock,
        reset_fn=reset_fn
        or (lambda: rd.apply(DEMO, session_factory=factory, storage=FakeStorage())),
        read_last=lambda: read_last_reset(factory),
        lock=lambda: pg_advisory_lock(engine),
    )


def _reset_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                sa.text("SELECT count(*) FROM audit_log WHERE action = 'demo.reset'")
            ).scalar_one()
        )


def _db_now(engine: Engine) -> datetime:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT now()")).scalar_one()


def _clear_resets(engine: Engine) -> None:
    """Test-only: forget earlier demo.reset rows in the SCRATCH database so each
    test decides 'due' from a known state (audit_log is append-only in the app)."""
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM audit_log WHERE action = 'demo.reset'"))


# ---------------------------------------------------------------------------
# Real tool, audit-backed schedule, restart
# ---------------------------------------------------------------------------


def test_tick_runs_the_real_tool_and_a_restart_reads_the_last_reset_from_audit(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    _clear_resets(engine)
    first = _scheduler(engine, factory)
    assert first.tick() == "reset"
    assert _reset_rows(engine) == 1  # the TOOL wrote it, in the reset transaction
    last = read_last_reset(factory)
    assert last is not None
    assert first.status.last_reset_at == last
    assert first.status.last_result["deleted"]["trips"] >= 1
    assert first.tick() == "not_due"

    # "Restart": a brand-new scheduler has no memory — only the audit log.
    db_now = _db_now(engine)
    in_an_hour = _scheduler(engine, factory, clock=lambda: db_now + timedelta(hours=1))
    assert in_an_hour.tick() == "not_due"
    assert in_an_hour.status.last_reset_at == last
    assert _reset_rows(engine) == 1

    next_day = _scheduler(engine, factory, clock=lambda: db_now + timedelta(hours=25))
    assert next_day.tick() == "reset"
    assert _reset_rows(engine) == 2


def test_only_the_exactly_named_demo_org_is_reset(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    """A lookalike name ('Atlas Demo Corp ') is refused by the tool; nothing written."""
    _clear_resets(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE organizations SET name = :bad WHERE name = :good"),
            {"bad": DEMO + " ", "good": DEMO},
        )
    try:
        sched = _scheduler(engine, factory)
        assert sched.tick() == "refused"
        assert sched.status.last_error is not None
        assert "expected exactly one organisation" in sched.status.last_error
        assert _reset_rows(engine) == 0
    finally:
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE organizations SET name = :good WHERE name = :bad"),
                {"bad": DEMO + " ", "good": DEMO},
            )


def test_audit_rows_of_another_org_never_count_as_a_demo_reset(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    _clear_resets(engine)
    with factory() as s:
        other = f.make_org(s, name="Lookalike Tenant")
        record_audit(
            s,
            organization_id=other.id,
            action="demo.reset",
            entity_type="organization",
            entity_id=other.id,
        )
        s.commit()
    assert read_last_reset(factory) is None


def test_other_org_level_audit_rows_of_the_demo_org_never_count_as_a_reset(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    """Only ``demo.reset`` counts. The test-data cleanup tool writes an
    org-level row for the SAME org (``ops.test_data_cleanup``,
    entity_type='organization', entity_id=org id); if it counted, a cleanup run
    would silently push the next scheduled reset back by a full interval."""
    from database.models.identity import Organization  # noqa: PLC0415

    _clear_resets(engine)
    with factory() as s:
        demo_id = s.scalar(sa.select(Organization.id).where(Organization.name == DEMO))
        assert demo_id is not None
        record_audit(
            s,
            organization_id=demo_id,
            action="ops.test_data_cleanup",
            entity_type="organization",
            entity_id=demo_id,
            after_state={"deleted": {"expense_reports": 1}},
        )
        s.commit()
    try:
        assert read_last_reset(factory) is None
        sched = _scheduler(engine, factory)
        assert sched.tick() == "reset"  # the cleanup row did not make it "not_due"
        assert _reset_rows(engine) == 1
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM audit_log WHERE action = 'ops.test_data_cleanup'"))


def test_env_flag_removed_at_runtime_writes_nothing(
    engine: Engine, factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_resets(engine)
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    monkeypatch.delenv(rd.ENABLE_ENV, raising=False)
    sched = _scheduler(engine, factory)  # config says enabled; the gate re-checks
    assert sched.tick() == "gate_closed"
    # Even a scheduler whose gate is bypassed cannot write: the tool re-checks.
    bypass = _scheduler(engine, factory)
    bypass._gate = lambda: None
    assert bypass.tick() == "refused"
    assert _reset_rows(engine) == 0


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


def test_lock_held_by_another_instance_skips_the_reset(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    _clear_resets(engine)
    with engine.connect() as other:
        assert other.execute(
            sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
        ).scalar()
        try:
            sched = _scheduler(engine, factory)
            assert sched.tick() == "locked"
            assert _reset_rows(engine) == 0
        finally:
            other.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            other.commit()
    # Released (and our own lock released after the tick): the next tick runs.
    assert _scheduler(engine, factory).tick() == "reset"
    with engine.connect() as conn:  # nobody holds it any more
        assert conn.execute(
            sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
        ).scalar()
        conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})


def test_two_schedulers_ticking_at_once_reset_exactly_once(
    engine: Engine, factory: Callable[[], Session], gates_open: None
) -> None:
    _clear_resets(engine)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def slow_reset() -> Any:
        report = rd.apply(DEMO, session_factory=factory, storage=FakeStorage())
        return report

    def run() -> None:
        sched = _scheduler(engine, factory, reset_fn=slow_reset)
        original = sched._read_last

        def synced_read() -> Any:
            value = original()
            if not getattr(synced_read, "done", False):
                synced_read.done = True  # type: ignore[attr-defined]
                barrier.wait(10)  # both have decided "due" before either locks
            return value

        sched._read_last = synced_read
        outcomes.append(sched.tick())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert sorted(outcomes) in (["locked", "reset"], ["not_due", "reset"])
    assert _reset_rows(engine) == 1


# ---------------------------------------------------------------------------
# Lifespan: never started implicitly under pytest
# ---------------------------------------------------------------------------


def test_lifespan_does_not_start_the_scheduler_under_pytest(
    gates_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.main_entry.app import create_app  # noqa: PLC0415

    ticks: list[int] = []
    monkeypatch.setattr(get_settings(), "DEMO_RESET_SCHEDULE", "interval")

    # Belt and braces: even a started scheduler could not touch any database.
    def no_tick(self: DemoResetScheduler) -> str:
        ticks.append(1)
        return "disabled"

    monkeypatch.setattr(DemoResetScheduler, "tick", no_tick)
    application = create_app()
    with TestClient(application) as client:
        sched = application.state.demo_reset_scheduler
        assert sched.config.enabled is True  # every flag set ...
        assert sched.running is False  # ... yet not started under pytest
        assert client.get(f"{API}/public/config").json()["demo_reset"]["enabled"] is True
    assert ticks == []
    assert drs.running_under_pytest()


def _lifespan_spies(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, list[str]]:
    """Make the lifespan believe it is NOT under pytest, and replace ``tick`` with a
    recorder (no database is ever touched) and wrap ``stop`` with a spy."""
    import backend.app.main_entry.app as app_module  # noqa: PLC0415

    ticked = threading.Event()
    calls: list[str] = []

    def recording_tick(self: DemoResetScheduler) -> str:
        calls.append("tick")
        ticked.set()
        return "not_due"

    original_stop = DemoResetScheduler.stop

    async def spy_stop(self: DemoResetScheduler, grace_seconds: float = 30.0) -> None:
        calls.append("stop")
        await original_stop(self, grace_seconds)

    monkeypatch.setattr(app_module, "running_under_pytest", lambda: False)
    monkeypatch.setattr(DemoResetScheduler, "tick", recording_tick)
    monkeypatch.setattr(DemoResetScheduler, "stop", spy_stop)
    return ticked, calls


def test_lifespan_starts_the_scheduler_and_stops_it_on_shutdown(
    gates_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production wiring: with every gate open (and outside pytest) the lifespan
    STARTS the loop, which ticks; on shutdown it awaits ``stop`` which cancels the
    loop and clears the task. Removing either call turns this red."""
    from backend.app.main_entry.app import create_app  # noqa: PLC0415

    ticked, calls = _lifespan_spies(monkeypatch)
    monkeypatch.setattr(get_settings(), "DEMO_RESET_SCHEDULE", "interval")
    application = create_app()
    with TestClient(application):
        sched = application.state.demo_reset_scheduler
        assert sched.config.enabled is True
        assert sched.running is True  # started by the lifespan
        task = sched._task
        assert task is not None
        assert ticked.wait(10), "the started loop never ticked"
        assert "stop" not in calls
    # shutdown: stop() was awaited by the lifespan, the loop is cancelled
    assert calls.count("stop") == 1
    assert sched.running is False
    assert sched._task is None  # cleared by stop(), not merely by loop teardown
    assert task.done() and task.cancelled()


def test_lifespan_does_not_start_a_disabled_scheduler(
    gates_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside pytest but with the schedule OFF the loop is never started."""
    from backend.app.main_entry.app import create_app  # noqa: PLC0415

    ticked, calls = _lifespan_spies(monkeypatch)
    monkeypatch.setattr(get_settings(), "DEMO_RESET_SCHEDULE", "off")
    application = create_app()
    with TestClient(application) as client:
        sched = application.state.demo_reset_scheduler
        assert sched.config.enabled is False
        assert sched.running is False
        assert client.get(f"{API}/public/config").json()["demo_reset"]["enabled"] is False
    assert not ticked.is_set()
    assert "tick" not in calls
    assert sched._task is None


# ---------------------------------------------------------------------------
# GET /ops/demo-reset — finance only, tenant-scoped counts (shared test DB,
# rolled back by the db_session fixture)
# ---------------------------------------------------------------------------


def _client(app: Any, user: User) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update(
        {"Authorization": f"Bearer {create_access_token(user.id, user.organization_id)}"}
    )
    return client


@pytest.fixture()
def demo_tenant(db_session: Session, rbac_catalog: None) -> dict[str, Any]:
    org = f.make_org(db_session, name=DEMO)
    finance = f.make_user(db_session, org=org, role=RoleKey.finance_admin)
    record_audit(
        db_session,
        organization_id=org.id,
        action="demo.reset",
        entity_type="organization",
        entity_id=org.id,
        after_state={
            "deleted": {"trips": 4},
            "restored": {"seeded accounts": 1},
            "restored_user_ids": ["not-for-the-api"],
            "storage_objects_to_delete": 2,
        },
    )
    db_session.commit()
    return {"org": org, "finance": finance}


def test_ops_demo_reset_is_finance_only(
    app: Any,
    employee_client: TestClient,
    manager_client: TestClient,
    finance_client: TestClient,
) -> None:
    assert employee_client.get(f"{API}/ops/demo-reset").status_code == 403
    assert manager_client.get(f"{API}/ops/demo-reset").status_code == 403
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get(f"{API}/ops/demo-reset").status_code == 401
    resp = finance_client.get(f"{API}/ops/demo-reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["schedule"] == "off"
    assert body["last_result"] is None  # org A is not the demo org


def test_ops_demo_reset_shows_counts_only_to_the_demo_org(
    app: Any,
    demo_tenant: dict[str, Any],
    finance_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sched = DemoResetScheduler(
        DAILY,
        read_last=lambda: None,
        lock=lambda: nullcontext(False),
        gate=lambda: None,
        reset_fn=lambda: None,
    )
    sched.status.failures_total = 2
    sched.status.last_error = "RuntimeError"
    monkeypatch.setattr(app.state, "demo_reset_scheduler", sched, raising=False)

    body = _client(app, demo_tenant["finance"]).get(f"{API}/ops/demo-reset").json()
    assert body["enabled"] is True and body["interval_hours"] == 24
    assert body["failures_total"] == 2 and body["last_error"] == "RuntimeError"
    assert body["in_progress"] is False
    assert body["last_result"]["deleted"] == {"trips": 4}
    assert body["last_result"]["restored"] == {"seeded accounts": 1}
    assert body["last_result"]["storage_objects_to_delete"] == 2
    assert "not-for-the-api" not in str(body)

    other = finance_client.get(f"{API}/ops/demo-reset").json()
    assert other["last_result"] is None  # another tenant's finance admin
    assert other["failures_total"] == 2  # deployment-wide status is shared


def test_ops_demo_reset_ignores_other_org_level_rows_of_the_demo_org(
    app: Any, db_session: Session, rbac_catalog: None
) -> None:
    """A non-``demo.reset`` org-level row of the demo org (e.g. the test-data
    cleanup audit) is never reported as the last reset."""
    org = f.make_org(db_session, name=DEMO)
    finance = f.make_user(db_session, org=org, role=RoleKey.finance_admin)
    record_audit(
        db_session,
        organization_id=org.id,
        action="ops.test_data_cleanup",
        entity_type="organization",
        entity_id=org.id,
        after_state={"deleted": {"trips": 9}},
    )
    db_session.commit()
    resp = _client(app, finance).get(f"{API}/ops/demo-reset")
    assert resp.status_code == 200
    assert resp.json()["last_result"] is None


def test_ops_demo_reset_never_offers_a_reset_now(app: Any) -> None:
    routes = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
    assert (f"{API}/ops/demo-reset", "GET") in routes
    assert not any(p.startswith(f"{API}/ops/demo-reset") and m != "GET" for p, m in routes)
