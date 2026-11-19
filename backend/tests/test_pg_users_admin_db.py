"""Product gap 3 — user administration (POST/PATCH /users, deactivate/reactivate).

Covers every safeguard with a test that fails if the guard is removed:
finance-only (403), org scope (404), self-deactivate / self-demote, the last
active finance admin, self-manager, manager cycles, manager validity, managers
with active reports, per-org email uniqueness (migration 0022), password strength (never echoed),
audit rows without the password, and deactivation killing login + existing
access / refresh tokens.

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import sqlalchemy as sa
from jose import jwt
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token, create_refresh_token
from database.models.audit_log import AuditLog
from database.models.identity import Organization, RoleKey, User, UserStatus

pytestmark = pytest.mark.db

API = "/api/v1"
STRONG = "Correct-Horse-42-battery"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": "new.hire@example.test",
        "name": "New Hire",
        "role": "employee",
        "department": "Sales",
        "password": STRONG,
    }
    body.update(overrides)
    return body


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


# ---------------------------------------------------------------------------
# Gate: finance only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_fixture", ["employee_client", "manager_client"])
def test_non_finance_gets_403_on_every_admin_endpoint(
    request: pytest.FixtureRequest, client_fixture: str, users_a: dict[str, User]
) -> None:
    client: TestClient = request.getfixturevalue(client_fixture)
    target = users_a["employee"].id
    calls = [
        client.post(f"{API}/users", json=_create_body()),
        client.patch(f"{API}/users/{target}", json={"name": "X"}),
        client.post(f"{API}/users/{target}/deactivate"),
        client.post(f"{API}/users/{target}/reactivate"),
    ]
    for resp in calls:
        assert resp.status_code == 403, resp.text
        assert _code(resp) == "FORBIDDEN"


def test_role_gate_holds_even_if_users_read_is_granted(
    app: Any,
    rbac_catalog: None,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role check is a second, independent gate: a manager whose role somehow
    held users.read still cannot administer users."""
    import backend.app.core.rbac as rbac_mod

    real = rbac_mod.get_user_permissions
    monkeypatch.setattr(
        rbac_mod, "get_user_permissions", lambda user, session: real(user, session) | {"users.read"}
    )
    resp = _client(app, users_a["manager"]).post(f"{API}/users", json=_create_body())
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_finance_creates_user_who_can_log_in(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    resp = finance_client.post(
        f"{API}/users",
        json=_create_body(email="  New.Hire@Example.TEST ", manager_id=str(users_a["manager"].id)),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["email"] == "new.hire@example.test"
    assert data["role"] == "employee"
    assert data["status"] == "active"
    assert data["manager_id"] == str(users_a["manager"].id)
    assert data["manager_name"] == users_a["manager"].name
    assert data["organization_id"] == str(users_a["finance"].organization_id)
    assert "password" not in resp.text.lower() and STRONG not in resp.text

    login = finance_client.post(
        f"{API}/auth/login", json={"email": "new.hire@example.test", "password": STRONG}
    )
    assert login.status_code == 200, login.text


def test_create_audit_row_never_contains_password(
    finance_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    resp = finance_client.post(f"{API}/users", json=_create_body())
    assert resp.status_code == 201
    row = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "user.created",
            AuditLog.entity_id == resp.json()["id"],
        )
    ).one()
    assert row.actor_id == users_a["finance"].id
    assert row.after_state is not None
    dumped = json.dumps(row.after_state)
    assert STRONG not in dumped and "password" not in dumped
    assert row.after_state["email"] == "new.hire@example.test"
    assert STRONG not in caplog.text


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("Short-1a", "at least 12"),
        ("alllowercaseletters", "three of"),
        ("New.hire-Pass-99", "email"),
        ("A1-" + "x" * 80, "at most 72"),
    ],
)
def test_weak_password_is_422_with_reason_and_not_echoed(
    finance_client: TestClient, password: str, fragment: str
) -> None:
    resp = finance_client.post(
        f"{API}/users", json=_create_body(email="new.hire@example.test", password=password)
    )
    assert resp.status_code == 422, resp.text
    assert fragment in resp.json()["error"]["message"]
    assert password not in resp.text


def test_email_unique_per_org_case_insensitive(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    resp = finance_client.post(
        f"{API}/users", json=_create_body(email=users_a["employee"].email.upper())
    )
    assert resp.status_code == 409
    assert _code(resp) == "EMAIL_TAKEN"


def test_email_in_another_org_is_allowed_and_leaks_nothing(
    finance_client: TestClient, db_session: Session, users_b: dict[str, User]
) -> None:
    # Intent changed twice.  Product gap 3 refused this (409) because login
    # resolved users by email alone; known-limits item "cross-organisation email
    # clash" (migration 0022) made login organization-aware, so email is unique
    # per organization again and the check never probes another tenant — an
    # admin can no longer squat org B's address or learn that it exists.
    victim = users_b["employee"]
    resp = finance_client.post(f"{API}/users", json=_create_body(email=victim.email.upper()))
    assert resp.status_code == 201, resp.text
    assert str(victim.organization_id) not in resp.text and victim.name not in resp.text
    count = db_session.scalar(
        sa.select(sa.func.count())
        .select_from(User)
        .where(sa.func.lower(User.email) == victim.email.lower())
    )
    assert count == 2


def test_other_org_user_can_still_log_in_after_a_takeover_attempt(
    finance_client: TestClient,
    db_session: Session,
    users_b: dict[str, User],
    org_b: Organization,
) -> None:
    victim = users_b["employee"]
    created = finance_client.post(f"{API}/users", json=_create_body(email=victim.email))
    assert created.status_code == 201
    # An ordinary update to the victim's row used to flip which duplicate the
    # unordered login lookup returned; make sure nothing like that can matter.
    victim.department = "Ops"
    db_session.flush()
    # Updated (0022 + review fix): the victim can name their organization, and
    # even without it the squat does not lock them out — the password picks
    # their account (the squatter's differs).
    ok = finance_client.post(
        f"{API}/auth/login",
        json={"email": victim.email, "password": f._DEFAULT_PASSWORD, "organization": org_b.slug},
    )
    assert ok.status_code == 200, ok.text
    hijack = finance_client.post(
        f"{API}/auth/login",
        json={"email": victim.email, "password": STRONG, "organization": org_b.slug},
    )
    assert hijack.status_code == 401
    no_code = finance_client.post(
        f"{API}/auth/login", json={"email": victim.email, "password": f._DEFAULT_PASSWORD}
    )
    assert no_code.status_code == 200, no_code.text
    assert jwt.get_unverified_claims(no_code.json()["access_token"])["org"] == str(org_b.id)


def test_database_enforces_one_account_per_email_within_an_org(
    db_session: Session, users_a: dict[str, User], users_b: dict[str, User], org_a: Organization
) -> None:
    # Backstop for the race between the pre-check and the insert: the unique
    # index on (organization_id, lower(email)) (migration 0022) refuses a
    # same-org duplicate in any casing.  A cross-org duplicate is now legal.
    f.make_user(db_session, org=org_a, email=users_b["employee"].email.upper())
    with pytest.raises(sa.exc.IntegrityError), db_session.begin_nested():
        f.make_user(db_session, org=org_a, email=users_a["employee"].email.upper())


def test_create_maps_a_racing_duplicate_to_email_taken(
    finance_client: TestClient, users_a: dict[str, User], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate losing the race: the pre-check passes, the insert collides.
    import backend.app.modules.identity.admin as admin_mod

    monkeypatch.setattr(admin_mod, "_ensure_email_free", lambda *a, **k: None)
    resp = finance_client.post(f"{API}/users", json=_create_body(email=users_a["employee"].email))
    assert resp.status_code == 409, resp.text
    assert _code(resp) == "EMAIL_TAKEN"
    assert resp.json()["error"]["message"] == "A user with this email already exists."


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"email": "not-an-email"}, "valid email"),
        ({"name": "   "}, "Full name"),
        ({"role": "superuser"}, "Invalid role"),
    ],
)
def test_create_field_validation_messages(
    finance_client: TestClient, overrides: dict[str, Any], fragment: str
) -> None:
    resp = finance_client.post(f"{API}/users", json=_create_body(**overrides))
    assert resp.status_code == 422
    assert fragment in resp.json()["error"]["message"]


def test_create_refuses_unknown_fields(finance_client: TestClient) -> None:
    resp = finance_client.post(f"{API}/users", json=_create_body(status="inactive"))
    assert resp.status_code == 422


def test_create_rejects_manager_from_other_org_or_wrong_role(
    finance_client: TestClient, users_a: dict[str, User], users_b: dict[str, User]
) -> None:
    other_org = finance_client.post(
        f"{API}/users", json=_create_body(manager_id=str(users_b["finance"].id))
    )
    assert other_org.status_code == 422
    employee_mgr = finance_client.post(
        f"{API}/users", json=_create_body(manager_id=str(users_a["employee"].id))
    )
    assert employee_mgr.status_code == 422
    assert "manager or finance admin" in employee_mgr.json()["error"]["message"]


def test_create_rejects_inactive_manager(
    finance_client: TestClient, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    gone = f.make_user(db_session, org=org_a, role=RoleKey.manager, status=UserStatus.inactive)
    resp = finance_client.post(f"{API}/users", json=_create_body(manager_id=str(gone.id)))
    assert resp.status_code == 422
    assert "inactive" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Org scope
# ---------------------------------------------------------------------------


def test_cross_org_target_is_404_everywhere(
    finance_client: TestClient, users_b: dict[str, User], db_session: Session
) -> None:
    target = users_b["employee"].id
    for resp in (
        finance_client.patch(f"{API}/users/{target}", json={"name": "Hijack"}),
        finance_client.post(f"{API}/users/{target}/deactivate"),
        finance_client.post(f"{API}/users/{target}/reactivate"),
    ):
        assert resp.status_code == 404
    db_session.refresh(users_b["employee"])
    assert users_b["employee"].name != "Hijack"
    assert users_b["employee"].status == UserStatus.active


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_fields_and_audit_diff(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User], org_a: Organization
) -> None:
    emp = users_a["employee"]
    other_mgr = f.make_user(db_session, org=org_a, role=RoleKey.manager, name="Other Boss")
    resp = finance_client.patch(
        f"{API}/users/{emp.id}",
        json={"name": " Renamed  Person ", "department": "Ops", "manager_id": str(other_mgr.id)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Renamed Person"
    assert data["department"] == "Ops"
    assert data["manager_name"] == "Other Boss"
    row = db_session.scalars(
        sa.select(AuditLog).where(AuditLog.action == "user.updated", AuditLog.entity_id == emp.id)
    ).one()
    assert row.after_state is not None
    assert set(row.after_state) == {"name", "department", "manager_id"}
    assert row.after_state["manager_id"] == str(other_mgr.id)


def test_update_clear_manager_with_explicit_null(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    resp = finance_client.patch(f"{API}/users/{users_a['employee'].id}", json={"manager_id": None})
    assert resp.status_code == 200
    assert resp.json()["manager_id"] is None


def test_noop_update_writes_no_audit(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    resp = finance_client.patch(f"{API}/users/{emp.id}", json={"name": emp.name})
    assert resp.status_code == 200
    assert (
        db_session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "user.updated", AuditLog.entity_id == emp.id)
        )
        == 0
    )


def test_user_cannot_be_their_own_manager(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    mgr = users_a["manager"]
    resp = finance_client.patch(f"{API}/users/{mgr.id}", json={"manager_id": str(mgr.id)})
    assert resp.status_code == 422
    assert "own manager" in resp.json()["error"]["message"]


def test_manager_cycle_is_refused(
    finance_client: TestClient, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    """top <- mid <- users_a.manager; making top report to users_a.manager closes a cycle."""
    top = f.make_user(db_session, org=org_a, role=RoleKey.manager, name="Top")
    mid = f.make_user(db_session, org=org_a, role=RoleKey.manager, manager_id=top.id)
    mgr = users_a["manager"]
    mgr.manager_id = mid.id
    db_session.flush()
    resp = finance_client.patch(f"{API}/users/{top.id}", json={"manager_id": str(mgr.id)})
    assert resp.status_code == 409, resp.text
    assert _code(resp) == "MANAGER_CYCLE"
    db_session.refresh(top)
    assert top.manager_id is None


def test_demoting_manager_with_active_reports_is_refused(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    resp = finance_client.patch(f"{API}/users/{users_a['manager'].id}", json={"role": "employee"})
    assert resp.status_code == 409
    assert _code(resp) == "HAS_DIRECT_REPORTS"


def test_admin_cannot_demote_self(
    finance_client: TestClient, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)  # not the last admin
    resp = finance_client.patch(f"{API}/users/{users_a['finance'].id}", json={"role": "manager"})
    assert resp.status_code == 409
    assert _code(resp) == "SELF_ADMIN_CHANGE"
    db_session.refresh(users_a["finance"])
    assert users_a["finance"].role == RoleKey.finance_admin


def test_last_finance_admin_cannot_be_demoted(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """users_a has exactly one finance admin: demoting them is a last-admin refusal
    (checked BEFORE the self rule, so this test fails if the last-admin guard goes)."""
    resp = finance_client.patch(f"{API}/users/{users_a['finance'].id}", json={"role": "employee"})
    assert resp.status_code == 409
    assert _code(resp) == "LAST_FINANCE_ADMIN"


def test_other_finance_admin_can_be_demoted_when_one_remains(
    finance_client: TestClient, db_session: Session, org_a: Organization
) -> None:
    other = f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)
    resp = finance_client.patch(f"{API}/users/{other.id}", json={"role": "manager"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "manager"


def test_last_admin_guard_ignores_inactive_admins(
    app: Any, rbac_catalog: None, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    """An INACTIVE second finance admin does not count: the active one is still last."""
    f.make_user(db_session, org=org_a, role=RoleKey.finance_admin, status=UserStatus.inactive)
    resp = _client(app, users_a["finance"]).post(f"{API}/users/{users_a['finance'].id}/deactivate")
    assert resp.status_code == 409
    assert _code(resp) == "LAST_FINANCE_ADMIN"


# ---------------------------------------------------------------------------
# Deactivate / reactivate
# ---------------------------------------------------------------------------


def test_admin_cannot_deactivate_self(
    finance_client: TestClient, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)
    resp = finance_client.post(f"{API}/users/{users_a['finance'].id}/deactivate")
    assert resp.status_code == 409
    assert _code(resp) == "SELF_ADMIN_CHANGE"


def test_manager_with_active_reports_cannot_be_deactivated(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    resp = finance_client.post(f"{API}/users/{users_a['manager'].id}/deactivate")
    assert resp.status_code == 409
    assert _code(resp) == "HAS_DIRECT_REPORTS"


def test_deactivation_blocks_login_and_kills_existing_tokens(
    app: Any, finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    emp_client = _client(app, emp)  # token minted BEFORE deactivation
    refresh = create_refresh_token(emp.id, emp.organization_id)
    assert emp_client.get(f"{API}/me").status_code == 200

    resp = finance_client.post(f"{API}/users/{emp.id}/deactivate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "inactive"

    me = emp_client.get(f"{API}/me")
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "UNAUTHORIZED"
    assert emp_client.get(f"{API}/expense-reports").status_code == 401
    assert (
        finance_client.post(f"{API}/auth/refresh", json={"refresh_token": refresh}).status_code
        == 401
    )
    # The seeded factory password hash is unknown here; a wrong-or-right password
    # must be rejected identically for an inactive account.
    login = finance_client.post(f"{API}/auth/login", json={"email": emp.email, "password": "x"})
    assert login.status_code == 401

    audit = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "user.deactivated", AuditLog.entity_id == emp.id
        )
    ).one()
    assert audit.after_state == {"status": "inactive"}

    again = finance_client.post(f"{API}/users/{emp.id}/deactivate")
    assert again.status_code == 409 and _code(again) == "USER_STATUS_UNCHANGED"

    back = finance_client.post(f"{API}/users/{emp.id}/reactivate")
    assert back.status_code == 200 and back.json()["status"] == "active"
    assert emp_client.get(f"{API}/me").status_code == 200
    assert db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "user.reactivated", AuditLog.entity_id == emp.id)
    )


def test_deactivated_user_with_correct_password_cannot_log_in(
    finance_client: TestClient,
) -> None:
    created = finance_client.post(f"{API}/users", json=_create_body())
    assert created.status_code == 201
    uid = created.json()["id"]
    ok = finance_client.post(
        f"{API}/auth/login", json={"email": "new.hire@example.test", "password": STRONG}
    )
    assert ok.status_code == 200
    assert finance_client.post(f"{API}/users/{uid}/deactivate").status_code == 200
    denied = finance_client.post(
        f"{API}/auth/login", json={"email": "new.hire@example.test", "password": STRONG}
    )
    assert denied.status_code == 401


# ---------------------------------------------------------------------------
# Login is case-insensitive on email (admin-created emails are stored lower-case)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed", ["Jane.Doe@Example.test", "jane.doe@example.test", "  JANE.DOE@EXAMPLE.TEST "]
)
def test_admin_created_mixed_case_user_logs_in_with_any_casing(
    finance_client: TestClient, typed: str
) -> None:
    created = finance_client.post(f"{API}/users", json=_create_body(email="Jane.Doe@Example.test"))
    assert created.status_code == 201, created.text
    assert created.json()["email"] == "jane.doe@example.test"
    login = finance_client.post(f"{API}/auth/login", json={"email": typed, "password": STRONG})
    assert login.status_code == 200, login.text


def test_legacy_mixed_case_stored_email_still_logs_in(
    finance_client: TestClient, db_session: Session, org_a: Organization
) -> None:
    f.make_user(db_session, org=org_a, email="Legacy.Person@Example.test")
    for typed in ("Legacy.Person@Example.test", "legacy.person@example.test"):
        resp = finance_client.post(
            f"{API}/auth/login", json={"email": typed, "password": f._DEFAULT_PASSWORD}
        )
        assert resp.status_code == 200, (typed, resp.text)


def test_case_insensitive_login_still_rejects_wrong_password(
    finance_client: TestClient,
) -> None:
    assert finance_client.post(f"{API}/users", json=_create_body()).status_code == 201
    resp = finance_client.post(
        f"{API}/auth/login", json={"email": "NEW.HIRE@example.test", "password": STRONG + "x"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Request-validation failures never log or echo the password
# ---------------------------------------------------------------------------


def _without(key: str) -> dict[str, Any]:
    body = _create_body()
    body.pop(key)
    return body


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/users", _without("role")),
        ("/users", _create_body(sneaky="x")),
        ("/users", _create_body(password=STRONG * 60)),  # > max_length 1024
        ("/users", _create_body(role=["not", "a", "string"])),
        ("/auth/login", {"password": STRONG}),  # missing email
        ("/auth/login", {"email": 7, "password": STRONG}),
    ],
    ids=[
        "users-missing-role",
        "users-extra-field",
        "users-long-pw",
        "users-bad-type",
        "login-missing-email",
        "login-bad-email-type",
    ],
)
def test_validation_error_never_logs_or_echoes_the_password(
    finance_client: TestClient,
    caplog: pytest.LogCaptureFixture,
    path: str,
    body: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG)
    resp = finance_client.post(f"{API}{path}", json=body)
    assert resp.status_code == 422, resp.text
    assert _code(resp) == "VALIDATION_ERROR"
    assert STRONG not in resp.text
    assert "Validation error" in caplog.text  # the handler did log ...
    assert STRONG not in caplog.text  # ... but never the submitted values


# ---------------------------------------------------------------------------
# List search (q) + manager names
# ---------------------------------------------------------------------------


def test_list_q_filter_escapes_wildcards_and_resolves_manager_names(
    finance_client: TestClient, db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    f.make_user(db_session, org=org_a, name="Percent 100% Person")
    f.make_user(db_session, org=org_a, name="Plain Person")
    f.make_user(db_session, org=org_a, name="Score 1005 Person")  # matches only if % is a wildcard
    resp = finance_client.get(f"{API}/users", params={"q": "100%"})
    assert resp.status_code == 200
    names = [u["name"] for u in resp.json()["items"]]
    assert names == ["Percent 100% Person"]

    by_mail = finance_client.get(f"{API}/users", params={"q": users_a["employee"].email.upper()})
    items = by_mail.json()["items"]
    assert [u["id"] for u in items] == [str(users_a["employee"].id)]
    assert items[0]["manager_name"] == users_a["manager"].name
