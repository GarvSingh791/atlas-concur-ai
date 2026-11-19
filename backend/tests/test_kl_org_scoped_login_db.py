"""Known limits — organization-aware login and per-organization email uniqueness.

Before: ``users.email`` was unique across ALL organizations (0020) because login
resolved a user by email alone.  An admin could claim another tenant's address
first (squatting) and learn that an address existed somewhere (enumeration).

Now (migration 0022): email is unique per organization (case-insensitive) and
login takes an optional organization login code (``organizations.slug``):

* with a code      -> the user is looked up inside THAT organization only;
* without a code   -> the password is verified against every ACTIVE account
                      with that email, padded with the dummy hash to exactly K
                      verifies, and succeeds only when exactly one matches;
                      0 or several -> the same generic 401 as a wrong password
                      (never "which" or "how many"); more than K accounts ->
                      only the K OLDEST are candidates (phase 11 integration:
                      newer duplicates cannot crowd out an established
                      account; a newer one signs in with its organization code);
* every failure    -> the same generic 401; the verify count depends only on
                      whether a code was given (K without, 1 with), never on
                      what exists, so create-then-login is not an oracle.

DB-backed (shared conftest: rolled-back session, real JWT clients).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from jose import jwt
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
)
from database.models.audit_log import AuditLog
from database.models.identity import ORG_SLUG_RE, Organization, User, UserStatus

pytestmark = pytest.mark.db

API = "/api/v1"
SHARED = "shared.person@example.test"
PW_A = "Alpha-Password-0001!"
PW_B = "Bravo-Password-0002!"
STRONG = "Correct-Horse-42-battery"


def _err(resp: Any) -> dict[str, Any]:
    error = dict(resp.json()["error"])
    error.pop("request_id", None)
    error.pop("correlation_id", None)
    return error


def _org_claim(token: str) -> str:
    return str(jwt.get_unverified_claims(token)["org"])


def _login(client: TestClient, email: str, password: str, org: str | None = None) -> Any:
    body: dict[str, Any] = {"email": email, "password": password}
    if org is not None:
        body["organization"] = org
    return client.post(f"{API}/auth/login", json=body)


@pytest.fixture()
def anon(app: Any) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def twins(db_session: Session, org_a: Organization, org_b: Organization) -> tuple[User, User]:
    """The same email in two organizations, with different passwords."""
    a = f.make_user(db_session, org=org_a, email=SHARED, password_hash=hash_password(PW_A))
    b = f.make_user(db_session, org=org_b, email=SHARED.upper(), password_hash=hash_password(PW_B))
    return a, b


# ---------------------------------------------------------------------------
# Schema: per-organization uniqueness, every org has a login code
# ---------------------------------------------------------------------------


def test_same_email_may_exist_in_two_organizations(twins: tuple[User, User]) -> None:
    a, b = twins
    assert a.organization_id != b.organization_id
    assert a.email.lower() == b.email.lower()


def test_database_refuses_a_case_insensitive_duplicate_inside_one_org(
    db_session: Session, twins: tuple[User, User], org_a: Organization
) -> None:
    with pytest.raises(sa.exc.IntegrityError), db_session.begin_nested():
        f.make_user(db_session, org=org_a, email="Shared.Person@Example.TEST")


def test_every_new_organization_gets_a_valid_unique_login_code(db_session: Session) -> None:
    one = f.make_org(db_session, name="Same Name Ltd")
    two = f.make_org(db_session, name="Same Name Ltd")
    assert one.slug != two.slug
    for org in (one, two):
        assert ORG_SLUG_RE.match(org.slug), org.slug
        assert org.slug.startswith("same-name-ltd-")
    # Raw SQL inserts (no ORM default) get the server default — still valid.
    raw = db_session.execute(
        sa.text(
            "INSERT INTO organizations (name, base_currency) VALUES ('Raw', 'USD') "
            "RETURNING slug"
        )
    ).scalar_one()
    assert ORG_SLUG_RE.match(raw), raw


def test_database_refuses_a_malformed_or_duplicate_login_code(
    db_session: Session, org_a: Organization
) -> None:
    for bad in ("Has Space", "UPPER", "-lead", "a"):
        with pytest.raises(sa.exc.IntegrityError), db_session.begin_nested():
            f.make_org(db_session, slug=bad)
    with pytest.raises(sa.exc.IntegrityError), db_session.begin_nested():
        f.make_org(db_session, slug=org_a.slug)


# ---------------------------------------------------------------------------
# Login resolution
# ---------------------------------------------------------------------------


def test_without_an_org_the_password_picks_the_one_matching_account(
    anon: TestClient, twins: tuple[User, User], org_a: Organization, org_b: Organization
) -> None:
    # Review fix (create-then-login oracle): with no code, the password is
    # checked against a FIXED number of candidates and the login succeeds only
    # when exactly one of them matches.  Each twin reaches their own org, so a
    # same-email account elsewhere neither locks them out nor changes the answer.
    for password, org in ((PW_A, org_a), (PW_B, org_b)):
        for typed in (SHARED, SHARED.upper(), f"  {SHARED} "):
            resp = _login(anon, typed, password)
            assert resp.status_code == 200, resp.text
            assert _org_claim(resp.json()["access_token"]) == str(org.id)


def test_without_an_org_a_password_matching_two_accounts_fails_closed(
    db_session: Session, anon: TestClient, twins: tuple[User, User]
) -> None:
    _a, b = twins
    b.password_hash = hash_password(PW_A)
    db_session.flush()
    wrong = _login(anon, "nobody@example.test", "nope")
    for typed in (SHARED, SHARED.upper()):
        resp = _login(anon, typed, PW_A)
        # Correct password for BOTH rows: refused, indistinguishable from an
        # unknown email (never "which" or "how many").
        assert resp.status_code == 401, resp.text
        assert _err(resp) == _err(wrong)
        assert "access_token" not in resp.text
    assert _login(anon, SHARED, PW_B).status_code == 401


def test_with_an_org_each_user_reaches_only_their_own_org(
    anon: TestClient, twins: tuple[User, User], org_a: Organization, org_b: Organization
) -> None:
    a, b = twins
    for user, org, password in ((a, org_a, PW_A), (b, org_b, PW_B)):
        for typed_org in (org.slug, org.slug.upper(), f" {org.slug} "):
            resp = _login(anon, SHARED, password, typed_org)
            assert resp.status_code == 200, resp.text
            tokens = resp.json()
            assert _org_claim(tokens["access_token"]) == str(org.id)
            assert _org_claim(tokens["refresh_token"]) == str(org.id)
            me = anon.get(
                f"{API}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            assert me.status_code == 200
            assert me.json()["id"] == str(user.id)


def test_wrong_or_unknown_org_is_the_generic_401(
    anon: TestClient,
    twins: tuple[User, User],
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
) -> None:
    wrong = _login(anon, "nobody@example.test", "nope")
    cases = [
        (SHARED, PW_A, org_b.slug),  # A's password in B's org
        (SHARED, PW_B, org_a.slug),  # B's password in A's org
        (users_a["employee"].email, f._DEFAULT_PASSWORD, org_b.slug),  # not in that org
        (SHARED, PW_A, "no-such-org"),
        (SHARED, PW_A, "x" * 100),
    ]
    for email, password, org in cases:
        resp = _login(anon, email, password, org)
        assert resp.status_code == 401, (org, resp.text)
        assert _err(resp) == _err(wrong)


def test_single_account_still_logs_in_without_an_org(
    anon: TestClient, users_a: dict[str, User], org_a: Organization
) -> None:
    emp = users_a["employee"]
    for org in (None, "", "   ", org_a.slug):
        resp = _login(anon, emp.email, f._DEFAULT_PASSWORD, org)
        assert resp.status_code == 200, (org, resp.text)
        assert _org_claim(resp.json()["access_token"]) == str(org_a.id)


def test_an_inactive_twin_does_not_make_the_email_ambiguous(
    db_session: Session, anon: TestClient, twins: tuple[User, User], org_b: Organization
) -> None:
    a, _b = twins
    a.status = UserStatus.inactive
    db_session.flush()
    resp = _login(anon, SHARED, PW_B)
    assert resp.status_code == 200, resp.text
    assert _org_claim(resp.json()["access_token"]) == str(org_b.id)


def test_a_deactivated_duplicate_with_the_same_password_is_ignored(
    db_session: Session, anon: TestClient, twins: tuple[User, User], org_b: Organization
) -> None:
    """Deactivated accounts are never candidates: a deactivated twin sharing the
    active user's password does not make the no-code login ambiguous."""
    a, b = twins
    a.password_hash = hash_password(PW_B)
    a.status = UserStatus.inactive
    db_session.flush()
    resp = _login(anon, SHARED, PW_B)
    assert resp.status_code == 200, resp.text
    assert _org_claim(resp.json()["access_token"]) == str(org_b.id)
    assert b.status == UserStatus.active


def test_inactive_accounts_never_take_a_candidate_slot(
    db_session: Session, anon: TestClient
) -> None:
    """Only ACTIVE accounts are candidates: K newer deactivated same-email
    accounts must not push the one active account out of the K slots."""
    import backend.app.modules.identity.service as service_mod

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    email = "returning@example.test"
    live_org = f.make_org(db_session)
    live = f.make_user(db_session, org=live_org, email=email, password_hash=hash_password(PW_A))
    dead = [
        f.make_user(db_session, org=f.make_org(db_session), email=email, status=UserStatus.inactive)
        for _ in range(k)
    ]
    db_session.flush()
    base = datetime.now(timezone.utc)
    live.created_at = base - timedelta(days=1)
    for i, user in enumerate(dead):
        user.created_at = base + timedelta(minutes=i)
    db_session.flush()
    resp = _login(anon, email, PW_A)
    assert resp.status_code == 200, resp.text
    assert _org_claim(resp.json()["access_token"]) == str(live_org.id)


def test_deactivated_users_cannot_log_in_on_either_path(
    db_session: Session,
    anon: TestClient,
    twins: tuple[User, User],
    users_a: dict[str, User],
    org_a: Organization,
) -> None:
    a, _b = twins
    a.status = UserStatus.inactive
    solo = users_a["employee"]
    solo.status = UserStatus.inactive
    db_session.flush()
    wrong = _login(anon, "nobody@example.test", "nope")
    for email, password, org in (
        (SHARED, PW_A, org_a.slug),
        (SHARED, PW_A, None),
        (solo.email, f._DEFAULT_PASSWORD, org_a.slug),
        (solo.email, f._DEFAULT_PASSWORD, None),
    ):
        resp = _login(anon, email, password, org)
        assert resp.status_code == 401, (email, org, resp.text)
        assert _err(resp) == _err(wrong)


def test_verify_count_never_depends_on_how_many_accounts_share_the_email(
    anon: TestClient,
    twins: tuple[User, User],
    db_session: Session,
    users_a: dict[str, User],
    org_a: Organization,
    org_b: Organization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timing model (review fix + integration security fix + phase 11 cap).

    Without a code every attempt verifies the password against every active
    account with the email, padded with the dummy hash to EXACTLY
    ``NO_ORG_LOGIN_CANDIDATES`` (K): 0, 1 or 2 matching accounts — success or
    failure — all run exactly K bcrypt verifies.  Phase 11: more than K
    accounts no longer run one verify per account (that was unbounded work per
    attempt); only the K oldest are verified.
    With a code every attempt runs exactly one.
    """
    import backend.app.modules.identity.service as service_mod

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    assert k >= 2
    real_verify = service_mod.verify_password
    calls: list[int] = []

    def counting(plaintext: str, hashed: str) -> bool:
        calls.append(1)
        return real_verify(plaintext, hashed)

    monkeypatch.setattr(service_mod, "verify_password", counting)
    inactive = f.make_user(db_session, org=org_a, status=UserStatus.inactive)
    crowd = "crowd@example.test"
    for _ in range(k + 2):
        f.make_user(db_session, org=f.make_org(db_session), email=crowd)
    emp = users_a["employee"].email
    no_org = {
        "unknown email": ("nobody@example.test", "nope", 401),
        "one account, wrong password": (emp, "nope", 401),
        "one account, right password": (emp, f._DEFAULT_PASSWORD, 200),
        "two accounts, A's password": (SHARED, PW_A, 200),
        "two accounts, wrong password": (SHARED, "nope", 401),
        "more accounts than K, same password": (crowd, f._DEFAULT_PASSWORD, 401),
        "more accounts than K, wrong password": (crowd, "nope", 401),
        "inactive": (inactive.email, f._DEFAULT_PASSWORD, 401),
    }
    for label, (email, password, status) in no_org.items():
        calls.clear()
        resp = _login(anon, email, password)
        assert resp.status_code == status, (label, resp.text)
        assert len(calls) == k, f"{label}: {len(calls)} verify calls"
    with_org = {
        "unknown org": (SHARED, PW_A, "no-such-org", 401),
        "email not in that org": (emp, f._DEFAULT_PASSWORD, org_b.slug, 401),
        "wrong password in org": (SHARED, PW_B, org_a.slug, 401),
        "inactive, in org": (inactive.email, f._DEFAULT_PASSWORD, org_a.slug, 401),
        "success in org": (SHARED, PW_A, org_a.slug, 200),
    }
    for label, (email, password, org, status) in with_org.items():
        calls.clear()
        resp = _login(anon, email, password, org)
        assert resp.status_code == status, (label, resp.text)
        assert len(calls) == 1, f"{label}: {len(calls)} verify calls"


def test_ambiguous_login_logs_only_a_count(
    db_session: Session,
    anon: TestClient,
    twins: tuple[User, User],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _a, b = twins
    b.password_hash = hash_password(PW_A)
    db_session.flush()
    with caplog.at_level("WARNING"):
        assert _login(anon, SHARED, PW_A).status_code == 401
    text = caplog.text.lower()
    assert "ambiguous" in text
    assert SHARED not in text


# ---------------------------------------------------------------------------
# Create-then-login is not an existence oracle (review fix)
# ---------------------------------------------------------------------------


def test_admin_probe_account_logs_in_whether_or_not_the_email_exists_elsewhere(
    finance_client: TestClient,
    anon: TestClient,
    users_b: dict[str, User],
    org_a: Organization,
    org_b: Organization,
) -> None:
    """The reviewer's reproduction: an org-A admin creates E with a password of
    their choice, then logs in as E with no code.  Before the fix the answer was
    401 when E was active in another org and 200 otherwise.  Now both are 200
    into org A, and the rightful org-B user is not locked out either."""
    taken = users_b["employee"].email
    fresh = "nobody-anywhere@example.test"
    results: dict[str, tuple[int, str]] = {}
    for email in (taken, fresh):
        created = finance_client.post(f"{API}/users", json=_create_body(email))
        assert created.status_code == 201, created.text
        resp = _login(anon, email, STRONG)
        claim = _org_claim(resp.json()["access_token"]) if resp.status_code == 200 else ""
        results[email] = (resp.status_code, claim)
    assert results[taken] == results[fresh] == (200, str(org_a.id)), results
    victim = _login(anon, taken, f._DEFAULT_PASSWORD)
    assert victim.status_code == 200, victim.text
    assert _org_claim(victim.json()["access_token"]) == str(org_b.id)


def _same_email_accounts(
    db_session: Session, email: str, n: int
) -> list[tuple[User, Organization]]:
    """``n`` active accounts with ``email`` in ``n`` organisations, password
    ``pw-<i>-Xx!``; users[0] is the OLDEST (the victim), the rest are newer."""
    users = []
    for i in range(n):
        org = f.make_org(db_session)
        users.append(
            (
                f.make_user(
                    db_session, org=org, email=email, password_hash=hash_password(f"pw-{i}-Xx!")
                ),
                org,
            )
        )
        db_session.flush()
    # created_at is the transaction time in this rolled-back session; make the
    # order explicit.
    base = datetime.now(timezone.utc)
    for i, (user, _org) in enumerate(users):
        user.created_at = base - timedelta(minutes=n - i)
    db_session.flush()
    return users


def test_every_account_up_to_the_cap_is_a_candidate(db_session: Session, anon: TestClient) -> None:
    """Integration security fix (no-org login DoS), kept under the phase-11 cap.
    The branch once tried only the newest few accounts, so admins of other
    organisations creating the victim's address pushed the victim's OLDER
    account out.  With up to K accounts sharing the email every one is still a
    candidate: the victim logs in with exactly one matching password, and so
    does each newer account."""
    import backend.app.modules.identity.service as service_mod

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    email = "popular@example.test"
    users = _same_email_accounts(db_session, email, k)
    for i, (_user, org) in enumerate(users):
        resp = _login(anon, email, f"pw-{i}-Xx!")
        assert resp.status_code == 200, (i, resp.text)
        assert _org_claim(resp.json()["access_token"]) == str(org.id)
    assert _login(anon, email, "pw-0-Xx!", users[0][1].slug).status_code == 200
    assert _login(anon, email, "not-any-of-them").status_code == 401


def test_over_the_cap_the_oldest_accounts_are_the_candidates(
    db_session: Session, anon: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration review fix (c): with MORE than K active accounts on one
    email the K OLDEST (created_at, id ascending) are the candidates, so newer
    duplicates created by other tenants can never crowd out an established
    account.  Every attempt still runs exactly K verifies, against those K real
    hashes; a newer account beyond the cap signs in with its organization code."""
    import backend.app.modules.identity.service as service_mod

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    email = "crowded@example.test"
    users = _same_email_accounts(db_session, email, k + 3)  # users[0] is the oldest
    wrong = _login(anon, "nobody@example.test", "nope")
    real_verify = service_mod.verify_password
    calls: list[str] = []

    def counting(plaintext: str, hashed: str) -> bool:
        calls.append(hashed)
        return real_verify(plaintext, hashed)

    monkeypatch.setattr(service_mod, "verify_password", counting)
    oldest_hashes = {u.password_hash for u, _org in users[:k]}
    for i, (_user, org) in enumerate(users):
        calls.clear()
        resp = _login(anon, email, f"pw-{i}-Xx!")
        assert len(calls) == k
        assert set(calls) == oldest_hashes  # always the K oldest, never a newer one
        if i < k:
            assert resp.status_code == 200, (i, resp.text)
            assert _org_claim(resp.json()["access_token"]) == str(org.id)
        else:
            assert resp.status_code == 401, (i, resp.text)
            assert _err(resp) == _err(wrong)
        ok = _login(anon, email, f"pw-{i}-Xx!", org.slug)
        assert ok.status_code == 200, (i, ok.text)
        assert _org_claim(ok.json()["access_token"]) == str(org.id)


def test_newer_tenant_duplicates_cannot_crowd_out_the_established_account(
    db_session: Session, anon: TestClient
) -> None:
    """Attacker admins in many organisations create the victim's address with
    passwords of their choice AFTER the victim's account exists: the victim's
    no-code login still works."""
    import backend.app.modules.identity.service as service_mod

    email = "victim.crowd@example.test"
    users = _same_email_accounts(db_session, email, service_mod.NO_ORG_LOGIN_CANDIDATES * 3)
    resp = _login(anon, email, "pw-0-Xx!")
    assert resp.status_code == 200, resp.text
    assert _org_claim(resp.json()["access_token"]) == str(users[0][1].id)


def test_over_cap_refusal_logs_no_email(
    db_session: Session, anon: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import backend.app.modules.identity.service as service_mod

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    email = "loud@example.test"
    _same_email_accounts(db_session, email, k + 1)
    with caplog.at_level("WARNING"):
        assert _login(anon, email, f"pw-{k}-Xx!").status_code == 401  # the newest one
    assert "oldest" in caplog.text
    assert email not in caplog.text.lower()


def test_ambiguous_no_code_failures_do_not_feed_the_email_signal(
    db_session: Session, anon: TestClient, app: Any
) -> None:
    """Failures caused by the cap / an ambiguous match are not evidence against
    the email: they never count toward the per-email signal."""
    import backend.app.modules.identity.service as service_mod
    from backend.tests.rate_limit_support import installed, make_limiter

    k = service_mod.NO_ORG_LOGIN_CANDIDATES
    email = "ambiguous.signal@example.test"
    _same_email_accounts(db_session, email, k + 1)
    limiter = make_limiter(LOGIN_BACKOFF_THRESHOLD=1)
    with installed(app, limiter):
        assert _login(anon, email, f"pw-{k}-Xx!").status_code == 401  # beyond the cap
        assert limiter.snapshot()["login_email_soft_signals"] == 0
        assert _login(anon, "plain.wrong@example.test", "nope").status_code == 401
        assert limiter.snapshot()["login_email_soft_signals"] == 1


def test_victim_logs_in_despite_an_attacker_org_duplicate(
    finance_client: TestClient,
    anon: TestClient,
    users_b: dict[str, User],
    org_b: Organization,
) -> None:
    """Another org's admin creates the victim's address with a password of their
    choice; the victim's no-code login with their own password still works."""
    victim = users_b["employee"]
    created = finance_client.post(f"{API}/users", json=_create_body(victim.email))
    assert created.status_code == 201, created.text
    resp = _login(anon, victim.email, f._DEFAULT_PASSWORD)
    assert resp.status_code == 200, resp.text
    assert _org_claim(resp.json()["access_token"]) == str(org_b.id)


# ---------------------------------------------------------------------------
# User administration: own-org uniqueness only, nothing about other orgs leaks
# ---------------------------------------------------------------------------


def _create_body(email: str) -> dict[str, Any]:
    return {
        "email": email,
        "name": "New Hire",
        "role": "employee",
        "department": "Sales",
        "password": STRONG,
    }


def test_admin_can_create_an_address_that_exists_in_another_org(
    finance_client: TestClient,
    db_session: Session,
    anon: TestClient,
    users_a: dict[str, User],
    users_b: dict[str, User],
    org_a: Organization,
    org_b: Organization,
) -> None:
    other = users_b["employee"]
    resp = finance_client.post(f"{API}/users", json=_create_body(other.email.upper()))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["organization_id"] == str(org_a.id)
    # Nothing about the other tenant comes back.
    assert str(org_b.id) not in resp.text and str(other.id) not in resp.text
    assert other.name not in resp.text
    # No refusal was audited (there was nothing to refuse).
    assert (
        db_session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "user.create_refused")
        )
        == 0
    )
    # The rightful user is never locked out: they name their organization.
    ok = _login(anon, other.email, f._DEFAULT_PASSWORD, org_b.slug)
    assert ok.status_code == 200, ok.text
    assert _org_claim(ok.json()["access_token"]) == str(org_b.id)
    # And the squatter's password does not open the victim's org.
    assert _login(anon, other.email, STRONG, org_b.slug).status_code == 401
    assert _login(anon, other.email, STRONG, org_a.slug).status_code == 200


def test_same_org_duplicate_is_the_uniform_conflict(
    finance_client: TestClient, users_a: dict[str, User]
) -> None:
    resp = finance_client.post(f"{API}/users", json=_create_body(users_a["employee"].email.upper()))
    assert resp.status_code == 409
    assert _err(resp)["code"] == "EMAIL_TAKEN"
    assert _err(resp)["message"] == "A user with this email already exists."


@pytest.mark.parametrize("stored", ["race.case@example.test", "Race.Case@Example.TEST"])
def test_same_org_racing_duplicate_maps_to_email_taken(
    finance_client: TestClient,
    db_session: Session,
    org_a: Organization,
    stored: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exact-case clash hits the original uq_users_organization_id_email; a
    # legacy mixed-case row is caught ONLY by the case-insensitive
    # uq_users_org_email_lower.  Both must map to the same 409.
    import backend.app.modules.identity.admin as admin_mod

    f.make_user(db_session, org=org_a, email=stored)
    monkeypatch.setattr(admin_mod, "_ensure_email_free", lambda *a, **k: None)
    resp = finance_client.post(f"{API}/users", json=_create_body("race.case@example.test"))
    assert resp.status_code == 409, resp.text
    assert _err(resp)["code"] == "EMAIL_TAKEN"


# ---------------------------------------------------------------------------
# Tokens stay bound to the organization they were issued for
# ---------------------------------------------------------------------------


def test_tokens_with_a_foreign_org_claim_are_refused(
    anon: TestClient, users_a: dict[str, User], org_b: Organization
) -> None:
    user = users_a["employee"]
    forged_access = create_access_token(user.id, org_b.id)
    forged_refresh = create_refresh_token(user.id, org_b.id)
    me = anon.get(f"{API}/me", headers={"Authorization": f"Bearer {forged_access}"})
    assert me.status_code == 401
    refresh = anon.post(f"{API}/auth/refresh", json={"refresh_token": forged_refresh})
    assert refresh.status_code == 401


def test_refresh_keeps_the_org_of_the_login(
    anon: TestClient, twins: tuple[User, User], org_b: Organization
) -> None:
    login = _login(anon, SHARED, PW_B, org_b.slug)
    refreshed = anon.post(
        f"{API}/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert _org_claim(refreshed.json()["access_token"]) == str(org_b.id)


# ---------------------------------------------------------------------------
# GET /organization — the login code, read-only, finance admins only
# ---------------------------------------------------------------------------


def test_finance_admin_sees_their_own_org_login_code(
    finance_client: TestClient, org_a: Organization, org_b: Organization
) -> None:
    resp = finance_client.get(f"{API}/organization")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"id": str(org_a.id), "name": org_a.name, "login_code": org_a.slug}
    assert org_b.slug not in resp.text


@pytest.mark.parametrize("client_fixture", ["employee_client", "manager_client"])
def test_non_finance_cannot_read_the_org_endpoint(
    request: pytest.FixtureRequest, client_fixture: str
) -> None:
    client: TestClient = request.getfixturevalue(client_fixture)
    assert client.get(f"{API}/organization").status_code == 403


def test_org_endpoint_is_read_only(finance_client: TestClient) -> None:
    for method in ("post", "put", "patch", "delete"):
        resp = getattr(finance_client, method)(f"{API}/organization")
        assert resp.status_code == 405, method


def test_login_rejects_unknown_body_fields_quietly(anon: TestClient) -> None:
    # Unknown fields were always ignored by LoginRequest; nothing about that
    # changes (the demo client sends exactly email + password).
    resp = anon.post(
        f"{API}/auth/login",
        json={"email": "nobody@example.test", "password": "x", "org_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Demo seeds never act on another organization's same-email account
# ---------------------------------------------------------------------------


def test_seed_password_resync_ignores_a_same_email_user_in_another_org(
    db_session: Session,
) -> None:
    from database.seeds.seed_all import _resync_user_passwords

    email = "employee@atlas-demo.internal"
    # On a fresh test DB the squatter is the only row with this email, so an
    # unscoped lookup would pick it (mutation-proven); the assertion holds either way.
    outsider_org = f.make_org(db_session, name="Not The Demo Org")
    squatter = f.make_user(
        db_session, org=outsider_org, email=email, password_hash=hash_password(PW_A)
    )
    before = squatter.password_hash
    _resync_user_passwords(db_session)
    db_session.flush()
    db_session.refresh(squatter)
    assert squatter.password_hash == before
