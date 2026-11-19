"""Product-gaps integration, security item (a) — identity across organizations.

Updated for the known-limits item "cross-organisation email clash" (migration
0022): email is unique per organization again and login takes an optional
organization login code.  What these tests still pin:

* login WITHOUT an organization fails CLOSED (the same generic 401 as a wrong
  password — never a 500 and never "the first row") when more than one active
  account matches an email (now a legal state, not only legacy data);
* POST /users never consults another organization: a foreign address is simply
  created (see test_kl_org_scoped_login_db.py); the own-org EMAIL_TAKEN is the
  same on the pre-check and the racing-insert path;
* every refused attempt writes one audit row in the ADMIN's organization with the
  actor, a masked email and a keyed digest — never the raw address.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from database.models.audit_log import AuditLog
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

API = "/api/v1"
STRONG = "Correct-Horse-42-battery"


def _create_body(email: str) -> dict[str, Any]:
    return {
        "email": email,
        "name": "New Hire",
        "role": "employee",
        "department": "Sales",
        "password": STRONG,
    }


def _error_without_request_id(resp: Any) -> dict[str, Any]:
    error = dict(resp.json()["error"])
    error.pop("request_id", None)
    error.pop("correlation_id", None)
    return error


def _refusals(session: Session, org_id: uuid.UUID) -> list[AuditLog]:
    return list(
        session.scalars(
            sa.select(AuditLog)
            .where(
                AuditLog.organization_id == org_id,
                AuditLog.action == "user.create_refused",
            )
            .order_by(AuditLog.created_at)
        )
    )


# ---------------------------------------------------------------------------
# Login fails closed on an ambiguous email
# ---------------------------------------------------------------------------


def test_login_fails_closed_when_an_email_matches_two_accounts(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    org_b: Organization,
) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    victim = users_a["employee"]
    ok = client.post(
        f"{API}/auth/login", json={"email": victim.email, "password": f._DEFAULT_PASSWORD}
    )
    assert ok.status_code == 200, ok.text

    # A cross-org duplicate (legal since 0022; before, legacy data only).
    f.make_user(db_session, org=org_b, email=victim.email.upper())
    db_session.flush()

    wrong = client.post(f"{API}/auth/login", json={"email": victim.email, "password": "nope"})
    assert wrong.status_code == 401
    for typed in (victim.email, victim.email.upper()):
        resp = client.post(
            f"{API}/auth/login", json={"email": typed, "password": f._DEFAULT_PASSWORD}
        )
        # Correct password for BOTH rows, still refused — and indistinguishable
        # from a wrong password.
        assert resp.status_code == 401, resp.text
        assert _error_without_request_id(resp) == _error_without_request_id(wrong)
        assert "access_token" not in resp.text


# ---------------------------------------------------------------------------
# EMAIL_TAKEN is uniform and audited
# ---------------------------------------------------------------------------


def test_email_taken_is_identical_for_case_variants_and_never_for_a_foreign_org(
    finance_client: TestClient, users_a: dict[str, User], users_b: dict[str, User]
) -> None:
    # Intent changed (0022): a foreign-org address is no longer refused at all,
    # so there is nothing to compare it against; the own-org refusal is uniform
    # across casings, and the foreign address is created.
    own = finance_client.post(f"{API}/users", json=_create_body(users_a["employee"].email))
    own_upper = finance_client.post(
        f"{API}/users", json=_create_body(users_a["employee"].email.upper())
    )
    assert own.status_code == own_upper.status_code == 409
    assert _error_without_request_id(own) == _error_without_request_id(own_upper)
    assert _error_without_request_id(own)["code"] == "EMAIL_TAKEN"
    foreign = finance_client.post(f"{API}/users", json=_create_body(users_b["employee"].email))
    assert foreign.status_code == 201, foreign.text


def test_each_refused_create_is_audited_without_the_raw_email(
    finance_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    admin = users_a["finance"]
    first_email = users_a["employee"].email
    second_email = users_a["manager"].email

    finance_client.post(f"{API}/users", json=_create_body(first_email))
    finance_client.post(f"{API}/users", json=_create_body(second_email.upper()))
    finance_client.post(f"{API}/users", json=_create_body(second_email))
    # Production get_db rolls the request session back on the 409: the audit row
    # must already be committed to survive.  Mimic that rollback here.
    db_session.rollback()

    rows = _refusals(db_session, admin.organization_id)
    assert len(rows) == 3
    for row in rows:
        assert row.actor_id == admin.id
        assert row.entity_type == "user_email"
        state = row.after_state or {}
        assert state["reason"] == "EMAIL_TAKEN"
        assert "***@" in state["email_masked"]
        assert len(state["email_hmac_sha256"]) == 64
        dumped = json.dumps({"after": row.after_state, "before": row.before_state}).lower()
        assert first_email.lower() not in dumped
        assert second_email.lower() not in dumped
        assert str(users_a["employee"].id) not in dumped
        assert str(users_a["manager"].id) not in dumped
        assert row.entity_id not in {users_a["employee"].id, users_a["manager"].id}
    # Repeated attempts at one address correlate (same digest, case-insensitive);
    # different addresses do not.  The three requests can share one
    # ``created_at`` (one test transaction), so rows are compared without order.
    by_entity = Counter(row.entity_id for row in rows)
    assert sorted(by_entity.values()) == [1, 2]
    # Nothing about the refusal lands in the other tenant's audit log.
    assert _refusals(db_session, users_b["employee"].organization_id) == []


def test_racing_duplicate_refusal_is_audited_and_uniform(
    finance_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.identity.admin as admin_mod

    taken = users_a["employee"].email
    precheck = finance_client.post(f"{API}/users", json=_create_body(taken))
    monkeypatch.setattr(admin_mod, "_ensure_email_free", lambda *a, **k: None)
    raced = finance_client.post(f"{API}/users", json=_create_body(taken.upper()))
    assert raced.status_code == precheck.status_code == 409, raced.text
    assert _error_without_request_id(raced) == _error_without_request_id(precheck)
    db_session.rollback()  # as get_db does on the error — the audit must be committed
    rows = _refusals(db_session, users_a["finance"].organization_id)
    assert len(rows) == 2
    assert rows[0].entity_id == rows[1].entity_id
    assert (
        db_session.scalar(
            sa.select(sa.func.count())
            .select_from(User)
            .where(sa.func.lower(User.email) == taken.lower())
        )
        == 1
    )
