"""Phase 11 — request rate limiting (hermetic: no database).

Covers the limiter core (sliding / fixed windows with an injected clock,
all-or-nothing consumption, memory bound + TTL eviction, failed-login backoff),
client identity (trusted proxy chain, spoofed X-Forwarded-For, bearer user),
the middleware (429 envelope + headers, exempt routes, per-IP vs per-user
isolation, logging without raw identities) and the settings validation.
Every route here is a throwaway one, so nothing touches the database.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from starlette.testclient import TestClient

from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.errors import AppError, app_error_handler
from backend.app.core.rate_limit import (
    Check,
    InMemoryRateLimitStore,
    RateLimitMiddleware,
    bearer_principal,
    classify,
    client_ip,
)
from backend.app.core.security import create_access_token, create_refresh_token
from backend.tests.rate_limit_support import NOON, FakeClock, installed, make_limiter
from config.settings import Settings
from config.settings.rates import Rate, parse_rate

DAY = 86400.0


# ---------------------------------------------------------------------------
# Rate specs + settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("30/minute", Rate(30, 60.0)),
        ("60/hour", Rate(60, 3600.0)),
        ("5/s", Rate(5, 1.0)),
        ("300/day", Rate(300, DAY, fixed=True)),
        (" 2 / Minutes ", Rate(2, 60.0)),
    ],
)
def test_parse_rate(spec: str, expected: Rate) -> None:
    assert parse_rate(spec) == expected


@pytest.mark.parametrize("spec", ["", "lots", "0/minute", "10/fortnight", "-1/day", "1.5/hour"])
def test_parse_rate_rejects_garbage(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_rate(spec)


def test_a_bad_rate_in_the_environment_fails_startup() -> None:
    """A security limit must never silently fall back to "no limit"."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            JWT_SECRET="x" * 40,
            DATABASE_URL="postgresql://u@h/d",
            RATE_LIMIT_AI_PER_USER="unlimited",
        )


def test_production_defaults_are_on_and_bounded() -> None:
    """The CODE defaults (what a deploy gets without overrides); the test suite
    only switches the limiter off through the environment (repo conftest.py)."""

    def default(name: str) -> Any:
        return Settings.model_fields[name].default

    assert default("RATE_LIMIT_ENABLED") is True
    assert default("TRUSTED_PROXY_COUNT") == 0
    assert parse_rate(default("RATE_LIMIT_LOGIN_PER_IP")) == Rate(30, 60.0)
    assert parse_rate(default("RATE_LIMIT_AI_PER_USER")) == Rate(60, 3600.0)
    assert parse_rate(default("RATE_LIMIT_AI_DAILY_PER_USER")) == Rate(300, DAY, fixed=True)
    assert parse_rate(default("RATE_LIMIT_AI_DAILY_PER_ORG")) == Rate(2000, DAY, fixed=True)
    assert parse_rate(default("RATE_LIMIT_RECEIPT_PER_USER")) == Rate(10, 60.0)
    assert parse_rate(default("RATE_LIMIT_DEFAULT")) == Rate(300, 60.0)


# ---------------------------------------------------------------------------
# Store: windows
# ---------------------------------------------------------------------------


def _hit(store: InMemoryRateLimitStore, now: float, rate: Rate, key: str = "k") -> Any:
    return store.consume([Check("r", key, rate)], now)


def test_sliding_window_boundaries_with_an_injected_clock() -> None:
    """3 per 10 s.  Three at t=100 (start of a window) pass, the fourth waits
    until the weighted previous window has decayed enough: est = 3*(1-f) + 0;
    allowed when est + 1 <= 3, i.e. f >= 1/3 of the NEXT window -> t >= 113.33."""
    store = InMemoryRateLimitStore()
    rate = Rate(3, 10.0)
    assert [_hit(store, 100.0, rate).allowed for _ in range(3)] == [True, True, True]
    denied = _hit(store, 100.0, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 14  # ceil(10 + 10/3)
    assert not _hit(store, 109.9, rate).allowed  # same window: still full
    assert not _hit(store, 113.3, rate).allowed  # 3*0.67 + 1 > 3
    assert _hit(store, 113.4, rate).allowed  # 3*0.66 + 1 <= 3
    assert not _hit(store, 113.4, rate).allowed  # and that one counted


def test_sliding_window_retry_after_when_the_previous_window_is_the_blocker() -> None:
    store = InMemoryRateLimitStore()
    rate = Rate(4, 10.0)
    for _ in range(4):
        assert _hit(store, 100.0, rate).allowed
    # Next window, 1 s in: est = 4*0.9 = 3.6 -> +1 > 4.  Needs f >= 1 - 3/4.
    denied = _hit(store, 111.0, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 2  # ceil(2.5 - 1)
    assert _hit(store, 112.5, rate).allowed


def test_remaining_counts_down_and_window_reset_is_reported() -> None:
    store = InMemoryRateLimitStore()
    rate = Rate(3, 60.0)
    first = _hit(store, NOON, rate)
    assert (first.limit, first.remaining, first.reset_seconds) == (3, 2, 60)
    assert _hit(store, NOON + 1, rate).remaining == 1
    assert _hit(store, NOON + 2, rate).remaining == 0
    assert first.headers() == {
        "X-RateLimit-Limit": "3",
        "X-RateLimit-Remaining": "2",
        "X-RateLimit-Reset": "60",
    }


def test_daily_quota_rolls_over_at_utc_midnight() -> None:
    store = InMemoryRateLimitStore()
    rate = parse_rate("2/day")
    just_before = (NOON // DAY + 1) * DAY - 1  # 23:59:59 UTC
    assert _hit(store, just_before - 3600, rate).allowed
    assert _hit(store, just_before - 60, rate).allowed
    denied = _hit(store, just_before, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 1
    # 00:00:00 UTC: a fresh quota (a fixed window carries nothing over).
    assert _hit(store, just_before + 1, rate).allowed
    assert _hit(store, just_before + 2, rate).allowed
    assert not _hit(store, just_before + 3, rate).allowed


def test_consumption_is_all_or_nothing() -> None:
    """A request denied by ONE counter must not burn the others (a rejected
    burst must not eat the daily quota)."""
    store = InMemoryRateLimitStore()
    small, big = Rate(1, 60.0), Rate(10, DAY, fixed=True)
    both = [Check("burst", "u", small), Check("daily", "u", big)]
    assert store.consume(both, NOON).allowed
    for _ in range(5):
        assert not store.consume(both, NOON + 1).allowed
    # Daily counter saw exactly ONE hit: nine left.
    assert store.consume([Check("daily", "u", big)], NOON + 2).remaining == 8


def test_keys_and_rules_are_isolated() -> None:
    store = InMemoryRateLimitStore()
    rate = Rate(1, 60.0)
    assert store.consume([Check("a", "x", rate)], NOON).allowed
    assert store.consume([Check("a", "y", rate)], NOON).allowed
    assert store.consume([Check("b", "x", rate)], NOON).allowed
    assert not store.consume([Check("a", "x", rate)], NOON).allowed


def test_memory_is_bounded() -> None:
    store = InMemoryRateLimitStore(max_keys=100)
    rate = Rate(5, 3600.0)
    for i in range(1000):
        store.consume([Check("r", f"k{i}", rate, owner=f"o{i}")], NOON)
        store.consume([Check("q", f"user:{i}", rate)], NOON)
    # Each table is bounded; overflow adds at most ``overflow_buckets`` hashed
    # buckets per rule (rate-limit hardening: no single global bucket).
    assert store.size() <= 2 * 100 + 2 * store.overflow_buckets


def test_expired_entries_are_evicted_before_live_ones() -> None:
    store = InMemoryRateLimitStore(max_keys=10)
    for i in range(5):
        store.consume([Check("short", f"old{i}", Rate(1, 1.0))], NOON)
    live = Rate(1, 3600.0)
    for i in range(5):
        store.consume([Check("long", f"live{i}", live)], NOON + 100)
    store.consume([Check("long", "live5", live)], NOON + 100)  # over capacity
    assert store.size() == 6  # the five expired windows went, the live ones stayed
    for i in range(6):
        assert not store.consume([Check("long", f"live{i}", live)], NOON + 101).allowed


# ---------------------------------------------------------------------------
# Store: failed-login backoff
# ---------------------------------------------------------------------------


def test_progressive_backoff_grows_caps_and_resets() -> None:
    store = InMemoryRateLimitStore()
    kw: dict[str, float] = {"threshold": 3, "base": 10, "cap": 40, "window": 100}
    t = NOON
    store.record_failure("e", t, **kw)  # type: ignore[arg-type]
    store.record_failure("e", t, **kw)  # type: ignore[arg-type]
    assert store.backoff_remaining("e", t) == 0
    waits = []
    for _ in range(4):
        store.record_failure("e", t, **kw)  # type: ignore[arg-type]
        waits.append(store.backoff_remaining("e", t))
    assert waits == [10, 20, 40, 40]
    assert store.backoff_remaining("e", t + 39) == 1
    assert store.backoff_remaining("e", t + 40) == 0
    store.clear_failures("e")
    store.record_failure("e", t + 41, **kw)  # type: ignore[arg-type]
    assert store.backoff_remaining("e", t + 41) == 0  # counting from one again


def test_old_failures_are_forgotten() -> None:
    store = InMemoryRateLimitStore()
    kw: dict[str, float] = {"threshold": 2, "base": 10, "cap": 40, "window": 100}
    store.record_failure("e", NOON, **kw)  # type: ignore[arg-type]
    store.record_failure("e", NOON + 101, **kw)  # type: ignore[arg-type]
    assert store.backoff_remaining("e", NOON + 101) == 0


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------


def _scope(peer: str = "10.0.0.9", xff: list[str] | None = None, auth: str | None = None) -> Any:
    headers = [(b"x-forwarded-for", v.encode()) for v in (xff or [])]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {"type": "http", "client": (peer, 5555), "headers": headers}


def test_no_trusted_proxy_ignores_forwarded_for() -> None:
    assert client_ip(_scope(xff=["1.2.3.4"]), 0) == "10.0.0.9"


def test_trusted_proxy_count_picks_from_the_right() -> None:
    chain = ["6.6.6.6, 203.0.113.7, 198.51.100.2"]
    assert client_ip(_scope(xff=chain), 1) == "198.51.100.2"
    assert client_ip(_scope(xff=chain), 2) == "203.0.113.7"
    # Repeated headers are one list, in order.
    assert client_ip(_scope(xff=["6.6.6.6", "198.51.100.2"]), 1) == "198.51.100.2"


def test_a_spoofed_leftmost_value_never_changes_the_identity() -> None:
    real = "198.51.100.2"
    seen = {client_ip(_scope(xff=[f"{spoof}, {real}"]), 1) for spoof in ("1.1.1.1", "2.2.2.2", "x")}
    assert seen == {real}


def test_short_or_malformed_chain_falls_back_to_the_peer() -> None:
    assert client_ip(_scope(xff=[]), 1) == "10.0.0.9"
    assert client_ip(_scope(xff=["198.51.100.2"]), 2) == "10.0.0.9"
    assert client_ip(_scope(xff=["not-an-ip"]), 1) == "10.0.0.9"


def test_bearer_principal_needs_a_valid_access_token() -> None:
    uid, oid = uuid.uuid4(), uuid.uuid4()
    good = create_access_token(uid, oid)
    assert bearer_principal(_scope(auth=f"Bearer {good}")) == (str(uid), str(oid))
    assert bearer_principal(_scope(auth=f"Bearer {create_refresh_token(uid, oid)}")) is None
    assert bearer_principal(_scope(auth=f"Bearer {good}x")) is None
    assert bearer_principal(_scope(auth=good)) is None
    assert bearer_principal(_scope()) is None


@pytest.mark.parametrize(
    ("method", "path", "policy"),
    [
        ("POST", "/auth/login", "auth_login"),
        ("POST", "/auth/refresh", "auth_refresh"),
        ("POST", "/users", "user_admin"),
        ("POST", "/users/", "user_admin"),
        ("PATCH", "/users/abc", "user_admin"),
        ("POST", "/users/abc/deactivate", "user_admin"),
        ("GET", "/users", "default"),
        ("POST", "/conversations/abc/messages", "ai"),
        ("POST", "/conversations", "default"),
        ("POST", "/policy/query", "ai"),
        ("POST", "/policy/evaluate", "ai"),
        ("POST", "/policies/abc/rules", "ai"),
        ("POST", "/receipts", "receipt_upload"),
        ("PATCH", "/receipts/abc", "default"),
        ("GET", "/me", "default"),
    ],
)
def test_classify(method: str, path: str, policy: str) -> None:
    assert classify(method, path) == policy


# ---------------------------------------------------------------------------
# Middleware on a throwaway app
# ---------------------------------------------------------------------------


def _app(limiter: Any) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://front.test"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After"],
    )
    app.add_middleware(CorrelationIDMiddleware)
    app.state.rate_limiter = limiter

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/landing")
    def landing() -> dict[str, str]:
        return {"page": "frontend"}

    @app.get("/api/v1/things")
    def things() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/policy/query")
    def ai() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/auth/login")
    def login() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _bearer(uid: uuid.UUID | None = None, org: uuid.UUID | None = None) -> dict[str, str]:
    token = create_access_token(uid or uuid.uuid4(), org or uuid.uuid4())
    return {"Authorization": f"Bearer {token}"}


def test_429_envelope_headers_and_rate_limit_headers() -> None:
    client = TestClient(_app(make_limiter(RATE_LIMIT_DEFAULT="2/minute")))
    ok = client.get("/api/v1/things")
    assert ok.status_code == 200
    assert ok.headers["X-RateLimit-Limit"] == "2"
    assert ok.headers["X-RateLimit-Remaining"] == "1"
    assert ok.headers["X-RateLimit-Reset"] == "60"
    assert client.get("/api/v1/things").status_code == 200
    resp = client.get("/api/v1/things")
    assert resp.status_code == 429
    body = resp.json()["error"]
    assert body["code"] == "RATE_LIMITED"
    assert body["retry_after_seconds"] == int(resp.headers["Retry-After"]) >= 1
    assert "try again" in body["message"].lower()
    assert body["correlation_id"] == resp.headers["X-Correlation-ID"]
    assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_cross_origin_429_is_readable_by_the_browser() -> None:
    client = TestClient(_app(make_limiter(RATE_LIMIT_DEFAULT="1/minute")))
    origin = {"Origin": "http://front.test"}
    client.get("/api/v1/things", headers=origin)
    resp = client.get("/api/v1/things", headers=origin)
    assert resp.status_code == 429
    assert resp.headers["access-control-allow-origin"] == "http://front.test"
    assert "retry-after" in resp.headers["access-control-expose-headers"].lower()


def test_health_frontend_and_preflight_are_exempt() -> None:
    limiter = make_limiter(RATE_LIMIT_DEFAULT="1/minute")
    client = TestClient(_app(limiter))
    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/landing").status_code == 200
        pre = client.options(
            "/api/v1/things",
            headers={"Origin": "http://front.test", "Access-Control-Request-Method": "GET"},
        )
        assert pre.status_code == 200
    assert "X-RateLimit-Limit" not in client.get("/health").headers
    assert limiter.store.size() == 0
    assert client.get("/api/v1/things").status_code == 200  # the one API call allowed


def test_disabled_limiter_limits_nothing() -> None:
    limiter = make_limiter(RATE_LIMIT_ENABLED=False, RATE_LIMIT_DEFAULT="1/minute")
    client = TestClient(_app(limiter))
    for _ in range(5):
        resp = client.get("/api/v1/things")
        assert resp.status_code == 200
        assert "X-RateLimit-Limit" not in resp.headers


def test_users_are_limited_separately_from_each_other_and_from_their_ip() -> None:
    client = TestClient(_app(make_limiter(RATE_LIMIT_DEFAULT="2/minute")))
    alice, bob = _bearer(), _bearer()
    assert [client.get("/api/v1/things", headers=alice).status_code for _ in range(3)] == [
        200,
        200,
        429,
    ]
    # Same IP (TestClient), different user: own budget.
    assert client.get("/api/v1/things", headers=bob).status_code == 200
    # Anonymous calls from the same IP: the IP's own budget.
    assert client.get("/api/v1/things").status_code == 200
    # An invalid token is anonymous, so it shares the IP budget (cannot mint users).
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get("/api/v1/things", headers=bad).status_code == 200
    assert client.get("/api/v1/things", headers=bad).status_code == 429


def test_per_ip_identity_follows_the_trusted_proxy_entry() -> None:
    limiter = make_limiter(TRUSTED_PROXY_COUNT=1, RATE_LIMIT_DEFAULT="2/minute")
    client = TestClient(_app(limiter))

    def call(xff: str) -> int:
        return client.get("/api/v1/things", headers={"X-Forwarded-For": xff}).status_code

    assert call("1.1.1.1, 198.51.100.2") == 200
    assert call("2.2.2.2, 198.51.100.2") == 200
    assert call("3.3.3.3, 198.51.100.2") == 429  # spoofing the left entry does not help
    assert call("198.51.100.3") == 200  # a different real client


def test_ai_policy_has_a_per_org_daily_quota() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, RATE_LIMIT_AI_DAILY_PER_ORG="3/day")
    client = TestClient(_app(limiter))
    org, other_org = uuid.uuid4(), uuid.uuid4()
    a, b = _bearer(org=org), _bearer(org=org)
    codes = [client.post("/api/v1/policy/query", headers=h).status_code for h in (a, b, a, b)]
    assert codes == [200, 200, 200, 429]
    assert client.post("/api/v1/policy/query", headers=_bearer(org=other_org)).status_code == 200
    clock.advance(DAY)  # next UTC day
    assert client.post("/api/v1/policy/query", headers=a).status_code == 200


def test_ai_policy_per_user_burst_and_daily_quota() -> None:
    clock = FakeClock()
    limiter = make_limiter(
        clock, RATE_LIMIT_AI_PER_USER="2/hour", RATE_LIMIT_AI_DAILY_PER_USER="3/day"
    )
    client = TestClient(_app(limiter))
    user = _bearer()

    def ask() -> int:
        return client.post("/api/v1/policy/query", headers=user).status_code

    assert [ask(), ask(), ask()] == [200, 200, 429]  # hourly burst
    clock.advance(2 * 3600)
    assert [ask(), ask()] == [200, 429]  # daily quota (3) is now spent
    clock.advance(DAY)
    assert ask() == 200


def test_throttle_is_logged_and_counted_without_raw_identities(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = make_limiter(TRUSTED_PROXY_COUNT=1, RATE_LIMIT_LOGIN_PER_IP="1/minute")
    client = TestClient(_app(limiter))
    ip = "198.51.100.77"
    headers = {"X-Forwarded-For": ip}
    client.post("/api/v1/auth/login", headers=headers)
    with caplog.at_level(logging.WARNING, logger="backend.app.core.rate_limit"):
        assert client.post("/api/v1/auth/login", headers=headers).status_code == 429
    records = [r for r in caplog.records if r.name == "backend.app.core.rate_limit"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "policy=auth_login" in records[0].getMessage()
    assert ip not in caplog.text
    snap = limiter.snapshot()
    assert snap["throttled"]["auth_login"] == 1
    assert ip not in repr(snap)
    assert "test-secret" not in repr(snap)


def test_login_is_limited_per_ip_even_with_a_valid_token() -> None:
    """Auth endpoints are always keyed by IP: a token cannot buy more attempts."""
    client = TestClient(_app(make_limiter(RATE_LIMIT_LOGIN_PER_IP="1/minute")))
    assert client.post("/api/v1/auth/login", headers=_bearer()).status_code == 200
    assert client.post("/api/v1/auth/login", headers=_bearer()).status_code == 429


def test_installed_helper_restores_the_previous_limiter() -> None:
    app = _app(None)
    limiter = make_limiter()
    with installed(app, limiter):
        assert app.state.rate_limiter is limiter
    assert app.state.rate_limiter is None


def test_auth_policies_key_by_ip_even_when_a_user_is_known() -> None:
    limiter = make_limiter()
    for policy in ("auth_login", "auth_refresh"):
        checks = limiter.checks_for(policy, ip_key="ip:a", user_key="user:b", org_key="org:c")
        assert {c.key for c in checks} == {"ip:a"}
    ai = limiter.checks_for("ai", ip_key="ip:a", user_key="user:b", org_key="org:c")
    assert {(c.rule, c.key) for c in ai} == {
        ("ai:user", "user:b"),
        ("ai:user_daily", "user:b"),
        ("ai:org_daily", "org:c"),
    }


# ---------------------------------------------------------------------------
# Integration review fixes (phase 11 integration): live counters are never
# evicted; quota counters live in their own table; keys per IP / per IPv6 /64
# are capped and overflow into a coarse shared bucket instead.
# ---------------------------------------------------------------------------


def test_a_full_table_never_evicts_live_counters() -> None:
    """An anonymous key-spraying flood must not reset anyone's limit (the old
    store dropped least-recently-used LIVE counters to make room)."""
    store = InMemoryRateLimitStore(max_keys=3)
    live = Rate(1, 3600.0)
    for ip in ("a", "b", "c"):
        assert store.consume([Check("r", ip, live, owner=ip)], NOON).allowed
    for i in range(50):  # flood from 50 other addresses
        store.consume([Check("r", f"flood{i}", live, owner=f"flood{i}")], NOON + 1)
    for ip in ("a", "b", "c"):  # every original counter is still spent
        assert not store.consume([Check("r", ip, live, owner=ip)], NOON + 2).allowed


def test_new_keys_share_their_networks_bucket_when_the_table_is_full() -> None:
    """Fail closed: once the table is full, one network's untracked identities
    share ONE hashed overflow bucket per rule, so spraying keys cannot mint
    fresh budgets (rate-limit hardening: the bucket is per network, no longer
    one global bucket for everyone — see test_rl_harden.py)."""
    store = InMemoryRateLimitStore(max_keys=2)
    rate = Rate(2, 3600.0)
    store.consume([Check("r", "a", rate, owner="a")], NOON)
    store.consume([Check("r", "b", rate, owner="b")], NOON)
    verdicts = [
        store.consume([Check("r", f"new{i}", rate, owner="flood", net="flood")], NOON + 1).allowed
        for i in range(4)
    ]
    assert verdicts == [True, True, False, False]
    assert store.size() <= 2 + 1  # the table plus that network's bucket for the rule
    assert store.overflow_events >= 4


def test_quota_counters_cannot_be_crowded_out_by_anonymous_traffic() -> None:
    """Per-user / per-org quota counters live in a SEPARATE table: no amount of
    anonymous per-IP traffic can fill it or reset a daily quota."""
    store = InMemoryRateLimitStore(max_keys=5)
    daily = Rate(1, DAY, fixed=True)
    assert store.consume([Check("ai:user_daily", "user:u", daily)], NOON).allowed
    for i in range(500):
        store.consume([Check("default:principal", f"ip:{i}", Rate(9, 60.0), owner=f"o{i}")], NOON)
    denied = store.consume([Check("ai:user_daily", "user:u", daily)], NOON + 1)
    assert not denied.allowed
    # ...and new users still get their OWN counters (the quota table has room),
    # not one shared overflow bucket.
    for user in ("user:v", "user:w", "user:x"):
        assert store.consume([Check("ai:user_daily", user, daily)], NOON + 1).allowed


def test_keys_per_ip_are_capped_and_overflow_into_that_ips_bucket() -> None:
    """One address spraying many emails gets at most ``max_keys_per_ip`` own
    counters; the rest share that address's overflow bucket (other addresses
    are unaffected and the table never fills)."""
    store = InMemoryRateLimitStore(max_keys=1000, max_keys_per_ip=3)
    rate = Rate(2, 60.0)
    sprayed = [
        store.consume([Check("login", f"ip/e{i}", rate, owner="ip")], NOON).allowed
        for i in range(10)
    ]
    # 3 own counters (allowed), then the shared overflow bucket: 2 more, then 429.
    assert sprayed == [True, True, True, True, True] + [False] * 5
    assert store.size() <= 3 + 1
    assert store.consume([Check("login", "other/e0", rate, owner="other")], NOON).allowed


def test_ipv6_addresses_are_grouped_by_their_slash64() -> None:
    from backend.app.core.rate_limit import ip_owner

    assert ip_owner("2001:db8:1:2::1") == ip_owner("2001:db8:1:2:ffff:ffff:ffff:ffff")
    assert ip_owner("2001:db8:1:2::1") != ip_owner("2001:db8:1:3::1")
    assert ip_owner("198.51.100.7") != ip_owner("198.51.100.8")
    assert ip_owner("::ffff:198.51.100.7") == ip_owner("198.51.100.7")
    assert ip_owner("testclient") == "testclient"  # non-IP peers stay themselves


def test_an_ipv6_client_cannot_rotate_addresses_to_escape_the_per_ip_key_cap() -> None:
    limiter = make_limiter(RATE_LIMIT_MAX_KEYS_PER_IP=4, TRUSTED_PROXY_COUNT=1)
    client = TestClient(_app(limiter))
    for i in range(40):
        client.get("/api/v1/things", headers={"X-Forwarded-For": f"2001:db8:0:7::{i:x}"})
    # 40 addresses of ONE /64: at most the per-prefix cap (+ its overflow bucket).
    assert limiter.store.size() <= 4 + 1


def test_backoff_lockouts_are_never_evicted_by_a_flood() -> None:
    store = InMemoryRateLimitStore(max_keys=3)
    kw: dict[str, Any] = {"threshold": 1, "base": 600, "cap": 600, "window": 900}
    store.record_failure("attacker/victim", NOON, owner="attacker", **kw)
    for i in range(100):
        store.record_failure(f"o{i}/e", NOON + 1, owner=f"o{i}", **kw)
    assert store.backoff_remaining("attacker/victim", NOON + 2, owner="attacker") > 0


def test_backoff_spray_past_the_per_ip_cap_locks_that_ip_out() -> None:
    store = InMemoryRateLimitStore(max_keys=1000, max_keys_per_ip=2)
    kw: dict[str, Any] = {"threshold": 2, "base": 60, "cap": 60, "window": 900}
    for i in range(6):
        store.record_failure(f"ip/e{i}", NOON, owner="ip", **kw)
    # e2..e5 overflowed into the ip's shared backoff row (4 failures >= 2).
    assert store.backoff_remaining("ip/e9", NOON + 1, owner="ip") > 0
    assert store.backoff_remaining("other/e9", NOON + 1, owner="other") == 0


# ---------------------------------------------------------------------------
# Integration: middleware order on the REAL app (rate limiter BEFORE the
# body-size guard, both inside CORS and the correlation id).
# ---------------------------------------------------------------------------


def test_real_app_runs_the_rate_limiter_before_the_body_size_guard() -> None:
    from backend.app.core.body_limit import BodySizeLimitMiddleware, SecurityHeadersMiddleware
    from backend.app.main_entry.app import create_app

    app = create_app()
    order: list[Any] = [m.cls for m in app.user_middleware]  # outermost first
    assert order.index(CorrelationIDMiddleware) < order.index(CORSMiddleware)
    assert order.index(CORSMiddleware) < order.index(SecurityHeadersMiddleware)
    assert order.index(SecurityHeadersMiddleware) < order.index(RateLimitMiddleware)
    assert order.index(RateLimitMiddleware) < order.index(BodySizeLimitMiddleware)

    # Behaviour: a throttled caller is refused (429) without its oversized body
    # ever being looked at (the body guard would have answered 413).
    app.state.rate_limiter = make_limiter(RATE_LIMIT_RECEIPT_PER_USER="1/minute")
    client = TestClient(app, raise_server_exceptions=False)
    huge = {"Content-Length": str(50 * 1024 * 1024), "Content-Type": "multipart/form-data"}
    first = client.post("/api/v1/receipts", content=b"", headers=huge)
    assert first.status_code == 413
    second = client.post("/api/v1/receipts", content=b"", headers=huge)
    assert second.status_code == 429
    assert second.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Correlation-ID" in second.headers
