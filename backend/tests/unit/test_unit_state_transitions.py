"""P7-T1 · item F — expense + trip state-transition guards (service layer).

Targets ``transition_report`` (``backend.app.modules.expense.service``) and
``transition_trip`` (``backend.app.modules.travel.service``). These assert the
CURRENT, source-verified behavior — NOT the doc-ideal:

  * Expense transition is validated FIRST (illegal → ConflictError), THEN the
    Phase-4 authorization gate fires: submitted→{approved,rejected,reimbursed}
    raises ForbiddenError (``_PHASE4_GATED_TARGETS``, expense/service.py:369-378).
    draft→approved / approved→approved / reimbursed→* are illegal → ConflictError.
    draft→submitted is legal and stamps ``submitted_at``.
  * Trip illegal transition → ConflictError; trip uses a manual ``version += 1``
    (no CAS), so no version-conflict test belongs here.

Every illegal / forbidden path must write NO audit row — asserted by counting
``audit_log`` rows for the entity before and after. All live-DB (inherited
``db_session``; self-skips if Postgres is unreachable).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.errors import ConflictError, ForbiddenError
from backend.app.modules.expense.service import transition_report
from backend.app.modules.travel.service import transition_trip
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User
from database.models.trip import TripStatus


def _audit_count(session: Session, *, entity_type: str, entity_id: uuid.UUID) -> int:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == entity_id,
            )
        )
        or 0
    )


@pytest.fixture()
def finance_user(db_session: Session, org_a: Organization) -> User:
    """An org-A finance_admin who OWNS every subject in this module.

    C7-01: mutations are owner-only for every role (a non-owner reader gets 403
    from require_owner before any transition check), so the subjects are created
    with this user as owner/traveler to isolate the transition guard under test.
    """
    return f.make_user(db_session, org=org_a, role=RoleKey.finance_admin)


# ===========================================================================
# Expense report transitions
# ===========================================================================


def test_expense_draft_to_submitted_succeeds_and_sets_submitted_at(
    db_session: Session, org_a: Organization, finance_user: User
) -> None:
    report = f.make_expense_report(db_session, owner=finance_user, status=ExpenseReportStatus.draft)
    # UAT2 (M12): empty reports are refused; over the auto-approve ceiling so the
    # report stays "submitted" (escalated to a human).
    f.make_expense(db_session, report=report, amount="750.00")
    assert report.submitted_at is None
    before_audits = _audit_count(db_session, entity_type="expense_report", entity_id=report.id)

    result = transition_report(db_session, finance_user, report.id, ExpenseReportStatus.submitted)

    assert result.status == ExpenseReportStatus.submitted
    db_session.refresh(report)
    assert report.submitted_at is not None  # stamped on submit
    after_audits = _audit_count(db_session, entity_type="expense_report", entity_id=report.id)
    assert after_audits == before_audits + 1  # legal path DOES audit


def test_expense_submitted_to_approved_is_phase4_forbidden(
    db_session: Session, org_a: Organization, finance_user: User
) -> None:
    """submitted→approved is a LEGAL transition but Phase-4-gated → ForbiddenError
    (not ConflictError). No audit row is written on the blocked path.

    Mutation target: removing the ``_PHASE4_GATED_TARGETS`` guard would let this
    through (ForbiddenError no longer raised).
    """
    report = f.make_expense_report(
        db_session, owner=finance_user, status=ExpenseReportStatus.submitted
    )
    before_audits = _audit_count(db_session, entity_type="expense_report", entity_id=report.id)

    with pytest.raises(ForbiddenError):
        transition_report(db_session, finance_user, report.id, ExpenseReportStatus.approved)

    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.submitted  # unchanged
    assert (
        _audit_count(db_session, entity_type="expense_report", entity_id=report.id) == before_audits
    )


@pytest.mark.parametrize("target", ["rejected", "reimbursed"])
def test_expense_submitted_to_other_gated_targets_forbidden(
    db_session: Session,
    org_a: Organization,
    finance_user: User,
    target: str,
) -> None:
    """submitted→rejected is legal-but-gated → ForbiddenError. submitted→reimbursed
    is illegal AND gated; since the transition check runs first, it is a
    ConflictError. This parametrize pins both branches."""
    report = f.make_expense_report(
        db_session, owner=finance_user, status=ExpenseReportStatus.submitted
    )
    target_status = ExpenseReportStatus(target)
    legal = target_status in {ExpenseReportStatus.approved, ExpenseReportStatus.rejected}
    expected = ForbiddenError if legal else ConflictError

    with pytest.raises(expected):
        transition_report(db_session, finance_user, report.id, target_status)

    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.submitted


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (ExpenseReportStatus.draft, ExpenseReportStatus.approved),
        (ExpenseReportStatus.draft, ExpenseReportStatus.reimbursed),
        (ExpenseReportStatus.approved, ExpenseReportStatus.approved),
        (ExpenseReportStatus.reimbursed, ExpenseReportStatus.draft),
        (ExpenseReportStatus.reimbursed, ExpenseReportStatus.submitted),
    ],
)
def test_expense_illegal_transition_raises_conflict_no_audit(
    db_session: Session,
    org_a: Organization,
    finance_user: User,
    start: ExpenseReportStatus,
    target: ExpenseReportStatus,
) -> None:
    """Illegal transitions (rejected by LEGAL_TRANSITIONS before the Phase-4 gate)
    raise ConflictError, leave status unchanged, and write no audit row."""
    report = f.make_expense_report(db_session, owner=finance_user, status=start)
    before_audits = _audit_count(db_session, entity_type="expense_report", entity_id=report.id)

    with pytest.raises(ConflictError):
        transition_report(db_session, finance_user, report.id, target)

    db_session.refresh(report)
    assert report.status == start
    assert (
        _audit_count(db_session, entity_type="expense_report", entity_id=report.id) == before_audits
    )


# ===========================================================================
# Trip transitions
# ===========================================================================


def test_trip_legal_lifecycle_progresses(
    db_session: Session, org_a: Organization, finance_user: User
) -> None:
    """The owner-driven legal edges succeed, each writing one audit row and bumping
    the manual version: draft→pending_approval, and approved→completed.

    pending_approval→approved is a legal state-machine edge but an approval-
    workflow DECISION: the traveler's PATCH is refused with ForbiddenError, no
    state change, no audit row (C7-02 — previously this test drove the traveler
    straight through draft→pending_approval→approved→completed).
    """
    trip = f.make_trip(db_session, traveler=finance_user, status=TripStatus.draft)
    start_version = trip.version

    result = transition_trip(db_session, finance_user, trip.id, TripStatus.pending_approval)
    assert result.status == TripStatus.pending_approval

    with pytest.raises(ForbiddenError):
        transition_trip(db_session, finance_user, trip.id, TripStatus.approved)

    db_session.refresh(trip)
    assert trip.status == TripStatus.pending_approval
    assert trip.version == start_version + 1
    assert _audit_count(db_session, entity_type="trip", entity_id=trip.id) == 1

    # Product gap 8: completion needs a trip that has ENDED (end_date <= today).
    approved = f.make_trip(
        db_session,
        traveler=finance_user,
        status=TripStatus.approved,
        start_date=date.today() - timedelta(days=5),
        end_date=date.today() - timedelta(days=2),
    )
    done = transition_trip(db_session, finance_user, approved.id, TripStatus.completed)
    assert done.status == TripStatus.completed
    assert _audit_count(db_session, entity_type="trip", entity_id=approved.id) == 1


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (TripStatus.draft, TripStatus.approved),  # skips pending_approval
        (TripStatus.draft, TripStatus.completed),
        (TripStatus.completed, TripStatus.approved),  # terminal
        (TripStatus.cancelled, TripStatus.draft),  # terminal
        (TripStatus.rejected, TripStatus.approved),  # terminal
    ],
)
def test_trip_illegal_transition_raises_conflict_no_audit(
    db_session: Session,
    org_a: Organization,
    finance_user: User,
    start: TripStatus,
    target: TripStatus,
) -> None:
    """Illegal trip transitions raise ConflictError, leave status + version
    unchanged, and write no audit row.

    Mutation target: removing the ``not in LEGAL_TRANSITIONS`` check in
    transition_trip lets an illegal target through (no ConflictError).
    """
    trip = f.make_trip(db_session, traveler=finance_user, status=start)
    start_version = trip.version

    with pytest.raises(ConflictError):
        transition_trip(db_session, finance_user, trip.id, target)

    db_session.refresh(trip)
    assert trip.status == start
    assert trip.version == start_version  # no manual bump on the illegal path
    assert _audit_count(db_session, entity_type="trip", entity_id=trip.id) == 0
