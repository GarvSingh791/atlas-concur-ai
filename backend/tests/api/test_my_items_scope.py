"""UAT2 Issue 7 — personal lists (``?mine=true``) + read-only non-owner detail.

Contract:
  * ``GET /expense-reports?mine=true`` and ``GET /trips?mine=true`` return ONLY
    the caller's own items, for every role (the personal "My expenses" /
    "My Trips" pages always send it).
  * Without ``mine`` the role read scope is UNCHANGED (manager = self + direct
    reports, finance_admin = org-wide) — approvals, dashboards, analytics and the
    existing contract tests depend on it.
  * ``mine`` only narrows (AND): combined with a foreign ``owner_id`` filter the
    result is empty; a non-boolean value is a 422 in the standard envelope.
  * A manager can still READ a direct report's draft by id (now with the
    owner's display name for the read-only banner), but every owner action —
    including the UAT2 delete / attach endpoints — stays 403 / 404 and changes
    nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from backend.tests.receipt_images import scenario_upload
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.receipt import OcrStatus
from database.models.trip import TripStatus

pytestmark = pytest.mark.db


def _ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text[:300]
    return {item["id"] for item in resp.json()["items"]}


@pytest.fixture()
def team(db_session: Session, org_a: Organization, users_a: dict[str, User]) -> dict[str, Any]:
    """One report + one trip each for the manager, their direct report, finance,
    and an unrelated same-org employee."""
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    out: dict[str, Any] = {"stranger": stranger}
    for key, user in (
        ("manager", users_a["manager"]),
        ("employee", users_a["employee"]),
        ("finance", users_a["finance"]),
        ("stranger", stranger),
    ):
        out[f"{key}_report"] = f.make_expense_report(db_session, owner=user, title=f"{key} report")
        out[f"{key}_trip"] = f.make_trip(db_session, traveler=user)
    return out


# ---------------------------------------------------------------------------
# Lists — ?mine=true narrows, the default scope is unchanged
# ---------------------------------------------------------------------------


def test_manager_mine_lists_only_own_reports_default_keeps_team(
    team: dict[str, Any], users_a: dict[str, User], manager_client: TestClient
) -> None:
    mine = manager_client.get(f"{API}/expense-reports", params={"mine": "true", "size": 100})
    assert _ids(mine) == {str(team["manager_report"].id)}
    assert mine.json()["total"] == 1
    assert all(i["owner_id"] == str(users_a["manager"].id) for i in mine.json()["items"])

    default = _ids(manager_client.get(f"{API}/expense-reports", params={"size": 100}))
    assert {str(team["manager_report"].id), str(team["employee_report"].id)} <= default
    assert str(team["stranger_report"].id) not in default


def test_manager_mine_lists_only_own_trips_default_keeps_team(
    team: dict[str, Any], users_a: dict[str, User], manager_client: TestClient
) -> None:
    mine = manager_client.get(f"{API}/trips", params={"mine": "true", "size": 100})
    assert _ids(mine) == {str(team["manager_trip"].id)}
    assert mine.json()["total"] == 1

    default = _ids(manager_client.get(f"{API}/trips", params={"size": 100}))
    assert {str(team["manager_trip"].id), str(team["employee_trip"].id)} <= default
    assert str(team["stranger_trip"].id) not in default


def test_finance_mine_is_own_only_and_default_still_org_wide(
    team: dict[str, Any], finance_client: TestClient
) -> None:
    for path, key in (("expense-reports", "report"), ("trips", "trip")):
        mine = _ids(finance_client.get(f"{API}/{path}", params={"mine": "true", "size": 100}))
        assert mine == {str(team[f"finance_{key}"].id)}
        default = _ids(finance_client.get(f"{API}/{path}", params={"size": 100}))
        expected = {str(team[f"{who}_{key}"].id) for who in ("manager", "employee", "stranger")}
        assert expected <= default


def test_employee_mine_equals_default(team: dict[str, Any], employee_client: TestClient) -> None:
    for path, key in (("expense-reports", "report"), ("trips", "trip")):
        mine = _ids(employee_client.get(f"{API}/{path}", params={"mine": "true"}))
        default = _ids(employee_client.get(f"{API}/{path}"))
        assert mine == default == {str(team[f"employee_{key}"].id)}


def test_mine_false_is_the_default_scope(team: dict[str, Any], manager_client: TestClient) -> None:
    explicit = _ids(manager_client.get(f"{API}/expense-reports", params={"mine": "false"}))
    default = _ids(manager_client.get(f"{API}/expense-reports"))
    assert explicit == default
    assert str(team["employee_report"].id) in explicit


def test_mine_only_narrows_combined_with_a_foreign_owner_filter(
    team: dict[str, Any], users_a: dict[str, User], manager_client: TestClient
) -> None:
    employee_id = str(users_a["employee"].id)
    reports = manager_client.get(
        f"{API}/expense-reports", params={"mine": "true", "owner_id": employee_id}
    )
    assert reports.status_code == 200 and reports.json()["total"] == 0
    trips = manager_client.get(f"{API}/trips", params={"mine": "true", "traveler_id": employee_id})
    assert trips.status_code == 200 and trips.json()["total"] == 0
    # Other filters still compose with mine.
    drafts = manager_client.get(
        f"{API}/expense-reports", params={"mine": "true", "status": "draft"}
    )
    assert _ids(drafts) == {str(team["manager_report"].id)}


@pytest.mark.parametrize("path", ["expense-reports", "trips"])
def test_mine_with_a_non_boolean_value_is_422(
    team: dict[str, Any], manager_client: TestClient, path: str
) -> None:
    assert_error_envelope(manager_client.get(f"{API}/{path}", params={"mine": "banana"}), 422)


# ---------------------------------------------------------------------------
# Detail — readable (with the owner's name) but never writable by a non-owner
# ---------------------------------------------------------------------------


def test_detail_carries_owner_display_name(
    team: dict[str, Any], users_a: dict[str, User], manager_client: TestClient
) -> None:
    report = manager_client.get(f"{API}/expense-reports/{team['employee_report'].id}")
    assert report.status_code == 200
    assert report.json()["owner_name"] == users_a["employee"].name
    trip = manager_client.get(f"{API}/trips/{team['employee_trip'].id}")
    assert trip.status_code == 200
    assert trip.json()["traveler_name"] == users_a["employee"].name
    own = manager_client.get(f"{API}/expense-reports/{team['manager_report'].id}")
    assert own.json()["owner_name"] == users_a["manager"].name


def test_manager_reads_teammates_draft_but_every_owner_action_is_refused(
    db_session: Session,
    team: dict[str, Any],
    users_a: dict[str, User],
    manager_client: TestClient,
    local_providers: None,
) -> None:
    report: ExpenseReport = team["employee_report"]
    entry = f.make_expense(db_session, report=report, amount="40.00")
    report.total_amount = entry.amount
    trip = team["employee_trip"]
    own_draft = team["manager_report"]
    own_receipt = f.make_receipt(
        db_session, uploaded_by=users_a["manager"], ocr_status=OcrStatus.processed
    )
    db_session.flush()
    rid, eid = report.id, entry.id

    assert manager_client.get(f"{API}/expense-reports/{rid}").status_code == 200
    assert manager_client.get(f"{API}/expense-reports/{rid}/entries").status_code == 200

    refused = {
        "submit": manager_client.patch(
            f"{API}/expense-reports/{rid}", json={"status": "submitted"}
        ),
        "rename": manager_client.patch(
            f"{API}/expense-reports/{rid}/details", json={"title": "hijacked"}
        ),
        "move": manager_client.post(
            f"{API}/expense-reports/{rid}/entries/move",
            json={"entry_ids": [str(eid)], "target_report_id": str(own_draft.id)},
        ),
        "edit_entry": manager_client.patch(f"{API}/expenses/{eid}", json={"amount": "1.00"}),
        "attach_existing": manager_client.post(
            f"{API}/expenses/{eid}/receipts", json={"receipt_id": str(own_receipt.id)}
        ),
        "delete_entry": manager_client.delete(f"{API}/expenses/{eid}"),
        "delete_report": manager_client.delete(f"{API}/expense-reports/{rid}"),
        "submit_trip": manager_client.patch(
            f"{API}/trips/{trip.id}", json={"status": "pending_approval"}
        ),
        "cancel_trip": manager_client.patch(f"{API}/trips/{trip.id}", json={"status": "cancelled"}),
    }
    for action, resp in refused.items():
        assert resp.status_code == 403, f"{action}: {resp.status_code} {resp.text[:200]}"
        assert_error_envelope(resp, 403)

    # Upload-and-attach is owner-scoped before anything is stored: 404, no leak.
    upload = manager_client.post(
        f"{API}/receipts",
        params={"expense_id": str(eid)},
        files={"file": scenario_upload("fake", "r.png")},
    )
    assert_error_envelope(upload, 404)

    db_session.expire_all()
    fresh = db_session.get(ExpenseReport, rid)
    assert fresh is not None
    assert fresh.status == ExpenseReportStatus.draft
    assert fresh.title == "employee report"
    kept = db_session.get(Expense, eid)
    assert kept is not None and kept.report_id == rid
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft
    db_session.refresh(own_receipt)
    assert own_receipt.expense_id is None
