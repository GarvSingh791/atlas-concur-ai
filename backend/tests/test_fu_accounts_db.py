"""Phase 12 follow-ups — account recovery end to end (DB-backed, real app).

Password reset, password change, invitations and email verification, plus the
security invariants each one rests on:

* no account-existence oracle (forgot-password / peek / accept): same status,
  body and DB / hashing work whatever exists;
* tokens: hash-only at rest, single use, short TTL, purpose- and org-bound;
* a reset / change / acceptance revokes every session minted before it;
* DEMO_MODE protected accounts: no reset, no change, no verification email;
* nothing secret (token, link, password) in logs, audit rows or errors;
* emails leave only after the commit (never for a rolled-back request);
* links come only from APP_BASE_URL (never the Host header);
* tokens are never accepted from a query string.

Email delivery is captured by overriding the ``get_email_provider`` dependency.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token, create_refresh_token
from backend.app.modules.identity import accounts
from backend.app.modules.identity.password_policy import unusable_password_hash
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.auth_token import AuthToken, AuthTokenPurpose
from database.models.identity import Organization, RoleKey, User, UserStatus
from database.seeds.seed_demo_grades import DEMO_ORG_NAME
from services.email_provider.factory import get_email_provider
from services.email_provider.interface import EmailMessage, EmailProvider

pytestmark = pytest.mark.db

API = "/api/v1"
NEW_PASSWORD = "Brand-New-Passw0rd!"
TOKEN_RE = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class RecordingMailer(EmailProvider):
    """Captures messages instead of sending them.  ``delivering`` mimics smtp."""

    def __init__(self, *, delivering: bool, events: list[str] | None = None) -> None:
        self.name = "smtp" if delivering else "console"
        self._delivering = delivering
        self.sent: list[EmailMessage] = []
        self.events = events if events is not None else []

    @property
    def delivers(self) -> bool:
        return self._delivering

    def send(self, message: EmailMessage) -> bool:
        self.events.append("send")
        self.sent.append(message)
        return True


@pytest.fixture()
def mailer(app: Any) -> RecordingMailer:
    rec = RecordingMailer(delivering=True)
    app.dependency_overrides[get_email_provider] = lambda: rec
    return rec


@pytest.fixture()
def console_mailer(app: Any) -> RecordingMailer:
    rec = RecordingMailer(delivering=False)
    app.dependency_overrides[get_email_provider] = lambda: rec
    return rec


@pytest.fixture()
def anon(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def base_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "https://atlas.example.test"
    monkeypatch.setattr(get_settings(), "APP_BASE_URL", url)
    return url


def _client(app: Any, user: User) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.headers["Authorization"] = f"Bearer {create_access_token(user.id, user.organization_id)}"
    return client


def _token_of(message: EmailMessage) -> str:
    match = TOKEN_RE.search(message.text_body)
    assert match, "no token link in the message"
    return match.group(1)


def _forgot(client: TestClient, email: str, organization: str | None = None) -> Any:
    body: dict[str, Any] = {"email": email}
    if organization is not None:
        body["organization"] = organization
    return client.post(f"{API}/auth/forgot-password", json=body)


def _reset(client: TestClient, token: str, password: str = NEW_PASSWORD) -> Any:
    return client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": password}
    )


def _login(client: TestClient, email: str, password: str, org: str | None = None) -> Any:
    body: dict[str, Any] = {"email": email, "password": password}
    if org:
        body["organization"] = org
    return client.post(f"{API}/auth/login", json=body)


def _err(resp: Any) -> dict[str, Any]:
    error = dict(resp.json()["error"])
    error.pop("correlation_id", None)
    return error


def _invite(client: TestClient, **over: Any) -> Any:
    body = {"email": "new.hire@example.test", "name": "New Hire", "role": "employee"}
    body.update(over)
    return client.post(f"{API}/users/invite", json=body)


def _tokens(db: Session, user: User, purpose: str) -> list[AuthToken]:
    return list(
        db.scalars(
            sa.select(AuthToken)
            .where(AuthToken.user_id == user.id, AuthToken.purpose == purpose)
            .order_by(AuthToken.created_at)
        ).all()
    )


def _audit_rows(db: Session, user_id: Any) -> list[AuditLog]:
    return list(db.scalars(sa.select(AuditLog).where(AuditLog.entity_id == user_id)).all())


def _demo_account(db: Session) -> User:
    org = db.scalars(
        sa.select(Organization).where(Organization.name == DEMO_ORG_NAME)
    ).first() or f.make_org(db, name=DEMO_ORG_NAME)
    email = "employee@atlas-demo.internal"
    user = db.scalars(
        sa.select(User).where(User.organization_id == org.id, User.email == email)
    ).first() or f.make_user(db, org=org, email=email)
    # The shared test DB may hold a committed seed (another password, the same
    # email in other organisations): pin the password and address it by code.
    user.status = UserStatus.active
    user.password_hash = f._DEFAULT_PW_HASH
    db.flush()
    return user


def _slug(db: Session, user: User) -> str:
    return db.scalars(
        sa.select(Organization.slug).where(Organization.id == user.organization_id)
    ).one()


@pytest.fixture()
def demo_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)


# ---------------------------------------------------------------------------
# Forgot password — no oracle, hash-only, invalidates earlier tokens
# ---------------------------------------------------------------------------


def test_forgot_password_emails_an_active_account_a_hash_only_token(
    anon: TestClient,
    mailer: RecordingMailer,
    base_url: str,
    users_a: dict[str, User],
    db_session: Session,
) -> None:
    emp = users_a["employee"]
    resp = _forgot(anon, emp.email.upper())  # any casing
    assert resp.status_code == 202
    assert resp.json() == {"message": accounts.FORGOT_PASSWORD_ACCEPTED}
    (message,) = mailer.sent
    assert message.to == emp.email and message.purpose == "reset"
    raw = _token_of(message)
    assert f"{base_url}/reset-password?token={raw}" in message.text_body
    assert f"{base_url}/reset-password?token={raw}" in message.html_body
    (row,) = _tokens(db_session, emp, "password_reset")
    assert row.token_hash == accounts.hash_token(raw) and raw not in row.token_hash
    assert row.organization_id == emp.organization_id and row.used_at is None
    ttl = row.expires_at - row.created_at
    assert timedelta(minutes=29) < ttl <= timedelta(minutes=31)
    assert row.requested_ip_hash and len(row.requested_ip_hash) == 32


def test_forgot_password_is_identical_for_every_kind_of_account(
    anon: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
    org_a: Organization,
    demo_on: None,
) -> None:
    """Existing, unknown, inactive, invited, protected: same status + body."""
    inactive = f.make_user(db_session, org=org_a, status=UserStatus.inactive)
    invited = f.make_user(
        db_session, org=org_a, status=UserStatus.inactive, password_hash="!unusable:abc"
    )
    protected = _demo_account(db_session)
    seen = {
        label: (r.status_code, r.json())
        for label, r in (
            ("real", _forgot(anon, users_a["employee"].email)),
            ("unknown", _forgot(anon, "nobody-at-all@example.test")),
            ("inactive", _forgot(anon, inactive.email)),
            ("invited", _forgot(anon, invited.email)),
            ("protected", _forgot(anon, protected.email, _slug(db_session, protected))),
            ("bad-shape", _forgot(anon, "not-an-email")),
        )
    }
    assert len(set(json.dumps(v, sort_keys=True) for v in seen.values())) == 1
    assert seen["real"][0] == 202
    # ...and only the real, active, unprotected account got an email.
    assert [m.to for m in mailer.sent] == [users_a["employee"].email]


def _work(app_client: TestClient, db: Session, email: str, monkeypatch: Any) -> tuple[int, int]:
    """(SQL statements executed, tokens generated) by one forgot-password call."""
    statements: list[str] = []
    generated = 0
    real_new_token = accounts.new_token

    def counting_new_token() -> tuple[str, str]:
        nonlocal generated
        generated += 1
        return real_new_token()

    monkeypatch.setattr(accounts, "new_token", counting_new_token)
    conn = db.connection()

    def _count(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        statements.append(statement.split()[0].upper())

    event.listen(conn, "before_cursor_execute", _count)
    try:
        assert _forgot(app_client, email).status_code == 202
    finally:
        event.remove(conn, "before_cursor_execute", _count)
        monkeypatch.setattr(accounts, "new_token", real_new_token)
    return len(statements), generated


def test_forgot_password_does_the_same_db_and_hash_work_whatever_exists(
    anon: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
    org_a: Organization,
    monkeypatch: pytest.MonkeyPatch,
    demo_on: None,
) -> None:
    """Timing parity: identical statement count and token generations for an
    existing account and for every account that must not get an email."""
    inactive = f.make_user(db_session, org=org_a, status=UserStatus.inactive)
    protected = _demo_account(db_session)
    real = _work(anon, db_session, users_a["employee"].email, monkeypatch)
    for email in ("nobody-at-all@example.test", inactive.email, protected.email):
        assert _work(anon, db_session, email, monkeypatch) == real, email
    assert real[1] == 5  # NO_ORG_LOGIN_CANDIDATES slots, always


def test_a_new_reset_request_voids_the_earlier_link(
    anon: TestClient, mailer: RecordingMailer, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    _forgot(anon, emp.email)
    _forgot(anon, emp.email)
    first, second = (_token_of(m) for m in mailer.sent)
    assert _reset(anon, first).status_code == 400
    assert _reset(anon, second).status_code == 204


def test_forgot_password_is_per_org_when_a_code_is_given(
    anon: TestClient, mailer: RecordingMailer, db_session: Session
) -> None:
    """The same email in two organisations: no code -> one org-bound link each;
    a code -> only that organisation's account."""
    org_x = f.make_org(db_session, name="Org X")
    org_y = f.make_org(db_session, name="Org Y")
    in_x = f.make_user(db_session, org=org_x, email="shared@example.test")
    in_y = f.make_user(db_session, org=org_y, email="shared@example.test")
    _forgot(anon, "shared@example.test")
    assert len(mailer.sent) == 2
    _forgot(anon, "shared@example.test", organization=org_y.slug.upper())
    only = _token_of(mailer.sent[-1])
    row = db_session.scalars(
        sa.select(AuthToken).where(AuthToken.token_hash == accounts.hash_token(only))
    ).one()
    assert row.user_id == in_y.id and row.organization_id == org_y.id
    assert in_x.id != in_y.id


# ---------------------------------------------------------------------------
# Reset password — single use, TTL, purpose, org, sessions revoked
# ---------------------------------------------------------------------------


def _reset_token(anon: TestClient, mailer: RecordingMailer, user: User) -> str:
    _forgot(anon, user.email)
    return _token_of(mailer.sent[-1])


def test_reset_sets_the_password_once_and_revokes_old_sessions(
    app: Any,
    anon: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
) -> None:
    emp = users_a["employee"]
    old_access = create_access_token(emp.id, emp.organization_id)
    old_refresh = create_refresh_token(emp.id, emp.organization_id)
    assert _client(app, emp).get(f"{API}/me").status_code == 200

    token = _reset_token(anon, mailer, emp)
    resp = _reset(anon, token)
    assert resp.status_code == 204 and resp.content == b""

    # Single use.
    again = _reset(anon, token, "Another-Passw0rd!x")
    assert again.status_code == 400
    assert _err(again)["code"] == "INVALID_OR_EXPIRED_TOKEN"
    # New password works, the old one does not.
    assert _login(anon, emp.email, f._DEFAULT_PASSWORD).status_code == 401
    fresh = _login(anon, emp.email, NEW_PASSWORD)
    assert fresh.status_code == 200
    # Every token minted before the reset is dead; the new session works.
    stale = TestClient(app, raise_server_exceptions=False)
    stale.headers["Authorization"] = f"Bearer {old_access}"
    assert stale.get(f"{API}/me").status_code == 401
    assert anon.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401
    new_client = TestClient(app, raise_server_exceptions=False)
    new_client.headers["Authorization"] = f"Bearer {fresh.json()['access_token']}"
    me = new_client.get(f"{API}/me")
    assert me.status_code == 200 and me.json()["email_verified_at"] is not None
    refreshed = anon.post(
        f"{API}/auth/refresh", json={"refresh_token": fresh.json()["refresh_token"]}
    )
    assert refreshed.status_code == 200
    db_session.refresh(emp)
    assert emp.password_changed_at is not None and emp.email_verified_at is not None
    actions = [a.action for a in _audit_rows(db_session, emp.id)]
    assert "user.password_reset" in actions


@pytest.mark.parametrize("case", ["garbage", "expired", "used", "other_org", "deactivated"])
def test_every_bad_reset_token_gets_the_same_400(
    case: str,
    anon: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    users_b: dict[str, User],
    db_session: Session,
) -> None:
    emp = users_a["employee"]
    token = _reset_token(anon, mailer, emp)
    row = db_session.scalars(
        sa.select(AuthToken).where(AuthToken.token_hash == accounts.hash_token(token))
    ).one()
    if case == "garbage":
        token = "A" * 43
    elif case == "expired":
        row.expires_at = row.created_at - timedelta(seconds=1)
    elif case == "used":
        row.used_at = row.created_at
    elif case == "other_org":
        row.organization_id = users_b["employee"].organization_id
    elif case == "deactivated":
        emp.status = UserStatus.inactive
    db_session.flush()
    resp = _reset(anon, token)
    assert resp.status_code == 400
    assert _err(resp) == {
        "code": "INVALID_OR_EXPIRED_TOKEN",
        "message": accounts.INVALID_TOKEN_MESSAGE,
    }


def test_a_weak_password_is_refused_without_burning_the_token(
    anon: TestClient, mailer: RecordingMailer, users_a: dict[str, User]
) -> None:
    token = _reset_token(anon, mailer, users_a["employee"])
    weak = _reset(anon, token, "short")
    assert weak.status_code == 422
    assert "12 characters" in weak.json()["error"]["message"]
    assert _reset(anon, token).status_code == 204


def test_tokens_are_purpose_bound(
    anon: TestClient,
    mailer: RecordingMailer,
    finance_client: TestClient,
    users_a: dict[str, User],
) -> None:
    reset = _reset_token(anon, mailer, users_a["employee"])
    _invite(finance_client)
    invite = _token_of(mailer.sent[-1])
    accept_body = {"token": reset, "password": NEW_PASSWORD}
    assert anon.post(f"{API}/auth/accept-invite", json=accept_body).status_code == 400
    assert anon.post(f"{API}/auth/verify-email", json={"token": reset}).status_code == 400
    assert anon.get(f"{API}/auth/invite/{reset}").status_code == 404
    assert _reset(anon, invite).status_code == 400
    assert anon.post(f"{API}/auth/verify-email", json={"token": invite}).status_code == 400
    # Both still work for their own purpose.
    assert _reset(anon, reset).status_code == 204
    accept = {"token": invite, "password": NEW_PASSWORD}
    assert anon.post(f"{API}/auth/accept-invite", json=accept).status_code == 204


def test_tokens_are_never_read_from_the_query_string(
    anon: TestClient, mailer: RecordingMailer, users_a: dict[str, User]
) -> None:
    token = _reset_token(anon, mailer, users_a["employee"])
    resp = anon.post(
        f"{API}/auth/reset-password?token={token}", json={"new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 422  # the body's token is required; the URL's is ignored
    assert anon.post(f"{API}/auth/verify-email?token={token}", json={}).status_code == 422
    assert _reset(anon, token).status_code == 204  # still unused


def test_links_come_from_app_base_url_never_the_host_header(
    app: Any, mailer: RecordingMailer, base_url: str, users_a: dict[str, User]
) -> None:
    evil = TestClient(app, base_url="https://evil.example", raise_server_exceptions=False)
    resp = evil.post(
        f"{API}/auth/forgot-password",
        json={"email": users_a["employee"].email},
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
    )
    assert resp.status_code == 202
    (message,) = mailer.sent
    assert "evil.example" not in message.text_body + message.html_body
    assert f"{base_url}/reset-password?token=" in message.text_body


# ---------------------------------------------------------------------------
# DEMO_MODE protected accounts
# ---------------------------------------------------------------------------


def test_protected_accounts_cannot_reset_change_or_verify(
    app: Any,
    anon: TestClient,
    mailer: RecordingMailer,
    db_session: Session,
    rbac_catalog: None,
    demo_on: None,
) -> None:
    demo = _demo_account(db_session)
    code = _slug(db_session, demo)
    assert _forgot(anon, demo.email, code).status_code == 202
    assert mailer.sent == []
    # A token that slipped out before DEMO_MODE was switched on is refused too.
    raw, digest = accounts.new_token()
    db_session.add(
        AuthToken(
            organization_id=demo.organization_id,
            user_id=demo.id,
            purpose="password_reset",
            token_hash=digest,
            expires_at=accounts._now() + timedelta(minutes=30),
        )
    )
    db_session.flush()
    assert _reset(anon, raw).status_code == 400

    client = _client(app, demo)
    change = client.post(
        f"{API}/auth/change-password",
        json={"current_password": f._DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert change.status_code == 403
    assert change.json()["error"]["code"] == "DEMO_ACCOUNT_PROTECTED"
    send = client.post(f"{API}/auth/send-verification")
    assert send.status_code == 403
    assert client.get(f"{API}/me").json()["demo_protected"] is True
    assert _login(anon, demo.email, f._DEFAULT_PASSWORD, code).status_code == 200  # unchanged


# ---------------------------------------------------------------------------
# Change password (signed in)
# ---------------------------------------------------------------------------


def test_change_password_reissues_this_session_and_ends_the_others(
    app: Any, employee_client: TestClient, users_a: dict[str, User], db_session: Session
) -> None:
    emp = users_a["employee"]
    wrong = employee_client.post(
        f"{API}/auth/change-password",
        json={"current_password": "not-it", "new_password": NEW_PASSWORD},
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "INVALID_CURRENT_PASSWORD"
    same = employee_client.post(
        f"{API}/auth/change-password",
        json={"current_password": f._DEFAULT_PASSWORD, "new_password": f._DEFAULT_PASSWORD},
    )
    assert same.status_code == 422

    other_device = _client(app, emp)
    ok = employee_client.post(
        f"{API}/auth/change-password",
        json={"current_password": f._DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert ok.status_code == 200
    tokens = ok.json()
    assert employee_client.get(f"{API}/me").status_code == 401  # the old token
    assert other_device.get(f"{API}/me").status_code == 401
    renewed = TestClient(app, raise_server_exceptions=False)
    renewed.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    assert renewed.get(f"{API}/me").status_code == 200
    actions = [a.action for a in _audit_rows(db_session, emp.id)]
    assert "user.password_changed" in actions


def test_change_password_voids_an_outstanding_reset_link(
    anon: TestClient,
    employee_client: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
) -> None:
    """A reset link emailed before a deliberate change must not take the account back."""
    emp = users_a["employee"]
    token = _reset_token(anon, mailer, emp)
    ok = employee_client.post(
        f"{API}/auth/change-password",
        json={"current_password": f._DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert ok.status_code == 200

    takeover = _reset(anon, token, "Attacker-Passw0rd!x")
    assert takeover.status_code == 400
    assert _err(takeover) == {
        "code": "INVALID_OR_EXPIRED_TOKEN",
        "message": accounts.INVALID_TOKEN_MESSAGE,
    }
    row = db_session.scalars(
        sa.select(AuthToken).where(AuthToken.token_hash == accounts.hash_token(token))
    ).one()
    db_session.refresh(row)
    assert row.used_at is not None
    # The deliberately chosen password still stands.
    assert _login(anon, emp.email, NEW_PASSWORD).status_code == 200
    assert _login(anon, emp.email, "Attacker-Passw0rd!x").status_code == 401


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


def test_invite_creates_an_unusable_account_and_hands_the_link_over_only_without_email(
    app: Any,
    finance_client: TestClient,
    console_mailer: RecordingMailer,
    base_url: str,
    db_session: Session,
) -> None:
    resp = _invite(finance_client, email="New.Hire@Example.test", grade="manager")
    assert resp.status_code == 201
    body = resp.json()
    assert resp.headers["Cache-Control"] == "no-store"
    user = body["user"]
    assert user["status"] == "inactive" and user["invite_pending"] is True
    assert user["email"] == "new.hire@example.test" and user["email_verified_at"] is None
    assert body["email_provider"] == "console"
    url = body["invite_url"]
    assert url.startswith(f"{base_url}/accept-invite?token=")
    (message,) = console_mailer.sent
    assert _token_of(message) == TOKEN_RE.search(url).group(1)  # type: ignore[union-attr]
    row = db_session.get(User, user["id"])
    assert row is not None and row.password_hash.startswith("!unusable:")
    # Nobody can sign in as the placeholder.
    anon = TestClient(app, raise_server_exceptions=False)
    assert _login(anon, "new.hire@example.test", "").status_code in (401, 422)
    assert _login(anon, "new.hire@example.test", row.password_hash).status_code == 401


def test_invite_url_is_withheld_when_email_is_delivered(
    finance_client: TestClient, mailer: RecordingMailer
) -> None:
    body = _invite(finance_client).json()
    assert body["invite_url"] is None and body["email_provider"] == "smtp"
    assert len(mailer.sent) == 1


def test_invite_reuses_the_create_user_validations(
    finance_client: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    taken = _invite(finance_client, email=users_a["employee"].email.upper())
    created = finance_client.post(
        f"{API}/users",
        json={
            "email": users_a["employee"].email,
            "name": "X",
            "role": "employee",
            "password": NEW_PASSWORD,
        },
    )
    assert taken.status_code == created.status_code == 409
    assert _err(taken) == _err(created)  # the same generic EMAIL_TAKEN
    assert _invite(finance_client, role="ceo").status_code == 422
    assert _invite(finance_client, grade="admiral").status_code == 422
    bad_manager = _invite(finance_client, manager_id=str(users_a["employee"].id))
    assert bad_manager.status_code == 422
    # Per organisation: the same address in another organisation is fine.
    assert _invite(finance_client, email=users_b["employee"].email).status_code == 201
    assert len(mailer.sent) == 1


def test_peek_and_accept_activate_the_account(
    app: Any,
    finance_client: TestClient,
    mailer: RecordingMailer,
    org_a: Organization,
    db_session: Session,
) -> None:
    user_id = _invite(finance_client).json()["user"]["id"]
    token = _token_of(mailer.sent[0])
    anon = TestClient(app, raise_server_exceptions=False)
    peek = anon.get(f"{API}/auth/invite/{token}")
    assert peek.status_code == 200
    assert peek.headers["Cache-Control"] == "no-store"
    assert peek.json()["organization_name"] == org_a.name
    assert peek.json()["email_masked"] == "n***@example.test"
    assert peek.json()["name"] == "New Hire"

    weak = anon.post(f"{API}/auth/accept-invite", json={"token": token, "password": "weak"})
    assert weak.status_code == 422
    resp = anon.post(
        f"{API}/auth/accept-invite",
        json={"token": token, "password": NEW_PASSWORD, "name": "  New   Hire-Smith "},
    )
    assert resp.status_code == 204 and resp.content == b""  # no session is issued
    user = db_session.get(User, user_id)
    assert user is not None
    db_session.refresh(user)
    assert user.status == UserStatus.active and user.name == "New Hire-Smith"
    assert user.email_verified_at is not None and user.password_changed_at is not None
    assert _login(anon, "new.hire@example.test", NEW_PASSWORD).status_code == 200
    # Used: neither the peek nor a second accept works any more.
    assert anon.get(f"{API}/auth/invite/{token}").status_code == 404
    again = anon.post(
        f"{API}/auth/accept-invite", json={"token": token, "password": "Other-Passw0rd!!"}
    )
    assert again.status_code == 400
    actions = [a.action for a in _audit_rows(db_session, user.id)]
    assert {"user.invited", "user.invite_accepted"} <= set(actions)


def test_the_invite_peek_is_one_generic_404(
    app: Any, finance_client: TestClient, mailer: RecordingMailer, db_session: Session
) -> None:
    _invite(finance_client)
    token = _token_of(mailer.sent[0])
    row = db_session.scalars(
        sa.select(AuthToken).where(AuthToken.token_hash == accounts.hash_token(token))
    ).one()
    anon = TestClient(app, raise_server_exceptions=False)
    answers = [_err(anon.get(f"{API}/auth/invite/{'B' * 43}"))]
    answers.append(_err(anon.get(f"{API}/auth/invite/short")))
    row.expires_at = row.created_at - timedelta(seconds=1)
    db_session.flush()
    expired = anon.get(f"{API}/auth/invite/{token}")
    assert expired.status_code == 404
    answers.append(_err(expired))
    assert all(a == answers[0] for a in answers)
    assert answers[0]["message"] == accounts.INVITE_LINK_MESSAGE


def test_an_account_activated_outside_the_accept_flow_cannot_be_taken_over_by_its_invite(
    app: Any, finance_client: TestClient, mailer: RecordingMailer, db_session: Session
) -> None:
    user_id = _invite(finance_client).json()["user"]["id"]
    token = _token_of(mailer.sent[0])
    user = db_session.get(User, user_id)
    assert user is not None
    # What a seed / demo-reset restore does: the account is live with a real
    # password while its invitation token is still unused and unexpired.
    user.status = UserStatus.active
    user.password_hash = f._DEFAULT_PW_HASH
    db_session.flush()
    anon = TestClient(app, raise_server_exceptions=False)

    peek = anon.get(f"{API}/auth/invite/{token}")
    assert peek.status_code == 404
    assert _err(peek)["message"] == accounts.INVITE_LINK_MESSAGE

    resp = anon.post(f"{API}/auth/accept-invite", json={"token": token, "password": NEW_PASSWORD})
    assert resp.status_code == 400
    assert _err(resp)["code"] == "INVALID_OR_EXPIRED_TOKEN"
    db_session.refresh(user)
    assert user.password_hash == f._DEFAULT_PW_HASH
    assert user.status == UserStatus.active
    (row,) = _tokens(db_session, user, "invite")
    assert row.used_at is None
    assert _login(anon, user.email, NEW_PASSWORD).status_code == 401


def test_a_protected_placeholder_cannot_be_activated_by_an_invite(
    app: Any,
    anon: TestClient,
    db_session: Session,
    rbac_catalog: None,
    demo_on: None,
) -> None:
    demo = _demo_account(db_session)
    demo.status = UserStatus.inactive
    demo.password_hash = unusable_password_hash()
    db_session.flush()
    raw, _expires = accounts._issue(
        db_session,
        user=demo,
        purpose=AuthTokenPurpose.invite,
        ttl=timedelta(hours=1),
        created_by=None,
    )
    hash_before = demo.password_hash
    resp = anon.post(f"{API}/auth/accept-invite", json={"token": raw, "password": NEW_PASSWORD})
    assert resp.status_code == 400
    assert _err(resp)["code"] == "INVALID_OR_EXPIRED_TOKEN"
    db_session.refresh(demo)
    assert demo.status == UserStatus.inactive
    assert demo.password_hash == hash_before
    (row,) = _tokens(db_session, demo, "invite")
    assert row.used_at is None


def test_resend_voids_the_old_link_and_revoke_voids_all(
    app: Any,
    finance_client: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
) -> None:
    user_id = _invite(finance_client).json()["user"]["id"]
    first = _token_of(mailer.sent[0])
    resent = finance_client.post(f"{API}/users/{user_id}/invite/resend")
    assert resent.status_code == 200 and resent.json()["user"]["invite_pending"] is True
    second = _token_of(mailer.sent[1])
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get(f"{API}/auth/invite/{first}").status_code == 404
    assert anon.get(f"{API}/auth/invite/{second}").status_code == 200

    # Reactivating a never-activated account is refused (no password exists).
    react = finance_client.post(f"{API}/users/{user_id}/reactivate")
    assert react.status_code == 409 and react.json()["error"]["code"] == "INVITE_PENDING"

    revoked = finance_client.post(f"{API}/users/{user_id}/invite/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "inactive" and revoked.json()["invite_pending"] is False
    assert revoked.json()["awaiting_activation"] is True  # resend, don't reactivate
    assert anon.get(f"{API}/auth/invite/{second}").status_code == 404
    accept = {"token": second, "password": NEW_PASSWORD}
    assert anon.post(f"{API}/auth/accept-invite", json=accept).status_code == 400

    # Active accounts have no invitation to resend or revoke.
    emp = users_a["employee"].id
    for op in ("resend", "revoke"):
        resp = finance_client.post(f"{API}/users/{emp}/invite/{op}")
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "NOT_INVITED"
    # Another organisation's account: 404, like every admin endpoint.
    other = f.make_user(db_session, org=f.make_org(db_session), status=UserStatus.inactive)
    assert finance_client.post(f"{API}/users/{other.id}/invite/resend").status_code == 404
    actions = {a.action for a in _audit_rows(db_session, db_session.get(User, user_id).id)}  # type: ignore[union-attr]
    assert {"user.invited", "user.invite_resent", "user.invite_revoked"} <= actions


def test_users_list_shows_invited_and_verified_state(
    finance_client: TestClient, mailer: RecordingMailer
) -> None:
    _invite(finance_client)
    items = finance_client.get(f"{API}/users", params={"size": 100}).json()["items"]
    invited = next(u for u in items if u["email"] == "new.hire@example.test")
    assert invited["invite_pending"] is True and invited["invite_expires_at"] is not None
    assert invited["awaiting_activation"] is True
    active = [u for u in items if u["status"] == "active"]
    assert active and all(not u["invite_pending"] and not u["awaiting_activation"] for u in active)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def test_send_and_verify_email(
    app: Any, employee_client: TestClient, mailer: RecordingMailer, users_a: dict[str, User]
) -> None:
    assert employee_client.get(f"{API}/me").json()["email_verified_at"] is None
    sent = employee_client.post(f"{API}/auth/send-verification")
    assert sent.status_code == 202 and sent.json()["message"] == accounts.VERIFICATION_SENT
    sent_again = employee_client.post(f"{API}/auth/send-verification")
    assert sent_again.status_code == 202
    first, second = (_token_of(m) for m in mailer.sent)
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.post(f"{API}/auth/verify-email", json={"token": first}).status_code == 400
    assert anon.post(f"{API}/auth/verify-email", json={"token": second}).status_code == 204
    assert employee_client.get(f"{API}/me").json()["email_verified_at"] is not None
    done = employee_client.post(f"{API}/auth/send-verification")
    assert done.json()["message"] == accounts.ALREADY_VERIFIED
    assert len(mailer.sent) == 2
    assert anon.post(f"{API}/auth/verify-email", json={"token": second}).status_code == 400


def test_a_verification_link_dies_with_the_account(
    app: Any,
    employee_client: TestClient,
    finance_client: TestClient,
    mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
) -> None:
    """A link issued while active must not touch the account once deactivated."""
    emp = users_a["employee"]
    sent = employee_client.post(f"{API}/auth/send-verification")
    assert sent.status_code == 202
    token = _token_of(mailer.sent[-1])
    gone = finance_client.post(f"{API}/users/{emp.id}/deactivate")
    assert gone.status_code == 200, gone.text

    anon = TestClient(app, raise_server_exceptions=False)
    resp = anon.post(f"{API}/auth/verify-email", json={"token": token})
    assert resp.status_code == 400
    assert _err(resp) == {
        "code": "INVALID_OR_EXPIRED_TOKEN",
        "message": accounts.INVALID_TOKEN_MESSAGE,
    }
    db_session.refresh(emp)
    assert emp.email_verified_at is None
    actions = [a.action for a in _audit_rows(db_session, emp.id)]
    assert "user.email_verified" not in actions
    row = db_session.scalars(
        sa.select(AuthToken).where(AuthToken.token_hash == accounts.hash_token(token))
    ).one()
    db_session.refresh(row)
    assert row.used_at is None  # refused, not redeemed


# ---------------------------------------------------------------------------
# Email after commit; no secrets anywhere
# ---------------------------------------------------------------------------


def test_email_is_sent_only_after_the_commit(
    app: Any,
    finance_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    rec = RecordingMailer(delivering=True, events=events)
    app.dependency_overrides[get_email_provider] = lambda: rec
    real_commit = db_session.commit

    def tracking_commit() -> None:
        events.append("commit")
        real_commit()

    monkeypatch.setattr(db_session, "commit", tracking_commit)
    assert _invite(finance_client).status_code == 201
    assert events == ["commit", "send"]
    # A refused (rolled back) invitation sends nothing.
    events.clear()
    assert _invite(finance_client, email=users_a["manager"].email).status_code == 409
    assert "send" not in events


def test_no_token_link_or_password_in_logs_audit_or_errors(
    app: Any,
    finance_client: TestClient,
    console_mailer: RecordingMailer,
    users_a: dict[str, User],
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    anon = TestClient(app, raise_server_exceptions=False)
    secrets_seen: list[str] = [NEW_PASSWORD]
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        invite = _invite(finance_client)
        token = TOKEN_RE.search(invite.json()["invite_url"]).group(1)  # type: ignore[union-attr]
        secrets_seen += [token, invite.json()["invite_url"]]
        bodies.append(anon.get(f"{API}/auth/invite/{token}x").text)  # a 404 is logged
        bodies.append(
            anon.post(f"{API}/auth/accept-invite", json={"token": token, "password": "weak"}).text
        )
        anon.post(f"{API}/auth/accept-invite", json={"token": token, "password": NEW_PASSWORD})
        _forgot(anon, users_a["employee"].email)
        reset = _token_of(console_mailer.sent[-1])
        secrets_seen.append(reset)
        bodies.append(_reset(anon, reset + "x").text)
        _reset(anon, reset)
    # Server-side records only: the TEST client's own httpx logger prints the
    # URLs it requests (that is the browser's side of the wire, not the app's).
    log_text = "\n".join(
        f"{r.name} {r.getMessage()}" for r in caplog.records if not r.name.startswith("httpx")
    )
    audit = json.dumps(
        [
            [a.action, a.before_state, a.after_state]
            for a in db_session.scalars(sa.select(AuditLog)).all()
        ],
        default=str,
    )
    for secret in secrets_seen:
        assert secret not in log_text, "secret in logs"
        assert secret not in audit, "secret in audit"
        for body in bodies:
            assert secret not in body, "secret in an error body"
    assert "/auth/invite/[redacted]" in log_text
    assert "email not delivered: provider=console" not in log_text  # recorder, not console
    hashes = db_session.scalars(sa.select(AuthToken.token_hash)).all()
    assert token not in hashes and reset not in hashes


def test_console_provider_logs_masked_lines_only(
    app: Any, finance_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """With the real console provider: the one masked line, never the link."""
    with caplog.at_level(logging.INFO):
        body = _invite(finance_client, email="secret.person@hidden-domain.test").json()
    assert "email not delivered: provider=console purpose=invite to=s***@h***.test" in caplog.text
    assert body["invite_url"].split("token=")[1] not in caplog.text
    assert "secret.person" not in caplog.text and "hidden-domain" not in caplog.text


def test_role_gate_on_invitations(
    employee_client: TestClient, manager_client: TestClient, mailer: RecordingMailer
) -> None:
    for client in (employee_client, manager_client):
        assert _invite(client).status_code == 403
    assert mailer.sent == []


def test_invited_user_is_counted_in_the_org_like_any_user(
    finance_client: TestClient, mailer: RecordingMailer, db_session: Session, org_a: Organization
) -> None:
    """An invitation is a real (inactive) user row in the admin's organisation."""
    _invite(finance_client, role="manager", department="Ops")
    row = db_session.scalars(sa.select(User).where(User.email == "new.hire@example.test")).one()
    assert row.organization_id == org_a.id and row.role == RoleKey.manager
    assert row.department == "Ops" and row.status == UserStatus.inactive


@pytest.fixture(autouse=True)
def _restore_overrides(app: Any) -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_email_provider, None)


# ---------------------------------------------------------------------------
# Review fix: login is no invitation oracle (same bcrypt work for placeholders)
# ---------------------------------------------------------------------------


def test_org_code_login_does_full_bcrypt_work_for_an_invited_placeholder(
    anon: TestClient,
    users_a: dict[str, User],
    db_session: Session,
    org_a: Organization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending (or revoked) invitee's row carries the non-bcrypt
    ``!unusable:`` marker.  bcrypt rejects it in microseconds, so verifying
    against it would make the 401 measurably faster than for an unknown email
    or a real account.  Every org-code login must verify a real ``$2b$`` hash."""
    from backend.app.modules.identity import service as identity_service
    from backend.app.modules.identity.password_policy import unusable_password_hash

    invited = f.make_user(
        db_session,
        org=org_a,
        email="pending.invitee@example.test",
        status=UserStatus.inactive,
        password_hash=unusable_password_hash(),
    )
    slug = _slug(db_session, invited)
    real_verify = identity_service.verify_password
    seen: list[str] = []

    def recording(password: str, hashed: str) -> bool:
        seen.append(hashed[:4])
        return real_verify(password, hashed)

    monkeypatch.setattr(identity_service, "verify_password", recording)
    for email in (invited.email, "nobody-at-all@example.test", users_a["employee"].email):
        seen.clear()
        resp = _login(anon, email, "Wrong-Passw0rd-123!", slug)
        assert resp.status_code == 401, email
        assert seen == ["$2b$"], (email, seen)
    # The substituted dummy hash must never let anyone in: even an ACTIVE row
    # with a placeholder hash (defence in depth) refuses the dummy's password.
    odd = f.make_user(
        db_session,
        org=org_a,
        email="active.placeholder@example.test",
        password_hash=unusable_password_hash(),
    )
    assert odd.status == UserStatus.active
    assert _login(anon, odd.email, "__atlas_dummy__", slug).status_code == 401
    assert _login(anon, invited.email, "__atlas_dummy__", slug).status_code == 401
