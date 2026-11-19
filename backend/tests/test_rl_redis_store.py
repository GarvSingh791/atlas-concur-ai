"""Redis rate-limit store — the Redis-only guarantees (phase 12 follow-up).

Hermetic: fakeredis with real Lua (lupa); no Redis server, no database.  The
semantic contract shared with the in-memory store is test_rl_store_contract.py;
this file covers what only Redis has: selection by settings, failure modes
(memory / closed / open) and recovery, NOSCRIPT reload, TTLs on every key, no
raw identifiers in Redis, server time, and two instances sharing ONE budget.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, UnauthorizedError, app_error_handler
from backend.app.core.rate_limit import (
    Check,
    InMemoryRateLimitStore,
    RateLimitConfig,
    RateLimiter,
    RateLimitMiddleware,
    RateLimitUnavailableError,
    guard_login,
)
from backend.app.core.rate_limit_redis import (
    RedisRateLimitStore,
    build_rate_limiter,
    derive_secret,
)
from backend.tests.rate_limit_support import NOON, FakeClock, make_limiter
from config.settings import Settings
from config.settings.rates import Rate

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("lupa") is None or importlib.util.find_spec("fakeredis") is None,
    reason=(
        "!!! fakeredis Lua support (pip install 'fakeredis[lua]') is MISSING — "
        "the Redis rate-limit store is NOT verified by this run !!!"
    ),
)

DAY = 86400.0
PREFIX = "atlas:rl:"
JWT = "unit-test-jwt-secret-" + "k3" * 16


def _server() -> Any:
    import fakeredis  # noqa: PLC0415

    return fakeredis.FakeServer()


def _client(server: Any = None) -> Any:
    import fakeredis  # noqa: PLC0415

    return fakeredis.FakeRedis(server=server if server is not None else _server())


@pytest.fixture(autouse=True)
def _no_redis_debug_logging() -> Any:
    """Isolation: when an earlier test leaves DEBUG logging on, redis-py's
    failure debug line calls ``str()`` on fakeredis' connection, which raises
    AttributeError instead of the emulated ConnectionError (a fakeredis quirk;
    a real connection formats fine)."""
    lg = logging.getLogger("redis")
    previous = lg.level
    lg.setLevel(logging.INFO)
    yield
    lg.setLevel(previous)


class Mono:
    """A controllable monotonic clock for the circuit breaker / warning throttle."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _store(client: Any = None, **kw: Any) -> RedisRateLimitStore:
    kw.setdefault("secret", b"redis-test-secret")
    kw.setdefault("server_time", False)
    return RedisRateLimitStore(client if client is not None else _client(), **kw)


def _settings(**update: Any) -> Any:
    base: dict[str, Any] = {"RATE_LIMIT_ENABLED": True, "JWT_SECRET": JWT}
    return get_settings().model_copy(update={**base, **update})


def _app(limiter: Any) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_middleware(RateLimitMiddleware)
    app.state.rate_limiter = limiter

    @app.get("/api/v1/things")
    def things() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _wrong_password() -> str:
    raise UnauthorizedError("Invalid email or password.")


def _every_key_and_value(client: Any) -> list[str]:
    """Every key, hash field / value and sorted-set member, decoded."""
    out: list[str] = []
    for raw in client.scan_iter("*"):
        key = raw.decode()
        out.append(key)
        kind = client.type(raw).decode()
        if kind == "hash":
            for f, v in client.hgetall(raw).items():
                out += [f.decode(), v.decode()]
        elif kind == "zset":
            out += [m.decode() for m in client.zrange(raw, 0, -1)]
        elif kind == "string":
            out.append(client.get(raw).decode())
    return out


# ---------------------------------------------------------------------------
# Selection by settings; defaults unchanged
# ---------------------------------------------------------------------------


def test_defaults_keep_the_in_memory_store() -> None:
    def default(name: str) -> Any:
        return Settings.model_fields[name].default

    assert default("RATE_LIMIT_STORE") == "memory"
    assert default("REDIS_URL") == ""
    assert default("RATE_LIMIT_REDIS_TIMEOUT_MS") == 50
    assert default("RATE_LIMIT_REDIS_PREFIX") == "atlas:rl:"
    assert default("RATE_LIMIT_REDIS_FAILURE") == "memory"
    limiter = build_rate_limiter(_settings())
    assert isinstance(limiter.store, InMemoryRateLimitStore)
    assert limiter.snapshot()["storage"] == "in-process (single instance)"
    assert limiter.snapshot()["store"] == {"backend": "memory", "shared": False}


def test_store_settings_are_validated_and_normalised() -> None:
    def make(**kw: Any) -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            JWT_SECRET="x" * 40,
            DATABASE_URL="postgresql://u@h/d",
            **kw,
        )

    assert make(RATE_LIMIT_STORE=" Redis ").RATE_LIMIT_STORE == "redis"
    assert make(RATE_LIMIT_REDIS_FAILURE="CLOSED").RATE_LIMIT_REDIS_FAILURE == "closed"
    for bad in ({"RATE_LIMIT_STORE": "mongo"}, {"RATE_LIMIT_REDIS_FAILURE": "maybe"}):
        with pytest.raises(ValidationError):
            make(**bad)
    with pytest.raises(ValidationError):
        make(RATE_LIMIT_REDIS_TIMEOUT_MS=0)


def test_redis_is_selected_by_settings_and_uses_server_time_and_the_prefix() -> None:
    client = _client()
    limiter = build_rate_limiter(
        _settings(RATE_LIMIT_STORE="redis", RATE_LIMIT_REDIS_PREFIX="t1:"), client=client
    )
    store = limiter.store
    assert isinstance(store, RedisRateLimitStore)
    assert store.server_time is True and store.failure_mode == "memory"
    TestClient(_app(limiter)).get("/api/v1/things")
    keys = [k.decode() for k in client.scan_iter("*")]
    assert keys and all(k.startswith("t1:") for k in keys)
    snap = limiter.snapshot()
    assert snap["storage"] == "redis (shared by every instance)"
    assert snap["store"]["backend"] == "redis" and snap["store"]["healthy"] is True


def test_boot_with_an_empty_redis_url_never_blocks_and_still_limits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        limiter = build_rate_limiter(
            _settings(RATE_LIMIT_STORE="redis", REDIS_URL="", RATE_LIMIT_DEFAULT="2/minute")
        )
    assert "REDIS_URL is empty or unusable" in caplog.text
    client = TestClient(_app(limiter))
    assert [client.get("/api/v1/things").status_code for _ in range(3)] == [200, 200, 429]
    status = limiter.snapshot()["store"]
    assert status["configured"] is False and status["healthy"] is False
    assert status["fallback_events"] >= 3


def test_boot_with_an_unreachable_redis_logs_and_runs_on_the_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = "pw-" + "z9" * 8
    url = f"redis://:{password}@127.0.0.1:1/0"  # nothing listens on port 1
    with caplog.at_level(logging.WARNING):
        limiter = build_rate_limiter(
            _settings(RATE_LIMIT_STORE="redis", REDIS_URL=url, RATE_LIMIT_DEFAULT="1/minute")
        )
    assert "not answering at boot" in caplog.text
    client = TestClient(_app(limiter))
    assert [client.get("/api/v1/things").status_code for _ in range(2)] == [200, 429]
    snap = limiter.snapshot()
    assert snap["store"]["healthy"] is False and snap["store"]["redis_errors"] >= 1
    for text in (repr(snap), caplog.text):
        assert password not in text and "127.0.0.1:1" not in text


def test_a_malformed_redis_url_never_blocks_boot() -> None:
    limiter = build_rate_limiter(_settings(RATE_LIMIT_STORE="redis", REDIS_URL="not a url"))
    assert isinstance(limiter.store, RedisRateLimitStore)
    assert limiter.store.consume([Check("r", "k", Rate(1, 60.0))], NOON).allowed


# ---------------------------------------------------------------------------
# Shared budget across instances
# ---------------------------------------------------------------------------


def test_two_instances_sharing_one_redis_enforce_one_budget() -> None:
    server = _server()
    settings = _settings(RATE_LIMIT_STORE="redis", RATE_LIMIT_DEFAULT="4/minute")
    a = TestClient(_app(build_rate_limiter(settings, client=_client(server))))
    b = TestClient(_app(build_rate_limiter(settings, client=_client(server))))
    codes = [(a if i % 2 else b).get("/api/v1/things").status_code for i in range(8)]
    assert codes.count(200) == 4  # one budget of 4, not 4 per instance
    assert codes[4:] == [429] * 4


def test_two_instances_share_login_backoff_and_the_distributed_guard() -> None:
    server = _server()
    settings = _settings(
        RATE_LIMIT_STORE="redis", LOGIN_BACKOFF_THRESHOLD=2, LOGIN_EMAIL_FAILURE_ALERT=3
    )
    one = build_rate_limiter(settings, client=_client(server))
    two = build_rate_limiter(settings, client=_client(server))

    def login(limiter: RateLimiter, ip: str, ok: bool) -> str:
        app = FastAPI()
        app.state.rate_limiter = limiter
        scope = {"type": "http", "app": app, "client": (ip, 1), "headers": []}

        def attempt() -> str:
            if ok:
                return "ok"
            raise UnauthorizedError("Invalid email or password.")

        try:
            return guard_login(Request(scope), "victim@example.test", attempt)
        except AppError as exc:
            return str(exc.status_code)

    # Failures from one address alternate between instances: ONE backoff row.
    assert [login(one, "203.0.113.1", False), login(two, "203.0.113.1", False)] == ["401"] * 2
    assert login(one, "203.0.113.1", True) == "429"
    assert login(two, "203.0.113.1", True) == "429"
    # The per-email count is shared too: the third failing address (on the
    # other instance) crosses the alert and is stepped up.
    assert login(two, "203.0.113.2", False) == "401"
    assert login(one, "203.0.113.2", True) == "429"
    assert one.snapshot()["login_email_failure_alert"]["emails_alerted"] == 1
    assert two.snapshot()["login_email_failure_alert"]["emails_alerted"] == 1  # shared metric


def test_redis_time_makes_instances_with_skewed_clocks_agree() -> None:
    """server_time=True: the script reads Redis TIME, so an instance whose clock
    is a day off still counts against the SAME daily window."""
    server = _server()
    rate = Rate(2, DAY, fixed=True)
    late = _store(_client(server), server_time=True)
    early = _store(_client(server), server_time=True)
    checks = [Check("ai:user_daily", "user:u", rate)]
    assert late.consume(checks, NOON).allowed
    assert early.consume(checks, NOON - DAY).allowed
    assert not late.consume(checks, NOON + DAY).allowed  # caller clocks are ignored


def test_the_identity_secret_is_derived_not_the_app_secret() -> None:
    secret = derive_secret(JWT)
    assert secret == derive_secret(JWT) and secret != derive_secret(JWT + "x")
    assert JWT.encode() not in secret and len(secret) == 32
    assert derive_secret(JWT, "rate-limit-store") != secret


def test_concurrent_instances_never_overspend() -> None:
    server = _server()
    stores = [_store(_client(server)) for _ in range(4)]
    allowed: list[bool] = []
    lock = threading.Lock()
    rate = Rate(50, 3600.0)

    def worker(store: RedisRateLimitStore) -> None:
        for _ in range(30):
            ok = store.consume([Check("r", "shared", rate)], NOON).allowed
            with lock:
                allowed.append(ok)

    threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allowed.count(True) == 50 and len(allowed) == 120


# ---------------------------------------------------------------------------
# Keys: TTLs everywhere, no raw identifiers
# ---------------------------------------------------------------------------


def _workload(store: RedisRateLimitStore) -> None:
    kw: dict[str, Any] = {"threshold": 1, "base": 60, "cap": 60, "window": 900}
    for i in range(6):  # per-IP rows past the owner cap -> owner overflow row
        store.consume([Check("r", f"ip/{i}", Rate(5, 60.0), owner="ip", net="net")], NOON)
    store.consume([Check("q", "user:u", Rate(5, DAY, fixed=True))], NOON)
    for i in range(6):  # backoff rows past the owner cap -> backoff overflow row
        store.record_failure(f"ip/e{i}", NOON, owner="ip", net="net", **kw)
    store.record_signal("email:e", NOON, window=3600.0)
    store.incr_metric("throttled:ai")


def test_every_key_has_a_ttl() -> None:
    client = _client()
    store = _store(client, max_keys_per_ip=4)
    _workload(store)
    keys = list(client.scan_iter("*"))
    kinds = {k.decode().split(":")[2] for k in keys}
    assert {"r", "t", "ow", "nw", "ob", "nb", "m"} <= kinds
    assert any(b":r:o:" in k for k in keys) and any(b":r:x:" in k for k in keys)
    for key in keys:
        ttl = client.pttl(key)
        assert ttl > 0, (key, ttl)


def test_key_ttls_follow_the_window() -> None:
    client = _client()
    store = _store(client)
    store.consume([Check("r", "k", Rate(5, 60.0))], NOON)  # sliding: up to 2 windows
    ttl = client.pttl(PREFIX + "r:q:r|k")
    assert 0 < ttl <= 120_000 + store.TTL_SLACK_MS


def test_no_raw_identifier_ever_reaches_redis() -> None:
    client = _client()
    limiter = build_rate_limiter(
        _settings(RATE_LIMIT_STORE="redis", TRUSTED_PROXY_COUNT=1, LOGIN_BACKOFF_THRESHOLD=1),
        client=client,
    )
    ip, ip6, email = "198.51.100.77", "2001:db8:77::5", "Victim.Person@Example.test"
    app = _app(limiter)
    http = TestClient(app)
    http.get("/api/v1/things", headers={"X-Forwarded-For": ip})
    http.get("/api/v1/things", headers={"X-Forwarded-For": ip6})
    for peer in (ip, ip6):
        scope = {"type": "http", "app": app, "client": (peer, 1), "headers": []}
        with pytest.raises(UnauthorizedError):
            guard_login(Request(scope), email, _wrong_password)
    dump = "\n".join(_every_key_and_value(client)).lower()
    assert dump  # something was written
    for raw in (ip, ip6, "2001:db8:77", email.lower(), "victim", "example.test", JWT.lower()):
        assert raw not in dump, raw


# ---------------------------------------------------------------------------
# Script loading
# ---------------------------------------------------------------------------


def test_noscript_is_recovered_by_reloading_the_script() -> None:
    client = _client()
    store = _store(client)
    rate = Rate(2, 60.0)
    assert store.consume([Check("r", "k", rate)], NOON).allowed
    assert store.script_loads == 1
    client.script_flush()  # e.g. Redis restarted
    assert store.consume([Check("r", "k", rate)], NOON).allowed
    assert not store.consume([Check("r", "k", rate)], NOON).allowed  # same counter survived
    assert store.script_loads == 2
    assert store.fallback_events == 0 and store.redis_errors == 0


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------


def test_memory_mode_falls_back_counts_warns_once_a_minute_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, mono = _server(), Mono()
    store = _store(_client(server), monotonic=mono, retry_seconds=1.0)
    rate = Rate(2, 60.0)

    def hit() -> bool:
        return bool(store.consume([Check("r", "k", rate)], NOON).allowed)

    assert hit()  # one unit in Redis
    server.connected = False
    with caplog.at_level(logging.WARNING, logger="backend.app.core.rate_limit_redis"):
        verdicts = []
        for _ in range(3):
            mono.t += 2  # past the circuit's retry window: Redis is tried each time
            verdicts.append(hit())
        assert verdicts == [True, True, False]  # the local store still limits
        warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
        assert len(warnings) == 1  # once a minute, not once a call
        assert "ConnectionError" in warnings[0].getMessage()
        mono.t += 61
        hit()
        warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
        assert len(warnings) == 2
        status = store.status()
        assert status["healthy"] is False and status["fallback_events"] == 4
        assert status["redis_errors"] == 4 and status["last_error"] == "ConnectionError"
        server.connected = True
        mono.t += 2
        assert hit()  # back on Redis: its counter had 1 of 2 used
        assert not hit()
        assert any("answering again" in r.getMessage() for r in caplog.records)
    assert store.status()["healthy"] is True
    assert store.status()["fallback_events"] == 4


def test_the_circuit_skips_redis_until_the_retry_window_passes() -> None:
    server, mono = _server(), Mono()
    client = _client(server)
    store = _store(client, monotonic=mono, retry_seconds=5.0)
    calls = {"n": 0}
    real = client.evalsha

    def counting(*a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real(*a, **kw)

    client.evalsha = counting  # type: ignore[method-assign]
    store.consume([Check("r", "k", Rate(9, 60.0))], NOON)
    server.connected = False
    store.consume([Check("r", "k", Rate(9, 60.0))], NOON)  # fails, opens the circuit
    tried = calls["n"]
    for _ in range(5):
        store.consume([Check("r", "k", Rate(9, 60.0))], NOON)
    assert calls["n"] == tried  # no timeout paid per request while down
    server.connected = True
    mono.t += 5
    store.consume([Check("r", "k", Rate(9, 60.0))], NOON)
    assert calls["n"] == tried + 1


def test_any_redis_error_degrades_instead_of_crashing() -> None:
    import redis  # noqa: PLC0415

    client = _client()
    store = _store(client)

    def oom(*_a: Any, **_k: Any) -> Any:
        raise redis.exceptions.ResponseError("OOM command not allowed")

    client.evalsha = oom  # type: ignore[method-assign]
    assert store.consume([Check("r", "k", Rate(1, 60.0))], NOON).allowed
    assert store.status()["last_error"] == "ResponseError"


def _down_limiter(mode: str, **overrides: Any) -> tuple[RateLimiter, Any]:
    server = _server()
    server.connected = False

    def factory(config: RateLimitConfig) -> RedisRateLimitStore:
        return _store(_client(server), failure_mode=mode, retry_seconds=0.0)

    return make_limiter(FakeClock(), store_factory=factory, **overrides), server


def test_closed_mode_answers_503_in_the_uniform_envelope() -> None:
    limiter, _server_ = _down_limiter("closed")
    resp = TestClient(_app(limiter)).get("/api/v1/things")
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "RATE_LIMIT_UNAVAILABLE"
    assert body["retry_after_seconds"] == int(resp.headers["Retry-After"]) == 5
    assert "correlation_id" in body


def test_closed_mode_refuses_logins_but_bookkeeping_never_raises() -> None:
    limiter, server = _down_limiter("closed")
    app = FastAPI()
    app.state.rate_limiter = limiter
    scope = {"type": "http", "app": app, "client": ("203.0.113.5", 1), "headers": []}
    with pytest.raises(RateLimitUnavailableError):
        guard_login(Request(scope), "a@example.test", lambda: "ok")
    # Post-attempt book-keeping (Redis died mid-request) degrades to local.
    limiter.record_login_failure("login:x", owner="o")
    limiter.note_email_failure("email:x")
    limiter.clear_login_failures("login:x")
    assert limiter.snapshot()["store"]["failure_mode"] == "closed"


def test_open_mode_admits_while_redis_is_down() -> None:
    limiter, server = _down_limiter("open", RATE_LIMIT_DEFAULT="1/minute")
    client = TestClient(_app(limiter))
    responses = [client.get("/api/v1/things") for _ in range(4)]
    assert [r.status_code for r in responses] == [200] * 4
    assert "X-RateLimit-Limit" not in responses[0].headers  # unmetered: no "limit 0"
    assert limiter.store.backoff_remaining("k", NOON) == 0
    server.connected = True  # recovers on its own: limits apply again
    assert [client.get("/api/v1/things").status_code for _ in range(2)] == [200, 429]


def test_memory_mode_never_fails_the_request_path() -> None:
    limiter, _server_ = _down_limiter("memory", RATE_LIMIT_DEFAULT="2/minute")
    client = TestClient(_app(limiter))
    assert [client.get("/api/v1/things").status_code for _ in range(3)] == [200, 200, 429]
    snap = limiter.snapshot()
    assert snap["throttled"]["default"] == 1  # counted locally while Redis is down
    assert snap["store"]["fallback_events"] >= 3


def test_an_unknown_failure_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        _store(failure_mode="sometimes")


# ---------------------------------------------------------------------------
# Snapshot (GET /ops/rate-limits body)
# ---------------------------------------------------------------------------


def test_snapshot_reports_shared_counters_and_health_without_secrets() -> None:
    url_secret = "pw-" + "q4" * 8
    client = _client()
    settings = _settings(
        RATE_LIMIT_STORE="redis",
        REDIS_URL=f"redis://:{url_secret}@cache.internal:6379/0",
        RATE_LIMIT_DEFAULT="1/minute",
    )
    limiter = build_rate_limiter(settings, client=client)
    http = TestClient(_app(limiter))
    http.get("/api/v1/things")
    http.get("/api/v1/things")
    snap = limiter.snapshot()
    assert snap["throttled"]["default"] == 1
    assert snap["tracked_keys"] >= 1
    assert snap["overflow_events"] == 0
    store = snap["store"]
    assert store["backend"] == "redis" and store["healthy"] is True
    assert store["failure_mode"] == "memory" and store["fallback_events"] == 0
    text = repr(snap)
    for secret in (url_secret, "cache.internal", JWT, "testclient", "ip:"):
        assert secret not in text
