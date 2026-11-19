"""Request rate limiting for the public demo (phase 11).

Why: anyone can reach ``POST /auth/login``, and anyone who logs in can make
the backend spend money (LLM / embedding / rerank calls, vision OCR, object
storage).  This module bounds both, without a new dependency.

Shape
-----
* :class:`InMemoryRateLimitStore` — sliding-window counters (two buckets per
  key, O(1) memory), fixed UTC-day quota counters and a failed-login backoff
  table, behind the small :class:`RateLimitStore` protocol so a Redis / DB
  store can replace it when the app runs more than one process.  Thread-safe
  (one lock), memory-bounded, clock injected by the caller.  A LIVE counter
  is never evicted to make room: per-IP counters, per-user / per-org quota
  counters and login backoff rows live in separate bounded tables; keys per
  IP (per IPv6 /64) and per IPv6 /48 are capped; an identity that finds no
  room is accounted in its own address's overflow row or in one of a fixed
  number of HMAC-hashed network buckets (fail closed, never a fresh budget,
  and never ONE global bucket a flood could exhaust for everyone).
* :class:`RateLimiter` — the policies: which counters a request consumes, the
  throttle counter behind ``GET /ops/rate-limits``, the WARNING log line.
  Identities are HMAC-hashed with a per-process random key before they are
  stored or logged, so no raw IP, email or token is ever kept.
* :class:`RateLimitMiddleware` — classifies every ``/api/v1`` request into ONE
  policy, consumes its counters and answers ``429`` in the uniform error
  envelope (code ``RATE_LIMITED``, ``retry_after_seconds``, ``Retry-After``)
  or adds ``X-RateLimit-Limit/Remaining/Reset`` to the response.  ``/health``
  and every non-API path are exempt.
* :func:`guard_login` — the parts of login protection that need the email:
  a per-(IP, email) window and progressive backoff after repeated failures.
  The backoff HARD block is keyed by (client IP, email hash, "a code was
  given"), so a remote attacker can only throttle their own address, never
  lock a victim out, and cannot mint keys by varying the org code.  A
  per-email failure counter across all addresses steps up the lock a FAILING
  address earns once it passes ``LOGIN_EMAIL_FAILURE_ALERT`` an hour (a
  distributed attack); a correct password from a clean address is never
  blocked.  Everything is checked before any database read, so a throttled
  attempt is the same 429 whether or not the account (or org code) exists.

The default store is per process.  Render runs ONE uvicorn worker on ONE
instance, so these counters are global for the demo.  ``RATE_LIMIT_STORE=redis``
swaps in :class:`backend.app.core.rate_limit_redis.RedisRateLimitStore` (same
protocol, same guarantees, shared by every worker / instance); see
docs/runbooks/rate-limit-redis.md.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.errors import (
    AmbiguousCredentialsError,
    AppError,
    ErrorCode,
    UnauthorizedError,
    app_error_handler,
)
from config.settings.rates import Rate, parse_rate

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
T = TypeVar("T")

# Response headers a cross-origin browser client must be allowed to read.
RATE_LIMIT_EXPOSED_HEADERS = (
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)

# Float slack for window arithmetic (counts are integers; times are floats).
_EPS = 1e-9


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One counter a request consumes: ``rule`` names it, ``key`` is the hashed identity.

    ``owner`` is the hashed client-address group (the IP, or its IPv6 /64) that
    an ANONYMOUS / per-IP counter belongs to; such counters live in the per-IP
    table, capped per owner.  ``None`` marks a per-user / per-org quota counter,
    kept in a separate table that anonymous traffic cannot fill.  ``net`` is
    the hashed network aggregate (:func:`ip_net`: IPv6 /48, else the address):
    rows per network are capped too, and overflow is bucketed by it.
    """

    rule: str
    key: str
    rate: Rate
    owner: str | None = None
    net: str | None = None


@dataclass(frozen=True)
class Decision:
    """The outcome of consuming a set of counters (all-or-nothing)."""

    allowed: bool
    rule: str
    limit: int
    remaining: int
    reset_seconds: int
    retry_after_seconds: int = 0

    def headers(self) -> dict[str, str]:
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after_seconds)
        return out


def _ceil_seconds(value: float) -> int:
    return max(1, math.ceil(value - _EPS))


def window_decision(check: Check, now: float, curr: float, prev: float) -> Decision:
    """The verdict for consuming one unit of ``check`` given its window counts.

    ``curr`` / ``prev`` are the counts of the current / previous window (index
    ``floor(now / W)``).  Shared by every store so they answer identically; the
    Redis store's Lua script evaluates the same ``allowed`` expression.
    """
    window = check.rate.window_seconds
    limit = check.rate.limit
    idx = math.floor(now / window)
    elapsed = now - idx * window
    to_boundary = window - elapsed
    if check.rate.fixed:
        estimate = curr
    else:
        estimate = prev * (1.0 - elapsed / window) + curr
    if estimate + 1 <= limit + _EPS:
        remaining = max(0, math.floor(limit - estimate - 1 + _EPS))
        return Decision(True, check.rule, limit, remaining, _ceil_seconds(to_boundary))
    if check.rate.fixed:
        wait = to_boundary
    elif curr + 1 <= limit:
        # The weighted previous window must decay enough.
        wait = window * (1.0 - (limit - 1 - curr) / prev) - elapsed
    else:
        # Wait for the next window, then for this one to decay.
        wait = to_boundary + window * max(0.0, 1.0 - (limit - 1) / curr)
    return Decision(False, check.rule, limit, 0, _ceil_seconds(to_boundary), _ceil_seconds(wait))


def combine_decisions(decisions: Sequence[Decision]) -> Decision:
    """One answer for a request: the longest wait if any counter denies, else the tightest."""
    if not decisions:
        return Decision(True, "none", 0, 0, 0)
    denied = [d for d in decisions if not d.allowed]
    if denied:
        return max(denied, key=lambda d: d.retry_after_seconds)
    return min(decisions, key=lambda d: (d.remaining, -d.reset_seconds))


def signal_result(before: float, thresholds: Sequence[int]) -> tuple[int, tuple[int, ...]]:
    """``record_signal``'s answer given the windowed count BEFORE this event."""
    after = before + 1
    crossed = tuple(t for t in thresholds if before + _EPS < t <= after + _EPS)
    return math.floor(after + _EPS), crossed


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class RateLimitStore(Protocol):
    """What the limiter needs from its storage (swap for Redis / DB later)."""

    def consume(self, checks: Sequence[Check], now: float) -> Decision:
        """Consume one unit from every counter, or none if any would exceed its rate."""
        ...

    def backoff_remaining(
        self, key: str, now: float, *, owner: str | None = None, net: str | None = None
    ) -> float:
        """Seconds until ``key`` may try again (0 when not cooling down)."""
        ...

    def record_failure(
        self,
        key: str,
        now: float,
        *,
        threshold: int,
        base: float,
        cap: float,
        window: float,
        owner: str | None = None,
        net: str | None = None,
        min_lock: float = 0.0,
    ) -> None:
        """Count one failure for ``key``; start / extend its cooldown past ``threshold``.

        ``min_lock`` > 0 locks ``key`` for at least that long right away (the
        per-email distributed guard's step-up), whatever its own count.
        """
        ...

    def clear_failures(self, key: str) -> None: ...

    def record_signal(
        self, key: str, now: float, *, window: float, thresholds: Sequence[int] = ()
    ) -> tuple[int, tuple[int, ...]]:
        """Count one event for ``key`` in a sliding ``window``.

        Returns the count and the ``thresholds`` this event crossed (each is
        reported once per upward pass).
        """
        ...

    def size(self) -> int: ...

    def incr_metric(self, name: str, amount: int = 1) -> None:
        """Add ``amount`` to the named counter shown by ``GET /ops/rate-limits``."""
        ...

    def metrics(self) -> dict[str, int]:
        """Every named counter (throttles per policy, soft signals, overflow ...)."""
        ...

    def status(self) -> dict[str, Any]:
        """Backend name and health for ``GET /ops/rate-limits`` (no secrets)."""
        ...


@dataclass(slots=True)
class _Entry:
    values: list[float]
    expires: float
    owner: str | None
    net: str | None = None


def _decrement(counts: dict[str, int], group: str | None) -> None:
    if group is None:
        return
    left = counts.get(group, 1) - 1
    if left > 0:
        counts[group] = left
    else:
        counts.pop(group, None)


class _BoundedTable:
    """Live entries with a hard size bound, a per-owner and a per-network bound.

    Nothing unexpired is ever evicted: when there is no room, :meth:`room_for`
    says so and the caller decides (an overflow row / bucket, or no row).
    Expired rows are swept periodically and, rate-limited to once a second so
    a flood cannot turn every request into a full scan, whenever a bound is
    reached.
    """

    def __init__(
        self, max_keys: int, per_owner: int | None = None, per_net: int | None = None
    ) -> None:
        self.max_keys = max(1, max_keys)
        self.per_owner = per_owner
        self.per_net = per_net
        self.rows: dict[str, _Entry] = {}
        self._owner_counts: dict[str, int] = {}
        self._net_counts: dict[str, int] = {}
        self._last_full_sweep = float("-inf")

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, key: str, now: float) -> _Entry | None:
        entry = self.rows.get(key)
        if entry is not None and entry.expires <= now:
            self.drop(key)
            return None
        return entry

    def drop(self, key: str) -> None:
        entry = self.rows.pop(key, None)
        if entry is not None:
            _decrement(self._owner_counts, entry.owner)
            _decrement(self._net_counts, entry.net)

    def sweep(self, now: float) -> None:
        for key in [k for k, e in self.rows.items() if e.expires <= now]:
            self.drop(key)

    def _sweep_when_full(self, now: float) -> None:
        if now - self._last_full_sweep >= 1.0:
            self._last_full_sweep = now
            self.sweep(now)

    def room_for(self, key: str, owner: str | None, now: float, net: str | None = None) -> str:
        """``"ok"``, ``"owner_full"``, ``"net_full"`` or ``"table_full"`` for ``key``.

        A row is present only while it is live: an expired, not-yet-swept row
        is dropped here and ``key`` is checked against the caps as a new row
        (the Redis store's ``room()`` does the same — follow-up review fix).
        """
        entry = self.rows.get(key)
        if entry is not None:
            if entry.expires > now:
                return "ok"
            self.drop(key)
        if (
            owner is not None
            and self.per_owner is not None
            and self._owner_counts.get(owner, 0) >= self.per_owner
        ):
            self._sweep_when_full(now)
            if self._owner_counts.get(owner, 0) >= self.per_owner:
                return "owner_full"
        if (
            net is not None
            and self.per_net is not None
            and self._net_counts.get(net, 0) >= self.per_net
        ):
            self._sweep_when_full(now)
            if self._net_counts.get(net, 0) >= self.per_net:
                return "net_full"
        if len(self.rows) >= self.max_keys:
            self._sweep_when_full(now)
            if len(self.rows) >= self.max_keys:
                return "table_full"
        return "ok"

    def put(
        self,
        key: str,
        values: list[float],
        expires: float,
        owner: str | None,
        net: str | None = None,
    ) -> None:
        entry = self.rows.get(key)
        if entry is None:
            self.rows[key] = _Entry(values, expires, owner, net)
            if owner is not None:
                self._owner_counts[owner] = self._owner_counts.get(owner, 0) + 1
            if net is not None:
                self._net_counts[net] = self._net_counts.get(net, 0) + 1
        else:
            entry.values = values
            entry.expires = expires


class InMemoryRateLimitStore:
    """Thread-safe, bounded, in-process store.

    Sliding window (Cloudflare-style approximation): each key keeps the count of
    the current and the previous window; the estimate is
    ``prev * (1 - elapsed / W) + curr``.  A fixed window (``Rate.fixed``) uses
    ``curr`` only, aligned to multiples of W since the epoch — for a day that is
    00:00 UTC, which is the daily-quota rollover.

    Memory bound WITHOUT evicting live counters (integration review fix — the
    first version dropped least-recently-used live counters, so an anonymous
    key-spraying flood could reset everyone's limits and daily quotas):

    * per-IP counters (``Check.owner`` set): at most ``max_keys`` rows, at most
      ``max_keys_per_ip`` per owner (an IPv4 address, or an IPv6 /64) and at
      most ``16 * max_keys_per_ip`` per network (``Check.net``: an IPv6 /48,
      else the address), so ONE /48 can never fill the table.
    * per-user / per-org quota counters (``Check.owner is None``): a separate
      table that anonymous traffic cannot reach (only a verified access token
      names a user).
    * Overflow (rate-limit hardening, finding 2 — the old fallback was ONE
      global bucket per rule, so one attacker could push every new client into
      a single exhausted budget): an owner over its cap shares ONE row per rule
      (``rule|~ip:<owner>``, its own); anything else that finds no room
      (table full, network over its cap, owner rows exhausted) is accounted in
      one of ``overflow_buckets`` hashed buckets per rule, chosen by a keyed
      hash of its NETWORK (quota counters: of the user / org key).  A flood
      only crowds the buckets its own networks hash into; a client elsewhere
      keeps a reduced (shared with its bucket) but non-zero budget, and the
      total budget a flood can mint is bounded by ``overflow_buckets`` x limit.
      ``overflow_buckets = clamp(max_keys // 200, 16, 4096)`` (250 for the
      default 50 000): memory stays O(max_keys).
    * failed-login backoff rows: their own table, capped per owner and per
      network; an owner over its cap (or a full table) accumulates failures in
      one shared row that locks THAT owner out, a network over its cap in one
      row that locks THAT network out.  Existing lockouts are never dropped.
    * per-email failure counters (sliding window): their own table; when it is
      full they fall back to the hashed buckets, so filling the table cannot
      switch the distributed-attack guard off.

    ``overflow_events`` counts every time a request had to use an overflow row
    or bucket, or could not get a backoff row (shown by ``GET /ops/rate-limits``).
    """

    _SWEEP_EVERY = 1024
    # Rows one network aggregate (IPv6 /48) may own, as a multiple of the
    # per-owner (IPv4 address / IPv6 /64) cap.
    NET_FACTOR = 16

    def __init__(self, *, max_keys: int = 50_000, max_keys_per_ip: int = 64) -> None:
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        per_ip = max(1, max_keys_per_ip)
        self.max_keys_per_net = per_ip * self.NET_FACTOR
        self.overflow_buckets = min(4096, max(16, self._max_keys // 200))
        # Keyed so nobody can aim an address at a chosen bucket.
        self._bucket_key = secrets.token_bytes(16)
        # values: [window_index, curr, prev]
        self._windows = _BoundedTable(self._max_keys, per_ip, self.max_keys_per_net)
        self._quota = _BoundedTable(self._max_keys)
        # One row per (rule, owner over its cap): at most max_keys / per_ip
        # owners can be at their cap, times the rules.
        self._overflow = _BoundedTable(self._max_keys)
        # Hashed buckets: at most overflow_buckets per rule (rules are a fixed
        # set in code) plus overflow_buckets for the per-email counters, so
        # they are always admitted.
        self._buckets = _BoundedTable(self.overflow_buckets * 64)
        # values: [failures, last_failure_at, locked_until]
        self._backoff = _BoundedTable(self._max_keys, per_ip, self.max_keys_per_net)
        self._backoff_overflow = _BoundedTable(self._max_keys)
        # values: [window_index, curr, prev]
        self._signals = _BoundedTable(self._max_keys)
        self._ops = 0
        self.overflow_events = 0
        self._metrics: dict[str, int] = {}

    # -- internals (lock held) ------------------------------------------------

    def _tables(self) -> tuple[_BoundedTable, ...]:
        return (
            self._windows,
            self._quota,
            self._overflow,
            self._buckets,
            self._backoff,
            self._backoff_overflow,
            self._signals,
        )

    def _tick(self, now: float) -> None:
        self._ops += 1
        if self._ops % self._SWEEP_EVERY == 0:
            for table in self._tables():
                table.sweep(now)

    def overflow_bucket(self, group: str) -> int:
        """The hashed overflow bucket ``group`` (a network / user key) falls into."""
        digest = hashlib.blake2b(group.encode(), digest_size=8, key=self._bucket_key).digest()
        return int.from_bytes(digest, "big") % self.overflow_buckets

    def _resolve(self, check: Check, now: float) -> tuple[_BoundedTable, str]:
        """The table and key that ``check`` counts against (never evicts)."""
        key = f"{check.rule}|{check.key}"
        table = self._windows if check.owner is not None else self._quota
        verdict = table.room_for(key, check.owner, now, check.net)
        if verdict == "ok":
            return table, key
        self.overflow_events += 1
        if verdict == "owner_full":
            owner_key = f"{check.rule}|~ip:{check.owner}"
            if self._overflow.room_for(owner_key, None, now) == "ok":
                return self._overflow, owner_key
        group = check.net or check.owner or check.key
        return self._buckets, f"{check.rule}|~b{self.overflow_bucket(group)}"

    # -- counters ---------------------------------------------------------------

    def consume(self, checks: Sequence[Check], now: float) -> Decision:
        with self._lock:
            self._tick(now)
            planned: list[tuple[_BoundedTable, str, int, float, float, float, Check, Decision]] = []
            for check in checks:
                window = check.rate.window_seconds
                table, key = self._resolve(check, now)
                idx = math.floor(now / window)
                curr = prev = 0.0
                entry = table.get(key, now)
                if entry is not None:
                    state = entry.values
                    if state[0] == idx:
                        curr, prev = state[1], state[2]
                    elif state[0] == idx - 1:
                        prev = state[1]
                decision = window_decision(check, now, curr, prev)
                expires = (idx + (1 if check.rate.fixed else 2)) * window
                planned.append((table, key, idx, curr, prev, expires, check, decision))
            verdict = combine_decisions([p[7] for p in planned])
            if not verdict.allowed:
                return verdict
            for table, key, idx, curr, prev, expires, check, _decision in planned:
                if table is self._windows:
                    table.put(key, [float(idx), curr + 1, prev], expires, check.owner, check.net)
                else:
                    table.put(key, [float(idx), curr + 1, prev], expires, None)
            return verdict

    # -- failed-login backoff ---------------------------------------------------

    def backoff_remaining(
        self, key: str, now: float, *, owner: str | None = None, net: str | None = None
    ) -> float:
        with self._lock:
            wait = 0.0
            rows = [self._backoff.get(key, now)]
            if owner is not None:
                rows.append(self._backoff_overflow.get(f"~ip:{owner}", now))
            if net is not None:
                rows.append(self._backoff_overflow.get(f"~net:{net}", now))
            for entry in rows:
                if entry is not None:
                    wait = max(wait, entry.values[2] - now)
            return wait

    def record_failure(
        self,
        key: str,
        now: float,
        *,
        threshold: int,
        base: float,
        cap: float,
        window: float,
        owner: str | None = None,
        net: str | None = None,
        min_lock: float = 0.0,
    ) -> None:
        with self._lock:
            self._tick(now)
            table, row_key, row_owner, row_net = self._backoff, key, owner, net
            verdict = table.room_for(key, owner, now, net)
            if verdict != "ok":
                self.overflow_events += 1
                if verdict == "net_full" and net is not None:
                    row_key = f"~net:{net}"
                elif owner is not None:
                    # Owner over its cap, or the whole table full: the failing
                    # address's own row (locks THAT address, nobody else).
                    row_key = f"~ip:{owner}"
                else:
                    return  # no owner row to share; existing lockouts stay
                table, row_owner, row_net = self._backoff_overflow, None, None
                if table.room_for(row_key, None, now) != "ok":
                    return
            entry = table.get(row_key, now)
            state = entry.values if entry is not None else None
            if state is not None and now - state[1] > window:
                state = None  # the failures are old: start counting again
            failures = (state[0] if state is not None else 0) + 1
            locked_until = state[2] if state is not None else 0.0
            if failures >= threshold:
                exponent = min(int(failures - threshold), 30)
                locked_until = now + min(cap, base * (2**exponent))
            if min_lock > 0:
                locked_until = max(locked_until, now + min_lock)
            expires = max(locked_until, now + window)
            table.put(row_key, [failures, now, locked_until], expires, row_owner, row_net)

    def clear_failures(self, key: str) -> None:
        with self._lock:
            self._backoff.drop(key)

    # -- per-email failure counters -----------------------------------------------

    def record_signal(
        self, key: str, now: float, *, window: float, thresholds: Sequence[int] = ()
    ) -> tuple[int, tuple[int, ...]]:
        with self._lock:
            self._tick(now)
            table, row_key = self._signals, key
            if table.room_for(key, None, now) != "ok":
                # Full: count in the key's hashed bucket (never switched off).
                self.overflow_events += 1
                table, row_key = self._buckets, f"~signal|~b{self.overflow_bucket(key)}"
            idx = math.floor(now / window)
            curr = prev = 0.0
            entry = table.get(row_key, now)
            if entry is not None:
                state = entry.values
                if state[0] == idx:
                    curr, prev = state[1], state[2]
                elif state[0] == idx - 1:
                    prev = state[1]
            before = prev * (1.0 - (now - idx * window) / window) + curr
            table.put(row_key, [float(idx), curr + 1, prev], (idx + 2) * window, None)
            return signal_result(before, thresholds)

    def size(self) -> int:
        with self._lock:
            return sum(len(table) for table in self._tables())

    # -- named counters (GET /ops/rate-limits) -----------------------------------

    def incr_metric(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[name] = self._metrics.get(name, 0) + amount

    def metrics(self) -> dict[str, int]:
        with self._lock:
            out = dict(self._metrics)
            out["overflow_events"] = self.overflow_events
            return out

    def status(self) -> dict[str, Any]:
        return {"backend": "memory", "shared": False}


# ---------------------------------------------------------------------------
# Configuration and policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool
    trusted_proxy_count: int
    max_keys: int
    rates: Mapping[str, Rate]
    backoff_threshold: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    backoff_window_seconds: float
    api_prefix: str = "/api/v1"
    max_keys_per_ip: int = 64
    email_failure_alert: int = 50

    @classmethod
    def from_settings(cls, settings: Any) -> RateLimitConfig:
        s = settings
        return cls(
            enabled=bool(s.RATE_LIMIT_ENABLED),
            trusted_proxy_count=int(s.TRUSTED_PROXY_COUNT),
            max_keys=int(s.RATE_LIMIT_MAX_KEYS),
            rates={
                "login_ip": parse_rate(s.RATE_LIMIT_LOGIN_PER_IP),
                "login_ip_email": parse_rate(s.RATE_LIMIT_LOGIN_PER_IP_EMAIL),
                "refresh_ip": parse_rate(s.RATE_LIMIT_REFRESH_PER_IP),
                "user_admin": parse_rate(s.RATE_LIMIT_USER_ADMIN_PER_USER),
                "user_admin_daily": parse_rate(s.RATE_LIMIT_USER_ADMIN_DAILY_PER_USER),
                "ai_user": parse_rate(s.RATE_LIMIT_AI_PER_USER),
                "ai_user_daily": parse_rate(s.RATE_LIMIT_AI_DAILY_PER_USER),
                "ai_org_daily": parse_rate(s.RATE_LIMIT_AI_DAILY_PER_ORG),
                "receipt_user": parse_rate(s.RATE_LIMIT_RECEIPT_PER_USER),
                "receipt_user_daily": parse_rate(s.RATE_LIMIT_RECEIPT_DAILY_PER_USER),
                "default": parse_rate(s.RATE_LIMIT_DEFAULT),
                # Account recovery (phase 12 follow-ups).
                "forgot_password_ip_email": parse_rate(s.RATE_LIMIT_FORGOT_PER_IP_EMAIL),
                "forgot_password_ip": parse_rate(s.RATE_LIMIT_FORGOT_PER_IP),
                "auth_token_ip": parse_rate(s.RATE_LIMIT_ACCOUNT_LINK_PER_IP),
                "invite_peek_ip": parse_rate(s.RATE_LIMIT_INVITE_PEEK_PER_IP),
                "invite_user": parse_rate(s.RATE_LIMIT_INVITE_PER_USER),
                "send_verification_user": parse_rate(s.RATE_LIMIT_SEND_VERIFICATION_PER_USER),
            },
            backoff_threshold=int(s.LOGIN_BACKOFF_THRESHOLD),
            backoff_base_seconds=float(s.LOGIN_BACKOFF_BASE_SECONDS),
            backoff_max_seconds=float(s.LOGIN_BACKOFF_MAX_SECONDS),
            backoff_window_seconds=float(s.LOGIN_BACKOFF_WINDOW_SECONDS),
            api_prefix=str(s.API_V1_PREFIX),
            max_keys_per_ip=int(s.RATE_LIMIT_MAX_KEYS_PER_IP),
            email_failure_alert=int(s.LOGIN_EMAIL_FAILURE_ALERT),
        )


# (policy, method, path pattern relative to the API prefix).  First match wins;
# anything else under the prefix is the "default" safety net.
_ID = r"[^/]+"
POLICY_ROUTES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (policy, method, re.compile(f"^{pattern}$"))
    for policy, method, pattern in (
        ("auth_login", "POST", r"/auth/login"),
        ("auth_refresh", "POST", r"/auth/refresh"),
        # Account recovery (phase 12 follow-ups): anonymous token endpoints are
        # limited per IP before the body is even read.
        ("auth_forgot_password", "POST", r"/auth/forgot-password"),
        (
            "auth_token",
            "POST",
            r"/auth/(?:reset-password|accept-invite|verify-email|change-password)",
        ),
        ("auth_invite_peek", "GET", rf"/auth/invite/{_ID}"),
        ("auth_send_verification", "POST", r"/auth/send-verification"),
        ("user_invite", "POST", r"/users/invite"),
        ("user_invite", "POST", rf"/users/{_ID}/invite/(?:resend|revoke)"),
        ("user_admin", "POST", r"/users"),
        ("user_admin", "PATCH", rf"/users/{_ID}"),
        ("user_admin", "POST", rf"/users/{_ID}/(?:deactivate|reactivate)"),
        ("ai", "POST", rf"/conversations/{_ID}/messages"),
        ("ai", "POST", r"/policy/query"),
        ("ai", "POST", r"/policy/evaluate"),
        ("ai", "POST", rf"/policies/{_ID}/rules"),
        ("receipt_upload", "POST", r"/receipts"),
    )
)

POLICIES: tuple[str, ...] = (
    "auth_login",
    "auth_login_email",
    "auth_login_backoff",
    "auth_refresh",
    "auth_forgot_password",
    "auth_forgot_password_email",
    "auth_token",
    "auth_invite_peek",
    "auth_send_verification",
    "user_invite",
    "user_admin",
    "ai",
    "receipt_upload",
    "default",
)


def classify(method: str, path: str) -> str:
    """The policy for ``method path`` (path relative to the API prefix)."""
    normalised = path.rstrip("/") or "/"
    for policy, verb, pattern in POLICY_ROUTES:
        if verb == method and pattern.match(normalised):
            return policy
    return "default"


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------


def client_ip(scope: Scope, trusted_proxy_count: int) -> str:
    """The caller's IP, trusting only the configured proxy chain.

    Each trusted proxy APPENDS the address it received the request from to
    X-Forwarded-For, so with N trusted proxies the client is the Nth entry from
    the RIGHT.  Entries further left were written by the client and are never
    read; a missing / short / malformed header falls back to the socket peer.
    """
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    if trusted_proxy_count <= 0:
        return peer
    raw = ",".join(
        value.decode("latin-1")
        for name, value in scope.get("headers", [])
        if name.lower() == b"x-forwarded-for"
    )
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    if len(entries) < trusted_proxy_count:
        return peer
    try:
        return str(ipaddress.ip_address(entries[-trusted_proxy_count]))
    except ValueError:
        return peer


def ip_owner(ip: str) -> str:
    """The address group a client's per-IP counters are capped by.

    IPv4: the address itself.  IPv6: its /64 prefix — one subscriber usually
    holds a whole /64, so rotating addresses inside it must not mint new keys.
    IPv4-mapped IPv6 is treated as the IPv4 address; a non-IP peer (tests,
    unix sockets) is its own group.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def ip_net(ip: str) -> str:
    """The network aggregate a client's rows are capped and bucketed by.

    IPv6: its /48 (a typical site allocation; one holder of a /48 controls
    65 536 /64s, so the /64 owner cap alone would let it fill the table).
    IPv4 and IPv4-mapped IPv6: the address itself (aggregating IPv4 would
    make neighbours on a shared /24 pay for each other).  A non-IP peer is
    its own network.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/48", strict=False))
    return str(addr)


def bearer_principal(scope: Scope) -> tuple[str, str] | None:
    """``(user_id, org_id)`` from a VALID access token, else ``None``.

    Only a signature-verified, unexpired access token names a user; anything
    else is treated as anonymous (limited per IP).  The route still runs its
    own authentication — this only picks the counter.
    """
    from backend.app.core.security import decode_token  # noqa: PLC0415

    header = ""
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            header = value.decode("latin-1")
            break
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        payload = decode_token(token.strip(), expected_type="access")
    except Exception:  # invalid / expired / wrong type -> anonymous
        return None
    return str(payload["sub"]), str(payload["org"])


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


class RateLimitedError(AppError):
    """429 in the uniform envelope, with ``retry_after_seconds`` and the headers."""

    status_code = 429
    code = ErrorCode.RATE_LIMITED

    def __init__(self, decision: Decision) -> None:
        seconds = max(1, decision.retry_after_seconds)
        unit = "second" if seconds == 1 else "seconds"
        super().__init__(f"Too many requests. Please wait {seconds} {unit} and try again.")
        self.retry_after_seconds = seconds
        self.headers = decision.headers()
        self.extra = {"retry_after_seconds": seconds}


class RateLimitUnavailableError(AppError):
    """503 in the uniform envelope: the shared store is down and the operator
    chose ``RATE_LIMIT_REDIS_FAILURE=closed`` (refuse rather than admit unmetered)."""

    status_code = 503
    code = ErrorCode.RATE_LIMIT_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("Service temporarily unavailable. Please try again shortly.")
        self.headers = {"Retry-After": "5"}
        self.extra = {"retry_after_seconds": 5}


class RateLimiter:
    def __init__(
        self,
        config: RateLimitConfig,
        *,
        store: RateLimitStore | None = None,
        clock: Clock = time.time,
        secret: bytes | None = None,
    ) -> None:
        self.config = config
        self.store: RateLimitStore = store or InMemoryRateLimitStore(
            max_keys=config.max_keys, max_keys_per_ip=config.max_keys_per_ip
        )
        self.clock = clock
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        # The counters behind GET /ops/rate-limits live in the STORE (shared by
        # every instance when it is Redis): "throttled:<policy>"; the per-email
        # failed-login SOFT signal "email_soft_signals" (emails whose failures,
        # from any address, reached the backoff threshold — a metric, never a
        # block); the distributed guard's "emails_alerted" (emails past
        # LOGIN_EMAIL_FAILURE_ALERT within an hour) and "escalated_failures"
        # (failing attempts that earned a stepped-up lock because of it).
        self._started_at = clock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def identity(self, kind: str, raw: str) -> str:
        """A keyed hash of an identity (``ip``, ``user``, ``org``, ``email``)."""
        digest = hmac.new(self._secret, f"{kind}:{raw}".encode(), hashlib.sha256).hexdigest()
        return f"{kind}:{digest[:24]}"

    def checks_for(
        self,
        policy: str,
        *,
        ip_key: str,
        user_key: str | None,
        org_key: str | None,
        owner: str | None = None,
        net: str | None = None,
    ) -> list[Check]:
        """The counters ``policy`` consumes.

        ``owner`` is the hashed address group (:func:`ip_owner`) the per-IP
        counters are capped by (defaults to ``ip_key``), ``net`` the hashed
        network aggregate (:func:`ip_net`).  Counters keyed by a verified
        user / org carry neither: they live in the quota table.
        """
        r = self.config.rates
        ip_owner_key = owner or ip_key
        principal = user_key or ip_key
        p_owner = None if user_key else ip_owner_key
        p_net = None if user_key else net
        if policy == "auth_login":
            return [Check("auth_login:ip", ip_key, r["login_ip"], ip_owner_key, net)]
        if policy == "auth_refresh":
            return [Check("auth_refresh:ip", ip_key, r["refresh_ip"], ip_owner_key, net)]
        if policy == "auth_forgot_password":
            return [
                Check("auth_forgot_password:ip", ip_key, r["forgot_password_ip"], ip_owner_key, net)
            ]
        if policy == "auth_token":
            return [Check("auth_token:ip", ip_key, r["auth_token_ip"], ip_owner_key, net)]
        if policy == "auth_invite_peek":
            return [Check("auth_invite_peek:ip", ip_key, r["invite_peek_ip"], ip_owner_key, net)]
        if policy == "auth_send_verification":
            return [
                Check(
                    "auth_send_verification:user",
                    principal,
                    r["send_verification_user"],
                    p_owner,
                    p_net,
                )
            ]
        if policy == "user_invite":
            return [
                Check("user_invite:user", principal, r["invite_user"], p_owner, p_net),
                Check("user_admin:user", principal, r["user_admin"], p_owner, p_net),
                Check("user_admin:user_daily", principal, r["user_admin_daily"], p_owner, p_net),
            ]
        if policy == "user_admin":
            return [
                Check("user_admin:user", principal, r["user_admin"], p_owner, p_net),
                Check("user_admin:user_daily", principal, r["user_admin_daily"], p_owner, p_net),
            ]
        if policy == "ai":
            checks = [
                Check("ai:user", principal, r["ai_user"], p_owner, p_net),
                Check("ai:user_daily", principal, r["ai_user_daily"], p_owner, p_net),
            ]
            if org_key is not None:
                checks.append(Check("ai:org_daily", org_key, r["ai_org_daily"]))
            return checks
        if policy == "receipt_upload":
            return [
                Check("receipt_upload:user", principal, r["receipt_user"], p_owner, p_net),
                Check(
                    "receipt_upload:user_daily", principal, r["receipt_user_daily"], p_owner, p_net
                ),
            ]
        return [Check("default:principal", principal, r["default"], p_owner, p_net)]

    def _note_throttle(self, policy: str, identity: str, decision: Decision) -> None:
        self.store.incr_metric(f"throttled:{policy}")
        logger.warning(
            "rate limit exceeded | policy=%s rule=%s identity=%s retry_after=%ss",
            policy,
            decision.rule,
            identity,
            decision.retry_after_seconds,
        )

    def check(self, policy: str, checks: Sequence[Check]) -> Decision:
        decision = self.store.consume(checks, self.clock())
        if not decision.allowed:
            identity = next((c.key for c in checks if c.rule == decision.rule), "-")
            self._note_throttle(policy, identity, decision)
        return decision

    def enforce(self, policy: str, checks: Sequence[Check]) -> Decision:
        decision = self.check(policy, checks)
        if not decision.allowed:
            raise RateLimitedError(decision)
        return decision

    # -- failed-login backoff -------------------------------------------------

    #
    # The HARD block is keyed by (client IP, email hash, "org code given"):
    # only the address that keeps failing is cooled down.  Keying it by the
    # email alone let anyone who knew an address lock its owner out from
    # anywhere; keying it by the raw org code let an attacker mint a fresh key
    # per attempt by varying the code.

    # Window of the per-email distributed-attack counter (seconds).
    EMAIL_FAILURE_WINDOW_SECONDS = 3600.0

    def enforce_login_backoff(
        self, login_key: str, *, owner: str | None = None, net: str | None = None
    ) -> None:
        wait = self.store.backoff_remaining(login_key, self.clock(), owner=owner, net=net)
        if wait > 0:
            decision = Decision(
                False, "auth_login_backoff:ip_email", 0, 0, _ceil_seconds(wait), _ceil_seconds(wait)
            )
            self._note_throttle("auth_login_backoff", login_key, decision)
            raise RateLimitedError(decision)

    def record_login_failure(
        self,
        login_key: str,
        *,
        owner: str | None = None,
        net: str | None = None,
        min_lock: float = 0.0,
        lock_also: Sequence[str] = (),
    ) -> None:
        """Count a failure for ``login_key``; a step-up ``min_lock`` also locks ``lock_also``."""
        c = self.config
        now = self.clock()
        kw: dict[str, Any] = {
            "threshold": c.backoff_threshold,
            "base": c.backoff_base_seconds,
            "cap": c.backoff_max_seconds,
            "window": c.backoff_window_seconds,
            "owner": owner,
            "net": net,
        }
        self.store.record_failure(login_key, now, min_lock=min_lock, **kw)
        if min_lock > 0:
            self.store.incr_metric("escalated_failures")
            for key in lock_also:
                self.store.record_failure(key, now, min_lock=min_lock, **kw)

    def clear_login_failures(self, login_key: str) -> None:
        self.store.clear_failures(login_key)

    def note_email_failure(self, email_key: str) -> int:
        """Count one failed login for the email, from ANY address (sliding hour).

        Crossing the backoff threshold is the soft signal (metric + log);
        crossing ``LOGIN_EMAIL_FAILURE_ALERT`` is a distributed attack (metric
        + one WARNING).  Returns the count, for :meth:`distributed_lock_seconds`.
        A success does NOT reset it: the attack goes on whether or not the
        owner signs in meanwhile.
        """
        c = self.config
        count, crossed = self.store.record_signal(
            email_key,
            self.clock(),
            window=self.EMAIL_FAILURE_WINDOW_SECONDS,
            thresholds=(c.backoff_threshold, c.email_failure_alert),
        )
        if c.backoff_threshold in crossed:
            self.store.incr_metric("email_soft_signals")
            logger.warning(
                "login failures for one email reached %d from any address | identity=%s "
                "(soft signal only; the hard block is per address)",
                c.backoff_threshold,
                email_key,
            )
        if c.email_failure_alert in crossed:
            self.store.incr_metric("emails_alerted")
            logger.warning(
                "distributed login attack suspected: failures for one email reached %d within "
                "an hour from any address | identity=%s (each further FAILING address is "
                "locked out progressively longer; a correct password from a clean address "
                "is never blocked)",
                c.email_failure_alert,
                email_key,
            )
        return count

    def distributed_lock_seconds(self, count: int) -> float:
        """The step-up lock a FAILING address earns when its email's count is ``count``.

        0 below ``LOGIN_EMAIL_FAILURE_ALERT``; from it on
        ``BASE * 2^(count - alert)`` seconds, capped at ``LOGIN_BACKOFF_MAX_SECONDS``.
        """
        c = self.config
        if count < c.email_failure_alert:
            return 0.0
        exponent = min(count - c.email_failure_alert, 30)
        return float(min(c.backoff_max_seconds, c.backoff_base_seconds * (2**exponent)))

    # -- read-only snapshot (GET /ops/rate-limits) -----------------------------

    def snapshot(self) -> dict[str, Any]:
        metrics = self.store.metrics()
        throttled = {p: int(metrics.get(f"throttled:{p}", 0)) for p in POLICIES}
        email_signals = int(metrics.get("email_soft_signals", 0))
        emails_alerted = int(metrics.get("emails_alerted", 0))
        escalated = int(metrics.get("escalated_failures", 0))
        status = self.store.status()
        c = self.config
        return {
            "enabled": c.enabled,
            "storage": (
                "redis (shared by every instance)"
                if status.get("shared")
                else "in-process (single instance)"
            ),
            "store": status,
            "trusted_proxy_count": c.trusted_proxy_count,
            "tracked_keys": self.store.size(),
            "max_keys": c.max_keys,
            "since": int(self._started_at),
            "limits": {
                name: {
                    "limit": rate.limit,
                    "window_seconds": int(rate.window_seconds),
                    "fixed_utc_day": rate.fixed,
                }
                for name, rate in sorted(c.rates.items())
            },
            "login_backoff": {
                "threshold": c.backoff_threshold,
                "base_seconds": int(c.backoff_base_seconds),
                "max_seconds": int(c.backoff_max_seconds),
                "window_seconds": int(c.backoff_window_seconds),
            },
            "throttled": throttled,
            "login_email_soft_signals": email_signals,
            "login_email_failure_alert": {
                "threshold": c.email_failure_alert,
                "window_seconds": int(self.EMAIL_FAILURE_WINDOW_SECONDS),
                "emails_alerted": emails_alerted,
                "escalated_failures": escalated,
            },
            "max_keys_per_ip": c.max_keys_per_ip,
            "max_keys_per_network": int(getattr(self.store, "max_keys_per_net", 0)),
            "overflow_buckets": int(getattr(self.store, "overflow_buckets", 0)),
            "overflow_events": int(metrics.get("overflow_events", 0)),
        }


def limiter_from_app(app: Any) -> RateLimiter | None:
    """The app's limiter when one is installed AND enabled."""
    state = getattr(app, "state", None)
    limiter = getattr(state, "rate_limiter", None) if state is not None else None
    if isinstance(limiter, RateLimiter) and limiter.enabled:
        return limiter
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


# Anonymous credential / one-time-token endpoints: limited per client address
# only — a bearer token sent along never buys a per-user budget there.
_PER_IP_ONLY_POLICIES = frozenset(
    {"auth_login", "auth_refresh", "auth_forgot_password", "auth_token", "auth_invite_peek"}
)


class RateLimitMiddleware:
    """Pure ASGI middleware: one policy per API request; non-API paths exempt."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limiter = limiter_from_app(scope.get("app"))
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        if limiter is None or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        prefix = limiter.config.api_prefix.rstrip("/")
        if not (path == prefix or path.startswith(prefix + "/")):
            await self.app(scope, receive, send)  # /health, /docs, ... exempt
            return
        policy = classify(method, path[len(prefix) :])
        ip = client_ip(scope, limiter.config.trusted_proxy_count)
        ip_key = limiter.identity("ip", ip)
        owner = limiter.identity("ipnet", ip_owner(ip))
        net = limiter.identity("ipnet48", ip_net(ip))
        user_key = org_key = None
        if policy not in _PER_IP_ONLY_POLICIES:
            principal = bearer_principal(scope)
            if principal is not None:
                user_key = limiter.identity("user", principal[0])
                org_key = limiter.identity("org", principal[1])
        checks = limiter.checks_for(
            policy, ip_key=ip_key, user_key=user_key, org_key=org_key, owner=owner, net=net
        )
        try:
            decision = limiter.check(policy, checks)
        except AppError as exc:  # shared store down with RATE_LIMIT_REDIS_FAILURE=closed
            response = await app_error_handler(Request(scope), exc)
            await response(scope, receive, send)
            return
        if not decision.allowed:
            response = await app_error_handler(Request(scope), RateLimitedError(decision))
            await response(scope, receive, send)
            return
        # "none": nothing was metered (shared store down, RATE_LIMIT_REDIS_FAILURE=open)
        # — no X-RateLimit-* headers rather than a misleading "limit 0".
        extra = decision.headers() if decision.rule != "none" else {}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------
# Login guard (needs the email, so it runs inside the route)
# ---------------------------------------------------------------------------


def guard_login(
    request: Request,
    email: str,
    attempt: Callable[[], T],
    *,
    organization: str | None = None,
) -> T:
    """Per-(IP, email) window + failed-login backoff around ``attempt``.

    Order: cooldown check, then the (IP, email) window — both BEFORE ``attempt``
    touches the database or bcrypt, keyed only by hashes of what the CLIENT
    sent, so the answer never depends on whether the account exists.

    * The cooldown (hard block) is keyed by (client address — an IPv6 client
      by its /64 —, email, "an org code was given").  The code VALUE is not
      part of the key (rate-limit hardening): every non-blank code shares one
      row, so varying the code cannot mint
      fresh keys, and nothing is resolved — a key that depended on whether the
      code names a real organisation would be an existence oracle (fail with
      code C, then see whether garbage code G is locked).  A correct password
      from another address is never blocked by someone else's failures.  An
      address that sprays many emails past its per-IP key cap (or a /48 past
      its network cap) shares one backoff row that locks that address out.
    * Every generic 401 counts against that row.  Plain credential failures
      also feed the per-email counter (any address, sliding hour): at the
      backoff threshold a soft signal, at ``LOGIN_EMAIL_FAILURE_ALERT`` a
      distributed attack — from then on the failing address is locked for
      ``BASE * 2^(count - alert)`` seconds (capped).  Only the address that
      FAILED is locked; an :class:`AmbiguousCredentialsError` (no-org login
      over the candidate cap, or a password matching several accounts) is no
      evidence against the email and feeds neither.
    * A success clears that address's row (not the per-email counter).
    """
    limiter = limiter_from_app(request.app)
    if limiter is None:
        return attempt()
    email_norm = email.strip().lower()
    has_code = bool((organization or "").strip())
    ip = client_ip(request.scope, limiter.config.trusted_proxy_count)
    ip_key = limiter.identity("ip", ip)
    group = ip_owner(ip)  # the address (IPv6: its /64 — rotating inside it is one client)
    owner = limiter.identity("ipnet", group)
    net = limiter.identity("ipnet48", ip_net(ip))
    email_key = limiter.identity("email", email_norm)

    def _login_key(coded: bool) -> str:
        return limiter.identity("login", f"{group}\x00{email_norm}\x00{'~code' if coded else ''}")

    login_key = _login_key(has_code)
    limiter.enforce_login_backoff(login_key, owner=owner, net=net)
    limiter.enforce(
        "auth_login_email",
        [
            Check(
                "auth_login:ip_email",
                f"{ip_key}/{email_key}",
                limiter.config.rates["login_ip_email"],
                owner,
                net,
            )
        ],
    )
    try:
        result = attempt()
    except AmbiguousCredentialsError:
        limiter.record_login_failure(login_key, owner=owner, net=net)
        raise
    except UnauthorizedError:
        count = limiter.note_email_failure(email_key)
        # A step-up lock covers BOTH rows of this address + email, so switching
        # between "no code" and "a code" buys no extra failing attempt.
        limiter.record_login_failure(
            login_key,
            owner=owner,
            net=net,
            min_lock=limiter.distributed_lock_seconds(count),
            lock_also=(_login_key(not has_code),),
        )
        raise
    limiter.clear_login_failures(login_key)
    return result


# ---------------------------------------------------------------------------
# Forgot-password guard (needs the email, so it runs inside the route)
# ---------------------------------------------------------------------------


def guard_forgot_password(request: Request, email: str) -> None:
    """Per-(IP, email) window for ``POST /auth/forgot-password`` (phase 12 follow-ups).

    Runs BEFORE any database read, keyed only by hashes of what the CLIENT sent
    (the address and the normalised email), so a throttled request is the same
    429 whether or not the account exists.  The per-IP window runs in the
    middleware.
    """
    limiter = limiter_from_app(request.app)
    if limiter is None:
        return
    ip = client_ip(request.scope, limiter.config.trusted_proxy_count)
    ip_key = limiter.identity("ip", ip)
    owner = limiter.identity("ipnet", ip_owner(ip))
    net = limiter.identity("ipnet48", ip_net(ip))
    email_key = limiter.identity("email", email.strip().lower())
    limiter.enforce(
        "auth_forgot_password_email",
        [
            Check(
                "auth_forgot_password:ip_email",
                f"{ip_key}/{email_key}",
                limiter.config.rates["forgot_password_ip_email"],
                owner,
                net,
            )
        ],
    )
