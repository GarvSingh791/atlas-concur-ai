"""P2-T4 Validate gate (part 1): RBAC dependency tests.

Tests require_permission / require_role on throwaway FastAPI routes.
Live-DB tests self-skip when Postgres is unreachable.

Assertions:
  (a) User WITH required permission → 200
  (b) User WITHOUT required permission → 403 standard envelope (code FORBIDDEN)
  (c) No token at all → 401 standard envelope (code UNAUTHORIZED)
      This case is HERMETIC — it does NOT use the db_engine fixture so it runs
      even when Postgres is unreachable.  Reasoning: get_db builds a lazy Session
      (no network connection until first query); get_current_user raises
      UnauthorizedError before any query executes when credentials is None.
  (d) Forged token: access token minted for an employee with a phantom
      "role": "finance_admin" claim in the payload does NOT escalate —
      permissions are resolved from the DB user, not from the token.
  (e) require_role: correct role → 200; wrong role → 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import Table
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.core.correlation import CorrelationIDMiddleware
from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.errors import (
    AppError,
    ErrorCode,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from backend.app.core.rbac import require_permission, require_role
from backend.app.core.security import create_access_token, hash_password
from config.constants.permissions import (
    EXPENSE_READ_OWN,
    POLICY_MANAGE,
)
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
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

_settings = get_settings()

# ---------------------------------------------------------------------------
# Throwaway app wired with the real error handlers
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
]


def _build_app() -> FastAPI:
    """Minimal FastAPI app mirroring create_app's error wiring + throwaway RBAC routes."""
    application = FastAPI()
    application.add_middleware(CorrelationIDMiddleware)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]

    @application.get("/protected/expense-read")
    def _expense_read(
        _user: Annotated[Any, Depends(require_permission(EXPENSE_READ_OWN))],
    ) -> dict[str, str]:
        return {"ok": "true"}

    @application.get("/protected/policy-manage")
    def _policy_manage(
        _user: Annotated[Any, Depends(require_permission(POLICY_MANAGE))],
    ) -> dict[str, str]:
        return {"ok": "true"}

    @application.get("/protected/manager-only")
    def _manager_only(
        _user: Annotated[Any, Depends(require_role(RoleKey.manager))],
    ) -> dict[str, str]:
        return {"ok": "true"}

    return application


# ---------------------------------------------------------------------------
# DB fixture: create tables, seed RBAC catalog, seed test users
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping RBAC tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


# Test org + user emails (distinct from demo seed to avoid collisions)
_RBAC_ORG = "RBAC Test Org"
_EMPLOYEE_EMAIL = "rbac-employee@test.internal"
_MANAGER_EMAIL = "rbac-manager@test.internal"
_FINANCE_EMAIL = "rbac-finance@test.internal"
_TEST_PW = "test-password-rbac"


@pytest.fixture(scope="module")
def seeded_users(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed RBAC catalog + test org + three users.  Cleans up on teardown."""
    factory = get_sessionmaker()

    # Prune any stale data from a previous incomplete run.
    with factory() as session:
        stale = (
            session.query(User)
            .filter(User.email.in_([_EMPLOYEE_EMAIL, _MANAGER_EMAIL, _FINANCE_EMAIL]))
            .all()
        )
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        # Seed the authoritative RBAC catalog (roles / permissions / role_permissions).
        seed_rbac_catalog(session)

        org = Organization(name=_RBAC_ORG, base_currency="USD")
        session.add(org)
        session.flush()

        hpw = hash_password(_TEST_PW)

        mgr = User(
            organization_id=org.id,
            name="RBAC Manager",
            email=_MANAGER_EMAIL,
            role=RoleKey.manager,
            department="RBAC",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(mgr)
        session.flush()

        emp = User(
            organization_id=org.id,
            name="RBAC Employee",
            email=_EMPLOYEE_EMAIL,
            role=RoleKey.employee,
            department="RBAC",
            manager_id=mgr.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(emp)

        fin = User(
            organization_id=org.id,
            name="RBAC Finance",
            email=_FINANCE_EMAIL,
            role=RoleKey.finance_admin,
            department="RBAC",
            manager_id=mgr.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(fin)
        session.commit()

    yield {
        "org_id": org.id,
        "employee_id": emp.id,
        "manager_id": mgr.id,
        "finance_id": fin.id,
    }

    # Teardown
    with factory() as session:
        session.query(User).filter(User.organization_id == org.id).delete()
        session.query(Organization).filter(Organization.id == org.id).delete()
        session.commit()


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: mint a token directly (isolates authz; login is tested in P2-T3)
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestRequirePermission:
    def test_employee_with_expense_read_own_allowed(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        """Employee has expense.read.own → 200."""
        headers = _bearer(seeded_users["employee_id"], seeded_users["org_id"])
        r = client.get("/protected/expense-read", headers=headers)
        assert r.status_code == 200

    def test_employee_missing_policy_manage_forbidden(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        """Employee lacks policy.manage → 403 with standard envelope."""
        headers = _bearer(seeded_users["employee_id"], seeded_users["org_id"])
        r = client.get("/protected/policy-manage", headers=headers)
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == ErrorCode.FORBIDDEN
        assert "correlation_id" in body["error"]

    def test_finance_with_policy_manage_allowed(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        """Finance has policy.manage → 200."""
        headers = _bearer(seeded_users["finance_id"], seeded_users["org_id"])
        r = client.get("/protected/policy-manage", headers=headers)
        assert r.status_code == 200

    def test_manager_has_policy_read(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        """Manager has policy.read (expense.read.own route used as a proxy)."""
        headers = _bearer(seeded_users["manager_id"], seeded_users["org_id"])
        r = client.get("/protected/expense-read", headers=headers)
        assert r.status_code == 200

    def test_no_token_returns_401(self, client: TestClient) -> None:
        """No token → 401 (UNAUTHORIZED).

        This test is intentionally NOT parameterized with db_engine — it runs
        hermetically even when Postgres is unreachable.  get_db builds a lazy
        Session (no network connection); get_current_user raises UnauthorizedError
        before any DB query executes.
        """
        r = client.get("/protected/expense-read")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == ErrorCode.UNAUTHORIZED

    def test_forged_role_claim_does_not_escalate(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        """A token with a forged 'role' claim does NOT grant finance_admin perms.

        We mint a token whose payload has an extra 'role' claim set to
        'finance_admin', but the user is employee in the DB.  The RBAC dep
        resolves permissions from the DB-loaded user, not from the token, so
        the request is correctly denied with 403.
        """
        from datetime import datetime, timedelta  # noqa: PLC0415

        now = datetime.now(tz=__import__("datetime").timezone.utc)
        payload = {
            "sub": str(seeded_users["employee_id"]),
            "org": str(seeded_users["org_id"]),
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "role": "finance_admin",  # forged — should be ignored
        }
        forged_token = jwt.encode(payload, _settings.JWT_SECRET, algorithm=_settings.JWT_ALGORITHM)
        headers = {"Authorization": f"Bearer {forged_token}"}
        r = client.get("/protected/policy-manage", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN


class TestRequireRole:
    def test_manager_role_allowed(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded_users["manager_id"], seeded_users["org_id"])
        r = client.get("/protected/manager-only", headers=headers)
        assert r.status_code == 200

    def test_employee_role_forbidden(
        self, db_engine: Engine, seeded_users: dict[str, Any], client: TestClient
    ) -> None:
        headers = _bearer(seeded_users["employee_id"], seeded_users["org_id"])
        r = client.get("/protected/manager-only", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_no_token_returns_401_for_role_route(self, client: TestClient) -> None:
        """No token → 401, not 403 (get_current_user raises before require_role)."""
        r = client.get("/protected/manager-only")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == ErrorCode.UNAUTHORIZED
