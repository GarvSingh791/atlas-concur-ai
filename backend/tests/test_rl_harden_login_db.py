"""Rate-limit hardening (phase 11 follow-up) — login, end to end (DB-backed).

Finding 1(a): the failed-login hard block was keyed by (IP, email, RAW org
code), so varying the org-code string minted a fresh backoff key per attempt.
Now every non-blank code shares ONE backoff row per (IP, email) (no-code login
keeps its own row).  Whether a code names a real organisation never changes
the key — resolving it would be an existence oracle (failures with code C
would or would not lock a later garbage code G), see the hardening phase log.

Finding 1(b): a per-email failure counter across ALL addresses (sliding hour).
At ``LOGIN_EMAIL_FAILURE_ALERT`` it logs one WARNING (hashed identity) and from
then on every FAILING attempt locks the address it came from for
BASE * 2^(count - alert) seconds (capped at LOGIN_BACKOFF_MAX_SECONDS).  A
correct password from an address that has not itself failed always succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.rate_limit import RateLimiter
from backend.tests.rate_limit_support import FakeClock, installed, make_limiter
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

API = "/api/v1"
ATTACKER_IP = "203.0.113.66"
VICTIM_IP = "198.51.100.10"

Limit = Callable[..., tuple[RateLimiter, FakeClock]]


@pytest.fixture()
def limit(app: Any) -> Iterator[Limit]:
    stack: list[Any] = []

    def _install(**overrides: Any) -> tuple[RateLimiter, FakeClock]:
        clock = FakeClock()
        ctx = installed(app, make_limiter(clock, TRUSTED_PROXY_COUNT=1, **overrides))
        limiter = ctx.__enter__()
        stack.append(ctx)
        return limiter, clock

    yield _install
    while stack:
        stack.pop().__exit__(None, None, None)


@pytest.fixture()
def anon(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _login(client: TestClient, ip: str, email: str, password: str, org: str | None = None) -> Any:
    body: dict[str, Any] = {"email": email, "password": password}
    if org is not None:
        body["organization"] = org
    return client.post(f"{API}/auth/login", json=body, headers={"X-Forwarded-For": ip})


def _shape(resp: Any) -> tuple[Any, ...]:
    error = resp.json().get("error") or {}
    error = {k: v for k, v in error.items() if k != "correlation_id"}
    return (resp.status_code, error, resp.headers.get("Retry-After"))


# ---------------------------------------------------------------------------
# Finding 1(a) — org codes cannot mint backoff keys, and leak nothing
# ---------------------------------------------------------------------------


def test_varying_org_codes_cannot_mint_fresh_backoff_keys(
    limit: Limit, anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    limit(LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=300)
    emp = users_a["employee"].email
    assert _login(anon, ATTACKER_IP, emp, "wrong", org="junk-0001").status_code == 401
    assert _login(anon, ATTACKER_IP, emp, "wrong", org="JUNK-0002 ").status_code == 401
    # A third, never-seen code is NOT a fresh key: the address is cooling down.
    for code in ("junk-0003", "x" * 40, org_a.slug, org_a.slug.upper()):
        resp = _login(anon, ATTACKER_IP, emp, "wrong", org=code)
        assert resp.status_code == 429, code
    # The victim, from their own address, with the real code, signs in.
    ok = _login(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD, org=org_a.slug)
    assert ok.status_code == 200, ok.text


def test_no_code_and_coded_logins_keep_separate_rows(
    limit: Limit, anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    """Two rows per (IP, email) at most: 'no code' and 'any code'.  A user told
    to add their code after failed no-code attempts can still use it."""
    limit(LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=300)
    emp = users_a["employee"].email
    assert _login(anon, VICTIM_IP, emp, "typo-1").status_code == 401
    assert _login(anon, VICTIM_IP, emp, "typo-2", org="  ").status_code == 401  # blank = no code
    assert _login(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD).status_code == 429
    ok = _login(anon, VICTIM_IP, emp, f._DEFAULT_PASSWORD, org=org_a.slug)
    assert ok.status_code == 200, ok.text


def test_unknown_and_existing_org_codes_are_indistinguishable(
    limit: Limit, anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    """No organisation-existence oracle through the throttle.  The same probe
    sequence with a REAL code and with an unknown code — including the
    cross-probe (fail with code C, then try garbage code G) that would reveal a
    design treating real codes differently — yields identical responses."""
    emp = users_a["employee"].email
    seen: dict[str, list[Any]] = {}
    for code in (org_a.slug, "nope-000000"):
        limit(LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=45)
        responses = [
            _login(anon, ATTACKER_IP, emp, "wrong", org=code),
            _login(anon, ATTACKER_IP, emp, "wrong", org=code),
            _login(anon, ATTACKER_IP, emp, "wrong", org=code),
            _login(anon, ATTACKER_IP, emp, "wrong", org="garbage-zz"),
            _login(anon, ATTACKER_IP, emp, "wrong"),
        ]
        seen[code] = [_shape(r) for r in responses]
    assert seen[org_a.slug] == seen["nope-000000"]
    assert [s[0] for s in seen[org_a.slug]] == [401, 401, 429, 429, 401]


# ---------------------------------------------------------------------------
# Finding 1(b) — per-email distributed guard (step-up, never a lockout of the
# correct password from a clean address)
# ---------------------------------------------------------------------------


def _attack(anon: TestClient, email: str, n: int, start: int = 1) -> list[Any]:
    return [_login(anon, f"203.0.113.{i}", email, "wrong") for i in range(start, start + n)]


def test_distributed_attack_gets_escalating_delays_for_failing_attempts(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    limit(
        LOGIN_EMAIL_FAILURE_ALERT=3,
        LOGIN_BACKOFF_BASE_SECONDS=30,
        LOGIN_BACKOFF_MAX_SECONDS=240,
    )  # the per-address backoff threshold stays roomy: only the email guard acts
    emp = users_a["employee"].email
    first = _attack(anon, emp, 10)
    assert {r.status_code for r in first} == {401}  # the failing attempt itself: 401
    retry = []
    for i in range(1, 11):  # each address tries again
        resp = _login(anon, f"203.0.113.{i}", emp, "wrong")
        retry.append(int(resp.headers["Retry-After"]) if resp.status_code == 429 else 0)
    # Below the alert: no delay; from the alert on: 30, 60, 120, 240 (cap) ...
    assert retry == [0, 0, 30, 60, 120, 240, 240, 240, 240, 240]


def test_correct_password_from_a_clean_address_still_succeeds_during_the_attack(
    limit: Limit, anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    limit(LOGIN_EMAIL_FAILURE_ALERT=3, LOGIN_BACKOFF_MAX_SECONDS=900)
    emp = users_a["employee"].email
    _attack(anon, emp, 20)
    for ip in (VICTIM_IP, "2001:db8:77::1", "203.0.113.200"):
        ok = _login(anon, ip, emp, f._DEFAULT_PASSWORD)
        assert ok.status_code == 200, (ip, ok.text)
    ok = _login(anon, "198.51.100.11", emp, f._DEFAULT_PASSWORD, org=org_a.slug)
    assert ok.status_code == 200, ok.text
    # The victim's successes do not reset the attack's counter: the attacker
    # keeps paying the escalated delay.
    resp = _attack(anon, emp, 1, start=21)[0]
    assert resp.status_code == 401
    assert _login(anon, "203.0.113.21", emp, "wrong").status_code == 429


def test_other_emails_are_unaffected_by_an_attack_on_one_email(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    limit(LOGIN_EMAIL_FAILURE_ALERT=3)
    _attack(anon, users_a["employee"].email, 10)
    mgr = users_a["manager"].email
    assert _login(anon, "203.0.113.5", mgr, "typo").status_code == 401
    assert _login(anon, "203.0.113.5", mgr, f._DEFAULT_PASSWORD).status_code == 200


def test_distributed_guard_is_identical_for_existing_and_unknown_emails(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    seen: dict[str, list[Any]] = {}
    for email in (users_a["employee"].email, "ghost.person@example.test"):
        limit(LOGIN_EMAIL_FAILURE_ALERT=3)
        _attack(anon, email, 5)
        seen[email] = [_shape(_login(anon, f"203.0.113.{i}", email, "wrong")) for i in range(1, 6)]
    first, second = seen.values()
    assert first == second


def test_distributed_alert_is_logged_once_with_a_hashed_identity_and_exposed(
    limit: Limit,
    anon: TestClient,
    users_a: dict[str, User],
    finance_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter, _clock = limit(LOGIN_EMAIL_FAILURE_ALERT=3)
    emp = users_a["employee"].email
    with caplog.at_level(logging.WARNING, logger="backend.app.core.rate_limit"):
        _attack(anon, emp, 6)
    alerts = [r for r in caplog.records if "distributed" in r.getMessage()]
    assert len(alerts) == 1
    assert "identity=email:" in alerts[0].getMessage()
    assert emp not in caplog.text.lower()
    assert "203.0.113" not in caplog.text
    resp = finance_client.get(f"{API}/ops/rate-limits")
    assert resp.status_code == 200, resp.text
    guard = resp.json()["login_email_failure_alert"]
    assert guard["threshold"] == 3
    assert guard["window_seconds"] == 3600
    assert guard["emails_alerted"] == 1
    assert guard["escalated_failures"] == 4  # failures 3, 4, 5, 6
    assert emp not in resp.text and "email:" not in resp.text
    assert limiter.snapshot()["login_email_failure_alert"] == guard


def test_an_escalated_address_cannot_switch_code_rows_for_another_try(
    limit: Limit, anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    limit(LOGIN_EMAIL_FAILURE_ALERT=2, LOGIN_BACKOFF_BASE_SECONDS=60)
    emp = users_a["employee"].email
    _attack(anon, emp, 1)
    assert _login(anon, ATTACKER_IP, emp, "wrong").status_code == 401  # count 2: locked
    assert _login(anon, ATTACKER_IP, emp, "wrong").status_code == 429
    assert _login(anon, ATTACKER_IP, emp, "wrong", org=org_a.slug).status_code == 429
    assert _login(anon, ATTACKER_IP, emp, "wrong", org="junk-0001").status_code == 429


def test_ipv6_rotation_inside_one_slash64_shares_one_backoff_row(
    limit: Limit, anon: TestClient, users_a: dict[str, User]
) -> None:
    limit(LOGIN_BACKOFF_THRESHOLD=2, LOGIN_BACKOFF_BASE_SECONDS=300)
    emp = users_a["employee"].email
    assert _login(anon, "2001:db8:5:6::1", emp, "wrong").status_code == 401
    assert _login(anon, "2001:db8:5:6::2", emp, "wrong").status_code == 401
    assert _login(anon, "2001:db8:5:6::3", emp, "wrong").status_code == 429
    # Another /64 (even in the same /48) is another client.
    ok = _login(anon, "2001:db8:5:7::1", emp, f._DEFAULT_PASSWORD)
    assert ok.status_code == 200, ok.text
