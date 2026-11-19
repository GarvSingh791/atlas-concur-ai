"""Shared helpers for the phase-11 rate-limit tests (not a test module)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from backend.app.core.config import get_settings
from backend.app.core.rate_limit import RateLimitConfig, RateLimiter, RateLimitStore

# A Monday, 12:00:00 UTC — far from any UTC-day boundary unless a test moves it.
NOON = 1_767_614_400.0


class FakeClock:
    def __init__(self, now: float = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# Generous for everything a test does not pin down.
_ROOMY = {
    "RATE_LIMIT_LOGIN_PER_IP": "1000/minute",
    "RATE_LIMIT_LOGIN_PER_IP_EMAIL": "1000/minute",
    "RATE_LIMIT_REFRESH_PER_IP": "1000/minute",
    "RATE_LIMIT_USER_ADMIN_PER_USER": "1000/minute",
    "RATE_LIMIT_USER_ADMIN_DAILY_PER_USER": "1000/day",
    "RATE_LIMIT_AI_PER_USER": "1000/hour",
    "RATE_LIMIT_AI_DAILY_PER_USER": "1000/day",
    "RATE_LIMIT_AI_DAILY_PER_ORG": "1000/day",
    "RATE_LIMIT_RECEIPT_PER_USER": "1000/minute",
    "RATE_LIMIT_RECEIPT_DAILY_PER_USER": "1000/day",
    "RATE_LIMIT_DEFAULT": "1000/minute",
    "RATE_LIMIT_FORGOT_PER_IP_EMAIL": "1000/hour",
    "RATE_LIMIT_FORGOT_PER_IP": "1000/hour",
    "RATE_LIMIT_ACCOUNT_LINK_PER_IP": "1000/minute",
    "RATE_LIMIT_INVITE_PEEK_PER_IP": "1000/minute",
    "RATE_LIMIT_INVITE_PER_USER": "1000/hour",
    "RATE_LIMIT_SEND_VERIFICATION_PER_USER": "1000/hour",
    "LOGIN_BACKOFF_THRESHOLD": 1000,
}


def make_limiter(
    clock: FakeClock | None = None,
    *,
    store_factory: Callable[[RateLimitConfig], RateLimitStore] | None = None,
    **overrides: Any,
) -> RateLimiter:
    """An ENABLED limiter on a copy of the real settings, roomy except ``overrides``.

    ``store_factory`` builds the store from the config (the store contract
    suite runs the same scenarios on the in-memory and the Redis store).
    """
    update: dict[str, Any] = {"RATE_LIMIT_ENABLED": True, **_ROOMY, **overrides}
    settings = get_settings().model_copy(update=update)
    config = RateLimitConfig.from_settings(settings)
    return RateLimiter(
        config,
        store=store_factory(config) if store_factory is not None else None,
        clock=clock or FakeClock(),
        secret=b"test-secret-not-real",
    )


@contextmanager
def installed(app: Any, limiter: RateLimiter) -> Iterator[RateLimiter]:
    """Install ``limiter`` on ``app.state`` for the duration of the block."""
    previous = getattr(app.state, "rate_limiter", None)
    app.state.rate_limiter = limiter
    try:
        yield limiter
    finally:
        app.state.rate_limiter = previous
