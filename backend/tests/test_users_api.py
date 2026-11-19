"""P2-T5 Validate gate: GET /users and GET /users/{id} endpoints.

Assertions:
  (a) finance_admin → GET /users → 200, paginated, org-scoped, no password_hash
  (b) employee → GET /users → 403 standard envelope  (Validate gate)
  (c) manager → GET /users → 403 (lacks users.read)
  (d) finance_admin → GET /users/{in-org id} → 200
  (e) finance_admin → GET /users/{cross-org id} → 404 (not 403; no existence leak)
  (f) finance_admin → GET /users/{random uuid} → 404
  (g) employee → GET /users/{id} → 403 (permission denied, not scoping)
  (h) Pagination clamps; unknown sort/filter → 400; allow-listed sort/filter → work
  (i) Cross-tenant isolation: finance in org A never surfaces org B users

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.errors import ErrorCode
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
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

# ---------------------------------------------------------------------------
# Scoped tables for create/drop
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
]

# ---------------------------------------------------------------------------
# Unique email constants (isolated from other test modules)
# ---------------------------------------------------------------------------

_ORG_A_NAME = "Users API Test Org A"
_ORG_B_NAME = "Users API Test Org B"
_FINANCE_A_EMAIL = "users-finance-a@test.internal"
_MANAGER_A_EMAIL = "users-manager-a@test.internal"
_EMPLOYEE_A_EMAIL = "users-employee-a@test.internal"
_EXTRA_A_EMAIL = "users-extra-a@test.internal"
_FINANCE_B_EMAIL = "users-finance-b@test.internal"
_TEST_PW = "test-password-users-api"

# ---------------------------------------------------------------------------
# DB fixture
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
        pytest.skip("Postgres not reachable — skipping users API tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Seed fixture: two orgs, five users total
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed RBAC catalog + org A (4 users) + org B (1 user).  Cleans up on teardown."""
    factory = get_sessionmaker()

    all_emails = [
        _FINANCE_A_EMAIL,
        _MANAGER_A_EMAIL,
        _EMPLOYEE_A_EMAIL,
        _EXTRA_A_EMAIL,
        _FINANCE_B_EMAIL,
    ]

    # Prune stale data from a previous incomplete run.
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org_a = Organization(name=_ORG_A_NAME, base_currency="USD")
        org_b = Organization(name=_ORG_B_NAME, base_currency="USD")
        session.add_all([org_a, org_b])
        session.flush()

        hpw = hash_password(_TEST_PW)

        finance_a = User(
            organization_id=org_a.id,
            name="Finance Admin A",
            email=_FINANCE_A_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_a)
        session.flush()

        manager_a = User(
            organization_id=org_a.id,
            name="Manager A",
            email=_MANAGER_A_EMAIL,
            role=RoleKey.manager,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(manager_a)
        session.flush()

        employee_a = User(
            organization_id=org_a.id,
            name="Employee A",
            email=_EMPLOYEE_A_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager_a.id,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(employee_a)

        extra_a = User(
            organization_id=org_a.id,
            name="Extra A",
            email=_EXTRA_A_EMAIL,
            role=RoleKey.employee,
            department="HR",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(extra_a)

        finance_b = User(
            organization_id=org_b.id,
            name="Finance Admin B",
            email=_FINANCE_B_EMAIL,
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(finance_b)
        session.commit()

    yield {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "finance_a_id": finance_a.id,
        "manager_a_id": manager_a.id,
        "employee_a_id": employee_a.id,
        "extra_a_id": extra_a.id,
        "finance_b_id": finance_b.id,
    }

    # Teardown
    with factory() as session:
        session.query(User).filter(User.organization_id.in_([org_a.id, org_b.id])).delete()
        session.query(Organization).filter(Organization.id.in_([org_a.id, org_b.id])).delete()
        session.commit()


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: mint a bearer token (login tested in P2-T3)
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# (a) finance_admin → GET /users → 200, org-scoped, no password_hash
# ---------------------------------------------------------------------------


class TestListUsers:
    def test_finance_200_org_scoped(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance gets 200; all returned users belong to org A; no password_hash."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 200
        body = r.json()
        # Standard page envelope
        assert set(body.keys()) >= {"items", "total", "page", "size", "pages"}
        assert body["total"] >= 4  # at least 4 users seeded in org A
        items = body["items"]
        assert len(items) > 0
        for item in items:
            # Org isolation: every item belongs to org A
            assert str(item["organization_id"]) == str(seeded["org_a_id"])
            # Safe schema: password_hash must never appear
            assert "password_hash" not in item
            assert "password" not in item
            # password_hash must not appear anywhere in the raw JSON either
        raw = r.text
        assert "password_hash" not in raw

    def test_finance_response_has_required_fields(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Each item has the expected safe fields."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 200
        item = r.json()["items"][0]
        expected_fields = {
            "id",
            "name",
            "email",
            "role",
            "department",
            "manager_id",
            "status",
            "organization_id",
            "created_at",
        }
        assert expected_fields.issubset(item.keys())
        assert "password_hash" not in item
        assert "preferences" not in item

    # (b) Validate gate: employee → 403
    def test_employee_403(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee lacks users.read → 403 standard envelope."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == ErrorCode.FORBIDDEN
        assert "correlation_id" in body["error"]

    # (c) manager → 403
    def test_manager_403(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager lacks users.read → 403."""
        headers = _bearer(seeded["manager_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_no_token_401(self, client: TestClient) -> None:
        """No token → 401."""
        r = client.get("/api/v1/users")
        assert r.status_code == 401

    # (h) Pagination
    def test_pagination_size_1(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?size=1 returns exactly 1 item; total reflects the full org count."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?page=1&size=1", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        assert body["size"] == 1
        assert body["total"] >= 4

    def test_pagination_page_2(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Second page with size=2 returns next slice."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r1 = client.get("/api/v1/users?page=1&size=2", headers=headers)
        r2 = client.get("/api/v1/users?page=2&size=2", headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        ids_p1 = {i["id"] for i in r1.json()["items"]}
        ids_p2 = {i["id"] for i in r2.json()["items"]}
        # Pages must not overlap
        assert ids_p1.isdisjoint(ids_p2)

    def test_out_of_range_size_clamped(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?size=99999 is clamped to MAX_PAGE_SIZE, not rejected."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?size=99999", headers=headers)
        assert r.status_code == 200
        assert r.json()["size"] <= 100  # MAX_PAGE_SIZE

    # (h) Sort
    def test_sort_name_asc(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?sort=name:asc returns results; no error."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?sort=name:asc", headers=headers)
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert names == sorted(names)

    def test_sort_unknown_field_400(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?sort=password_hash:asc → 400 (not in allow-list)."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?sort=password_hash:asc", headers=headers)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST

    # (h) Filter
    def test_filter_role_employee(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?role=employee returns only employees, all in org A."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?role=employee", headers=headers)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 2  # employee_a and extra_a
        for item in items:
            assert item["role"] == "employee"
            assert str(item["organization_id"]) == str(seeded["org_a_id"])

    def test_filter_unknown_field_400(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?password_hash=anything → 400 (not in allow-list)."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?password_hash=anything", headers=headers)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST

    def test_filter_invalid_role_value_400(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """?role=superadmin (unknown enum value) → 400."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get("/api/v1/users?role=superadmin", headers=headers)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST

    # (i) Cross-tenant isolation
    def test_cross_tenant_isolation(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance in org A cannot retrieve any org B user via list endpoint."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        # Fetch all pages and ensure org B finance admin never appears
        page = 1
        org_b_id = str(seeded["org_b_id"])
        while True:
            r = client.get(f"/api/v1/users?page={page}&size=10", headers=headers)
            assert r.status_code == 200
            body = r.json()
            for item in body["items"]:
                assert (
                    str(item["organization_id"]) != org_b_id
                ), f"Org B user leaked into org A finance_admin response: {item}"
            if page >= body["pages"] or body["pages"] == 0:
                break
            page += 1


# ---------------------------------------------------------------------------
# (d/e/f/g) GET /users/{id}
# ---------------------------------------------------------------------------


class TestGetUser:
    def test_finance_in_org_user_200(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance can fetch any in-org user by id → 200."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/users/{seeded['employee_a_id']}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert str(body["id"]) == str(seeded["employee_a_id"])
        assert str(body["organization_id"]) == str(seeded["org_a_id"])
        assert "password_hash" not in body
        assert "password_hash" not in r.text

    def test_finance_cross_org_user_404(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance in org A fetching org B user → 404 (not 403; no existence leak)."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/users/{seeded['finance_b_id']}", headers=headers)
        assert r.status_code == 404
        body = r.json()
        assert body["error"]["code"] == ErrorCode.NOT_FOUND

    def test_finance_nonexistent_id_404(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """A completely nonexistent UUID → 404."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        random_id = uuid.uuid4()
        r = client.get(f"/api/v1/users/{random_id}", headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ErrorCode.NOT_FOUND

    def test_employee_forbidden_403(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Employee lacks users.read → 403 on detail endpoint too."""
        headers = _bearer(seeded["employee_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/users/{seeded['finance_a_id']}", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_manager_forbidden_403(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Manager lacks users.read → 403."""
        headers = _bearer(seeded["manager_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/users/{seeded['employee_a_id']}", headers=headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN

    def test_no_token_401(self, client: TestClient) -> None:
        """No token → 401."""
        r = client.get(f"/api/v1/users/{uuid.uuid4()}")
        assert r.status_code == 401

    def test_finance_own_record_200(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Finance admin can fetch their own record by id."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/users/{seeded['finance_a_id']}", headers=headers)
        assert r.status_code == 200
        assert str(r.json()["id"]) == str(seeded["finance_a_id"])

    def test_cross_org_and_nonexistent_return_same_code(
        self, db_engine: Engine, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Out-of-scope and nonexistent both return 404 with NOT_FOUND code (no existence leak)."""
        headers = _bearer(seeded["finance_a_id"], seeded["org_a_id"])
        r_cross = client.get(f"/api/v1/users/{seeded['finance_b_id']}", headers=headers)
        r_ghost = client.get(f"/api/v1/users/{uuid.uuid4()}", headers=headers)
        assert r_cross.status_code == 404
        assert r_ghost.status_code == 404
        assert r_cross.json()["error"]["code"] == ErrorCode.NOT_FOUND
        assert r_ghost.json()["error"]["code"] == ErrorCode.NOT_FOUND
