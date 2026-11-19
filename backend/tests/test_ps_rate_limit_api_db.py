"""Phase 11 — rate limiting end to end through the REAL app (DB-backed).

Each test installs an ENABLED limiter with tiny limits and a fake clock on
``app.state`` (the suite runs with RATE_LIMIT_ENABLED=false) and drives the
real routes with TestClient: login (per IP, per IP+email, failed-login backoff,
no account-existence oracle), refresh, user administration, the AI-spend
endpoints, receipt upload, the safety net and GET /ops/rate-limits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.rate_limit import RateLimiter
from backend.app.core.security import create_access_token, create_refresh_token
from backend.tests.rate_limit_support import FakeClock, installed, make_limiter
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

API = "/api/v1"
STRONG = "Correct-Horse-42-battery"


def _err(resp: Any) -> dict[str, Any]:
    error = dict(resp.json()["error"])
    error.pop("correlation_id", None)
    return error


Limit = Callable[..., tuple[RateLimiter, FakeClock]]


@pytest.fixture()
def limit(app: Any) -> Iterator[Limit]:
    """``limit(**settings_overrides)`` installs an enabled limiter for this test."""
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


def _login(client: TestClient, email: str, password: str) -> Any:
    return client.post(f"{API}/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_per_ip_window(limit: Limit, anon: TestClient, users_a: dict[str, User]) -> None:
    limit(RATE_LIMIT_LOGIN_PER_IP="2/minute")
    emp = users_a["employee"].email
    assert _login(anon, emp, f._DEFAULT_PASSWORD).status_code == 200
    assert _login(anon, "someone@example.test", "x").status_code == 401
    resp = _login(anon, emp, f._DEFAULT_PASSWORD)  # even the right password
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) >= 1


def test_login_per_ip_and_email_window(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    limit(RATE_LIMIT_LOGIN_PER_IP_EMAIL="2/minute")
    emp, mgr = users_a["employee"].email, users_a["manager"].email
    assert _login(anon, emp, "wrong-1").status_code == 401
    assert _login(anon, emp.upper(), "wrong-2").status_code == 401  # same email, any casing
    assert _login(anon, emp, f._DEFAULT_PASSWORD).status_code == 429
    assert _login(anon, mgr, f._DEFAULT_PASSWORD).status_code == 200  # another email


def test_failed_login_backoff_and_reset(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    _limiter, clock = limit(
        LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=30, LOGIN_BACKOFF_MAX_SECONDS=120
    )
    emp = users_a["employee"].email
    assert _login(anon, emp, "wrong-1").status_code == 401
    assert _login(anon, emp, "wrong-2").status_code == 401  # second failure: 30 s cooldown
    blocked = _login(anon, emp, f._DEFAULT_PASSWORD)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["retry_after_seconds"] == 30
    clock.advance(30)
    assert _login(anon, emp, "wrong-3").status_code == 401  # third: 60 s
    clock.advance(59)
    assert _login(anon, emp, f._DEFAULT_PASSWORD).status_code == 429
    clock.advance(1)
    assert _login(anon, emp, f._DEFAULT_PASSWORD).status_code == 200  # success resets
    assert _login(anon, emp, "wrong-4").status_code == 401  # counting from one again
    assert _login(anon, emp, f._DEFAULT_PASSWORD).status_code == 200


def test_throttled_login_is_identical_for_existing_and_unknown_accounts(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    """No account-existence oracle: the same sequence against a real account
    and an unknown address (each from a fresh limiter, so the shared per-IP
    counter does not differ) yields the same statuses, bodies and headers."""
    real = users_a["employee"].email
    ghost = "nobody-here@example.test"
    seen: dict[str, list[Any]] = {}
    for email in (real, ghost):
        limit(LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=45)
        responses = [_login(anon, email, "wrong") for _ in range(3)]
        seen[email] = [
            (
                r.status_code,
                _err(r),
                r.headers.get("Retry-After"),
                r.headers.get("X-RateLimit-Remaining"),
            )
            for r in responses
        ]
    assert seen[real] == seen[ghost]
    assert [s[0] for s in seen[real]] == [401, 401, 429]


def test_refresh_is_limited_per_ip(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    limit(RATE_LIMIT_REFRESH_PER_IP="2/minute")
    emp = users_a["employee"]
    token = create_refresh_token(emp.id, emp.organization_id)
    body = {"refresh_token": token}
    assert anon.post(f"{API}/auth/refresh", json=body).status_code == 200
    assert anon.post(f"{API}/auth/refresh", json=body).status_code == 200
    assert anon.post(f"{API}/auth/refresh", json=body).status_code == 429


# ---------------------------------------------------------------------------
# Authenticated policies
# ---------------------------------------------------------------------------


def _client(app: Any, user: User) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = f"Bearer {create_access_token(user.id, user.organization_id)}"
    return client


def _create_body(email: str) -> dict[str, Any]:
    return {"email": email, "name": "New Person", "role": "employee", "password": STRONG}


def test_user_administration_is_limited_per_admin(
    limit: Limit,
    app: Any,
    finance_client: TestClient,
    users_b: dict[str, User],
) -> None:
    limit(RATE_LIMIT_USER_ADMIN_PER_USER="2/minute")
    created = finance_client.post(f"{API}/users", json=_create_body("one@example.test"))
    assert created.status_code == 201, created.text
    assert created.headers["X-RateLimit-Limit"] == "2"
    uid = created.json()["id"]
    patched = finance_client.patch(f"{API}/users/{uid}", json={"name": "Renamed"})
    assert patched.status_code == 200, patched.text
    resp = finance_client.post(f"{API}/users", json=_create_body("two@example.test"))
    assert resp.status_code == 429
    # Another admin keeps their own budget.
    other = _client(app, users_b["finance"])
    assert other.post(f"{API}/users", json=_create_body("three@example.test")).status_code == 201
    # Reads are not user administration.
    assert finance_client.get(f"{API}/users").status_code == 200


def test_ai_endpoints_share_a_per_user_budget(
    limit: Limit, app: Any, employee_client: TestClient, users_a: dict[str, User]
) -> None:
    """Invalid bodies (422) still count: the limit is spent before the route."""
    limit(RATE_LIMIT_AI_PER_USER="3/hour")
    calls = [
        employee_client.post(f"{API}/policy/query", json={}),
        employee_client.post(f"{API}/policy/evaluate", json={}),
        employee_client.post(f"{API}/conversations/{users_a['employee'].id}/messages", json={}),
        employee_client.post(f"{API}/policy/query", json={}),
    ]
    assert [c.status_code for c in calls[:3]] == [422, 422, 422]
    assert calls[3].status_code == 429
    assert calls[3].json()["error"]["retry_after_seconds"] >= 1
    # Same org, different user: own budget.
    assert _client(app, users_a["manager"]).post(f"{API}/policy/query", json={}).status_code == 422


def test_ai_org_daily_quota_rolls_over(
    limit: Limit, app: Any, users_a: dict[str, User], users_b: dict[str, User], rbac_catalog: None
) -> None:
    _limiter, clock = limit(RATE_LIMIT_AI_DAILY_PER_ORG="2/day")
    emp, mgr = _client(app, users_a["employee"]), _client(app, users_a["manager"])
    assert emp.post(f"{API}/policy/query", json={}).status_code == 422
    assert mgr.post(f"{API}/policy/query", json={}).status_code == 422
    assert emp.post(f"{API}/policy/query", json={}).status_code == 429
    assert mgr.post(f"{API}/policy/query", json={}).status_code == 429
    other_org = _client(app, users_b["employee"])
    assert other_org.post(f"{API}/policy/query", json={}).status_code == 422
    clock.advance(86400)
    assert emp.post(f"{API}/policy/query", json={}).status_code == 422


def test_receipt_upload_is_limited_per_user(limit: Limit, employee_client: TestClient) -> None:
    limit(RATE_LIMIT_RECEIPT_PER_USER="1/minute", RATE_LIMIT_RECEIPT_DAILY_PER_USER="5/day")
    first = employee_client.post(f"{API}/receipts")
    assert first.status_code == 422  # no file — but it counted
    assert employee_client.post(f"{API}/receipts").status_code == 429


def test_receipt_daily_quota(limit: Limit, employee_client: TestClient) -> None:
    _limiter, clock = limit(RATE_LIMIT_RECEIPT_DAILY_PER_USER="2/day")
    assert employee_client.post(f"{API}/receipts").status_code == 422
    clock.advance(120)
    assert employee_client.post(f"{API}/receipts").status_code == 422
    clock.advance(120)
    assert employee_client.post(f"{API}/receipts").status_code == 429


def test_safety_net_limits_everything_else(
    limit: Limit, app: Any, employee_client: TestClient, users_a: dict[str, User]
) -> None:
    limit(RATE_LIMIT_DEFAULT="2/minute")
    assert employee_client.get(f"{API}/me").status_code == 200
    assert employee_client.get(f"{API}/trips").status_code == 200
    assert employee_client.get(f"{API}/me").status_code == 429
    assert _client(app, users_a["manager"]).get(f"{API}/me").status_code == 200
    # /health is never limited.
    for _ in range(5):
        assert employee_client.get("/health").status_code == 200


def test_ops_endpoint_is_finance_only_and_has_no_identities(
    limit: Limit,
    finance_client: TestClient,
    employee_client: TestClient,
    manager_client: TestClient,
    anon: TestClient,
    users_a: dict[str, User],
) -> None:
    limit(RATE_LIMIT_LOGIN_PER_IP="1/minute")
    email = users_a["employee"].email
    _login(anon, email, "wrong")
    assert _login(anon, email, "wrong").status_code == 429
    assert employee_client.get(f"{API}/ops/rate-limits").status_code == 403
    assert manager_client.get(f"{API}/ops/rate-limits").status_code == 403
    assert anon.get(f"{API}/ops/rate-limits").status_code == 401
    resp = finance_client.get(f"{API}/ops/rate-limits")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["throttled"]["auth_login"] == 1
    assert body["limits"]["login_ip"] == {"limit": 1, "window_seconds": 60, "fixed_utc_day": False}
    text = resp.text
    for secret in (email, "testclient", "127.0.0.1", "ip:", "email:", "user:"):
        assert secret not in text


def test_disabled_limiter_on_the_real_app_adds_nothing(
    app: Any, employee_client: TestClient, org_a: Organization
) -> None:
    """The suite-wide default (RATE_LIMIT_ENABLED=false): no limits, no headers."""
    limiter = app.state.rate_limiter
    assert isinstance(limiter, RateLimiter) and not limiter.enabled
    resp = employee_client.get(f"{API}/me")
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers


# ---------------------------------------------------------------------------
# Integration review fix (a): the failed-login HARD block is keyed by
# (client IP, email hash [, org code]); the per-email count is a soft signal.
# ---------------------------------------------------------------------------


def _login_from(
    client: TestClient, ip: str, email: str, password: str, org: str | None = None
) -> Any:
    body: dict[str, Any] = {"email": email, "password": password}
    if org is not None:
        body["organization"] = org
    return client.post(f"{API}/auth/login", json=body, headers={"X-Forwarded-For": ip})


ATTACKER_IP = "203.0.113.66"
VICTIM_IP = "198.51.100.10"


def test_other_ips_failures_never_block_the_correct_password(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    """A remote attacker who knows an email cannot lock its owner out: the
    backoff they trigger throttles THEIR address only."""
    limit(
        TRUSTED_PROXY_COUNT=1,
        LOGIN_BACKOFF_THRESHOLD=2,
        LOGIN_BACKOFF_BASE_SECONDS=300,
        LOGIN_BACKOFF_MAX_SECONDS=900,
    )
    emp = users_a["employee"].email
    assert _login_from(anon, ATTACKER_IP, emp, "wrong-1").status_code == 401
    assert _login_from(anon, ATTACKER_IP, emp, "wrong-2").status_code == 401
    # The failing address stays throttled — even with the right password.
    assert _login_from(anon, ATTACKER_IP, emp, f._DEFAULT_PASSWORD).status_code == 429
    # The owner, from another address, signs in.
    ok = _login_from(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD)
    assert ok.status_code == 200, ok.text
    # Their success resets only THEIR (IP, email) entry, not the attacker's.
    assert _login_from(anon, ATTACKER_IP, emp, f._DEFAULT_PASSWORD).status_code == 429


def test_per_email_failures_are_only_a_soft_signal(
    limit: Limit, anon: TestClient, users_a: dict[str, User], finance_client: TestClient
) -> None:
    limiter, _clock = limit(TRUSTED_PROXY_COUNT=1, LOGIN_BACKOFF_THRESHOLD=2)
    emp = users_a["employee"].email
    for i in range(4):  # one failure each from four addresses
        resp = _login_from(anon, f"203.0.113.{i + 1}", emp, "wrong")
        assert resp.status_code == 401, (i, resp.text)
    snap = limiter.snapshot()
    assert snap["login_email_soft_signals"] >= 1
    assert snap["throttled"]["auth_login_backoff"] == 0
    assert _login_from(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD).status_code == 200


def test_backoff_entry_is_cleared_only_for_the_ip_that_succeeded(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    _limiter, clock = limit(
        TRUSTED_PROXY_COUNT=1, LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=30
    )
    emp = users_a["employee"].email
    assert _login_from(anon, VICTIM_IP, emp, "typo-1").status_code == 401
    assert _login_from(anon, VICTIM_IP, emp, "typo-2").status_code == 401
    assert _login_from(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD).status_code == 429
    clock.advance(30)
    assert _login_from(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD).status_code == 200
    # Success reset that (IP, email) entry: one new typo is not a lockout.
    assert _login_from(anon, VICTIM_IP, emp, "typo-3").status_code == 401
    assert _login_from(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD).status_code == 200
