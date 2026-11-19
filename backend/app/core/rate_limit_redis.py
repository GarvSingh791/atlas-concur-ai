"""Redis-backed rate-limit store (follow-up to phase 11; opt-in).

Why: :class:`~backend.app.core.rate_limit.InMemoryRateLimitStore` keeps every
counter in one process, so counters reset on each restart / deploy and a
second worker or instance would multiply every budget.  With
``RATE_LIMIT_STORE=redis`` every instance shares ONE set of counters.

Same protocol, same guarantees
------------------------------
* One Lua script (``rate_limit_redis.lua``) does every read-decide-write, run
  with EVALSHA (loaded once, reloaded and retried on ``NOSCRIPT``), so it is
  atomic across instances: a request one counter denies burns none of the
  others, exactly like the in-memory store.
* Time comes from Redis ``TIME`` inside the script (``server_time=True``, the
  production setting), so instances with drifting clocks agree on windows and
  on the UTC-day quota rollover.  Tests pass ``server_time=False`` to drive the
  same script with an injected clock.
* Keys are built from the identities the limiter already HMAC-hashed (with a
  key derived from ``JWT_SECRET`` — :func:`derive_secret` — so every instance
  computes the same keys); no raw IP, email or token is ever written.
* Memory is bounded WITHOUT evicting live counters (see the phase-12 log for
  the exact arithmetic): every key carries a TTL equal to its logical expiry
  plus a small slack; each table (per-IP counters, quota counters, owner
  overflow rows, backoff rows, backoff overflow rows, per-email signals) is a
  counted index capped at ``RATE_LIMIT_MAX_KEYS``; every client address (IPv6:
  its /64) may own ``RATE_LIMIT_MAX_KEYS_PER_IP`` rows and every network (IPv6
  /48) 16x that.  An index member counts as an existing row only while its
  score (logical expiry) is greater than now: an expired member is removed
  from every index and checked against the caps as a new row (review fix:
  re-using an expired, still-indexed key used to skip the owner and network
  caps).  An identity that finds no room goes to the same overflow
  row / hashed per-network bucket as in memory (fail closed).  The
  in-memory "overflow bucket" design therefore maps 1:1 onto the per-owner
  counted cap.

Failure behaviour (``RATE_LIMIT_REDIS_FAILURE``)
-----------------------------------------------
A timeout / connection error (or any Redis error) never reaches the request
path.  The call is served according to the mode, counted, and a WARNING is
logged at most once a minute (exception class only — never the URL):

* ``memory`` (default): that call uses a process-local in-memory store.
* ``closed``: the GATES (``consume``, ``backoff_remaining``) raise
  :class:`~backend.app.core.rate_limit.RateLimitUnavailableError` (503
  ``RATE_LIMIT_UNAVAILABLE``).
* ``open``: the gates admit.

Book-keeping after a login attempt (failure counts, success reset, soft
signals, metrics) always falls back to the process-local store — it must not
turn a 401 into a 503.  After an error Redis is skipped for
``retry_seconds`` (a small circuit breaker, so a dead Redis does not add a
timeout to every request) and then tried again: recovery is automatic.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

from backend.app.core.rate_limit import (
    Check,
    Decision,
    InMemoryRateLimitStore,
    RateLimitConfig,
    RateLimiter,
    RateLimitUnavailableError,
    combine_decisions,
    signal_result,
    window_decision,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
FailureMode = Literal["memory", "closed", "open"]
FAILURE_MODES: tuple[str, ...] = ("memory", "closed", "open")

LUA_SOURCE = (Path(__file__).with_name("rate_limit_redis.lua")).read_text(encoding="utf-8")

# Table codes (row hashes "r:<t>:<member>", counted indexes "t:<t>").
_WINDOWS, _QUOTA, _OWNER_OVF, _BUCKETS = "w", "q", "o", "k"
_BACKOFF, _BACKOFF_OVF, _SIGNALS = "b", "x", "s"
_TABLES = (_WINDOWS, _QUOTA, _OWNER_OVF, _BUCKETS, _BACKOFF, _BACKOFF_OVF, _SIGNALS)


def derive_secret(app_secret: str, purpose: str = "rate-limit-identity") -> bytes:
    """A stable key for hashing identities, shared by every instance.

    Derived (HMAC-SHA256) from the app secret, never the secret itself, so a
    key name can never reveal it.  Rotating ``JWT_SECRET`` changes every key:
    counters start afresh (documented in the runbook).
    """
    return hmac.new(app_secret.encode(), f"atlas:{purpose}:v1".encode(), hashlib.sha256).digest()


class RedisRateLimitStore:
    """:class:`~backend.app.core.rate_limit.RateLimitStore` on a shared Redis."""

    NET_FACTOR = InMemoryRateLimitStore.NET_FACTOR
    # Real TTL beyond the logical expiry (so Redis never drops a row early).
    TTL_SLACK_MS = 2000
    WARN_EVERY_SECONDS = 60.0
    METRICS_TTL_SECONDS = 30 * 86400

    def __init__(
        self,
        client: Any,
        *,
        secret: bytes,
        max_keys: int = 50_000,
        max_keys_per_ip: int = 64,
        prefix: str = "atlas:rl:",
        failure_mode: str = "memory",
        server_time: bool = True,
        retry_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_mode not in FAILURE_MODES:
            raise ValueError(f"unknown rate-limit failure mode {failure_mode!r}")
        self._client = client
        self._max_keys = max(1, max_keys)
        self._per_ip = max(1, max_keys_per_ip)
        self.max_keys_per_net = self._per_ip * self.NET_FACTOR
        self.overflow_buckets = min(4096, max(16, self._max_keys // 200))
        # Keyed (and shared by every instance) so nobody can aim at a bucket.
        self._bucket_key = hmac.new(secret, b"overflow-buckets", hashlib.sha256).digest()[:16]
        self._prefix = prefix
        self.failure_mode = failure_mode
        self.server_time = server_time
        self.retry_seconds = retry_seconds
        self._monotonic = monotonic
        self._fallback = InMemoryRateLimitStore(
            max_keys=self._max_keys, max_keys_per_ip=self._per_ip
        )
        self._sha: str | None = None
        self._lock = threading.Lock()
        self._down_until = float("-inf")
        self._healthy = client is not None
        self._last_warn = float("-inf")
        self.redis_errors = 0
        self.fallback_events = 0
        self.script_loads = 0
        self._last_error: str | None = None
        self._last_error_at: int | None = None

    # -- keys -----------------------------------------------------------------

    def _k(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    def _row(self, table: str, member: str) -> str:
        return self._k("r", table, member)

    def _index(self, table: str) -> str:
        return self._k("t", table)

    def overflow_bucket(self, group: str) -> int:
        """The hashed overflow bucket ``group`` (a network / user key) falls into."""
        digest = hashlib.blake2b(group.encode(), digest_size=8, key=self._bucket_key).digest()
        return int.from_bytes(digest, "big") % self.overflow_buckets

    def _now_arg(self, now: float) -> str:
        return "" if self.server_time else repr(float(now))

    # -- the script -----------------------------------------------------------

    def _eval(self, keys: Sequence[str], args: Sequence[str]) -> Any:
        if self._client is None:
            raise ConnectionError("REDIS_URL is not configured")
        sha = self._sha
        if sha is None:
            sha = self._load_script()
        try:
            return self._client.evalsha(sha, len(keys), *keys, *args)
        except Exception as exc:
            if not _is_noscript(exc):
                raise
            # Redis restarted / SCRIPT FLUSH: load it again and retry once.
            sha = self._load_script()
            return self._client.evalsha(sha, len(keys), *keys, *args)

    def _load_script(self) -> str:
        sha = str(_text(self._client.script_load(LUA_SOURCE)))
        with self._lock:
            self._sha = sha
            self.script_loads += 1
        return sha

    # -- failure handling -----------------------------------------------------

    def _run(
        self,
        redis_call: Callable[[], T],
        fallback_call: Callable[[], T],
        *,
        gate: bool,
        open_value: Callable[[], T] | None = None,
    ) -> T:
        """Redis first; on any error (or while the circuit is open) degrade per mode."""
        if self._monotonic() >= self._down_until:
            try:
                result = redis_call()
            except Exception as exc:
                self._note_error(exc)
            else:
                self._note_ok()
                return result
        with self._lock:
            self.fallback_events += 1
        if gate and self.failure_mode == "closed":
            raise RateLimitUnavailableError()
        if gate and self.failure_mode == "open" and open_value is not None:
            return open_value()
        return fallback_call()

    def _note_error(self, exc: Exception) -> None:
        now = self._monotonic()
        with self._lock:
            self.redis_errors += 1
            self._healthy = False
            self._down_until = now + self.retry_seconds
            self._last_error = type(exc).__name__
            self._last_error_at = int(time.time())
            warn = now - self._last_warn >= self.WARN_EVERY_SECONDS
            if warn:
                self._last_warn = now
            errors, fallbacks = self.redis_errors, self.fallback_events
        if warn:
            # Class name only: a connection error's text can name the host.
            logger.warning(
                "rate-limit redis unavailable (%s); serving rate limits in failure mode %r "
                "until it answers | redis_errors=%d fallback_events=%d",
                type(exc).__name__,
                self.failure_mode,
                errors,
                fallbacks,
            )

    def _note_ok(self) -> None:
        if self._healthy:
            return
        with self._lock:
            was_down = not self._healthy and self.redis_errors > 0
            self._healthy = True
        if was_down:
            logger.warning("rate-limit redis is answering again; shared counters resumed")

    # -- counters ---------------------------------------------------------------

    def consume(self, checks: Sequence[Check], now: float) -> Decision:
        if not checks:
            return Decision(True, "none", 0, 0, 0)
        return self._run(
            lambda: self._consume_redis(checks, now),
            lambda: self._fallback.consume(checks, now),
            gate=True,
            open_value=lambda: Decision(True, "none", 0, 0, 0),
        )

    def _consume_redis(self, checks: Sequence[Check], now: float) -> Decision:
        keys = [self._k("m")]
        args = ["consume", self._now_arg(now), str(self.TTL_SLACK_MS), str(len(checks))]
        for check in checks:
            table = _WINDOWS if check.owner is not None else _QUOTA
            member = f"{check.rule}|{check.key}"
            owner_member = f"{check.rule}|~ip:{check.owner}" if check.owner is not None else ""
            group = check.net or check.owner or check.key
            bucket_member = f"{check.rule}|~b{self.overflow_bucket(group)}"
            cap_owner = self._per_ip if check.owner is not None else 0
            cap_net = self.max_keys_per_net if check.owner is not None and check.net else 0
            keys += [
                self._row(table, member),
                self._index(table),
                self._k("ow", check.owner or "-"),
                self._k("nw", check.net or "-"),
                self._row(_OWNER_OVF, owner_member or "-"),
                self._index(_OWNER_OVF),
                self._row(_BUCKETS, bucket_member),
                self._index(_BUCKETS),
            ]
            args += [
                repr(float(check.rate.window_seconds)),
                str(check.rate.limit),
                "1" if check.rate.fixed else "0",
                str(cap_owner),
                str(cap_net),
                str(self._max_keys),
                member,
                owner_member,
                bucket_member,
            ]
        reply = [_text(v) for v in self._eval(keys, args)]
        at = float(reply[1])
        decisions = [
            window_decision(check, at, float(reply[3 + 3 * i]), float(reply[4 + 3 * i]))
            for i, check in enumerate(checks)
        ]
        return combine_decisions(decisions)

    # -- failed-login backoff -----------------------------------------------------

    def backoff_remaining(
        self, key: str, now: float, *, owner: str | None = None, net: str | None = None
    ) -> float:
        def redis_call() -> float:
            keys = [self._k("m"), self._row(_BACKOFF, key)]
            if owner is not None:
                keys.append(self._row(_BACKOFF_OVF, f"~ip:{owner}"))
            if net is not None:
                keys.append(self._row(_BACKOFF_OVF, f"~net:{net}"))
            reply = self._eval(keys, ["backoff", self._now_arg(now), str(self.TTL_SLACK_MS)])
            return float(_text(reply))

        return self._run(
            redis_call,
            lambda: self._fallback.backoff_remaining(key, now, owner=owner, net=net),
            gate=True,
            open_value=lambda: 0.0,
        )

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
        def redis_call() -> None:
            keys = [
                self._k("m"),
                self._row(_BACKOFF, key),
                self._index(_BACKOFF),
                self._k("ob", owner or "-"),
                self._k("nb", net or "-"),
                self._row(_BACKOFF_OVF, f"~ip:{owner}" if owner is not None else "-"),
                self._row(_BACKOFF_OVF, f"~net:{net}" if net is not None else "-"),
                self._index(_BACKOFF_OVF),
            ]
            args = [
                "failure",
                self._now_arg(now),
                str(self.TTL_SLACK_MS),
                str(threshold),
                repr(float(base)),
                repr(float(cap)),
                repr(float(window)),
                repr(float(min_lock)),
                str(self._per_ip if owner is not None else 0),
                str(self.max_keys_per_net if net is not None else 0),
                str(self._max_keys),
                key,
                f"~ip:{owner}" if owner is not None else "",
                f"~net:{net}" if net is not None else "",
            ]
            self._eval(keys, args)

        self._run(
            redis_call,
            lambda: self._fallback.record_failure(
                key,
                now,
                threshold=threshold,
                base=base,
                cap=cap,
                window=window,
                owner=owner,
                net=net,
                min_lock=min_lock,
            ),
            gate=False,
        )

    def clear_failures(self, key: str) -> None:
        def redis_call() -> None:
            row = self._row(_BACKOFF, key)
            owner_index, net_index = (_text(v) for v in self._client.hmget(row, "o", "n"))
            pipe = self._client.pipeline(transaction=True)
            pipe.delete(row)
            pipe.zrem(self._index(_BACKOFF), key)
            for index in (owner_index, net_index):
                if index:
                    pipe.zrem(index, key)
            pipe.execute()

        # A success clears the row wherever it lives (Redis and the local fallback).
        self._fallback.clear_failures(key)
        self._run(redis_call, lambda: None, gate=False)

    # -- per-email failure counters -------------------------------------------------

    def record_signal(
        self, key: str, now: float, *, window: float, thresholds: Sequence[int] = ()
    ) -> tuple[int, tuple[int, ...]]:
        def redis_call() -> tuple[int, tuple[int, ...]]:
            bucket_member = f"~signal|~b{self.overflow_bucket(key)}"
            keys = [
                self._k("m"),
                self._row(_SIGNALS, key),
                self._index(_SIGNALS),
                self._row(_BUCKETS, bucket_member),
                self._index(_BUCKETS),
            ]
            args = [
                "signal",
                self._now_arg(now),
                str(self.TTL_SLACK_MS),
                repr(float(window)),
                str(self._max_keys),
                key,
                bucket_member,
            ]
            reply = [_text(v) for v in self._eval(keys, args)]
            return signal_result(float(reply[0]), thresholds)

        return self._run(
            redis_call,
            lambda: self._fallback.record_signal(key, now, window=window, thresholds=thresholds),
            gate=False,
        )

    def size(self) -> int:
        def redis_call() -> int:
            pipe = self._client.pipeline(transaction=False)
            for table in _TABLES:
                pipe.zcard(self._index(table))
            return int(sum(int(n) for n in pipe.execute()))

        return self._run(redis_call, self._fallback.size, gate=False)

    # -- named counters + health (GET /ops/rate-limits) ---------------------------

    def incr_metric(self, name: str, amount: int = 1) -> None:
        def redis_call() -> None:
            pipe = self._client.pipeline(transaction=True)
            pipe.hincrby(self._k("m"), name, amount)
            pipe.expire(self._k("m"), self.METRICS_TTL_SECONDS)
            pipe.execute()

        self._run(redis_call, lambda: self._fallback.incr_metric(name, amount), gate=False)

    def metrics(self) -> dict[str, int]:
        """Shared (every instance) counts plus this process's degraded-mode counts."""
        local = self._fallback.metrics()

        def redis_call() -> dict[str, int]:
            raw = self._client.hgetall(self._k("m"))
            return {_text(k): int(_text(v)) for k, v in raw.items()}

        def nothing_shared() -> dict[str, int]:
            return {}

        shared = self._run(redis_call, nothing_shared, gate=False)
        out = dict(shared)
        for name, value in local.items():
            out[name] = out.get(name, 0) + value
        return out

    @property
    def overflow_events(self) -> int:
        return int(self.metrics().get("overflow_events", 0))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backend": "redis",
                "shared": True,
                "configured": self._client is not None,
                "healthy": self._healthy,
                "failure_mode": self.failure_mode,
                "server_time": self.server_time,
                "redis_errors": self.redis_errors,
                "fallback_events": self.fallback_events,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "fallback_tracked_keys": self._fallback.size(),
            }

    def ping(self) -> bool:
        """One health probe (used at boot); never raises."""
        try:
            if self._client is None:
                raise ConnectionError("REDIS_URL is not configured")
            self._client.ping()
        except Exception as exc:
            self._note_error(exc)
            return False
        self._note_ok()
        return True


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value) if value is not None else ""


def _is_noscript(exc: Exception) -> bool:
    try:
        from redis.exceptions import NoScriptError  # noqa: PLC0415
    except ImportError:  # pragma: no cover - redis is a declared dependency
        return False
    return isinstance(exc, NoScriptError)


# ---------------------------------------------------------------------------
# Construction from settings (app factory)
# ---------------------------------------------------------------------------


def redis_client_from_url(url: str, timeout_ms: int) -> Any:
    """A redis-py client with short timeouts, or ``None`` when the URL is unusable."""
    if not url.strip():
        return None
    try:
        import redis  # noqa: PLC0415

        seconds = max(0.001, timeout_ms / 1000.0)
        return redis.Redis.from_url(
            url.strip(),
            socket_timeout=seconds,
            socket_connect_timeout=seconds,
            retry_on_timeout=False,
            health_check_interval=30,
        )
    except Exception as exc:  # malformed URL / missing package: never block boot
        logger.warning("rate-limit REDIS_URL is unusable (%s)", type(exc).__name__)
        return None


def build_rate_limiter(settings: Any, *, client: Any = None) -> RateLimiter:
    """The app's limiter: in-memory by default, Redis when RATE_LIMIT_STORE=redis.

    Never blocks boot: an empty / malformed REDIS_URL or an unreachable Redis
    is logged and the limiter starts in its failure mode, recovering on its
    own once Redis answers.
    """
    config = RateLimitConfig.from_settings(settings)
    if str(getattr(settings, "RATE_LIMIT_STORE", "memory")) != "redis":
        return RateLimiter(config)  # unchanged behaviour (per-process random key)
    secret = derive_secret(str(settings.JWT_SECRET))
    if client is None:
        client = redis_client_from_url(
            str(settings.REDIS_URL), int(settings.RATE_LIMIT_REDIS_TIMEOUT_MS)
        )
    store = RedisRateLimitStore(
        client,
        secret=derive_secret(str(settings.JWT_SECRET), "rate-limit-store"),
        max_keys=config.max_keys,
        max_keys_per_ip=config.max_keys_per_ip,
        prefix=str(settings.RATE_LIMIT_REDIS_PREFIX),
        failure_mode=str(settings.RATE_LIMIT_REDIS_FAILURE),
    )
    if client is None:
        logger.warning(
            "RATE_LIMIT_STORE=redis but REDIS_URL is empty or unusable; rate limits run in "
            "failure mode %r (see docs/runbooks/rate-limit-redis.md)",
            store.failure_mode,
        )
        store.ping()  # records the error so /ops/rate-limits shows it
    elif not store.ping():
        logger.warning(
            "rate-limit redis is not answering at boot; starting in failure mode %r and "
            "retrying automatically",
            store.failure_mode,
        )
    return RateLimiter(config, store=store, secret=secret)
