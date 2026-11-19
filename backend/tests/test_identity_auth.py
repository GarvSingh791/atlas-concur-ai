"""P2-T3 Validate gate: identity auth endpoints — login, refresh, /me.

NOT hermetic — needs a running Postgres; self-skips when unreachable.
Creates the identity tables (organizations, users, roles, permissions,
role_permissions) via scoped create_all + drops on teardown.  Does NOT run
alembic upgrade (the migration is verified manually); uses the ORM directly.

Assertions:
  (a) Login with valid creds → 200 with access + refresh tokens of correct shape
  (b) GET /me with a valid access token → 200, correct user, NO password_hash
  (c) Wrong password and unknown email → identical 401 standard envelope
  (d) GET /me with: no token, malformed, expired, refresh token → 401
  (e) POST /auth/refresh with valid refresh → new access token
      POST /auth/refresh with an access token (wrong type) → 401
  (f) Inactive user cannot authenticate
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timezone
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Table
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import hash_password
from backend.app.main_entry.app import app
from config.settings import get_settings
from database.models import Base
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)

pytestmark = pytest.mark.db

_settings = get_settings()

# Table references for scoped create/drop
_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
]


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield engine if Postgres is reachable, else skip; create/drop scoped tables.

    On a DB where the migration has already run, create_all is a no-op and
    teardown skips those pre-existing tables to avoid dropping production schema.
    """
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping identity auth tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


_SEED_PW = "TestP@ss1234"
_SEED_EMAIL = "alice@test.internal"
_SEED_INACTIVE_EMAIL = "inactive@test.internal"


@pytest.fixture(scope="module")
def seeded_user(db_engine: Engine) -> Iterator[dict]:
    """Insert one org + one active user + one inactive user; clean up on teardown.

    Prunes any stale rows left by previous runs (e.g. if teardown failed) before
    inserting, and deletes inserted rows on teardown.  Safe to run repeatedly
    against a persistent DB.
    """
    factory = get_sessionmaker()

    # Prune stale test data from any previous run that didn't clean up.
    with factory() as session:
        stale_users = (
            session.query(User).filter(User.email.in_([_SEED_EMAIL, _SEED_INACTIVE_EMAIL])).all()
        )
        stale_org_ids = {u.organization_id for u in stale_users}
        if stale_org_ids:
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        org = Organization(name="Auth Test Org", base_currency="USD")
        session.add(org)
        session.flush()
        org_id = org.id

        active_user = User(
            organization_id=org.id,
            name="Alice Test",
            email=_SEED_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            status=UserStatus.active,
            password_hash=hash_password(_SEED_PW),
        )
        session.add(active_user)

        inactive_user = User(
            organization_id=org.id,
            name="Inactive User",
            email=_SEED_INACTIVE_EMAIL,
            role=RoleKey.employee,
            department=None,
            status=UserStatus.inactive,
            password_hash=hash_password(_SEED_PW),
        )
        session.add(inactive_user)
        session.commit()

    result = {
        "org_id": org_id,
        "user_id": active_user.id,
        "email": _SEED_EMAIL,
        "password": _SEED_PW,
    }

    yield result

    # Teardown: remove inserted test data.
    with factory() as session:
        session.query(User).filter(User.organization_id == org_id).delete()
        session.query(Organization).filter(Organization.id == org_id).delete()
        session.commit()


@pytest.fixture(scope="module")
def client() -> TestClient:
    """TestClient WITHOUT lifespan context (no DB connectivity check at startup)."""
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _login(client: TestClient, email: str, password: str) -> Response:
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# (a) Login with valid credentials
# ---------------------------------------------------------------------------


def test_login_valid_returns_tokens(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    r = _login(client, seeded_user["email"], seeded_user["password"])
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and len(body["access_token"]) > 20
    assert isinstance(body["refresh_token"], str) and len(body["refresh_token"]) > 20
    # Tokens must be distinct
    assert body["access_token"] != body["refresh_token"]


# ---------------------------------------------------------------------------
# (b) GET /me returns correct user, no password_hash
# ---------------------------------------------------------------------------


def test_me_returns_user_no_hash(db_engine: Engine, seeded_user: dict, client: TestClient) -> None:
    tokens = _login(client, seeded_user["email"], seeded_user["password"]).json()
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == seeded_user["email"]
    assert body["name"] == "Alice Test"
    assert body["role"] == "employee"
    assert str(body["organization_id"]) == str(seeded_user["org_id"])
    # password_hash must never appear
    assert "password_hash" not in body
    assert "password" not in body


# ---------------------------------------------------------------------------
# (c) Wrong password and unknown email → identical 401 envelopes (no enum)
# ---------------------------------------------------------------------------


def test_wrong_password_returns_401(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    r = _login(client, seeded_user["email"], "wrong-password")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert "message" in body["error"]
    assert "correlation_id" in body["error"]


def test_unknown_email_returns_identical_401(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    r = _login(client, "nobody@nowhere.invalid", "any-password")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    # Same envelope shape and code — no leakage about email existence
    assert "message" in body["error"]
    assert "correlation_id" in body["error"]


def test_wrong_and_unknown_return_same_code(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    r_wrong_pw = _login(client, seeded_user["email"], "bad")
    r_unknown = _login(client, "ghost@nowhere.invalid", "bad")
    assert r_wrong_pw.json()["error"]["code"] == r_unknown.json()["error"]["code"]
    assert r_wrong_pw.json()["error"]["message"] == r_unknown.json()["error"]["message"]


# ---------------------------------------------------------------------------
# (d) GET /me with missing / malformed / refresh / expired token → 401
# ---------------------------------------------------------------------------


def test_me_no_token_returns_401(db_engine: Engine, client: TestClient) -> None:
    r = client.get("/api/v1/me")
    assert r.status_code == 401


def test_me_malformed_token_returns_401(db_engine: Engine, client: TestClient) -> None:
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_me_refresh_token_rejected(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    tokens = _login(client, seeded_user["email"], seeded_user["password"]).json()
    r = client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert r.status_code == 401


def test_me_expired_token_returns_401(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    # Craft a token with a past expiry using jose directly
    from datetime import datetime, timedelta

    from jose import jwt as jose_jwt

    expired_payload = {
        "sub": str(seeded_user["user_id"]),
        "org": str(seeded_user["org_id"]),
        "type": "access",
        "iat": datetime.now(tz=timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(tz=timezone.utc) - timedelta(hours=1),
    }
    expired_token = jose_jwt.encode(
        expired_payload, _settings.JWT_SECRET, algorithm=_settings.JWT_ALGORITHM
    )
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# (e) POST /auth/refresh
# ---------------------------------------------------------------------------


def test_refresh_valid_returns_new_access_token(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    tokens = _login(client, seeded_user["email"], seeded_user["password"]).json()
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


def test_refresh_with_access_token_rejected(
    db_engine: Engine, seeded_user: dict, client: TestClient
) -> None:
    tokens = _login(client, seeded_user["email"], seeded_user["password"]).json()
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401


def test_refresh_with_malformed_token_rejected(db_engine: Engine, client: TestClient) -> None:
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": "not.a.token"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# (f) Inactive user cannot authenticate
# ---------------------------------------------------------------------------


def test_inactive_user_cannot_login(db_engine: Engine, client: TestClient) -> None:
    r = _login(client, _SEED_INACTIVE_EMAIL, _SEED_PW)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
