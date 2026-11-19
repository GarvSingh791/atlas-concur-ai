"""#3b — GET /api/v1/audit HTTP contract (hermetic — NO Postgres).

Exercises the REAL router wiring (require_permission gate, paginated dep, sort
Query, _PAGINATION_KEYS strip, Page[AuditLogItem] envelope) against the real
FastAPI app, with get_db + get_current_user overridden and the audit query
backed by an in-process fake session. No database is touched.

Coverage:
  - finance_admin (audit.read) → 200 with a well-formed Page envelope
  - employee (no audit.read) → 403 standard envelope
  - manager (approval perms, no audit.read) → 403 standard envelope
  - the audit query is org-scoped (organization_id predicate in the WHERE clause)
  - read-only: POST/PATCH/DELETE on /audit → 405 (no write routes exist)
  - actor_id / action / entity_type / entity_id filters reach the WHERE clause
    (asserted against the WHERE clause only: the SELECT list names every column)
  - sort happy path (created_at:desc, created_at:asc, -created_at) is accepted;
    -created_at is parsed into an explicit desc spec, not the default fallback
  - invalid sort direction → 400
  - unknown filter field → 400 (deny-by-default via parse_filters)
  - unknown sort field → 400 (deny-by-default via parse_sort)
  - default order is created_at desc when no sort is supplied
  - actor_name is resolved server-side; null for system-initiated rows
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.core.database import get_db
from backend.app.core.errors import ErrorCode
from backend.app.core.pagination import parse_sort
from backend.app.core.security import get_current_user
from backend.app.main_entry.app import app as real_app
from backend.app.modules.audit import service as audit_service
from config.constants.permissions import (
    APPROVAL_DECIDE_REPORTS,
    APPROVAL_READ,
    AUDIT_READ,
    EXPENSE_READ_OWN,
    EXPENSE_READ_REPORTS,
)
from database.models.audit_log import AuditLog
from database.models.identity import RoleKey, User

_ORG_ID = uuid.uuid4()
_FINANCE_ID = uuid.uuid4()
_EMPLOYEE_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Lightweight stand-ins (no DB rows)
# ---------------------------------------------------------------------------


class _FakeUser:
    """Minimal duck-typed User for get_current_user / RBAC / service scoping."""

    def __init__(self, user_id: uuid.UUID, role: RoleKey) -> None:
        self.id = user_id
        self.organization_id = _ORG_ID
        self.role = role
        self.name = "Test User"


def _audit_row(
    *,
    action: str = "approval.decided",
    entity_type: str = "approval",
    actor_id: uuid.UUID | None = _ACTOR_ID,
) -> AuditLog:
    """An in-memory AuditLog instance (never added to a session)."""
    row = AuditLog(
        organization_id=_ORG_ID,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=uuid.uuid4(),
        before_state={"current_status": "pending"},
        after_state={"current_status": "approved"},
    )
    row.id = uuid.uuid4()
    row.created_at = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    return row


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class _FakeSession:
    """Routes the two queries the request makes:

      1. RBAC get_user_permissions → session.scalars(select(Permission.key)) → perms
      2. service.list_audit count  → session.scalar(select(count())) → row count
      3. service.list_audit rows   → session.scalars(select(AuditLog)...) → rows
      4. actor-name resolution     → session.scalars(select(User)...) → actor users

    The query intent is inferred from the SELECT's first column entity so the
    real router + service + RBAC code run unchanged against an in-memory store.
    """

    def __init__(self, permissions: set[str], rows: list[AuditLog]) -> None:
        self._permissions = permissions
        self._rows = rows
        self.captured: dict[str, Any] = {}

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _selected_entity(stmt: Any) -> Any:
        try:
            return stmt.column_descriptions[0]["entity"]
        except Exception:  # pragma: no cover - defensive
            return None

    # --- SQLAlchemy Session surface used by the code under test ------------

    def scalars(self, stmt: Any) -> _ScalarsResult:
        entity = self._selected_entity(stmt)
        if entity is AuditLog:
            # Capture the compiled SQL so tests can assert filters/order reached
            # the query layer without a real DB round-trip.
            self.captured["audit_sql"] = str(
                stmt.compile(compile_kwargs={"literal_binds": False})
            ).lower()
            return _ScalarsResult(self._rows)
        if entity is User:
            actor_ids = {r.actor_id for r in self._rows if r.actor_id is not None}
            users = [_FakeUser(aid, RoleKey.employee) for aid in actor_ids]
            return _ScalarsResult(users)
        # RBAC get_user_permissions selects Permission.key (a column, not entity).
        return _ScalarsResult(list(self._permissions))

    def scalar(self, stmt: Any) -> Any:
        # The only scalar() call in the path is the COUNT subquery.
        return len(self._rows)


# ---------------------------------------------------------------------------
# Fixtures: real app, deps overridden, no Postgres
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_client():
    created: list[TestClient] = []

    def _build(*, role: RoleKey, permissions: set[str], rows: list[AuditLog]) -> TestClient:
        user = _FakeUser(_FINANCE_ID if role == RoleKey.finance_admin else _EMPLOYEE_ID, role)
        session = _FakeSession(permissions, rows)

        def _override_get_db() -> Iterator[_FakeSession]:
            yield session

        real_app.dependency_overrides[get_db] = _override_get_db
        real_app.dependency_overrides[get_current_user] = lambda: user
        client = TestClient(real_app, raise_server_exceptions=False)
        client._fake_session = session  # type: ignore[attr-defined]
        created.append(client)
        return client

    yield _build
    real_app.dependency_overrides.clear()


def _finance_client(make_client, rows: list[AuditLog] | None = None) -> TestClient:
    return make_client(
        role=RoleKey.finance_admin,
        permissions={AUDIT_READ},
        rows=rows if rows is not None else [_audit_row()],
    )


def _employee_client(make_client) -> TestClient:
    return make_client(role=RoleKey.employee, permissions=set(), rows=[_audit_row()])


def _where_clause(sql: str) -> str:
    """Return only the WHERE clause of a captured (lower-cased) audit SELECT.

    ``select(AuditLog)`` names every column in its SELECT list, so asserting a
    column name against the whole statement is vacuous. Filter/scope assertions
    must look at the predicate text between ``where`` and ``order by`` only.
    """
    match = re.search(r"\swhere\s(.*?)(?:\sorder by\s|\slimit\s|$)", sql, re.DOTALL)
    assert match is not None, f"audit query has no WHERE clause: {sql}"
    return match.group(1)


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_finance_admin_gets_200_page_envelope(make_client) -> None:
    rows = [_audit_row(action="approval.decided"), _audit_row(action="trip.created")]
    client = _finance_client(make_client, rows=rows)
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Standard Page envelope.
    assert set(body.keys()) == {"items", "total", "page", "size", "pages"}
    assert body["total"] == 2
    assert body["page"] == 1
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert set(item.keys()) == {
        "id",
        "action",
        "entity_type",
        "entity_id",
        "actor_id",
        "actor_name",
        "before_state",
        "after_state",
        "created_at",
    }


def test_employee_forbidden(make_client) -> None:
    client = _employee_client(make_client)
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN


def test_manager_forbidden(make_client) -> None:
    # Manager holds approval/expense-report perms but NOT audit.read.
    client = make_client(
        role=RoleKey.manager,
        permissions={
            APPROVAL_READ,
            APPROVAL_DECIDE_REPORTS,
            EXPENSE_READ_OWN,
            EXPENSE_READ_REPORTS,
        },
        rows=[_audit_row()],
    )
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == ErrorCode.FORBIDDEN
    # The gate fires before the service: the audit query never ran.
    assert "audit_sql" not in client._fake_session.captured  # type: ignore[attr-defined]


def test_query_is_org_scoped(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "audit_log.organization_id = " in _where_clause(sql)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_write_methods_not_allowed(make_client, method: str) -> None:
    # The audit log is append-only via record_audit; the API is read-only.
    client = _finance_client(make_client)
    r = client.request(method.upper(), "/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 405, r.text
    assert "audit_sql" not in client._fake_session.captured  # type: ignore[attr-defined]


def test_unauthenticated_is_401() -> None:
    # No get_current_user override → real dependency rejects the missing token.
    def _override_get_db() -> Iterator[_FakeSession]:
        yield _FakeSession(set(), [])

    real_app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(real_app, raise_server_exceptions=False)
        r = client.get("/api/v1/audit")
        assert r.status_code == 401, r.text
    finally:
        real_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Content + actor-name resolution
# ---------------------------------------------------------------------------


def test_actor_name_resolved_and_null_for_system(make_client) -> None:
    rows = [_audit_row(actor_id=_ACTOR_ID), _audit_row(actor_id=None)]
    client = _finance_client(make_client, rows=rows)
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    by_actor = {i["actor_id"]: i for i in r.json()["items"]}
    assert by_actor[str(_ACTOR_ID)]["actor_name"] == "Test User"
    assert by_actor[None]["actor_name"] is None


# ---------------------------------------------------------------------------
# Filter / sort happy paths + deny-by-default
# ---------------------------------------------------------------------------


def test_filter_by_action_reaches_query(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?action=approval.decided", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "audit_log.action = " in _where_clause(sql)


def test_filter_by_entity_type_and_entity_id(make_client) -> None:
    client = _finance_client(make_client)
    entity_id = uuid.uuid4()
    r = client.get(
        f"/api/v1/audit?entity_type=approval&entity_id={entity_id}",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    where = _where_clause(sql)
    assert "audit_log.entity_type = " in where
    assert "audit_log.entity_id = " in where


def test_filter_by_actor_id_reaches_query(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get(f"/api/v1/audit?actor_id={_ACTOR_ID}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    where = _where_clause(sql)
    assert "audit_log.actor_id = " in where
    # The filter narrows within the org scope; it never replaces it.
    assert "audit_log.organization_id = " in where


def test_sort_created_at_desc_accepted(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?sort=created_at:desc", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "order by" in sql
    assert "audit_log.created_at desc" in sql


def _spy_parse_sort(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, str]]]:
    """Record every sort spec the audit service parses (the SQL alone cannot
    distinguish an explicit ``created_at desc`` from the default order)."""
    seen: list[list[tuple[str, str]]] = []

    def _spy(raw: str | None, allowed: set[str]) -> list[tuple[str, str]]:
        result = parse_sort(raw, allowed)
        seen.append(result)
        return result

    monkeypatch.setattr(audit_service, "parse_sort", _spy)
    return seen


def test_dash_prefix_sort_accepted(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_parse_sort(monkeypatch)
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?sort=-created_at", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    # '-created_at' is parsed into an explicit descending spec, not dropped to
    # the default-order fallback (which would yield an empty spec).
    assert seen == [[("created_at", "desc")]]
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "audit_log.created_at desc" in sql


def test_ascending_sort_accepted(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?sort=created_at:asc", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "audit_log.created_at asc" in sql


def test_default_order_is_created_at_desc(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_parse_sort(monkeypatch)
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    assert seen == [[]]  # no sort supplied -> service applies its own default
    sql = client._fake_session.captured["audit_sql"]  # type: ignore[attr-defined]
    assert "audit_log.created_at desc" in sql


def test_unknown_filter_field_rejected(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?bogus=1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST


def test_unknown_sort_field_rejected(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?sort=entity_type:asc", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST


def test_invalid_sort_direction_is_400(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?sort=created_at:sideways", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST


def test_invalid_entity_id_filter_is_400(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?entity_id=nope", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST


def test_invalid_actor_id_filter_is_400(make_client) -> None:
    client = _finance_client(make_client)
    r = client.get("/api/v1/audit?actor_id=not-a-uuid", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == ErrorCode.BAD_REQUEST
