"""Product gap 5 — manager Team view (PRD US-M3): GET /team/members[/{user_id}].

Contract:
  * The team is EXACTLY the existing read scope: a manager's direct reports
    (rbac.direct_report_ids — not the manager, not same-org strangers, never
    another org); finance_admin → every user of the org; employee → 403.
  * Each member carries open trips, reports by status, pending approvals and
    month-to-date / trailing-90-day spend (approved + reimbursed reports, base
    currency only, foreign-currency reports counted separately), as Decimal
    strings.
  * The drill-down returns the member's trips + reports (paginated); anyone
    outside the caller's team is a 404 (no existence leak).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from database.models.approval import ApprovalStatus
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

URL = f"{API}/team/members"


def _ids(resp: Any) -> list[str]:
    assert resp.status_code == 200, resp.text[:300]
    return [m["id"] for m in resp.json()["items"]]


@pytest.fixture()
def team(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> dict[str, Any]:
    manager = users_a["manager"]
    alice = users_a["employee"]  # direct report (fixture)
    alice.name = "Alice Report"
    alice.department = "Engineering"
    bob = f.make_user(
        db_session,
        org=org_a,
        role=RoleKey.employee,
        manager_id=manager.id,
        name="Bob Report",
        department="Sales",
    )
    stranger = f.make_user(db_session, org=org_a, name="Stranger Danger", department="Sales")
    db_session.flush()

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    in_month = month_start + (now - month_start) / 2

    # Alice: spend this month (USD) + 60 days ago (USD) + a EUR report + an old one.
    f.make_expense_report(
        db_session,
        owner=alice,
        status=ExpenseReportStatus.approved,
        total_amount=Decimal("100.50"),
        submitted_at=in_month,
    )
    f.make_expense_report(
        db_session,
        owner=alice,
        status=ExpenseReportStatus.reimbursed,
        total_amount=Decimal("200.00"),
        submitted_at=now - timedelta(days=60),
    )
    f.make_expense_report(
        db_session,
        owner=alice,
        status=ExpenseReportStatus.approved,
        total_amount=Decimal("999.00"),
        currency="EUR",
        submitted_at=now - timedelta(days=10),
    )
    f.make_expense_report(
        db_session,
        owner=alice,
        status=ExpenseReportStatus.approved,
        total_amount=Decimal("5000.00"),
        submitted_at=now - timedelta(days=120),
    )
    submitted = f.make_expense_report(
        db_session,
        owner=alice,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("75.00"),
        submitted_at=now - timedelta(days=1),
    )
    f.make_expense_report(db_session, owner=alice)  # draft, no spend
    f.make_approval(db_session, submitter=alice, subject_id=submitted.id)
    f.make_approval(
        db_session, submitter=alice, current_status=ApprovalStatus.approved
    )  # decided — not pending

    trips = {
        status: f.make_trip(db_session, traveler=alice, status=status)
        for status in (
            TripStatus.draft,
            TripStatus.pending_approval,
            TripStatus.approved,
            TripStatus.completed,
            TripStatus.cancelled,
        )
    }

    # Bob: a big report (sorts first by spend) + 2 pending approvals.
    f.make_expense_report(
        db_session,
        owner=bob,
        status=ExpenseReportStatus.approved,
        total_amount=Decimal("1500.00"),
        submitted_at=now - timedelta(days=30),
    )
    f.make_approval(db_session, submitter=bob)
    f.make_approval(db_session, submitter=bob)

    # Stranger + manager + other org: must never show for the manager.
    stranger_report = f.make_expense_report(db_session, owner=stranger, title="stranger report")
    stranger_trip = f.make_trip(db_session, traveler=stranger)
    f.make_expense_report(db_session, owner=manager, title="manager own")
    f.make_trip(db_session, traveler=manager)
    f.make_expense_report(db_session, owner=users_b["employee"])
    db_session.flush()
    return {
        "manager": manager,
        "alice": alice,
        "bob": bob,
        "stranger": stranger,
        "stranger_report": stranger_report,
        "stranger_trip": stranger_trip,
        "trips": trips,
        "other_org_user": users_b["employee"],
    }


# ---------------------------------------------------------------------------
# Team membership — exactly the direct reports
# ---------------------------------------------------------------------------


def test_manager_team_is_exactly_the_direct_reports(
    team: dict[str, Any], manager_client: TestClient
) -> None:
    resp = manager_client.get(URL, params={"size": 100})
    assert set(_ids(resp)) == {str(team["alice"].id), str(team["bob"].id)}
    assert resp.json()["total"] == 2
    # Deterministic default order: name ascending.
    assert [m["name"] for m in resp.json()["items"]] == ["Alice Report", "Bob Report"]


def test_member_metrics(team: dict[str, Any], manager_client: TestClient) -> None:
    items = {m["id"]: m for m in manager_client.get(URL).json()["items"]}
    alice = items[str(team["alice"].id)]
    assert alice["department"] == "Engineering"
    assert alice["open_trips"] == 3  # draft + pending_approval + approved
    assert alice["reports_by_status"] == {
        "draft": 1,
        "submitted": 1,
        "approved": 3,
        "rejected": 0,
        "reimbursed": 1,
    }
    assert alice["pending_approvals"] == 1
    assert Decimal(alice["spend_mtd"]) == Decimal("100.50")
    assert Decimal(alice["spend_90d"]) == Decimal("300.50")  # EUR + 120-day-old excluded
    assert alice["currency"] == "USD"
    assert alice["other_currency_reports"] == 1
    assert isinstance(alice["spend_90d"], str)  # Decimal on the wire, never float
    assert "email" not in alice

    bob = items[str(team["bob"].id)]
    assert bob["pending_approvals"] == 2
    assert Decimal(bob["spend_90d"]) == Decimal("1500.00")


def test_sort_and_search(team: dict[str, Any], manager_client: TestClient) -> None:
    by_spend = _ids(manager_client.get(URL, params={"sort": "-spend_90d"}))
    assert by_spend == [str(team["bob"].id), str(team["alice"].id)]
    by_pending = _ids(manager_client.get(URL, params={"sort": "pending_approvals:asc"}))
    assert by_pending == [str(team["alice"].id), str(team["bob"].id)]
    assert _ids(manager_client.get(URL, params={"q": "sales"})) == [str(team["bob"].id)]
    assert _ids(manager_client.get(URL, params={"q": "ALICE"})) == [str(team["alice"].id)]
    # The stranger (Sales) is never searchable by a manager.
    assert _ids(manager_client.get(URL, params={"q": "stranger"})) == []


def test_pagination_totals(team: dict[str, Any], manager_client: TestClient) -> None:
    page1 = manager_client.get(URL, params={"size": 1, "page": 1}).json()
    page2 = manager_client.get(URL, params={"size": 1, "page": 2}).json()
    assert page1["total"] == page2["total"] == 2 and page1["pages"] == 2
    assert {page1["items"][0]["id"], page2["items"][0]["id"]} == {
        str(team["alice"].id),
        str(team["bob"].id),
    }


@pytest.mark.parametrize(
    "params", [{"department": "x"}, {"sort": "email"}, {"q": "x" * 101}, {"sort": "name:up"}]
)
def test_invalid_params_are_400(
    team: dict[str, Any], manager_client: TestClient, params: dict[str, str]
) -> None:
    assert_error_envelope(manager_client.get(URL, params=params), 400)


def test_employee_is_forbidden(team: dict[str, Any], employee_client: TestClient) -> None:
    assert_error_envelope(employee_client.get(URL), 403)
    assert_error_envelope(employee_client.get(f"{URL}/{team['alice'].id}"), 403)


def test_finance_team_is_org_wide_and_tenant_bound(
    team: dict[str, Any], users_a: dict[str, User], finance_client: TestClient
) -> None:
    ids = set(_ids(finance_client.get(URL, params={"size": 100})))
    expected = {str(team[k].id) for k in ("alice", "bob", "stranger", "manager")}
    assert expected | {str(users_a["finance"].id)} == ids
    assert str(team["other_org_user"].id) not in ids


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


def test_member_detail_lists_only_that_members_items(
    team: dict[str, Any], manager_client: TestClient
) -> None:
    resp = manager_client.get(f"{URL}/{team['alice'].id}", params={"size": 50})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["member"]["id"] == str(team["alice"].id)
    assert body["trips"]["total"] == 5
    assert {t["id"] for t in body["trips"]["items"]} == {str(t.id) for t in team["trips"].values()}
    assert body["reports"]["total"] == 6
    assert {r["owner_id"] for r in body["reports"]["items"]} == {str(team["alice"].id)}


def test_member_detail_paginates_each_list(
    team: dict[str, Any], manager_client: TestClient
) -> None:
    body = manager_client.get(
        f"{URL}/{team['alice'].id}", params={"size": 2, "trips_page": 3, "reports_page": 2}
    ).json()
    assert body["trips"]["page"] == 3 and len(body["trips"]["items"]) == 1
    assert body["reports"]["page"] == 2 and len(body["reports"]["items"]) == 2


@pytest.mark.parametrize("who", ["stranger", "manager", "other_org_user", "random"])
def test_outside_the_team_is_404(
    team: dict[str, Any], manager_client: TestClient, who: str
) -> None:
    target = uuid.uuid4() if who == "random" else team[who].id
    assert_error_envelope(manager_client.get(f"{URL}/{target}"), 404)


def test_finance_can_open_any_org_member_but_not_another_org(
    team: dict[str, Any], finance_client: TestClient
) -> None:
    resp = finance_client.get(f"{URL}/{team['stranger'].id}")
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()["reports"]["items"]} == {str(team["stranger_report"].id)}
    assert_error_envelope(finance_client.get(f"{URL}/{team['other_org_user'].id}"), 404)


def test_manager_with_no_reports_has_an_empty_team(
    db_session: Session, org_a: Organization, app: Any, rbac_catalog: None
) -> None:
    from backend.tests.conftest import _bearer_client

    lonely = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    client = _bearer_client(app, lonely)
    resp = client.get(URL)
    assert resp.status_code == 200
    assert resp.json()["items"] == [] and resp.json()["total"] == 0
