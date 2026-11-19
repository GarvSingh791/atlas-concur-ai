"""Phase 12 follow-ups — throttling of the account-recovery endpoints (real app).

Each test installs an ENABLED limiter with tiny limits (the suite runs with
RATE_LIMIT_ENABLED=false) and drives the routes with TestClient:

* forgot-password: per (IP, email) and per IP, checked BEFORE any database
  work, and a throttled answer is identical for existing and unknown accounts;
* reset / accept / verify / change share one per-IP window (a bearer token
  never buys a per-user budget there);
* invite peek: per IP;  invitations: per admin;  send-verification: per user.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.app.core.rate_limit import RateLimiter
from backend.tests.rate_limit_support import FakeClock, installed, make_limiter
from database.models.auth_token import AuthToken
from database.models.identity import User
from services.email_provider.factory import get_email_provider
from services.email_provider.interface import EmailMessage, EmailProvider

pytestmark = pytest.mark.db

API = "/api/v1"
Limit = Callable[..., tuple[RateLimiter, FakeClock]]


class _Sink(EmailProvider):
    name = "smtp"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    @property
    def delivers(self) -> bool:
        return True

    def send(self, message: EmailMessage) -> bool:
        self.sent.append(message)
        return True


@pytest.fixture()
def sink(app: Any) -> Iterator[_Sink]:
    rec = _Sink()
    app.dependency_overrides[get_email_provider] = lambda: rec
    yield rec
    app.dependency_overrides.pop(get_email_provider, None)


@pytest.fixture()
def limit(app: Any) -> Iterator[Limit]:
    stack: list[Any] = []

    def _install(**overrides: Any) -> tuple[RateLimiter, FakeClock]:
        clock = FakeClock()
        ctx = installed(app, make_limiter(clock, **overrides))
        limiter = ctx.__enter__()
        stack.append(ctx)
        return limiter, clock

    yield _install
    while stack:
        stack.pop().__exit__(None, None, None)


@pytest.fixture()
def anon(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _forgot(client: TestClient, email: str) -> Any:
    return client.post(f"{API}/auth/forgot-password", json={"email": email})


def _snapshot(resp: Any) -> tuple[Any, ...]:
    body = resp.json()
    if "error" in body:
        body = {k: v for k, v in body["error"].items() if k != "correlation_id"}
    return (
        resp.status_code,
        body,
        resp.headers.get("Retry-After"),
        resp.headers.get("X-RateLimit-Remaining"),
    )


def test_forgot_password_per_ip_and_email(
    limit: Limit, anon: TestClient, sink: _Sink, users_a: dict[str, User]
) -> None:
    limit(RATE_LIMIT_FORGOT_PER_IP_EMAIL="2/hour")
    emp, mgr = users_a["employee"].email, users_a["manager"].email
    assert _forgot(anon, emp).status_code == 202
    assert _forgot(anon, emp.upper()).status_code == 202  # same email, any casing
    throttled = _forgot(anon, emp)
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "RATE_LIMITED"
    assert _forgot(anon, mgr).status_code == 202  # another email
    assert len(sink.sent) == 3


def test_forgot_password_per_ip(limit: Limit, anon: TestClient, sink: _Sink) -> None:
    limit(RATE_LIMIT_FORGOT_PER_IP="3/hour")
    statuses = [_forgot(anon, f"user{i}@example.test").status_code for i in range(4)]
    assert statuses == [202, 202, 202, 429]


def test_throttled_forgot_password_is_identical_and_does_no_work(
    limit: Limit, anon: TestClient, sink: _Sink, users_a: dict[str, User], db_session: Session
) -> None:
    real = users_a["employee"]
    seen: dict[str, list[Any]] = {}
    for email in (real.email, "ghost-account@example.test"):
        limit(RATE_LIMIT_FORGOT_PER_IP_EMAIL="1/hour")
        seen[email] = [_snapshot(_forgot(anon, email)) for _ in range(3)]
    assert seen[real.email] == seen["ghost-account@example.test"]
    assert [s[0] for s in seen[real.email]] == [202, 429, 429]
    # The throttled calls never reached the database: one token, one email.
    rows = db_session.scalars(sa.select(AuthToken).where(AuthToken.user_id == real.id)).all()
    assert len(rows) == 1 and len(sink.sent) == 1


def test_token_endpoints_share_one_per_ip_window_even_with_a_bearer(
    limit: Limit, anon: TestClient, employee_client: TestClient
) -> None:
    """Two anonymous calls and one signed-in call fill the SAME per-IP window,
    so the next signed-in call is throttled (a bearer never buys its own bucket)."""
    limit(RATE_LIMIT_ACCOUNT_LINK_PER_IP="3/minute")
    garbage = "C" * 43
    assert (
        anon.post(
            f"{API}/auth/reset-password", json={"token": garbage, "new_password": "x"}
        ).status_code
        == 400
    )
    accept = {"token": garbage, "password": "x"}
    assert anon.post(f"{API}/auth/accept-invite", json=accept).status_code == 400
    assert (
        employee_client.post(f"{API}/auth/verify-email", json={"token": garbage}).status_code == 400
    )
    throttled = employee_client.post(
        f"{API}/auth/change-password",
        json={"current_password": "x", "new_password": "y"},
    )
    assert throttled.status_code == 429


def test_invite_peek_is_limited_per_ip(limit: Limit, anon: TestClient) -> None:
    limit(RATE_LIMIT_INVITE_PEEK_PER_IP="2/minute")
    statuses = [anon.get(f"{API}/auth/invite/{'D' * 43}").status_code for _ in range(3)]
    assert statuses == [404, 404, 429]


def test_invitations_are_limited_per_admin(
    limit: Limit, finance_client: TestClient, sink: _Sink
) -> None:
    limit(RATE_LIMIT_INVITE_PER_USER="2/hour")
    statuses = [
        finance_client.post(
            f"{API}/users/invite",
            json={"email": f"hire{i}@example.test", "name": f"Hire {i}", "role": "employee"},
        ).status_code
        for i in range(3)
    ]
    assert statuses == [201, 201, 429]
    assert len(sink.sent) == 2


def test_send_verification_is_limited_per_user(
    limit: Limit, employee_client: TestClient, manager_client: TestClient, sink: _Sink
) -> None:
    limit(RATE_LIMIT_SEND_VERIFICATION_PER_USER="1/hour")
    assert employee_client.post(f"{API}/auth/send-verification").status_code == 202
    assert employee_client.post(f"{API}/auth/send-verification").status_code == 429
    assert manager_client.post(f"{API}/auth/send-verification").status_code == 202
