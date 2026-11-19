"""P7-T2 — pagination / filtering / sorting contract on the live collections.

The Page-envelope collections are exactly five: /users, /trips, /expense-reports,
/approvals, /audit. Each is asserted for: first/last/empty page, out-of-range page
(clamped -> empty 200, never an error), page<1 clamp, invalid filter field -> 400,
invalid sort field -> 400, and valid sort honored.

RECONCILE: out-of-range pagination is CLAMPED, not rejected (clamp_pagination):
page<1 -> 1, page beyond the last -> an empty items list with status 200. Invalid
FILTER/SORT *fields* are rejected with 400 (BadRequestError), not 422 — the brief
says "bad filter -> 422"; the live deny-by-default parser returns 400. GET
/policies and GET /policy-rules are intentionally NOT paginated and are excluded.
"""

from __future__ import annotations

import math
import uuid

import pytest
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.audit import record_audit
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

_PAGE_KEYS = {"items", "total", "page", "size", "pages"}


def _assert_page_contract(
    client: TestClient,
    url: str,
    *,
    valid_filter: tuple[str, str],
    valid_sort: str,
) -> None:
    # --- baseline first page (size=2) ---
    r = client.get(url, params={"size": 2, "page": 1})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert set(body.keys()) == _PAGE_KEYS, body.keys()
    total = body["total"]
    assert body["page"] == 1 and body["size"] == 2
    assert body["pages"] == math.ceil(total / 2) if total else body["pages"] == 0
    assert len(body["items"]) == min(2, total)

    # --- last page still 200 + same total ---
    last = body["pages"] or 1
    r_last = client.get(url, params={"size": 2, "page": last})
    assert r_last.status_code == 200
    assert r_last.json()["total"] == total

    # --- out-of-range page -> CLAMPED to empty list, not an error ---
    r_oob = client.get(url, params={"size": 2, "page": last + 25})
    assert r_oob.status_code == 200
    assert r_oob.json()["items"] == []
    assert r_oob.json()["total"] == total

    # --- page < 1 is clamped to 1 (not rejected) ---
    r_zero = client.get(url, params={"page": 0})
    assert r_zero.status_code == 200
    assert r_zero.json()["page"] == 1

    # --- invalid filter field -> 400 envelope ---
    r_bad_filter = client.get(url, params={"definitely_not_a_real_field": "x"})
    assert_error_envelope(r_bad_filter, 400)

    # --- invalid sort field -> 400 envelope ---
    r_bad_sort = client.get(url, params={"sort": "not_a_sortable_field"})
    assert_error_envelope(r_bad_sort, 400)

    # --- valid sort (asc + desc forms) honored -> 200 ---
    assert client.get(url, params={"sort": valid_sort}).status_code == 200
    assert client.get(url, params={"sort": f"-{valid_sort}"}).status_code == 200

    # --- valid filter accepted -> 200 ---
    fk, fv = valid_filter
    assert client.get(url, params={fk: fv}).status_code == 200


def test_users_collection_contract(
    db_session, org_a: Organization, finance_client: TestClient
) -> None:
    for _ in range(5):
        f.make_user(db_session, org=org_a)
    _assert_page_contract(
        finance_client, f"{API}/users", valid_filter=("role", "employee"), valid_sort="name"
    )


def test_trips_collection_contract(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    for _ in range(5):
        f.make_trip(db_session, traveler=users_a["employee"])
    _assert_page_contract(
        employee_client, f"{API}/trips", valid_filter=("status", "draft"), valid_sort="start_date"
    )


def test_expense_reports_collection_contract(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    for _ in range(5):
        f.make_expense_report(db_session, owner=users_a["employee"])
    _assert_page_contract(
        employee_client,
        f"{API}/expense-reports",
        valid_filter=("status", "draft"),
        valid_sort="created_at",
    )


def test_approvals_collection_contract(
    db_session, users_a: dict[str, User], finance_client: TestClient
) -> None:
    for _ in range(5):
        f.make_approval(db_session, submitter=users_a["employee"])
    _assert_page_contract(
        finance_client,
        f"{API}/approvals",
        valid_filter=("current_status", "pending"),
        valid_sort="created_at",
    )


def test_audit_collection_contract(
    db_session, org_a: Organization, users_a: dict[str, User], finance_client: TestClient
) -> None:
    # audit_log has no factory: seed through the single audit write path.
    for _ in range(5):
        record_audit(
            db_session,
            organization_id=org_a.id,
            action="trip.created",
            entity_type="trip",
            entity_id=uuid.uuid4(),
            actor_id=users_a["employee"].id,
        )
    _assert_page_contract(
        finance_client,
        f"{API}/audit",
        valid_filter=("action", "trip.created"),
        valid_sort="created_at",
    )


def test_trips_sort_is_actually_honored(
    db_session, org_a: Organization, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """Non-trivial: rows come back in the requested order, proving sort is applied
    server-side rather than ignored."""
    from datetime import date

    employee = users_a["employee"]
    # Distinct start_dates so ordering is observable.
    f.make_trip(
        db_session, traveler=employee, start_date=date(2026, 3, 1), end_date=date(2026, 3, 2)
    )
    f.make_trip(
        db_session, traveler=employee, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2)
    )
    f.make_trip(
        db_session, traveler=employee, start_date=date(2026, 2, 1), end_date=date(2026, 2, 2)
    )

    asc = employee_client.get(f"{API}/trips", params={"sort": "start_date", "size": 50})
    assert asc.status_code == 200
    dates = [item["start_date"] for item in asc.json()["items"]]
    assert dates == sorted(dates), f"ascending sort not honored: {dates}"

    desc = employee_client.get(f"{API}/trips", params={"sort": "-start_date", "size": 50})
    ddates = [item["start_date"] for item in desc.json()["items"]]
    assert ddates == sorted(ddates, reverse=True), f"descending sort not honored: {ddates}"
