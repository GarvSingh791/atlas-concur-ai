"""P7-T2 — record-level RBAC scoping + the 403-vs-404 information-leak convention.

First-class assertions:
  * out-of-scope / cross-tenant GET-by-id NEVER returns 200 with foreign data —
    it returns 404 (the codebase's chosen convention: existence of records
    outside the caller's scope is never leaked, so 404, not 403).
  * positive scoping holds: employee -> self, manager -> self + direct reports,
    finance_admin -> org-wide (but org-wide != all-orgs — cross-tenant is 404
    even for finance).

Owned resources used: trips (traveler_id), expense reports (owner_id), users
(org membership), receipts (uploaded_by), conversations (owner).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope, dummy_uuid
from backend.tests.api.endpoints import API
from database.models.identity import Organization, RoleKey, User

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Trips — full three-role scope + cross-tenant
# ---------------------------------------------------------------------------


def test_trip_scoping_positive_and_leak(
    db_session,
    org_a: Organization,
    users_a: dict[str, User],
    employee_client: TestClient,
    manager_client: TestClient,
    finance_client: TestClient,
    employee_client_b: TestClient,
) -> None:
    employee = users_a["employee"]  # reports to users_a["manager"]
    # A trip owned by the report-employee.
    own = f.make_trip(db_session, traveler=employee)
    # A trip owned by an UNRELATED employee in the same org (not a manager report).
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    foreign = f.make_trip(db_session, traveler=stranger)

    own_url = f"{API}/trips/{own.id}"
    foreign_url = f"{API}/trips/{foreign.id}"

    # Positive: employee sees own; manager sees a direct report's; finance org-wide.
    r = employee_client.get(own_url)
    assert r.status_code == 200
    assert r.json()["traveler_id"] == str(employee.id)
    assert manager_client.get(own_url).status_code == 200
    assert finance_client.get(own_url).status_code == 200

    # Leak guard: employee (scope=self) cannot read a stranger's trip -> 404.
    assert_error_envelope(employee_client.get(foreign_url), 404)
    # Manager (scope=self+reports) — stranger is not a report -> 404, never 200.
    assert_error_envelope(manager_client.get(foreign_url), 404)
    # Cross-tenant: org-B employee reading an org-A trip -> 404 (no data leak).
    assert_error_envelope(employee_client_b.get(own_url), 404)

    # Finance org-wide DOES see the stranger's trip (same org) — org-wide is real.
    assert finance_client.get(foreign_url).status_code == 200


def test_trip_segments_out_of_scope_is_404(
    db_session, org_a: Organization, employee_client: TestClient
) -> None:
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    trip = f.make_trip(db_session, traveler=stranger)
    assert_error_envelope(employee_client.get(f"{API}/trips/{trip.id}/segments"), 404)


# ---------------------------------------------------------------------------
# Expense reports — same convention
# ---------------------------------------------------------------------------


def test_expense_report_scoping_positive_and_leak(
    db_session,
    org_a: Organization,
    users_a: dict[str, User],
    employee_client: TestClient,
    manager_client: TestClient,
    finance_client: TestClient,
    employee_client_b: TestClient,
) -> None:
    employee = users_a["employee"]
    own = f.make_expense_report(db_session, owner=employee)
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    foreign = f.make_expense_report(db_session, owner=stranger)

    own_url = f"{API}/expense-reports/{own.id}"
    foreign_url = f"{API}/expense-reports/{foreign.id}"

    r = employee_client.get(own_url)
    assert r.status_code == 200
    assert r.json()["owner_id"] == str(employee.id)
    assert manager_client.get(own_url).status_code == 200  # direct report
    assert finance_client.get(own_url).status_code == 200  # org-wide

    assert_error_envelope(employee_client.get(foreign_url), 404)
    assert_error_envelope(manager_client.get(foreign_url), 404)
    assert_error_envelope(employee_client_b.get(own_url), 404)  # cross-tenant

    # entries sub-resource respects the same parent scoping.
    assert_error_envelope(employee_client.get(f"{API}/expense-reports/{foreign.id}/entries"), 404)


def test_nonexistent_and_out_of_scope_are_indistinguishable(
    db_session, org_a: Organization, employee_client: TestClient
) -> None:
    """A purely nonexistent id and a real-but-out-of-scope id BOTH return 404 —
    the same status, so existence is never inferable from the response."""
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    foreign = f.make_expense_report(db_session, owner=stranger)

    nonexistent = employee_client.get(f"{API}/expense-reports/{dummy_uuid()}")
    out_of_scope = employee_client.get(f"{API}/expense-reports/{foreign.id}")
    assert nonexistent.status_code == out_of_scope.status_code == 404
    assert_error_envelope(nonexistent, 404)
    assert_error_envelope(out_of_scope, 404)


# ---------------------------------------------------------------------------
# Users — finance is org-wide, but org-wide != all-orgs
# ---------------------------------------------------------------------------


def test_user_read_cross_tenant_is_404_even_for_finance(
    db_session,
    users_a: dict[str, User],
    users_b: dict[str, User],
    finance_client: TestClient,
) -> None:
    same_org_user = users_a["employee"]
    other_org_user = users_b["employee"]

    assert finance_client.get(f"{API}/users/{same_org_user.id}").status_code == 200
    # Cross-tenant read -> 404, NOT 200-with-foreign-data, even for org-wide finance.
    assert_error_envelope(finance_client.get(f"{API}/users/{other_org_user.id}"), 404)


# ---------------------------------------------------------------------------
# Receipts — owner-only correction
# ---------------------------------------------------------------------------


def test_receipt_correct_out_of_scope_is_404(
    db_session,
    org_a: Organization,
    users_a: dict[str, User],
    employee_client: TestClient,
    employee_client_b: TestClient,
) -> None:
    stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
    receipt = f.make_receipt(db_session, uploaded_by=stranger)
    body = {"field": "vendor", "value": "tampered"}

    url = f"{API}/receipts/{receipt.id}"
    # Same-org non-owner -> 404.
    assert_error_envelope(employee_client.patch(url, json=body), 404)
    # Cross-tenant -> 404.
    assert_error_envelope(employee_client_b.patch(url, json=body), 404)


# ---------------------------------------------------------------------------
# Conversations — owner-only read
# ---------------------------------------------------------------------------


def test_conversation_read_is_owner_only(
    employee_client: TestClient,
    manager_client: TestClient,
    employee_client_b: TestClient,
) -> None:
    created = employee_client.post(f"{API}/conversations", json={"context_type": "general"})
    assert created.status_code == 201
    conv_id = created.json()["id"]
    url = f"{API}/conversations/{conv_id}"

    # Owner reads it.
    assert employee_client.get(url).status_code == 200
    # Same-org non-owner -> 404 (owner-scoped, not role-scoped).
    assert_error_envelope(manager_client.get(url), 404)
    # Cross-tenant -> 404.
    assert_error_envelope(employee_client_b.get(url), 404)
