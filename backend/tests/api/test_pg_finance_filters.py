"""Product gap 6 — finance filters (PRD US-F2 AC1) on GET /approvals + GET /expense-reports.

AC1: "filter expense reports/trips by exception type, status, department, and date".

Contract:
  * New allow-listed filters: exception_type (canonical set, approval/filters.py),
    department (the OWNER's, case-insensitive), submitted_from/_to + decided_from/_to
    (inclusive ISO dates), owner_id, amount_min/_max; reports: exception_type,
    department, submitted_from/_to, amount_min/_max.
  * Filters AND onto scope_query: a manager filtering by another team's
    department / exception sees nothing — filters never widen.
  * Malformed values → 400 envelope; pagination totals reflect the filter.
  * GET /approvals/exception-types counts with the SAME predicate the filter uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

APPROVALS = f"{API}/approvals"
REPORTS = f"{API}/expense-reports"


def _utc(y: int, m: int, d: int, h: int = 12) -> datetime:
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text[:300]
    return {i["id"] for i in resp.json()["items"]}


@pytest.fixture()
def data(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> dict[str, Any]:
    employee = users_a["employee"]  # the manager's direct report
    employee.department = "Engineering"
    users_a["manager"].department = "Engineering"
    stranger = f.make_user(db_session, org=org_a, department="Sales")

    r1 = f.make_expense_report(
        db_session,
        owner=employee,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("500.00"),
        submitted_at=_utc(2026, 3, 10),
        pre_submit_flags={
            "flags": [
                {
                    "flag_type": "policy_violation",
                    "rule_type": "amount_cap",
                    "severity": "blocking",
                }
            ]
        },
    )
    r2 = f.make_expense_report(
        db_session,
        owner=stranger,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("50.00"),
        submitted_at=_utc(2026, 4, 20),
        pre_submit_flags={"flags": [{"flag_type": "missing_receipt", "severity": "blocking"}]},
    )
    r3 = f.make_expense_report(db_session, owner=stranger, title="clean draft")
    trip = f.make_trip(db_session, traveler=stranger)

    a1 = f.make_approval(
        db_session,
        submitter=employee,
        subject_id=r1.id,
        risk_summary={"flags": []},
        created_at=_utc(2026, 3, 10),
    )
    a2 = f.make_approval(
        db_session,
        submitter=stranger,
        subject_id=r2.id,
        risk_summary={"flags": [{"flag_type": "missing_receipt", "severity": "blocking"}]},
        created_at=_utc(2026, 4, 20, 23),  # late on the 'to' day — still inclusive
    )
    a3 = f.make_approval(
        db_session,
        submitter=stranger,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.approved,
        risk_summary={"flags": [{"segment_id": "s1", "in_policy": False}]},
        created_at=_utc(2026, 5, 1),
    )
    f.make_approval_step(
        db_session, approval=a3, decision=ApprovalDecision.approve, decided_at=_utc(2026, 5, 2)
    )
    # Another tenant carrying the same exception: never visible.
    other = f.make_expense_report(
        db_session,
        owner=users_b["employee"],
        pre_submit_flags={"flags": [{"flag_type": "missing_receipt"}]},
    )
    f.make_approval(
        db_session,
        submitter=users_b["employee"],
        subject_id=other.id,
        risk_summary={"flags": [{"flag_type": "missing_receipt"}]},
    )
    db_session.flush()
    return {"a1": a1, "a2": a2, "a3": a3, "r1": r1, "r2": r2, "r3": r3}


def _sid(data: dict[str, Any], *keys: str) -> set[str]:
    return {str(data[k].id) for k in keys}


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("amount_cap", {"a1"}),  # rule type, only in the REPORT's pre-submit flags
        ("policy_violation", {"a1"}),
        ("missing_receipt", {"a2"}),  # tenant B's identical flag is excluded
        ("out_of_policy_segment", {"a3"}),  # trip segment row in_policy=false
        ("possible_duplicate", set()),
    ],
)
def test_approvals_exception_type(
    data: dict[str, Any], finance_client: TestClient, value: str, expected: set[str]
) -> None:
    resp = finance_client.get(APPROVALS, params={"exception_type": value, "size": 100})
    assert _ids(resp) == _sid(data, *expected)
    assert resp.json()["total"] == len(expected)


def test_approvals_department_is_the_owners_and_case_insensitive(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    assert _ids(finance_client.get(APPROVALS, params={"department": "engineering"})) == _sid(
        data, "a1"
    )
    assert _ids(finance_client.get(APPROVALS, params={"department": "Sales"})) == _sid(
        data, "a2", "a3"
    )
    assert _ids(finance_client.get(APPROVALS, params={"department": "Nope"})) == set()


def test_approvals_date_ranges_are_inclusive(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    def ids(**params: str) -> set[str]:
        return _ids(finance_client.get(APPROVALS, params=params))

    assert ids(submitted_from="2026-04-20", submitted_to="2026-04-20") == _sid(data, "a2")
    assert ids(submitted_to="2026-03-10") == _sid(data, "a1")
    assert ids(submitted_from="2026-04-01") == _sid(data, "a2", "a3")
    assert ids(decided_from="2026-05-01", decided_to="2026-05-02") == _sid(data, "a3")
    assert ids(decided_to="2026-05-01") == set()


def test_approvals_amount_and_owner(
    data: dict[str, Any], users_a: dict[str, User], finance_client: TestClient
) -> None:
    def ids(**params: str) -> set[str]:
        return _ids(finance_client.get(APPROVALS, params=params))

    assert ids(amount_min="100") == _sid(data, "a1")
    assert ids(amount_max="100.00") == _sid(data, "a2")  # trip without a cost never matches
    assert ids(amount_min="50", amount_max="500") == _sid(data, "a1", "a2")
    assert ids(owner_id=str(users_a["employee"].id)) == _sid(data, "a1")


def test_approvals_filters_combine_and_paginate(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    params = {"department": "Sales", "current_status": "pending"}
    assert _ids(finance_client.get(APPROVALS, params=params)) == _sid(data, "a2")
    page = finance_client.get(APPROVALS, params={"department": "Sales", "size": 1}).json()
    assert page["total"] == 2 and page["pages"] == 2 and len(page["items"]) == 1


def test_list_items_carry_department_and_exception_types(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    items = {
        i["id"]: i for i in finance_client.get(APPROVALS, params={"size": 100}).json()["items"]
    }
    a1 = items[str(data["a1"].id)]
    assert a1["submitter_department"] == "Engineering"
    assert a1["exception_types"] == ["policy_violation", "amount_cap"]
    assert items[str(data["a3"].id)]["exception_types"] == ["out_of_policy_segment"]


def test_filters_never_widen_a_managers_scope(
    data: dict[str, Any], manager_client: TestClient
) -> None:
    assert _ids(manager_client.get(APPROVALS, params={"department": "Sales"})) == set()
    assert (
        _ids(manager_client.get(APPROVALS, params={"exception_type": "missing_receipt"})) == set()
    )
    assert _ids(manager_client.get(APPROVALS, params={"exception_type": "amount_cap"})) == _sid(
        data, "a1"
    )


@pytest.mark.parametrize(
    "params",
    [
        {"exception_type": "bogus"},
        {"exception_type": "amount_cap' OR 1=1 --"},
        {"submitted_from": "2026-13-01"},
        {"submitted_from": "10/03/2026"},
        {"decided_to": "yesterday"},
        {"submitted_from": "2026-05-02", "submitted_to": "2026-05-01"},
        {"amount_min": "-1"},
        {"amount_min": "1e5"},
        {"amount_min": "10.001"},
        {"amount_min": "200", "amount_max": "100"},
        {"department": "   "},
        {"department": "x" * 121},
        {"owner_id": "not-a-uuid"},
    ],
)
def test_invalid_approval_filter_values_are_400(
    data: dict[str, Any], finance_client: TestClient, params: dict[str, str]
) -> None:
    assert_error_envelope(finance_client.get(APPROVALS, params=params), 400)


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------


def test_exception_types_counts_are_scoped(
    data: dict[str, Any], finance_client: TestClient, manager_client: TestClient
) -> None:
    fin = {o["value"]: o for o in finance_client.get(f"{APPROVALS}/exception-types").json()}
    assert fin["amount_cap"]["count"] == 1 and fin["amount_cap"]["kind"] == "rule"
    assert fin["missing_receipt"]["count"] == 1  # tenant B excluded
    assert fin["out_of_policy_segment"]["count"] == 1
    assert fin["possible_duplicate"]["count"] == 0
    assert fin["missing_receipt"]["label"] == "Missing receipt"

    mgr = {
        o["value"]: o["count"] for o in manager_client.get(f"{APPROVALS}/exception-types").json()
    }
    assert mgr["missing_receipt"] == 0 and mgr["amount_cap"] == 1


def test_departments_are_the_readable_users(
    data: dict[str, Any], finance_client: TestClient, manager_client: TestClient
) -> None:
    fin = finance_client.get(f"{APPROVALS}/departments").json()
    assert {"Engineering", "Sales"} <= set(fin)
    assert manager_client.get(f"{APPROVALS}/departments").json() == ["Engineering"]


def test_filter_options_forbidden_for_employee(
    data: dict[str, Any], employee_client: TestClient
) -> None:
    assert_error_envelope(employee_client.get(f"{APPROVALS}/exception-types"), 403)
    assert_error_envelope(employee_client.get(f"{APPROVALS}/departments"), 403)


# ---------------------------------------------------------------------------
# Expense reports
# ---------------------------------------------------------------------------


def test_report_filters(data: dict[str, Any], finance_client: TestClient) -> None:
    def ids(**params: str) -> set[str]:
        return _ids(finance_client.get(REPORTS, params={**params, "size": "100"}))

    assert ids(exception_type="missing_receipt") == _sid(data, "r2")
    assert ids(exception_type="amount_cap") == _sid(data, "r1")
    assert ids(department="ENGINEERING") == _sid(data, "r1")
    assert ids(department="Sales") == _sid(data, "r2", "r3")
    assert ids(submitted_from="2026-04-01", submitted_to="2026-04-20") == _sid(data, "r2")
    assert ids(amount_min="100") == _sid(data, "r1")
    assert ids(department="Sales", exception_type="missing_receipt") == _sid(data, "r2")


def test_report_filters_never_widen_and_reject_bad_values(
    data: dict[str, Any], manager_client: TestClient, finance_client: TestClient
) -> None:
    assert _ids(manager_client.get(REPORTS, params={"department": "Sales"})) == set()
    for params in (
        {"exception_type": "nope"},
        {"submitted_to": "2026-02-30"},
        {"amount_max": "abc"},
        {"decided_from": "2026-01-01"},  # approvals-only filter: not allow-listed on reports
    ):
        assert_error_envelope(finance_client.get(REPORTS, params=params), 400)


def test_decided_range_needs_one_step_inside_the_range(
    data: dict[str, Any], db_session: Session, finance_client: TestClient
) -> None:
    """Two steps straddling the range (before AND after) must not match."""
    a2 = data["a2"]
    f.make_approval_step(
        db_session,
        approval=a2,
        decision=ApprovalDecision.request_changes,
        decided_at=_utc(2026, 1, 5),
    )
    f.make_approval_step(
        db_session,
        approval=a2,
        step_order=2,
        decision=ApprovalDecision.approve,
        decided_at=_utc(2026, 9, 5),
    )
    db_session.flush()
    params = {"decided_from": "2026-03-01", "decided_to": "2026-06-30"}
    assert _ids(finance_client.get(APPROVALS, params=params)) == _sid(data, "a3")
