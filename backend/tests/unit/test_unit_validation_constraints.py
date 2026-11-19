"""P7-T1 · item F — application validators + DB CHECK constraints.

Two layers:
  * ``backend.app.core.validation.validate_required_reason`` — pure validator,
    hermetic. Raises UnprocessableEntityError (422) on absent/blank reason.
  * DB CHECK constraints (live-DB): ``amount >= 0`` (``amount_non_negative``,
    expense.py:121) and ``end_date >= start_date`` (``end_date_gte_start_date``,
    trip.py:86). A bad row flushed to Postgres raises IntegrityError with pgcode
    23514 (check_violation).

Each CHECK test flushes the bad row inside a ``session.begin_nested()`` savepoint
so the failed flush is rolled back to the savepoint and does NOT poison the
session for the rest of the test / teardown. Live-DB tests self-skip if Postgres
is unreachable (inherited ``db_session``).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.errors import UnprocessableEntityError
from backend.app.core.validation import validate_required_reason
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.identity import Organization
from database.models.trip import Trip, TripStatus

# ===========================================================================
# validate_required_reason — hermetic
# ===========================================================================


@pytest.mark.parametrize("reason", ["because policy", "x", "  spaced reason  "])
def test_validate_required_reason_accepts_nonblank(reason: str) -> None:
    """A non-blank reason passes silently — the call returns without raising.

    (The function is typed ``-> None``; "passes" == "does not raise".)
    """
    validate_required_reason("reject", reason)  # must not raise


@pytest.mark.parametrize("reason", [None, "", "   ", "\t", "\n  \n"])
def test_validate_required_reason_rejects_blank(reason: str | None) -> None:
    """None / empty / whitespace-only all raise UnprocessableEntityError (422)."""
    with pytest.raises(UnprocessableEntityError):
        validate_required_reason("reject", reason)


def test_validate_required_reason_message_includes_action() -> None:
    """The action name is interpolated into the message (proves the param is
    used, not a static string)."""
    with pytest.raises(UnprocessableEntityError) as exc_info:
        validate_required_reason("request_changes", None)
    assert "request_changes" in str(exc_info.value)


# ===========================================================================
# CHECK constraints — live-DB (flush a bad row inside a savepoint)
# ===========================================================================


def test_expense_negative_amount_violates_check_constraint(
    db_session: Session, org_a: Organization
) -> None:
    """amount = -1 violates the ``amount_non_negative`` CHECK → IntegrityError
    with pgcode 23514. The savepoint isolates the failed flush."""
    report = f.make_expense_report(db_session, org=org_a)

    with db_session.begin_nested():
        bad = Expense(
            organization_id=org_a.id,
            report_id=report.id,
            category=ExpenseCategory.meals,
            amount=Decimal("-1.00"),  # violates amount >= 0
            currency="USD",
            expense_date=date.today(),
            status=ExpenseStatus.draft,
        )
        db_session.add(bad)
        with pytest.raises(IntegrityError) as exc_info:
            db_session.flush()

    assert getattr(exc_info.value.orig, "pgcode", None) == "23514"  # check_violation


def test_expense_zero_amount_is_allowed(db_session: Session, org_a: Organization) -> None:
    """Boundary: amount = 0 satisfies ``amount >= 0`` — confirms the CHECK is
    >= and not > (so the negative-amount test is testing the real boundary)."""
    report = f.make_expense_report(db_session, org=org_a)
    ok = Expense(
        organization_id=org_a.id,
        report_id=report.id,
        category=ExpenseCategory.meals,
        amount=Decimal("0.00"),
        currency="USD",
        expense_date=date.today(),
        status=ExpenseStatus.draft,
    )
    db_session.add(ok)
    db_session.flush()  # must NOT raise
    assert ok.id is not None


def test_trip_end_before_start_violates_check_constraint(
    db_session: Session, org_a: Organization
) -> None:
    """end_date < start_date violates ``end_date_gte_start_date`` → IntegrityError
    with pgcode 23514."""
    traveler = f.make_user(db_session, org=org_a)
    start = date.today()

    with db_session.begin_nested():
        bad = Trip(
            organization_id=org_a.id,
            traveler_id=traveler.id,
            origin="NYC",
            primary_destination="SFO",
            start_date=start,
            end_date=start - timedelta(days=1),  # before start — violates CHECK
            currency="USD",
            status=TripStatus.draft,
        )
        db_session.add(bad)
        with pytest.raises(IntegrityError) as exc_info:
            db_session.flush()

    assert getattr(exc_info.value.orig, "pgcode", None) == "23514"  # check_violation


def test_trip_equal_start_end_is_allowed(db_session: Session, org_a: Organization) -> None:
    """Boundary: end_date == start_date satisfies ``end_date >= start_date``."""
    traveler = f.make_user(db_session, org=org_a)
    same = date.today()
    ok = Trip(
        organization_id=org_a.id,
        traveler_id=traveler.id,
        origin="NYC",
        primary_destination="SFO",
        start_date=same,
        end_date=same,  # equal — allowed
        currency="USD",
        status=TripStatus.draft,
    )
    db_session.add(ok)
    db_session.flush()  # must NOT raise
    assert ok.id is not None


def test_session_usable_after_check_violation_savepoint(
    db_session: Session, org_a: Organization
) -> None:
    """After a CHECK violation rolled back to a savepoint, the session is still
    usable — proves the savepoint isolation pattern the other tests rely on."""
    report = f.make_expense_report(db_session, org=org_a)

    with db_session.begin_nested():
        bad = Expense(
            organization_id=org_a.id,
            report_id=report.id,
            category=ExpenseCategory.meals,
            amount=Decimal("-5.00"),
            currency="USD",
            expense_date=date.today(),
            status=ExpenseStatus.draft,
        )
        db_session.add(bad)
        with pytest.raises(IntegrityError):
            db_session.flush()

    # Session recovered — a fresh valid insert still works.
    good = f.make_expense(db_session, report=report, amount="10.00")
    assert good.id is not None
