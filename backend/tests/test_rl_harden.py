"""Rate-limit hardening (phase 11 follow-up) — hermetic, no database.

Finding 2 (shared overflow bucket): when the per-IP table is full, or a client
network is over its cap, new identities used to fall into ONE global bucket
per rule, so one attacker could push every new legitimate client into a single
exhausted budget.  Now overflow is accounted per hashed network bucket
(``RATE_LIMIT_MAX_KEYS / 200`` buckets, 16..4096; IPv6 grouped by /48, IPv4 by
address), and one IPv6 /48 may own at most 16 x ``RATE_LIMIT_MAX_KEYS_PER_IP``
rows, so a single /48 can no longer fill the table at all.

Finding 1(b) store side: the per-email failure counter keeps counting when its
table is full (hashed bucket fallback), so a key-spraying flood cannot switch
the distributed-attack guard off; the escalation math is capped.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.testclient import TestClient

from backend.app.core.errors import AppError, app_error_handler
from backend.app.core.rate_limit import (
    Check,
    InMemoryRateLimitStore,
    RateLimitMiddleware,
    ip_net,
    ip_owner,
)
from backend.tests.rate_limit_support import NOON, FakeClock, make_limiter
from config.settings import Settings
from config.settings.rates import Rate

DAY = 86400.0


def _app(limiter: Any) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_middleware(RateLimitMiddleware)
    app.state.rate_limiter = limiter

    @app.get("/api/v1/things")
    def things() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _get(client: TestClient, ip: str) -> int:
    return client.get("/api/v1/things", headers={"X-Forwarded-For": ip}).status_code


# ---------------------------------------------------------------------------
# Finding 2 — address grouping
# ---------------------------------------------------------------------------


def test_ip_net_groups_ipv6_by_slash48_and_keeps_ipv4_per_address() -> None:
    assert ip_net("2001:db8:1:2::1") == ip_net("2001:db8:1:ffff::9")
    assert ip_net("2001:db8:1:2::1") != ip_net("2001:db8:2:2::1")
    assert ip_owner("2001:db8:1:2::1") != ip_owner("2001:db8:1:3::1")  # /64 still per owner
    assert ip_net("198.51.100.7") == "198.51.100.7"
    assert ip_net("198.51.100.7") != ip_net("198.51.100.8")
    assert ip_net("::ffff:198.51.100.7") == "198.51.100.7"
    assert ip_net("testclient") == "testclient"


# ---------------------------------------------------------------------------
# Finding 2 — no shared global overflow bucket
# ---------------------------------------------------------------------------


def test_a_slash48_flood_cannot_push_other_networks_into_a_shared_exhausted_bucket() -> None:
    """One attacker /48 rotating through thousands of /64s used to fill the
    per-IP table and then exhaust the ONE global overflow bucket every new
    client fell into.  Now the /48 is capped and overflows into its own hashed
    bucket; clients from other networks keep their own counters."""
    limiter = make_limiter(
        TRUSTED_PROXY_COUNT=1,
        RATE_LIMIT_MAX_KEYS=100,
        RATE_LIMIT_MAX_KEYS_PER_IP=4,
        RATE_LIMIT_DEFAULT="2/minute",
    )
    client = TestClient(_app(limiter))
    for i in range(3000):  # 3000 distinct /64s inside 2001:db8:aaaa::/48
        _get(client, f"2001:db8:aaaa:{i:x}::1")
    # Clients from unrelated networks: a fresh, full budget each.
    for ip in ("2001:db8:bbbb::1", "2001:db8:cccc:1::7", "198.51.100.5", "203.0.113.9"):
        assert _get(client, ip) == 200, ip
        assert _get(client, ip) == 200, ip
        assert _get(client, ip) == 429, ip  # ...and their own limit still applies
    # The flooding /48 itself is throttled (it shares its own spent bucket).
    assert _get(client, "2001:db8:aaaa:ffff::1") == 429


def test_when_the_table_is_full_new_clients_get_per_bucket_budgets() -> None:
    """A botnet fills the table from many networks; a legit network whose
    hashed bucket the flood did not exhaust still gets a non-zero budget."""
    store = InMemoryRateLimitStore(max_keys=100, max_keys_per_ip=4)
    rate = Rate(2, 3600.0)

    def hit(net: str, key: str) -> bool:
        return store.consume([Check("r", key, rate, owner=net, net=net)], NOON).allowed

    for i in range(100):  # fill the table with 100 live counters
        assert hit(f"bot{i}", f"bot{i}")
    attacker_buckets: set[int] = set()
    # Only 8 more bot networks, so they can touch at most 8 of the 16 buckets however the
    # per-process secret hashes them (a larger flood would cover every bucket by chance).
    for i in range(100, 108):  # all in overflow, spent
        for _ in range(5):
            hit(f"bot{i}", f"bot{i}/k")
        attacker_buckets.add(store.overflow_bucket(f"bot{i}"))
    assert len(attacker_buckets) < store.overflow_buckets  # some buckets untouched
    legit = next(
        f"legit{j}"
        for j in range(10_000)
        if store.overflow_bucket(f"legit{j}") not in attacker_buckets
    )
    # Reduced (shared with its bucket) but NON-ZERO budget, not the bot's spent one.
    assert [hit(legit, legit) for _ in range(3)] == [True, True, False]
    # The original live counters were never evicted (bot0 has 1 of 2 left).
    assert hit("bot0", "bot0")
    assert not hit("bot0", "bot0")


def test_overflow_is_still_fail_closed_for_the_crowding_network() -> None:
    """The attacker's own network shares ONE bucket: key spraying never mints
    fresh budgets, whichever bound it hits."""
    store = InMemoryRateLimitStore(max_keys=100, max_keys_per_ip=4)
    rate = Rate(3, 3600.0)
    for i in range(100):
        store.consume([Check("r", f"fill{i}", rate, owner=f"fill{i}", net=f"fill{i}")], NOON)
    verdicts = [
        store.consume([Check("r", f"atk/{i}", rate, owner="atk", net="atk")], NOON + 1).allowed
        for i in range(10)
    ]
    assert verdicts == [True] * 3 + [False] * 7


def test_quota_table_overflow_is_per_bucket_not_global() -> None:
    store = InMemoryRateLimitStore(max_keys=100)
    daily = Rate(1, DAY, fixed=True)
    for i in range(100):
        assert store.consume([Check("ai:user_daily", f"user:{i}", daily)], NOON).allowed
    spent = {store.overflow_bucket("user:x0")}
    store.consume([Check("ai:user_daily", "user:x0", daily)], NOON)
    other = next(
        f"user:y{j}" for j in range(10_000) if store.overflow_bucket(f"user:y{j}") not in spent
    )
    assert store.consume([Check("ai:user_daily", other, daily)], NOON).allowed
    # Established quota counters were never evicted.
    assert not store.consume([Check("ai:user_daily", "user:0", daily)], NOON + 1).allowed


def test_overflow_memory_stays_bounded_under_a_huge_flood() -> None:
    store = InMemoryRateLimitStore(max_keys=200, max_keys_per_ip=4)
    rate = Rate(5, 3600.0)
    for i in range(20_000):
        net = f"n{i % 5000}"
        store.consume([Check("r", f"k{i}", rate, owner=f"o{i}", net=net)], NOON)
        store.consume([Check("q", f"user:{i}", rate)], NOON)
        store.record_signal(f"email:{i}", NOON, window=3600.0)
    buckets = store.overflow_buckets
    assert buckets == 16  # max(16, 200 // 200)
    # per-IP table + quota table + signals: each <= max_keys; owner-overflow rows
    # <= max_keys; hashed buckets <= buckets per rule (2 rules) + signal buckets.
    assert store.size() <= 4 * 200 + 3 * buckets


def test_overflow_bucket_count_is_proportional_to_max_keys() -> None:
    assert InMemoryRateLimitStore(max_keys=100).overflow_buckets == 16
    assert InMemoryRateLimitStore(max_keys=50_000).overflow_buckets == 250
    assert InMemoryRateLimitStore(max_keys=10_000_000).overflow_buckets == 4096


def test_lockouts_and_quota_counters_survive_a_slash48_flood() -> None:
    store = InMemoryRateLimitStore(max_keys=100, max_keys_per_ip=4)
    kw: dict[str, Any] = {"threshold": 1, "base": 600, "cap": 600, "window": 900}
    daily = Rate(1, DAY, fixed=True)
    store.record_failure("victim-ip/e", NOON, owner="victim-ip", net="victim-ip", **kw)
    assert store.consume([Check("ai:user_daily", "user:u", daily)], NOON).allowed
    for i in range(5000):
        owner = f"atk48/{i}"
        store.record_failure(f"{owner}/e", NOON + 1, owner=owner, net="atk48", **kw)
        store.consume([Check("r", owner, Rate(9, 60.0), owner=owner, net="atk48")], NOON + 1)
    assert store.backoff_remaining("victim-ip/e", NOON + 2, owner="victim-ip", net="victim-ip")
    assert not store.consume([Check("ai:user_daily", "user:u", daily)], NOON + 2).allowed
    # The /48 is capped in the backoff table too (it cannot fill it) ...
    assert len(store._backoff) <= 64 + 1
    # ... and its spray past the cap locks THAT /48, nobody else.
    assert store.backoff_remaining("atk48/new/e", NOON + 2, owner="atk48/new", net="atk48") > 0
    assert store.backoff_remaining("other/e", NOON + 2, owner="other", net="other") == 0


# ---------------------------------------------------------------------------
# Finding 1(b) — store side of the per-email distributed guard
# ---------------------------------------------------------------------------


def test_the_email_counter_keeps_counting_when_its_table_is_full() -> None:
    """Spraying emails to fill the signal table must not switch off the
    distributed guard for the victim's email (it falls back to a hashed bucket)."""
    store = InMemoryRateLimitStore(max_keys=100)
    for i in range(100):
        store.record_signal(f"email:spray{i}", NOON, window=3600.0)
    counts = [store.record_signal("email:victim", NOON + i, window=3600.0)[0] for i in range(5)]
    assert counts[-1] >= 5


def test_record_signal_reports_each_threshold_crossing_once() -> None:
    store = InMemoryRateLimitStore()
    crossed = [
        store.record_signal("email:e", NOON + i, window=3600.0, thresholds=(3, 5))[1]
        for i in range(7)
    ]
    assert crossed == [(), (), (3,), (), (5,), (), ()]


def test_record_signal_is_a_sliding_hour() -> None:
    store = InMemoryRateLimitStore()
    for i in range(10):
        store.record_signal("email:e", NOON + i, window=3600.0)
    # Two hours later the old failures have decayed away completely.
    assert store.record_signal("email:e", NOON + 7200, window=3600.0)[0] == 1


def test_distributed_lock_escalates_from_the_alert_and_caps() -> None:
    limiter = make_limiter(
        FakeClock(),
        LOGIN_EMAIL_FAILURE_ALERT=3,
        LOGIN_BACKOFF_BASE_SECONDS=30,
        LOGIN_BACKOFF_MAX_SECONDS=900,
    )
    assert [limiter.distributed_lock_seconds(n) for n in range(1, 11)] == [
        0,
        0,
        30,
        60,
        120,
        240,
        480,
        900,
        900,
        900,
    ]


def test_the_distributed_alert_default_is_fifty_and_validated() -> None:
    assert Settings.model_fields["LOGIN_EMAIL_FAILURE_ALERT"].default == 50
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            JWT_SECRET="x" * 40,
            DATABASE_URL="postgresql://u@h/d",
            LOGIN_EMAIL_FAILURE_ALERT=0,
        )
