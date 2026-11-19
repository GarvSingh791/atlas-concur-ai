"""C7R UAT — subjects submitted before submit-opens-approval get an approval (backfill).

``database.seeds.backfill_submitted_approvals`` finds reports still ``submitted``
and trips still ``pending_approval`` with NO approval row and runs them through
the same path a submit now uses.  Exercised on the rolled-back test session.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import User
from database.models.trip import TripStatus
from database.models.trip_segment import SegmentType
from database.seeds.backfill_submitted_approvals import _open_one, _stranded

pytestmark = pytest.mark.db


def test_stranded_submissions_get_an_approval_and_rerun_is_a_noop(
    rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    # A $750 report (escalates) submitted the old way: no approval row.
    report = f.make_expense_report(db_session, owner=employee, title="Old submit")
    report.currency = "USD"
    f.make_expense(db_session, report=report, amount="750.00", expense_date=date(2026, 6, 10))
    report.total_amount = Decimal("750.00")
    report.status = ExpenseReportStatus.submitted
    # A small in-policy trip submitted the old way (auto-approvable).
    start = date.today() + timedelta(days=14)
    trip = f.make_trip(
        db_session,
        traveler=employee,
        start_date=start,
        end_date=start + timedelta(days=2),
        estimated_cost=Decimal("298.00"),
        status=TripStatus.pending_approval,
    )
    f.make_trip_segment(
        db_session,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="Delta",
        amount=Decimal("298.00"),
        currency="USD",
        in_policy=True,
    )
    db_session.flush()

    stranded = {sid for _t, sid in _stranded(db_session)}
    assert {report.id, trip.id} <= stranded

    assert _open_one(db_session, ApprovalSubjectType.expense_report, report.id) == "pending"
    assert _open_one(db_session, ApprovalSubjectType.trip, trip.id) == "auto_approved"
    db_session.flush()

    approvals = db_session.scalars(
        sa.select(Approval).where(Approval.subject_id.in_([report.id, trip.id]))
    ).all()
    assert len(approvals) == 2
    by_subject = {a.subject_id: a for a in approvals}
    assert by_subject[report.id].current_status == ApprovalStatus.pending
    assert by_subject[report.id].submitter_id == employee.id
    db_session.refresh(trip)
    assert trip.status == TripStatus.approved

    # Idempotent: nothing of ours is stranded any more.
    again = {sid for _t, sid in _stranded(db_session)}
    assert report.id not in again and trip.id not in again
