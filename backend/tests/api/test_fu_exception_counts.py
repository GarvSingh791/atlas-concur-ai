"""Follow-up (UI polish) — exception-type counts follow the ACTIVE filters.

Old behaviour: GET /approvals/exception-types counted every approval in the
caller's scope, so with "Department: Sales" selected the dropdown still said
"Policy cap (1)" for an approval the list could never show.

Now the endpoint takes the same filter params as GET /approvals and counts each
exception type within that filtered set (faceted: the ``exception_type`` param
itself is ignored, so every option says how many rows choosing it would show).
Filters only AND onto the role scope (never widen); malformed values are the
same 400 envelope as the list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

URL = f"{API}/approvals/exception-types"
LIST = f"{API}/approvals"


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, tzinfo=timezone.utc)


@pytest.fixture()
def data(
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    employee = users_a["employee"]  # the manager's direct report
    employee.department = "Engineering"
    users_a["manager"].department = "Engineering"
    stranger = f.make_user(db_session, org=org_a, department="Sales")
    cap = {"flag_type": "policy_violation", "rule_type": "amount_cap", "severity": "blocking"}
    r1 = f.make_expense_report(
        db_session,
        owner=employee,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("500.00"),
        pre_submit_flags={"flags": [cap]},
    )
    r2 = f.make_expense_report(
        db_session,
        owner=stranger,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("50.00"),
        pre_submit_flags={"flags": [{"flag_type": "missing_receipt", "severity": "blocking"}]},
    )
    trip = f.make_trip(db_session, traveler=stranger)
    f.make_approval(db_session, submitter=employee, subject_id=r1.id, created_at=_utc(2026, 3, 10))
    f.make_approval(
        db_session,
        submitter=stranger,
        subject_id=r2.id,
        risk_summary={"flags": [{"flag_type": "missing_receipt", "severity": "blocking"}]},
        created_at=_utc(2026, 4, 20),
    )
    f.make_approval(
        db_session,
        submitter=stranger,
        subject_type=ApprovalSubjectType.trip,
        subject_id=trip.id,
        current_status=ApprovalStatus.approved,
        risk_summary={"flags": [{"segment_id": "s1", "in_policy": False}]},
        created_at=_utc(2026, 5, 1),
    )
    # Another tenant with the same exception: never counted.
    other = f.make_expense_report(
        db_session, owner=users_b["employee"], pre_submit_flags={"flags": [cap]}
    )
    f.make_approval(db_session, submitter=users_b["employee"], subject_id=other.id)
    db_session.flush()


def _counts(client: TestClient, **params: str) -> dict[str, int]:
    resp = client.get(URL, params=params)
    assert resp.status_code == 200, resp.text[:300]
    return {o["value"]: o["count"] for o in resp.json()}


@pytest.mark.usefixtures("data")
def test_counts_follow_the_other_active_filters(finance_client: TestClient) -> None:
    every = _counts(finance_client)
    assert (every["amount_cap"], every["missing_receipt"], every["out_of_policy_segment"]) == (
        1,
        1,
        1,
    )

    sales = _counts(finance_client, department="Sales")
    assert sales["amount_cap"] == 0  # the cap approval is Engineering's
    assert sales["missing_receipt"] == 1 and sales["out_of_policy_segment"] == 1

    approved = _counts(finance_client, current_status="approved")
    assert approved["out_of_policy_segment"] == 1
    assert approved["missing_receipt"] == 0 and approved["amount_cap"] == 0

    spring = _counts(finance_client, submitted_from="2026-04-01", subject_type="expense_report")
    assert spring == {**{k: 0 for k in every}, "missing_receipt": 1}


@pytest.mark.usefixtures("data")
def test_a_count_equals_the_list_total_for_the_same_filters(
    finance_client: TestClient,
) -> None:
    filters = {"department": "Sales", "submitted_to": "2026-04-30"}
    counts = _counts(finance_client, **filters)
    for value, count in counts.items():
        page = finance_client.get(LIST, params={**filters, "exception_type": value}).json()
        assert page["total"] == count, value


@pytest.mark.usefixtures("data")
def test_the_selected_exception_type_and_paging_keys_do_not_narrow_the_options(
    finance_client: TestClient,
) -> None:
    faceted = _counts(finance_client, department="Sales", exception_type="amount_cap")
    assert faceted == _counts(finance_client, department="Sales")
    assert _counts(finance_client, page="2", size="1", sort="created_at:desc") == _counts(
        finance_client
    )


@pytest.mark.usefixtures("data")
def test_filters_never_widen_scope_and_bad_values_are_400(
    finance_client: TestClient, manager_client: TestClient
) -> None:
    assert set(_counts(manager_client, department="Sales").values()) == {0}
    assert _counts(manager_client, department="Engineering")["amount_cap"] == 1
    for params in (
        {"submitted_from": "2026-13-01"},
        {"amount_min": "-1"},
        {"department": "   "},
        {"submitted_from": "2026-05-02", "submitted_to": "2026-05-01"},
        {"bogus_field": "x"},
    ):
        assert_error_envelope(finance_client.get(URL, params=params), 400)
