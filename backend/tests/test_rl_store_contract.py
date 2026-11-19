"""Rate-limit STORE contract (phase 12 follow-up) — hermetic, no database.

Every guarantee the in-memory store is tested for in test_ps_rate_limit.py and
test_rl_harden.py, run against BOTH stores: ``InMemoryRateLimitStore`` and
``RedisRateLimitStore`` (fakeredis with real Lua via lupa, injected clock).
A Redis store that drifts from the in-memory semantics fails here.

If fakeredis' Lua support (the ``lupa`` package) is missing, every Redis case
is SKIPPED with a loud reason — the Redis store is then NOT verified.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from backend.app.core.errors import AppError, UnauthorizedError, app_error_handler
from backend.app.core.rate_limit import (
    Check,
    InMemoryRateLimitStore,
    RateLimitConfig,
    RateLimitedError,
    RateLimiter,
    RateLimitMiddleware,
    RateLimitStore,
    guard_login,
)
from backend.app.core.rate_limit_redis import RedisRateLimitStore
from backend.tests.rate_limit_support import NOON, FakeClock, make_limiter
from config.settings.rates import Rate, parse_rate

DAY = 86400.0
PREFIX = "atlas:rl:"

_HAS_LUA = importlib.util.find_spec("lupa") is not None and (
    importlib.util.find_spec("fakeredis") is not None
)
_NO_LUA = (
    "!!! fakeredis Lua support (pip install 'fakeredis[lua]') is MISSING — "
    "the Redis rate-limit store is NOT verified by this run !!!"
)

KINDS = [
    pytest.param("memory", id="memory"),
    pytest.param("redis", id="redis", marks=pytest.mark.skipif(not _HAS_LUA, reason=_NO_LUA)),
]

StoreFactory = Callable[..., Any]


def build_store(kind: str, *, max_keys: int = 50_000, max_keys_per_ip: int = 64) -> Any:
    if kind == "memory":
        return InMemoryRateLimitStore(max_keys=max_keys, max_keys_per_ip=max_keys_per_ip)
    import fakeredis  # noqa: PLC0415

    client = fakeredis.FakeRedis(server=fakeredis.FakeServer())
    return RedisRateLimitStore(
        client,
        secret=b"contract-secret-not-real",
        max_keys=max_keys,
        max_keys_per_ip=max_keys_per_ip,
        prefix=PREFIX,
        server_time=False,  # the injected clock drives the same Lua script
    )


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def new_store(kind: str) -> StoreFactory:
    def factory(**kw: Any) -> Any:
        return build_store(kind, **kw)

    return factory


def _store_factory(kind: str) -> Callable[[RateLimitConfig], RateLimitStore]:
    def factory(config: RateLimitConfig) -> RateLimitStore:
        store: RateLimitStore = build_store(
            kind, max_keys=config.max_keys, max_keys_per_ip=config.max_keys_per_ip
        )
        return store

    return factory


def limiter_for(kind: str, clock: FakeClock | None = None, **overrides: Any) -> RateLimiter:
    return make_limiter(clock, store_factory=_store_factory(kind), **overrides)


def backoff_rows(store: Any) -> int:
    if isinstance(store, InMemoryRateLimitStore):
        return len(store._backoff)
    return int(store._client.zcard(PREFIX + "t:b"))


def _hit(store: Any, now: float, rate: Rate, key: str = "k") -> Any:
    return store.consume([Check("r", key, rate)], now)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_sliding_window_boundaries_with_an_injected_clock(new_store: StoreFactory) -> None:
    store = new_store()
    rate = Rate(3, 10.0)
    assert [_hit(store, 100.0, rate).allowed for _ in range(3)] == [True, True, True]
    denied = _hit(store, 100.0, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 14
    assert not _hit(store, 109.9, rate).allowed
    assert not _hit(store, 113.3, rate).allowed
    assert _hit(store, 113.4, rate).allowed
    assert not _hit(store, 113.4, rate).allowed


def test_retry_after_when_the_previous_window_is_the_blocker(new_store: StoreFactory) -> None:
    store = new_store()
    rate = Rate(4, 10.0)
    for _ in range(4):
        assert _hit(store, 100.0, rate).allowed
    denied = _hit(store, 111.0, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 2
    assert _hit(store, 112.5, rate).allowed


def test_remaining_counts_down_and_window_reset_is_reported(new_store: StoreFactory) -> None:
    store = new_store()
    rate = Rate(3, 60.0)
    first = _hit(store, NOON, rate)
    assert (first.limit, first.remaining, first.reset_seconds) == (3, 2, 60)
    assert _hit(store, NOON + 1, rate).remaining == 1
    assert _hit(store, NOON + 2, rate).remaining == 0


def test_daily_quota_rolls_over_at_utc_midnight(new_store: StoreFactory) -> None:
    store = new_store()
    rate = parse_rate("2/day")
    just_before = (NOON // DAY + 1) * DAY - 1
    assert _hit(store, just_before - 3600, rate).allowed
    assert _hit(store, just_before - 60, rate).allowed
    denied = _hit(store, just_before, rate)
    assert not denied.allowed
    assert denied.retry_after_seconds == 1
    assert _hit(store, just_before + 1, rate).allowed
    assert _hit(store, just_before + 2, rate).allowed
    assert not _hit(store, just_before + 3, rate).allowed


def test_consumption_is_all_or_nothing(new_store: StoreFactory) -> None:
    store = new_store()
    small, big = Rate(1, 60.0), Rate(10, DAY, fixed=True)
    both = [Check("burst", "u", small), Check("daily", "u", big)]
    assert store.consume(both, NOON).allowed
    for _ in range(5):
        assert not store.consume(both, NOON + 1).allowed
    assert store.consume([Check("daily", "u", big)], NOON + 2).remaining == 8


def test_all_or_nothing_when_the_LAST_check_denies(new_store: StoreFactory) -> None:
    """Order must not matter: a denial by the last counter burns the first ones neither."""
    store = new_store()
    big, small = Rate(10, DAY, fixed=True), Rate(1, 60.0)
    both = [Check("daily", "u", big), Check("burst", "u", small)]
    assert store.consume(both, NOON).allowed
    for _ in range(5):
        assert not store.consume(both, NOON + 1).allowed
    assert store.consume([Check("daily", "u", big)], NOON + 2).remaining == 8


def test_keys_and_rules_are_isolated(new_store: StoreFactory) -> None:
    store = new_store()
    rate = Rate(1, 60.0)
    assert store.consume([Check("a", "x", rate)], NOON).allowed
    assert store.consume([Check("a", "y", rate)], NOON).allowed
    assert store.consume([Check("b", "x", rate)], NOON).allowed
    assert not store.consume([Check("a", "x", rate)], NOON).allowed
    # Same rule + key, per-IP vs quota table: still one counter per (rule, key).
    assert store.consume([Check("c", "x", rate, owner="o")], NOON).allowed
    assert not store.consume([Check("c", "x", rate, owner="o")], NOON).allowed


def test_no_checks_is_allowed(new_store: StoreFactory) -> None:
    assert new_store().consume([], NOON).allowed


# ---------------------------------------------------------------------------
# Memory bounds: never evict a live counter
# ---------------------------------------------------------------------------


def test_memory_is_bounded(new_store: StoreFactory) -> None:
    store = new_store(max_keys=100)
    rate = Rate(5, 3600.0)
    for i in range(400):
        store.consume([Check("r", f"k{i}", rate, owner=f"o{i}")], NOON)
        store.consume([Check("q", f"user:{i}", rate)], NOON)
    assert store.size() <= 2 * 100 + 2 * store.overflow_buckets


def test_expired_entries_are_evicted_before_live_ones(new_store: StoreFactory) -> None:
    store = new_store(max_keys=10)
    for i in range(5):
        store.consume([Check("short", f"old{i}", Rate(1, 1.0))], NOON)
    live = Rate(1, 3600.0)
    for i in range(5):
        store.consume([Check("long", f"live{i}", live)], NOON + 100)
    store.consume([Check("long", "live5", live)], NOON + 100)
    assert store.size() == 6
    for i in range(6):
        assert not store.consume([Check("long", f"live{i}", live)], NOON + 101).allowed


def test_a_full_table_never_evicts_live_counters(new_store: StoreFactory) -> None:
    store = new_store(max_keys=3)
    live = Rate(1, 3600.0)
    for ip in ("a", "b", "c"):
        assert store.consume([Check("r", ip, live, owner=ip)], NOON).allowed
    for i in range(50):
        store.consume([Check("r", f"flood{i}", live, owner=f"flood{i}")], NOON + 1)
    for ip in ("a", "b", "c"):
        assert not store.consume([Check("r", ip, live, owner=ip)], NOON + 2).allowed


def test_new_keys_share_their_networks_bucket_when_the_table_is_full(
    new_store: StoreFactory,
) -> None:
    store = new_store(max_keys=2)
    rate = Rate(2, 3600.0)
    store.consume([Check("r", "a", rate, owner="a")], NOON)
    store.consume([Check("r", "b", rate, owner="b")], NOON)
    verdicts = [
        store.consume([Check("r", f"new{i}", rate, owner="flood", net="flood")], NOON + 1).allowed
        for i in range(4)
    ]
    assert verdicts == [True, True, False, False]
    assert store.size() <= 2 + 1
    assert store.metrics()["overflow_events"] >= 4


def test_quota_counters_cannot_be_crowded_out_by_anonymous_traffic(
    new_store: StoreFactory,
) -> None:
    store = new_store(max_keys=5)
    daily = Rate(1, DAY, fixed=True)
    assert store.consume([Check("ai:user_daily", "user:u", daily)], NOON).allowed
    for i in range(200):
        store.consume([Check("default:principal", f"ip:{i}", Rate(9, 60.0), owner=f"o{i}")], NOON)
    assert not store.consume([Check("ai:user_daily", "user:u", daily)], NOON + 1).allowed
    for user in ("user:v", "user:w", "user:x"):
        assert store.consume([Check("ai:user_daily", user, daily)], NOON + 1).allowed


def test_keys_per_owner_are_capped_and_overflow_into_that_owners_row(
    new_store: StoreFactory,
) -> None:
    """The per-owner counted cap: 3 own counters, then ONE shared row per rule."""
    store = new_store(max_keys=1000, max_keys_per_ip=3)
    rate = Rate(2, 60.0)
    sprayed = [
        store.consume([Check("login", f"ip/e{i}", rate, owner="ip")], NOON).allowed
        for i in range(10)
    ]
    assert sprayed == [True, True, True, True, True] + [False] * 5
    assert store.size() <= 3 + 1
    assert store.consume([Check("login", "other/e0", rate, owner="other")], NOON).allowed


def test_an_expired_owner_row_frees_its_slot(new_store: StoreFactory) -> None:
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    short = Rate(1, 1.0)
    for i in range(2):
        assert store.consume([Check("r", f"ip/e{i}", short, owner="ip")], NOON).allowed
    # Two windows later both rows are dead: the owner gets fresh own rows again.
    assert store.consume([Check("r", "ip/e9", short, owner="ip")], NOON + 5).allowed
    assert store.consume([Check("r", "ip/e8", short, owner="ip")], NOON + 5).allowed


def test_an_expired_but_still_indexed_row_counts_as_new_for_the_owner_cap(
    new_store: StoreFactory,
) -> None:
    """Review fix (follow-up): a row whose logical expiry has passed is NOT present.

    Each cycle fills the owner with fresh keys after the previous ones expired
    (adding them purges the owner index, but in Redis not the table index).
    Replaying every old key inside one window must still give the owner at
    most ``max_keys_per_ip`` own rows; the rest share its ``~ip:`` overflow row.
    Without the fix the Redis store re-admitted each stale table-index member
    as "present" and the owner grew to 6 own rows (all allowed, no overflow).
    """
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    rate = Rate(1, 10.0)  # a row expires two windows (20 s) after its last hit
    keys = [f"ip/e{i}" for i in range(6)]
    for cycle in range(3):
        for key in keys[cycle * 2 : cycle * 2 + 2]:
            assert store.consume([Check("r", key, rate, owner="ip")], NOON + 30 * cycle).allowed
    assert store.metrics().get("overflow_events", 0) == 0
    replay = [store.consume([Check("r", key, rate, owner="ip")], NOON + 90).allowed for key in keys]
    # 2 own rows, then the owner's ONE shared row (limit 1): allowed once.
    assert replay == [True, True, True, False, False, False]
    assert store.metrics()["overflow_events"] == 4


def test_an_expired_unswept_row_is_not_present_in_either_store(new_store: StoreFactory) -> None:
    """Review fix (follow-up): both stores treat a row as present only while live.

    The in-memory store used to answer "ok" for ANY key still in ``rows``, live
    or not.  After an owner got past its cap (two NEW rows planned in one
    request, which both stores allow — see the phase-12 log), that expired
    row kept being re-admitted as its own row while Redis overflowed it: the
    differential fuzz found the two stores disagreeing.
    """
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    short, long_ = Rate(9, 10.0), Rate(9, 3600.0)

    a = Check("short", "ip/a", short, owner="ip")
    store.consume([a, Check("long", "ip/b", long_, owner="ip")], NOON)
    # a has expired; it and c are planned as new rows against b alone.
    store.consume([a, Check("long", "ip/c", long_, owner="ip")], NOON + 30)
    assert store.metrics().get("overflow_events", 0) == 0
    # a has expired AGAIN: b and c are live, so the owner is full -> overflow.
    store.consume([a], NOON + 60)
    assert store.metrics()["overflow_events"] == 1


def test_an_expired_but_still_indexed_row_counts_as_new_for_the_network_cap(
    new_store: StoreFactory,
) -> None:
    """Same as above for the /48 cap (16 x max_keys_per_ip rows per network)."""
    store = new_store(max_keys=1000, max_keys_per_ip=1)
    rate = Rate(9, 10.0)
    owners = [f"net/{i}" for i in range(48)]
    for cycle in range(3):
        for owner in owners[cycle * 16 : cycle * 16 + 16]:
            check = Check("r", owner, rate, owner=owner, net="net")
            assert store.consume([check], NOON + 30 * cycle).allowed
    assert store.metrics().get("overflow_events", 0) == 0
    for owner in owners:
        store.consume([Check("r", owner, rate, owner=owner, net="net")], NOON + 90)
    # 16 own rows; the other 32 are accounted in the network's hashed bucket.
    assert store.metrics()["overflow_events"] == 32


def test_when_the_table_is_full_new_clients_get_per_bucket_budgets(
    new_store: StoreFactory,
) -> None:
    store = new_store(max_keys=100, max_keys_per_ip=4)
    rate = Rate(2, 3600.0)

    def hit(net: str, key: str) -> bool:
        return bool(store.consume([Check("r", key, rate, owner=net, net=net)], NOON).allowed)

    for i in range(100):
        assert hit(f"bot{i}", f"bot{i}")
    attacker_buckets: set[int] = set()
    for i in range(100, 108):
        for _ in range(5):
            hit(f"bot{i}", f"bot{i}/k")
        attacker_buckets.add(store.overflow_bucket(f"bot{i}"))
    assert len(attacker_buckets) < store.overflow_buckets
    legit = next(
        f"legit{j}"
        for j in range(10_000)
        if store.overflow_bucket(f"legit{j}") not in attacker_buckets
    )
    assert [hit(legit, legit) for _ in range(3)] == [True, True, False]
    assert hit("bot0", "bot0")
    assert not hit("bot0", "bot0")


def test_overflow_is_still_fail_closed_for_the_crowding_network(new_store: StoreFactory) -> None:
    store = new_store(max_keys=100, max_keys_per_ip=4)
    rate = Rate(3, 3600.0)
    for i in range(100):
        store.consume([Check("r", f"fill{i}", rate, owner=f"fill{i}", net=f"fill{i}")], NOON)
    verdicts = [
        store.consume([Check("r", f"atk/{i}", rate, owner="atk", net="atk")], NOON + 1).allowed
        for i in range(10)
    ]
    assert verdicts == [True] * 3 + [False] * 7


def test_quota_table_overflow_is_per_bucket_not_global(new_store: StoreFactory) -> None:
    store = new_store(max_keys=100)
    daily = Rate(1, DAY, fixed=True)
    for i in range(100):
        assert store.consume([Check("ai:user_daily", f"user:{i}", daily)], NOON).allowed
    spent = {store.overflow_bucket("user:x0")}
    store.consume([Check("ai:user_daily", "user:x0", daily)], NOON)
    other = next(
        f"user:y{j}" for j in range(10_000) if store.overflow_bucket(f"user:y{j}") not in spent
    )
    assert store.consume([Check("ai:user_daily", other, daily)], NOON).allowed
    assert not store.consume([Check("ai:user_daily", "user:0", daily)], NOON + 1).allowed


def test_overflow_memory_stays_bounded_under_a_flood(new_store: StoreFactory) -> None:
    store = new_store(max_keys=200, max_keys_per_ip=4)
    rate = Rate(5, 3600.0)
    for i in range(3000):
        net = f"n{i % 750}"
        store.consume([Check("r", f"k{i}", rate, owner=f"o{i}", net=net)], NOON)
        store.consume([Check("q", f"user:{i}", rate)], NOON)
        store.record_signal(f"email:{i}", NOON, window=3600.0)
    buckets = store.overflow_buckets
    assert buckets == 16
    assert store.size() <= 4 * 200 + 3 * buckets


def test_overflow_bucket_count_is_proportional_to_max_keys(new_store: StoreFactory) -> None:
    assert new_store(max_keys=100).overflow_buckets == 16
    assert new_store(max_keys=50_000).overflow_buckets == 250
    assert new_store(max_keys=10_000_000).overflow_buckets == 4096
    assert new_store(max_keys_per_ip=4).max_keys_per_net == 64


# ---------------------------------------------------------------------------
# Failed-login backoff rows
# ---------------------------------------------------------------------------


def test_progressive_backoff_grows_caps_and_resets(new_store: StoreFactory) -> None:
    store = new_store()
    kw: dict[str, Any] = {"threshold": 3, "base": 10, "cap": 40, "window": 100}
    t = NOON
    store.record_failure("e", t, **kw)
    store.record_failure("e", t, **kw)
    assert store.backoff_remaining("e", t) == 0
    waits = []
    for _ in range(4):
        store.record_failure("e", t, **kw)
        waits.append(store.backoff_remaining("e", t))
    assert waits == [10, 20, 40, 40]
    assert store.backoff_remaining("e", t + 39) == 1
    assert store.backoff_remaining("e", t + 40) == 0
    store.clear_failures("e")
    store.record_failure("e", t + 41, **kw)
    assert store.backoff_remaining("e", t + 41) == 0


def test_old_failures_are_forgotten(new_store: StoreFactory) -> None:
    store = new_store()
    kw: dict[str, Any] = {"threshold": 2, "base": 10, "cap": 40, "window": 100}
    store.record_failure("e", NOON, **kw)
    store.record_failure("e", NOON + 101, **kw)
    assert store.backoff_remaining("e", NOON + 101) == 0


def test_min_lock_locks_at_once_whatever_the_count(new_store: StoreFactory) -> None:
    store = new_store()
    kw: dict[str, Any] = {"threshold": 5, "base": 10, "cap": 900, "window": 900}
    store.record_failure("e", NOON, min_lock=120, **kw)
    assert store.backoff_remaining("e", NOON) == 120
    assert store.backoff_remaining("e", NOON + 120) == 0


def test_clearing_frees_the_owner_slot(new_store: StoreFactory) -> None:
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    kw: dict[str, Any] = {"threshold": 9, "base": 60, "cap": 60, "window": 900}
    store.record_failure("ip/e0", NOON, owner="ip", **kw)
    store.record_failure("ip/e1", NOON, owner="ip", **kw)
    store.clear_failures("ip/e0")
    store.record_failure("ip/e2", NOON, owner="ip", **kw)  # an own row again, not overflow
    assert store.metrics()["overflow_events"] == 0


def test_backoff_lockouts_are_never_evicted_by_a_flood(new_store: StoreFactory) -> None:
    store = new_store(max_keys=3)
    kw: dict[str, Any] = {"threshold": 1, "base": 600, "cap": 600, "window": 900}
    store.record_failure("attacker/victim", NOON, owner="attacker", **kw)
    for i in range(100):
        store.record_failure(f"o{i}/e", NOON + 1, owner=f"o{i}", **kw)
    assert store.backoff_remaining("attacker/victim", NOON + 2, owner="attacker") > 0


def test_backoff_spray_past_the_per_owner_cap_locks_that_owner_out(
    new_store: StoreFactory,
) -> None:
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    kw: dict[str, Any] = {"threshold": 2, "base": 60, "cap": 60, "window": 900}
    for i in range(6):
        store.record_failure(f"ip/e{i}", NOON, owner="ip", **kw)
    assert store.backoff_remaining("ip/e9", NOON + 1, owner="ip") > 0
    assert store.backoff_remaining("other/e9", NOON + 1, owner="other") == 0


def test_an_expired_but_still_indexed_backoff_row_counts_as_new_for_the_owner_cap(
    new_store: StoreFactory,
) -> None:
    """Review fix (follow-up): expire-then-reuse cannot grow an owner's backoff rows."""
    store = new_store(max_keys=1000, max_keys_per_ip=2)
    kw: dict[str, Any] = {"threshold": 3, "base": 60, "cap": 60, "window": 10}
    keys = [f"ip/e{i}" for i in range(6)]
    for cycle in range(3):
        for key in keys[cycle * 2 : cycle * 2 + 2]:
            store.record_failure(key, NOON + 30 * cycle, owner="ip", **kw)
    assert store.metrics().get("overflow_events", 0) == 0
    for key in keys:
        store.record_failure(key, NOON + 90, owner="ip", **kw)
    # 2 own rows (1 failure each); 4 failures land in the owner's shared row,
    # which crosses the threshold and locks THAT owner out.
    assert store.metrics()["overflow_events"] == 4
    assert store.backoff_remaining("ip/new", NOON + 90, owner="ip") > 0
    assert store.backoff_remaining("other/e", NOON + 90, owner="other") == 0


def test_an_expired_but_still_indexed_backoff_row_counts_as_new_for_the_network_cap(
    new_store: StoreFactory,
) -> None:
    store = new_store(max_keys=1000, max_keys_per_ip=1)
    kw: dict[str, Any] = {"threshold": 9, "base": 60, "cap": 60, "window": 10}
    owners = [f"net/{i}" for i in range(48)]
    for cycle in range(3):
        for owner in owners[cycle * 16 : cycle * 16 + 16]:
            store.record_failure(f"{owner}/e", NOON + 30 * cycle, owner=owner, net="net", **kw)
    assert store.metrics().get("overflow_events", 0) == 0
    for owner in owners:
        store.record_failure(f"{owner}/e", NOON + 90, owner=owner, net="net", **kw)
    assert store.metrics()["overflow_events"] == 32


def test_lockouts_and_quota_counters_survive_a_slash48_flood(new_store: StoreFactory) -> None:
    store = new_store(max_keys=100, max_keys_per_ip=4)
    kw: dict[str, Any] = {"threshold": 1, "base": 600, "cap": 600, "window": 900}
    daily = Rate(1, DAY, fixed=True)
    store.record_failure("victim-ip/e", NOON, owner="victim-ip", net="victim-ip", **kw)
    assert store.consume([Check("ai:user_daily", "user:u", daily)], NOON).allowed
    for i in range(1500):
        owner = f"atk48/{i}"
        store.record_failure(f"{owner}/e", NOON + 1, owner=owner, net="atk48", **kw)
        store.consume([Check("r", owner, Rate(9, 60.0), owner=owner, net="atk48")], NOON + 1)
    assert store.backoff_remaining("victim-ip/e", NOON + 2, owner="victim-ip", net="victim-ip")
    assert not store.consume([Check("ai:user_daily", "user:u", daily)], NOON + 2).allowed
    assert backoff_rows(store) <= 64 + 1
    assert store.backoff_remaining("atk48/new/e", NOON + 2, owner="atk48/new", net="atk48") > 0
    assert store.backoff_remaining("other/e", NOON + 2, owner="other", net="other") == 0


# ---------------------------------------------------------------------------
# Per-email signal counter
# ---------------------------------------------------------------------------


def test_the_email_counter_keeps_counting_when_its_table_is_full(new_store: StoreFactory) -> None:
    store = new_store(max_keys=100)
    for i in range(100):
        store.record_signal(f"email:spray{i}", NOON, window=3600.0)
    counts = [store.record_signal("email:victim", NOON + i, window=3600.0)[0] for i in range(5)]
    assert counts[-1] >= 5


def test_record_signal_reports_each_threshold_crossing_once(new_store: StoreFactory) -> None:
    store = new_store()
    crossed = [
        store.record_signal("email:e", NOON + i, window=3600.0, thresholds=(3, 5))[1]
        for i in range(7)
    ]
    assert crossed == [(), (), (3,), (), (5,), (), ()]


def test_record_signal_is_a_sliding_hour(new_store: StoreFactory) -> None:
    store = new_store()
    for i in range(10):
        store.record_signal("email:e", NOON + i, window=3600.0)
    assert store.record_signal("email:e", NOON + 7200, window=3600.0)[0] == 1


# ---------------------------------------------------------------------------
# Named counters
# ---------------------------------------------------------------------------


def test_metrics_count_and_are_named(new_store: StoreFactory) -> None:
    store = new_store()
    store.incr_metric("throttled:ai")
    store.incr_metric("throttled:ai", 2)
    store.incr_metric("emails_alerted")
    m = store.metrics()
    assert m["throttled:ai"] == 3 and m["emails_alerted"] == 1
    assert m.get("overflow_events", 0) == 0
    assert store.status()["backend"] in ("memory", "redis")


# ---------------------------------------------------------------------------
# Through the limiter + middleware (multi-check policies, unit isolation)
# ---------------------------------------------------------------------------


def _app(limiter: Any) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_middleware(RateLimitMiddleware)
    app.state.rate_limiter = limiter

    @app.get("/api/v1/things")
    def things() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/policy/query")
    def ai() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _bearer(org: Any = None) -> dict[str, str]:
    import uuid  # noqa: PLC0415

    from backend.app.core.security import create_access_token  # noqa: PLC0415

    return {"Authorization": f"Bearer {create_access_token(uuid.uuid4(), org or uuid.uuid4())}"}


def test_ai_policy_burst_daily_and_org_quota(kind: str) -> None:
    import uuid  # noqa: PLC0415

    clock = FakeClock()
    limiter = limiter_for(
        kind,
        clock,
        RATE_LIMIT_AI_PER_USER="2/hour",
        RATE_LIMIT_AI_DAILY_PER_USER="3/day",
        RATE_LIMIT_AI_DAILY_PER_ORG="5/day",
    )
    client = TestClient(_app(limiter))
    org = uuid.uuid4()
    user = _bearer(org)

    def ask(headers: dict[str, str]) -> int:
        return client.post("/api/v1/policy/query", headers=headers).status_code

    assert [ask(user), ask(user), ask(user)] == [200, 200, 429]  # hourly burst
    clock.advance(2 * 3600)
    assert [ask(user), ask(user)] == [200, 429]  # daily (3): the 429 burned nothing
    # The org quota saw exactly 3 of its 5: two more colleagues fit, the third does not.
    assert [ask(_bearer(org)), ask(_bearer(org)), ask(_bearer(org))] == [200, 200, 429]
    clock.advance(DAY)
    assert ask(user) == 200
    assert limiter.snapshot()["throttled"]["ai"] == 3


def test_a_slash48_flood_cannot_push_other_networks_into_an_exhausted_bucket(kind: str) -> None:
    limiter = limiter_for(
        kind,
        TRUSTED_PROXY_COUNT=1,
        RATE_LIMIT_MAX_KEYS=100,
        RATE_LIMIT_MAX_KEYS_PER_IP=4,
        RATE_LIMIT_DEFAULT="2/minute",
    )
    client = TestClient(_app(limiter))

    def get(ip: str) -> int:
        return client.get("/api/v1/things", headers={"X-Forwarded-For": ip}).status_code

    for i in range(600):
        get(f"2001:db8:aaaa:{i:x}::1")
    for ip in ("2001:db8:bbbb::1", "198.51.100.5", "203.0.113.9"):
        assert [get(ip), get(ip), get(ip)] == [200, 200, 429], ip
    assert get("2001:db8:aaaa:ffff::1") == 429


# ---------------------------------------------------------------------------
# Login guard: backoff, success reset, distributed escalation
# ---------------------------------------------------------------------------


def _request(limiter: RateLimiter, ip: str) -> Request:
    app = FastAPI()
    app.state.rate_limiter = limiter
    scope = {
        "type": "http",
        "app": app,
        "client": (ip, 4242),
        "headers": [],
        "method": "POST",
        "path": "/api/v1/auth/login",
    }
    return Request(scope)


def _fail() -> str:
    raise UnauthorizedError("Invalid email or password.")


def _ok() -> str:
    return "ok"


def _login(limiter: RateLimiter, ip: str, attempt: Callable[[], str]) -> str:
    try:
        return guard_login(_request(limiter, ip), "Victim@Example.test", attempt)
    except RateLimitedError:
        return "429"
    except UnauthorizedError:
        return "401"


def test_login_backoff_then_success_reset(kind: str) -> None:
    clock = FakeClock()
    limiter = limiter_for(kind, clock, LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=30)
    a, b = "203.0.113.1", "198.51.100.2"
    assert [_login(limiter, a, _fail), _login(limiter, a, _fail)] == ["401", "401"]
    assert _login(limiter, a, _ok) == "429"  # that address is cooling down
    assert _login(limiter, b, _ok) == "ok"  # nobody else is
    clock.advance(30)
    assert _login(limiter, a, _ok) == "ok"
    # The success cleared that row: one new typo is not a lockout.
    assert _login(limiter, a, _fail) == "401"
    assert _login(limiter, a, _ok) == "ok"
    assert limiter.snapshot()["throttled"]["auth_login_backoff"] == 1


def test_distributed_escalation_locks_only_failing_addresses(kind: str) -> None:
    limiter = limiter_for(kind, LOGIN_EMAIL_FAILURE_ALERT=3, LOGIN_BACKOFF_BASE_SECONDS=60)
    for i in range(1, 4):  # one failure each from three addresses: the third crosses
        assert _login(limiter, f"203.0.113.{i}", _fail) == "401"
    assert _login(limiter, "203.0.113.3", _ok) == "429"  # stepped-up lock
    assert _login(limiter, "203.0.113.1", _ok) == "ok"  # failed before the alert: free
    assert _login(limiter, "198.51.100.9", _ok) == "ok"  # clean address: never blocked
    snap = limiter.snapshot()["login_email_failure_alert"]
    assert (snap["emails_alerted"], snap["escalated_failures"]) == (1, 1)


# ---------------------------------------------------------------------------
# Differential: the two stores, driven by the same random operations, agree
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_LUA, reason=_NO_LUA)
@pytest.mark.parametrize("seed", range(12))
def test_memory_and_redis_agree_under_tight_caps(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix (follow-up): a seeded memory-vs-Redis fuzz with TIGHT caps.

    The contract cases above only use roomy caps for most paths; the adversarial
    review found the stores diverging once rows expired while still indexed.
    The in-memory store throttles its full-table sweep to once a second (a
    flood must not turn every request into a scan; Redis purges an index in
    O(log n) and needs no throttle); that throttle is switched off here so
    both stores purge at the same moments and every answer must be identical.
    """
    import random  # noqa: PLC0415

    from backend.app.core import rate_limit as rl  # noqa: PLC0415

    monkeypatch.setattr(rl._BoundedTable, "_sweep_when_full", lambda self, now: self.sweep(now))
    mem = build_store("memory", max_keys=10, max_keys_per_ip=1)
    red = build_store("redis", max_keys=10, max_keys_per_ip=1)
    red._bucket_key = mem._bucket_key  # same hashed buckets
    rnd = random.Random(seed)
    rates = [Rate(3, 10.0), Rate(5, 60.0), Rate(2, DAY, fixed=True)]
    now = NOON
    for step in range(200):
        now += rnd.choice([0, 0.5, 1, 2.5, 5, 10, 30])
        op = rnd.random()
        where = f"seed {seed} step {step}"
        if op < 0.6:
            checks = []
            for _ in range(rnd.randint(1, 3)):
                r, k = rnd.randint(0, 2), rnd.randint(0, 8)
                if rnd.random() < 0.7:
                    checks.append(Check(f"r{r}", f"k{k}", rates[r], f"o{k % 5}", f"n{k % 2}"))
                else:
                    checks.append(Check(f"r{r}", f"q{k}", rates[r]))
            assert mem.consume(checks, now) == red.consume(checks, now), where
        elif op < 0.8:
            e = rnd.randint(0, 5)
            kw: dict[str, Any] = {
                "threshold": 3,
                "base": 1.0,
                "cap": 30.0,
                "window": 20.0,
                "owner": f"o{e % 5}",
                "net": f"n{e % 2}",
                "min_lock": rnd.choice([0, 0, 5.0]),
            }
            mem.record_failure(f"e{e}", now, **kw)
            red.record_failure(f"e{e}", now, **kw)
        elif op < 0.85:
            e = rnd.randint(0, 5)
            mem.clear_failures(f"e{e}")
            red.clear_failures(f"e{e}")
        elif op < 0.93:
            e = rnd.randint(0, 5)
            args: dict[str, Any] = {"owner": f"o{e % 5}", "net": f"n{e % 2}"}
            wait_m = mem.backoff_remaining(f"e{e}", now, **args)
            wait_r = red.backoff_remaining(f"e{e}", now, **args)
            assert abs(wait_m - wait_r) < 1e-6, where
        else:
            key = f"s{rnd.randint(0, 30)}"
            got_m = mem.record_signal(key, now, window=60.0, thresholds=(2, 4))
            got_r = red.record_signal(key, now, window=60.0, thresholds=(2, 4))
            assert got_m == got_r, where
    assert mem.metrics()["overflow_events"] == red.metrics()["overflow_events"]
    assert red.redis_errors == 0
