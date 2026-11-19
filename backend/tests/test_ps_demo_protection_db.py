"""Phase 11 — public-demo protections (DEMO_MODE).

* the five seeded demo accounts (DEMO_GRADES emails in the organisation named
  exactly "Atlas Demo Corp") cannot be deactivated or edited (role, manager, grade,
  name, department) by ANY finance admin — 403 DEMO_ACCOUNT_PROTECTED, audited,
  nothing changed; email / password / organisation have no write path at all;
* a visitor-created user stays fully manageable; the same email in ANOTHER
  organisation is not protected; with DEMO_MODE off nothing is protected;
* the seeded policy rules cannot be archived (403 DEMO_RULE_PROTECTED, audited);
  activating them, and archiving visitor-authored rules, still works;
  DEMO_RESTRICT_DESTRUCTIVE=false lifts only that part;
* GET /public/config (no token) reports demo_mode; GET /users flags protected rows.

DB-backed (shared conftest: rolled-back session, real JWT clients).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.identity import Organization, RoleKey, User, UserStatus
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import POLICY_NAME, POLICY_VERSION
from database.seeds.reset_demo_data import SEED_ACCOUNTS
from database.seeds.seed_all import CAR_CAP_DRAFT_RULE, DEMO_GRADES
from database.seeds.seed_demo_grades import DEMO_ORG_NAME

pytestmark = pytest.mark.db

API = "/api/v1"
STRONG = "Correct-Horse-42-battery"
SEED_EMAILS = [a.email for a in SEED_ACCOUNTS]


def _client(app: Any, user: User) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update(
        {"Authorization": f"Bearer {create_access_token(user.id, user.organization_id)}"}
    )
    return client


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


@pytest.fixture()
def demo_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    monkeypatch.setattr(get_settings(), "DEMO_RESTRICT_DESTRUCTIVE", None)


@pytest.fixture()
def demo(db_session: Session, rbac_catalog: None) -> dict[str, Any]:
    """The demo org with its five seeded accounts at seed values (get-or-create: the
    shared test DB may already hold a committed seed; all changes roll back)."""
    org = db_session.scalars(
        sa.select(Organization)
        .where(Organization.name == DEMO_ORG_NAME)
        .order_by(Organization.created_at)
    ).first() or f.make_org(db_session, name=DEMO_ORG_NAME)
    users: dict[str, User] = {}
    for acct in SEED_ACCOUNTS:
        user = db_session.scalars(
            sa.select(User).where(User.organization_id == org.id, User.email == acct.email)
        ).first()
        manager = users.get(acct.manager_email) if acct.manager_email else None
        if user is None:
            user = f.make_user(db_session, org=org, email=acct.email, role=acct.role)
        user.name, user.role, user.department = acct.name, acct.role, acct.department
        user.manager_id = manager.id if manager else None
        user.status, user.grade = UserStatus.active, DEMO_GRADES[acct.email]
        db_session.flush()
        users[acct.email] = user
    finance = users["finance@atlas-demo.internal"]
    return {"org": org, "users": users, "finance": finance}


@pytest.fixture()
def demo_finance(app: Any, demo: dict[str, Any]) -> TestClient:
    return _client(app, demo["finance"])


def _visitor(client: TestClient, **over: Any) -> dict[str, Any]:
    body = {
        "email": "visitor.qa@example.test",
        "name": "Visitor QA",
        "role": "employee",
        "password": STRONG,
        **over,
    }
    resp = client.post(f"{API}/users", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _snapshot(db: Session, user: User) -> tuple[Any, ...]:
    db.expire(user)
    return (
        user.name,
        user.email,
        user.role,
        user.department,
        user.manager_id,
        user.status,
        user.grade,
        user.password_hash,
        user.organization_id,
    )


_EDITS: list[tuple[str, dict[str, Any] | None]] = [
    ("deactivate", None),
    ("role", {"role": "employee"}),
    ("role-up", {"role": "finance_admin"}),
    ("manager", {"manager_id": None}),
    ("grade", {"grade": "CSUITE"}),
    ("grade-clear", {"grade": None}),
    ("name", {"name": "Hacked Name"}),
    ("department", {"department": "Nowhere"}),
]


def _attempt(client: TestClient, user: User, edit: dict[str, Any] | None) -> Any:
    if edit is None:
        return client.post(f"{API}/users/{user.id}/deactivate")
    return client.patch(f"{API}/users/{user.id}", json=edit)


# ---------------------------------------------------------------------------
# Protected accounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("email", SEED_EMAILS)
@pytest.mark.parametrize(("label", "edit"), _EDITS, ids=[e[0] for e in _EDITS])
def test_every_protected_operation_refused_for_every_demo_account(
    demo_on: None,
    demo: dict[str, Any],
    demo_finance: TestClient,
    db_session: Session,
    email: str,
    label: str,
    edit: dict[str, Any] | None,
) -> None:
    target = demo["users"][email]
    before = _snapshot(db_session, target)

    resp = _attempt(demo_finance, target, edit)

    assert resp.status_code == 403, (label, resp.text)
    assert _code(resp) == "DEMO_ACCOUNT_PROTECTED"
    assert "shared demo account" in resp.json()["error"]["message"]
    assert _snapshot(db_session, target) == before
    row = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "user.demo_protected_refused", AuditLog.entity_id == target.id
        )
    ).first()
    assert row is not None and row.actor_id == demo["finance"].id
    assert (row.after_state or {}).get("reason") == "DEMO_ACCOUNT_PROTECTED"


def test_a_visitor_created_finance_admin_cannot_touch_demo_accounts_either(
    demo_on: None, app: Any, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    other = _visitor(demo_finance, role="finance_admin", email="second.admin@example.test")
    admin2 = db_session.get(User, other["id"])
    assert admin2 is not None
    client = _client(app, admin2)
    for email in SEED_EMAILS:
        target = demo["users"][email]
        assert _code(client.post(f"{API}/users/{target.id}/deactivate")) == (
            "DEMO_ACCOUNT_PROTECTED"
        )
        assert _code(client.patch(f"{API}/users/{target.id}", json={"role": "employee"})) == (
            "DEMO_ACCOUNT_PROTECTED"
        )


@pytest.mark.parametrize("email", SEED_EMAILS)
def test_email_password_and_organisation_have_no_write_path(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session, email: str
) -> None:
    target = demo["users"][email]
    before = _snapshot(db_session, target)
    other_org = f.make_org(db_session)
    for body in (
        {"email": "taken.over@example.test"},
        {"password": STRONG},
        {"organization_id": str(other_org.id)},
        {"status": "inactive"},
    ):
        resp = demo_finance.patch(f"{API}/users/{target.id}", json=body)
        assert resp.status_code == 422, (body, resp.text)
    for path in (f"/users/{target.id}/password", f"/users/{target.id}/reset-password"):
        assert demo_finance.post(f"{API}{path}", json={"password": STRONG}).status_code in (
            404,
            405,
        )
    # The organisation login code is read-only (no write method exists).
    assert demo_finance.patch(f"{API}/organization", json={"login_code": "x"}).status_code == 405
    assert demo_finance.put(f"{API}/organization", json={"login_code": "x"}).status_code == 405
    assert _snapshot(db_session, target) == before


def test_protected_account_can_be_reactivated(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    """Reactivation only RESTORES a demo account (e.g. one left inactive before
    DEMO_MODE was switched on), so it stays allowed."""
    target = demo["users"]["employee2@atlas-demo.internal"]
    target.status = UserStatus.inactive
    db_session.flush()
    resp = demo_finance.post(f"{API}/users/{target.id}/reactivate")
    assert resp.status_code == 200, resp.text


def test_visitor_created_user_stays_fully_manageable(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient
) -> None:
    created = _visitor(demo_finance)
    assert created["demo_protected"] is False
    uid = created["id"]
    manager = demo["users"]["manager2@atlas-demo.internal"]
    for body in (
        {"role": "manager"},
        {"grade": "VP"},
        {"manager_id": str(manager.id)},
        {"name": "Renamed Visitor", "department": "QA"},
    ):
        resp = demo_finance.patch(f"{API}/users/{uid}", json=body)
        assert resp.status_code == 200, (body, resp.text)
    assert demo_finance.post(f"{API}/users/{uid}/deactivate").status_code == 200
    assert demo_finance.post(f"{API}/users/{uid}/reactivate").status_code == 200


def test_same_email_in_another_organisation_is_not_protected(
    demo_on: None, app: Any, demo: dict[str, Any], db_session: Session
) -> None:
    org_b = f.make_org(db_session, name="Not The Demo")
    lookalike = f.make_user(db_session, org=org_b, email="manager@atlas-demo.internal")
    admin_b = f.make_user(db_session, org=org_b, role=RoleKey.finance_admin)
    client = _client(app, admin_b)
    assert client.patch(f"{API}/users/{lookalike.id}", json={"grade": "VP"}).status_code == 200
    assert client.post(f"{API}/users/{lookalike.id}/deactivate").status_code == 200


def test_protection_is_off_without_demo_mode(
    demo: dict[str, Any], demo_finance: TestClient
) -> None:
    assert get_settings().DEMO_MODE is False  # the code default
    employee2 = demo["users"]["employee2@atlas-demo.internal"]
    resp = demo_finance.patch(f"{API}/users/{employee2.id}", json={"grade": "VP"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["demo_protected"] is False
    assert demo_finance.post(f"{API}/users/{employee2.id}/deactivate").status_code == 200


def test_users_list_flags_exactly_the_protected_rows(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    visitor = _visitor(demo_finance)
    rows = demo_finance.get(f"{API}/users", params={"size": 100}).json()["items"]
    flagged = {r["email"] for r in rows if r["demo_protected"]}
    assert flagged == set(SEED_EMAILS)
    assert visitor["email"] not in flagged
    detail = demo_finance.get(f"{API}/users/{demo['finance'].id}").json()
    assert detail["demo_protected"] is True

    monkeypatch.setattr(get_settings(), "DEMO_MODE", False)
    rows = demo_finance.get(f"{API}/users", params={"size": 100}).json()["items"]
    assert not any(r["demo_protected"] for r in rows)


def test_last_finance_admin_guard_still_holds_under_demo_mode(
    demo_on: None, app: Any, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    """A visitor finance admin cannot demote themselves (self + last-admin guards stay)."""
    created = _visitor(demo_finance, role="finance_admin", email="solo.admin@example.test")
    admin2 = db_session.get(User, created["id"])
    assert admin2 is not None
    resp = _client(app, admin2).patch(f"{API}/users/{admin2.id}", json={"role": "employee"})
    assert resp.status_code == 409 and _code(resp) == "SELF_ADMIN_CHANGE"


# ---------------------------------------------------------------------------
# Seeded policy rules
# ---------------------------------------------------------------------------


def _stub(db: Session, org: Organization) -> Policy:
    policy = db.scalars(
        sa.select(Policy).where(
            Policy.organization_id == org.id,
            Policy.name == POLICY_NAME,
            Policy.version == POLICY_VERSION,
        )
    ).first()
    return policy or f.make_policy(db, org=org, name=POLICY_NAME, version=POLICY_VERSION)


def _rule(
    db: Session, policy: Policy, clause: str, status: PolicyRuleStatus, **kw: Any
) -> PolicyRule:
    return f.make_policy_rule(
        db,
        policy=policy,
        rule_type=kw.pop("rule_type", PolicyRuleType.amount_cap),
        applies_to=kw.pop("applies_to", PolicyAppliesTo.hotel),
        definition={"max_amount": 300, "currency": "USD", "per": "night"},
        status=status,
        source_clause_ref=clause,
    )


def _archive(client: TestClient, rule: PolicyRule) -> Any:
    return client.patch(f"{API}/policy-rules/{rule.id}", json={"status": "archived"})


def test_seeded_rule_cannot_be_archived_under_demo_mode(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    rule = _rule(db_session, _stub(db_session, demo["org"]), "HOTEL-CAP", PolicyRuleStatus.active)
    resp = _archive(demo_finance, rule)
    assert resp.status_code == 403, resp.text
    assert _code(resp) == "DEMO_RULE_PROTECTED"
    db_session.expire(rule)
    assert rule.status == PolicyRuleStatus.active
    assert (
        db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.action == "policy_rule.demo_protected_refused",
                AuditLog.entity_id == rule.id,
            )
        ).first()
        is not None
    )


def test_seeded_draft_rule_can_still_be_activated_but_not_archived(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    rule = _rule(
        db_session,
        _stub(db_session, demo["org"]),
        "CAR-CAP-DRAFT",
        PolicyRuleStatus.draft,
        applies_to=PolicyAppliesTo.car,
    )
    rule.definition = copy.deepcopy(CAR_CAP_DRAFT_RULE["definition"])
    db_session.flush()
    assert _archive(demo_finance, rule).status_code == 403
    resp = demo_finance.patch(f"{API}/policy-rules/{rule.id}", json={"status": "active"})
    assert resp.status_code == 200, resp.text


def test_visitor_authored_rule_can_be_archived_under_demo_mode(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    rule = _rule(db_session, _stub(db_session, demo["org"]), "VISITOR-01", PolicyRuleStatus.draft)
    resp = _archive(demo_finance, rule)
    assert resp.status_code == 200, resp.text


def _author_via_api_shape(db: Session, rule: PolicyRule, author: User | None) -> None:
    """What POST /policies/{id}/rules leaves behind for a rule it creates: the
    author as ``created_by`` and a ``policy_rule.drafted`` audit row (same
    transaction).  ``author=None`` = the author was deleted later (FK SET NULL
    on both the rule and the append-only audit row)."""
    from backend.app.core.audit import record_audit

    rule.created_by = author.id if author is not None else None
    record_audit(
        db,
        organization_id=demo_org_id(db, rule),
        action="policy_rule.drafted",
        entity_type="policy_rule",
        entity_id=rule.id,
        actor_id=author.id if author is not None else None,
        after_state={"status": rule.status.value, "provenance": "ai_generated"},
    )
    db.flush()


def demo_org_id(db: Session, rule: PolicyRule) -> Any:
    return db.scalars(sa.select(Policy.organization_id).where(Policy.id == rule.policy_id)).one()


@pytest.mark.parametrize("author_deleted", [False, True], ids=["author", "author-deleted"])
def test_visitor_lookalike_of_a_seeded_rule_stays_archivable(
    demo_on: None,
    demo: dict[str, Any],
    demo_finance: TestClient,
    db_session: Session,
    author_deleted: bool,
) -> None:
    """Integration review fix (d): a visitor-authored rule that copies a seeded
    rule's (clause, type, target) is NOT seeded — provenance decides, not
    content — while the real seeded rule stays protected."""
    stub = _stub(db_session, demo["org"])
    seeded = _rule(db_session, stub, "HOTEL-CAP", PolicyRuleStatus.active)
    lookalike = _rule(db_session, stub, "HOTEL-CAP", PolicyRuleStatus.draft)
    _author_via_api_shape(db_session, lookalike, None if author_deleted else demo["finance"])
    resp = _archive(demo_finance, lookalike)
    assert resp.status_code == 200, resp.text
    assert _code(_archive(demo_finance, seeded)) == "DEMO_RULE_PROTECTED"


def test_seed_clause_rule_of_another_org_can_be_archived(
    demo_on: None, app: Any, db_session: Session, rbac_catalog: None
) -> None:
    org_b = f.make_org(db_session, name="Other Tenant")
    admin_b = f.make_user(db_session, org=org_b, role=RoleKey.finance_admin)
    rule = _rule(db_session, _stub(db_session, org_b), "HOTEL-CAP", PolicyRuleStatus.active)
    assert _archive(_client(app, admin_b), rule).status_code == 200


@pytest.mark.parametrize(
    ("demo_mode", "restrict"), [(False, None), (True, False)], ids=["demo-off", "restrict-off"]
)
def test_seeded_rule_archive_allowed_when_switched_off(
    demo: dict[str, Any],
    demo_finance: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    demo_mode: bool,
    restrict: bool | None,
) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", demo_mode)
    monkeypatch.setattr(get_settings(), "DEMO_RESTRICT_DESTRUCTIVE", restrict)
    rule = _rule(db_session, _stub(db_session, demo["org"]), "HOTEL-CAP", PolicyRuleStatus.active)
    assert _archive(demo_finance, rule).status_code == 200


def test_restrict_off_keeps_account_protection(
    demo: dict[str, Any], demo_finance: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    monkeypatch.setattr(get_settings(), "DEMO_RESTRICT_DESTRUCTIVE", False)
    target = demo["users"]["manager@atlas-demo.internal"]
    assert _code(demo_finance.post(f"{API}/users/{target.id}/deactivate")) == (
        "DEMO_ACCOUNT_PROTECTED"
    )


# ---------------------------------------------------------------------------
# Public config
# ---------------------------------------------------------------------------


def test_public_config_reports_demo_mode_without_a_token(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    anon = TestClient(app, raise_server_exceptions=False)
    resp = anon.get(f"{API}/public/config")
    assert resp.status_code == 200
    # Phase 12 added the (default-off) scheduled demo reset block on purpose.
    reset_off = {
        "enabled": False,
        "interval_hours": None,
        "next_reset_at": None,
        "last_reset_at": None,
    }
    assert resp.json() == {"demo_mode": False, "demo_reset": reset_off, "demo_org_code": None}
    monkeypatch.setattr(get_settings(), "DEMO_MODE", True)
    assert anon.get(f"{API}/public/config").json() == {
        "demo_mode": True,
        "demo_reset": reset_off,
        "demo_org_code": "atlas-demo-corp",
    }


def test_refusal_audit_row_survives_the_request_rollback(
    demo_on: None, demo: dict[str, Any], demo_finance: TestClient, db_session: Session
) -> None:
    """The refused request's transaction is discarded by the error path, so the audit
    row must be COMMITTED before the 403 is raised (a rollback here mimics that)."""
    target_id = demo["users"]["manager@atlas-demo.internal"].id
    resp = demo_finance.post(f"{API}/users/{target_id}/deactivate")
    assert resp.status_code == 403
    db_session.rollback()
    assert (
        db_session.scalars(
            sa.select(AuditLog).where(
                AuditLog.action == "user.demo_protected_refused", AuditLog.entity_id == target_id
            )
        ).first()
        is not None
    )
