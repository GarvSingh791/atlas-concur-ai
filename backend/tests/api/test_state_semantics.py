"""P7-T2 — state-transition, required-reason, and optimistic-concurrency contracts.

  * Illegal state transitions are rejected with 409 + the standard envelope, with
    no state change.
  * reject / request_changes without a reason are rejected (required-reason rule).
  * A stale If-Match version on PATCH report / PATCH approval -> 409 (the
    lost-update guard), not a silent overwrite. A current version succeeds and
    bumps the version.

The approval decide endpoint enforces required-reason via the shared
``validate_required_reason`` helper, which raises ``UnprocessableEntityError`` ->
HTTP 422 (and also rejects whitespace-only reasons). These tests assert the 422.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api._helpers import assert_error_envelope
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import User
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

API = "/api/v1"


# ---------------------------------------------------------------------------
# Illegal state transitions -> 409
# ---------------------------------------------------------------------------


def test_illegal_trip_transition_is_409(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """draft -> approved is not a legal trip transition (draft -> {pending_approval,
    cancelled})."""
    trip = f.make_trip(db_session, traveler=users_a["employee"], status=TripStatus.draft)
    resp = employee_client.patch(f"{API}/trips/{trip.id}", json={"status": "approved"})
    assert_error_envelope(resp, 409)
    # No state change persisted.
    db_session.refresh(trip)
    assert trip.status == TripStatus.draft


def test_illegal_report_transition_is_409(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """draft -> approved is illegal (draft -> {submitted} only)."""
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.draft
    )
    resp = employee_client.patch(f"{API}/expense-reports/{report.id}", json={"status": "approved"})
    assert_error_envelope(resp, 409)
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft


# ---------------------------------------------------------------------------
# Required-reason rule on approval decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ["reject", "request_changes"])
def test_decide_without_reason_is_rejected(
    db_session, users_a: dict[str, User], finance_client: TestClient, decision: str
) -> None:
    """reject / request_changes require a non-empty reason -> rejected (422)."""
    approval = f.make_approval(
        db_session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        current_status=ApprovalStatus.pending,
    )
    resp = finance_client.patch(f"{API}/approvals/{approval.id}", json={"decision": decision})
    # Enforced via validate_required_reason -> UnprocessableEntityError (422).
    assert_error_envelope(resp, 422)
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending  # unchanged


def test_decide_reject_with_reason_succeeds(
    db_session, users_a: dict[str, User], finance_client: TestClient
) -> None:
    """Positive control: a reason present -> the decision is recorded (not 400)."""
    approval = f.make_approval(
        db_session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        current_status=ApprovalStatus.pending,
    )
    resp = finance_client.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "reject", "reason": "Over per-diem cap."},
    )
    assert resp.status_code == 200, resp.text[:300]


# ---------------------------------------------------------------------------
# Optimistic concurrency (If-Match) — stale version -> 409, current -> success
# ---------------------------------------------------------------------------


def test_stale_if_match_on_report_is_409(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """A legal transition (draft->submitted) with a stale If-Match -> 409, not a
    lost update."""
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.draft
    )
    resp = employee_client.patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": "999"},
    )
    assert_error_envelope(resp, 409)
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.draft  # no lost update


def test_current_if_match_on_report_succeeds_and_bumps_version(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """Positive control: the correct version is accepted and version increments,
    proving the guard is a real CAS (not always-409)."""
    report = f.make_expense_report(
        db_session, owner=users_a["employee"], status=ExpenseReportStatus.draft
    )
    # UAT2 (M12): an empty report is not submittable (REPORT_EMPTY) — give it a
    # line over the auto-approve ceiling so it stays "submitted" (escalated).
    f.make_expense(db_session, report=report, amount="750.00")
    current = employee_client.get(f"{API}/expense-reports/{report.id}").json()["version"]
    resp = employee_client.patch(
        f"{API}/expense-reports/{report.id}",
        json={"status": "submitted"},
        headers={"If-Match": str(current)},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["status"] == "submitted"
    assert resp.json()["version"] == current + 1


def test_stale_if_match_on_approval_is_409(
    db_session, users_a: dict[str, User], finance_client: TestClient
) -> None:
    approval = f.make_approval(
        db_session,
        submitter=users_a["employee"],
        subject_type=ApprovalSubjectType.expense_report,
        current_status=ApprovalStatus.pending,
    )
    resp = finance_client.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": "approve"},
        headers={"If-Match": "999"},
    )
    assert_error_envelope(resp, 409)
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending  # no lost update
