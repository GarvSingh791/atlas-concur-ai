"""Product gap 3 — global search (GET /search) is scoped exactly like the reads.

Fixture world (org A): manager -> {employee, employee2}; manager2 -> {employee3};
finance.  Every "Lisbon" record exists for employee AND for employee3 (another
team) AND in org B, so each assertion proves both inclusion and exclusion.

DB-backed (shared conftest: rolled-back session, real JWT role clients).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.app.modules.search import service as search_service
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import OcrStatus
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

API = "/api/v1"


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _search(client: TestClient, q: str, **params: Any) -> dict[str, list[dict[str, Any]]]:
    resp = client.get(f"{API}/search", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return {g["key"]: g["items"] for g in resp.json()["groups"]}


def _ids(groups: dict[str, list[dict[str, Any]]], key: str) -> set[str]:
    return {i["id"] for i in groups.get(key, [])}


def _world_for(db: Session, owner: User, tag: str) -> dict[str, Any]:
    """One Lisbon trip / report / entry / receipt (+ approval on the report)."""
    trip = f.make_trip(
        db,
        traveler=owner,
        origin="NYC",
        primary_destination="Lisbon",
        purpose=f"Summit {tag}",
        status=TripStatus.pending_approval,
    )
    report = f.make_expense_report(
        db, owner=owner, title=f"Lisbon summit {tag}", status=ExpenseReportStatus.submitted
    )
    entry = f.make_expense(db, report=report, vendor=f"Lisbon Cafe {tag}", amount="12.50")
    receipt = f.make_receipt(
        db,
        uploaded_by=owner,
        ocr_status=OcrStatus.processed,
        extracted_fields={"vendor": f"Lisbon Taxi {tag}", "amount": 30, "currency": "EUR"},
        expense_id=entry.id,
    )
    approval = f.make_approval(
        db,
        submitter=owner,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        current_status=ApprovalStatus.pending,
    )
    trip_approval = f.make_approval(
        db,
        submitter=owner,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.pending,
    )
    return {
        "trip": str(trip.id),
        "report": str(report.id),
        "entry": str(entry.id),
        "receipt": str(receipt.id),
        "approval": str(approval.id),
        "trip_approval": str(trip_approval.id),
    }


@pytest.fixture()
def world(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
    rbac_catalog: None,
) -> dict[str, Any]:
    manager2 = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    employee2 = f.make_user(
        db_session, org=org_a, role=RoleKey.employee, manager_id=users_a["manager"].id
    )
    employee3 = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=manager2.id)
    return {
        "users": {**users_a, "manager2": manager2, "employee2": employee2, "employee3": employee3},
        "users_b": users_b,
        "emp": _world_for(db_session, users_a["employee"], "E1"),
        "mgr": _world_for(db_session, users_a["manager"], "M1"),
        "emp3": _world_for(db_session, employee3, "E3"),
        "orgb": _world_for(db_session, users_b["employee"], "B"),
    }


def _expect(groups: dict[str, list[dict[str, Any]]], visible: list[dict[str, Any]]) -> None:
    for key, field in (
        ("trips", "trip"),
        ("reports", "report"),
        ("entries", "entry"),
        ("receipts", "receipt"),
    ):
        assert _ids(groups, key) == {w[field] for w in visible}, key


# ---------------------------------------------------------------------------
# Scope per role
# ---------------------------------------------------------------------------


def test_employee_sees_only_own_records_and_no_approvals(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users"]["employee"]), "lisbon")
    _expect(groups, [world["emp"]])
    assert "approvals" not in groups


def test_same_role_peer_sees_none_of_it(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users"]["employee2"]), "lisbon")
    _expect(groups, [])


def test_manager_sees_own_and_direct_reports_only(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users"]["manager"]), "lisbon")
    _expect(groups, [world["mgr"], world["emp"]])
    assert _ids(groups, "approvals") == {
        world["mgr"]["approval"],
        world["mgr"]["trip_approval"],
        world["emp"]["approval"],
        world["emp"]["trip_approval"],
    }


def test_other_manager_does_not_see_this_team(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users"]["manager2"]), "lisbon")
    _expect(groups, [world["emp3"]])


def test_finance_sees_org_wide_but_not_other_tenant(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users"]["finance"]), "lisbon", limit=10)
    _expect(groups, [world["emp"], world["mgr"], world["emp3"]])
    assert world["orgb"]["approval"] not in _ids(groups, "approvals")
    assert world["emp3"]["approval"] in _ids(groups, "approvals")


def test_other_tenant_sees_only_its_own(app: Any, world: dict[str, Any]) -> None:
    groups = _search(_client(app, world["users_b"]["employee"]), "lisbon")
    _expect(groups, [world["orgb"]])
    finance_b = _search(_client(app, world["users_b"]["finance"]), "lisbon", limit=10)
    _expect(finance_b, [world["orgb"]])


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_for_owner_and_for_manager_reading_a_teammate(
    app: Any, world: dict[str, Any]
) -> None:
    emp = world["emp"]
    own = _search(_client(app, world["users"]["employee"]), "lisbon")
    assert own["reports"][0]["link"] == f"/expenses?report={emp['report']}"
    assert own["reports"][0]["fallback_link"] is None  # employees cannot read approvals
    assert own["trips"][0]["link"] == f"/trips?trip={emp['trip']}"
    assert own["entries"][0]["link"] == f"/expenses?report={emp['report']}"
    assert own["receipts"][0]["link"] == f"/expenses?report={emp['report']}"

    mgr = _search(_client(app, world["users"]["manager"]), "Summit E1")
    report = next(i for i in mgr["reports"] if i["id"] == emp["report"])
    # The teammate's report opens on the (read-only) report view; the approval
    # view is offered as the fallback.
    assert report["link"] == f"/expenses?report={emp['report']}"
    assert report["fallback_link"] == f"/approvals?approval={emp['approval']}"
    trip = next(i for i in mgr["trips"] if i["id"] == emp["trip"])
    assert trip["link"] is None  # My Trips never lists a teammate's trip
    assert trip["fallback_link"] == f"/approvals?approval={emp['trip_approval']}"
    approval = next(i for i in mgr["approvals"] if i["id"] == emp["approval"])
    assert approval["link"] == f"/approvals?approval={emp['approval']}"


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", ["", " ", "a", " a "])
def test_short_queries_are_refused(app: Any, world: dict[str, Any], q: str) -> None:
    resp = _client(app, world["users"]["employee"]).get(f"{API}/search", params={"q": q})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "QUERY_TOO_SHORT"


def test_overlong_query_is_refused(app: Any, world: dict[str, Any]) -> None:
    resp = _client(app, world["users"]["employee"]).get(f"{API}/search", params={"q": "x" * 101})
    assert resp.status_code == 422


@pytest.mark.parametrize("q", ["%%", "__", "\\\\", "%_"])
def test_wildcards_match_literally(app: Any, world: dict[str, Any], q: str) -> None:
    """Unescaped, ``%%`` / ``__`` would match every record in scope."""
    assert _search(_client(app, world["users"]["finance"]), q) == {}


def test_literal_percent_is_findable(app: Any, db_session: Session, world: dict[str, Any]) -> None:
    emp = world["users"]["employee"]
    report = f.make_expense_report(db_session, owner=emp, title="100% offsite")
    f.make_expense_report(db_session, owner=emp, title="1000 offsite")
    groups = _search(_client(app, emp), "100%")
    assert _ids(groups, "reports") == {str(report.id)}


def test_per_group_cap_and_truncation(app: Any, db_session: Session, world: dict[str, Any]) -> None:
    emp = world["users"]["employee"]
    for n in range(search_service.MAX_PER_GROUP + 3):
        f.make_expense_report(db_session, owner=emp, title=f"Capped report {n}")
    client = _client(app, emp)
    resp = client.get(f"{API}/search", params={"q": "capped", "limit": 3}).json()
    (group,) = resp["groups"]
    assert len(group["items"]) == 3 and group["truncated"] is True
    resp = client.get(f"{API}/search", params={"q": "capped", "limit": 500}).json()
    assert len(resp["groups"][0]["items"]) == search_service.MAX_PER_GROUP


# ---------------------------------------------------------------------------
# Policy rules
# ---------------------------------------------------------------------------


def test_policy_rules_everyone_reads_active_only_managers_of_policy_see_drafts(
    app: Any, db_session: Session, org_a: Organization, world: dict[str, Any]
) -> None:
    policy = f.make_policy(db_session, org=org_a)
    active = f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.hotel,
        source_clause_ref="LODG-07",
    )
    draft = f.make_policy_rule(
        db_session, policy=policy, source_clause_ref="LODG-99", status=PolicyRuleStatus.draft
    )
    emp_groups = _search(_client(app, world["users"]["employee"]), "lodg")
    assert _ids(emp_groups, "policy_rules") == {str(active.id)}
    fin_groups = _search(_client(app, world["users"]["finance"]), "lodg")
    assert _ids(fin_groups, "policy_rules") == {str(active.id), str(draft.id)}
    # Title is the derived rule title (matched too).
    assert _ids(_search(_client(app, world["users"]["employee"]), "hotel cap"), "policy_rules") == {
        str(active.id)
    }
    # Org B never sees org A's rules.
    assert "policy_rules" not in _search(_client(app, world["users_b"]["employee"]), "lodg")


def test_unauthenticated_is_401(app: Any) -> None:
    resp = TestClient(app, raise_server_exceptions=False).get(f"{API}/search", params={"q": "xx"})
    assert resp.status_code == 401
