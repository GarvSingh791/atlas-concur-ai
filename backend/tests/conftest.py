"""Shared pytest foundation for Atlas backend tests (P7-T1 / P7-T2).

Architecture overview
─────────────────────
_schema  (session)
  └─ db_session (function)   — outer txn + create_savepoint Session
       └─ app (function)     — overrides get_db → db_session
            ├─ rbac_catalog  — seeds roles/perms inside the transaction
            ├─ org_a / org_b — two isolated tenant organisations
            ├─ users_a / users_b — role topology per org
            └─ employee_client / manager_client / finance_client / employee_client_b
                               — TestClient with real JWT bearer

IMPORTANT DEVIATIONS FROM TASK SPEC (sync vs async)
────────────────────────────────────────────────────
The codebase is synchronous (SQLAlchemy sync Session via psycopg2; no
pytest-asyncio; TestClient is the Starlette sync client). The spec was written
for an async stack. Two explicit adaptations:

1. join_transaction_mode="create_savepoint" (SQLAlchemy 2.0 sync recipe) is
   used instead of the async begin_nested() + after_transaction_end listener.
   It provides identical commit-safe rollback isolation. The listener form is
   redundant under SA 2.0 and is intentionally omitted.

2. httpx.AsyncClient is replaced with starlette.testclient.TestClient
   (matching all existing Atlas tests).

Identity strategy (confirmed with Garv): real-JWT role clients. The app fixture
does NOT globally override get_current_user; each client mints a real
create_access_token so the full JWT→DB user resolution path is exercised and
P7-T2 can test missing/expired/wrong-org tokens. An auth_as() hook is provided
for auth-agnostic test variants.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.database import get_db, get_engine
from backend.app.core.security import create_access_token, get_current_user
from backend.app.main_entry.app import app as fastapi_app
from database.models.identity import Organization, RoleKey, User
from database.seeds.identity_seed import seed_rbac_catalog
from services.embeddings_provider.factory import get_embeddings_provider
from services.embeddings_provider.local import LocalEmbeddingsProvider
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


# ---------------------------------------------------------------------------
# Session-scoped: pristine migrated schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Session-scoped autouse: ensure the test DB has a full Alembic schema.

    autouse=True so the schema is guaranteed before any test in backend/tests/,
    including the pre-existing module-scoped fixtures that call create_all on
    individual tables (which need FK targets to already exist).

    When Postgres is unreachable, yields None and returns without skipping —
    individual fixtures / tests that need DB call pytest.skip() themselves
    (db_session, db_engine in legacy tests, etc.).

    No teardown: the schema persists between sessions. Row-level isolation is
    handled by the function-scoped db_session (outer transaction + rollback).
    """
    engine = get_engine()
    try:
        with engine.connect() as probe:
            probe.execute(text("SELECT 1"))
    except Exception:
        yield None  # Not reachable; callers must skip themselves.
        return

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    cfg = Config(str(_ALEMBIC_INI))
    command.upgrade(cfg, "head")

    yield engine


# ---------------------------------------------------------------------------
# Function-scoped: commit-safe rollback session
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(_schema) -> Iterator[Session]:
    """Yield a SQLAlchemy Session wrapped in an outer transaction that is always
    rolled back — so even production code calling session.commit() leaves no
    trace after the test ends.

    SQLAlchemy 2.0 sync recipe: join_transaction_mode="create_savepoint" causes
    the Session to operate inside a SAVEPOINT chain rather than the outer
    transaction. Each session.commit() releases the current savepoint and opens
    a new one; the outer transaction never commits. trans.rollback() in teardown
    discards everything, including work that the code under test committed.
    """
    if _schema is None:
        pytest.skip("Postgres not reachable — skipping DB-backed tests")
    engine = _schema
    connection = engine.connect()
    trans = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Function-scoped: FastAPI app with get_db overridden
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(db_session: Session):
    """FastAPI app with get_db routed to the test session.

    Using the shared db_session means all request handlers, get_current_user's
    DB lookup, and RBAC's get_user_permissions all run in the same uncommitted
    transaction as the test factories — seeded-but-uncommitted rows are visible.

    get_current_user is NOT overridden here; identity is carried via real JWTs
    on each role client. Use auth_as() for the rare auth-agnostic variant.
    """

    def _override_get_db() -> Iterator[Session]:
        # Yield the SAME session the test owns; do NOT close it — db_session
        # fixture owns close() and rollback().
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# RBAC catalog
# ---------------------------------------------------------------------------


@pytest.fixture()
def rbac_catalog(db_session: Session) -> None:
    """Seed the roles/permissions/role_permissions reference data.

    seed_rbac_catalog() does an internal commit; under create_savepoint that
    commit releases a savepoint but the outer transaction rolls it back at test
    end — no permanent state leaks.

    Any route protected by require_permission()/require_role() needs this
    fixture (either directly or through a role-client fixture).
    """
    seed_rbac_catalog(db_session)


# ---------------------------------------------------------------------------
# Org / user topology — two isolated tenants
# ---------------------------------------------------------------------------


@pytest.fixture()
def org_a(db_session: Session) -> Organization:
    return f.make_org(db_session, name="Org Alpha")


@pytest.fixture()
def org_b(db_session: Session) -> Organization:
    return f.make_org(db_session, name="Org Beta")


@pytest.fixture()
def users_a(db_session: Session, org_a: Organization) -> dict[str, User]:
    """Three-role user set for org A.

    manager has one direct report (employee.manager_id = manager.id), which
    satisfies the spec requirement for a manager fixture with >=1 report.
    """
    manager = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    employee = f.make_user(
        db_session,
        org=org_a,
        role=RoleKey.employee,
        manager_id=manager.id,
    )
    finance = f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)
    return {"manager": manager, "employee": employee, "finance": finance}


@pytest.fixture()
def users_b(db_session: Session, org_b: Organization) -> dict[str, User]:
    """Two-role user set for org B — used in cross-tenant matrix tests."""
    employee = f.make_user(db_session, org=org_b, role=RoleKey.employee)
    finance = f.make_user(db_session, org=org_b, role=RoleKey.finance_admin)
    return {"employee": employee, "finance": finance}


# ---------------------------------------------------------------------------
# Role clients (real JWT, org-scoped)
# ---------------------------------------------------------------------------


def _bearer_client(application: Any, user: User) -> TestClient:
    """Return a TestClient carrying a real Bearer token for *user*."""
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(application, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def employee_client(app: Any, rbac_catalog: None, users_a: dict[str, User]) -> TestClient:
    """Org A employee client with real JWT."""
    return _bearer_client(app, users_a["employee"])


@pytest.fixture()
def manager_client(app: Any, rbac_catalog: None, users_a: dict[str, User]) -> TestClient:
    """Org A manager client with real JWT (has one direct report: users_a['employee'])."""
    return _bearer_client(app, users_a["manager"])


@pytest.fixture()
def finance_client(app: Any, rbac_catalog: None, users_a: dict[str, User]) -> TestClient:
    """Org A finance_admin client with real JWT."""
    return _bearer_client(app, users_a["finance"])


@pytest.fixture()
def employee_client_b(app: Any, rbac_catalog: None, users_b: dict[str, User]) -> TestClient:
    """Org B employee client — used for cross-tenant isolation probes."""
    return _bearer_client(app, users_b["employee"])


# ---------------------------------------------------------------------------
# Auth-as override hook (escape hatch for auth-agnostic tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_as(app: Any):
    """Yield a setter that overrides get_current_user to return a chosen ORM user.

    Usage::

        def test_something(auth_as, db_session):
            user = make_user(db_session)
            client = auth_as(user)
            resp = client.get("/api/v1/...")
    """

    def _set(user: User) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app, raise_server_exceptions=False)

    yield _set
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Deterministic AI wiring
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_ai(app: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force LocalLlmProvider and LocalEmbeddingsProvider for all AI paths.

    Covers two injection routes:
    1. Depends(get_llm_provider) / Depends(get_embeddings_provider) consumers
       via app.dependency_overrides.
    2. build_orchestrator()'s direct call to the module-imported get_llm_provider.
       Patched via monkeypatch on runtime's local binding + cache_clear().

    Default settings already select "local" providers, so this fixture is
    belt-and-suspenders against any settings drift and ensures determinism
    even when tests run against a non-default environment.
    """
    from backend.app.modules.ai import runtime

    # 1) Depends consumers
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    app.dependency_overrides[get_embeddings_provider] = lambda: LocalEmbeddingsProvider()

    # 2) Direct-call path inside build_orchestrator() — patch the imported symbol
    #    in runtime's own namespace and bust the lru_cache.
    monkeypatch.setattr(runtime, "get_llm_provider", lambda: LocalLlmProvider())
    runtime.build_orchestrator.cache_clear()

    yield

    runtime.build_orchestrator.cache_clear()
    # app fixture clears dependency_overrides in its own teardown
