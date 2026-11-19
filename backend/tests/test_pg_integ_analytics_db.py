"""Product-gaps integration, security/QA item (c) — Budget Posture keeps completed trips.

The finance "Budget Posture" KPI sums ``budget_amount - estimated_cost`` over
approved trips.  The lifecycle track's elapsed-trip sweep (gap 8) moves every past
approved trip to ``completed`` — on deploy the posture would silently lose them.
Completed trips were approved, so the KPI counts approved + completed; draft,
pending, rejected, cancelled and withdrawn trips stay out.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from database.models.identity import User
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

API = "/api/v1"


def _posture(client: TestClient) -> float:
    r = client.get(f"{API}/analytics/overview")
    assert r.status_code == 200, r.text
    kpis = r.json()["finance"]["kpis"]
    return float(next(k for k in kpis if k["label"] == "Budget Posture")["value"])


def _trip(db: Session, traveler: User, status: TripStatus, budget: str, cost: str, **kw: Any):
    return f.make_trip(
        db,
        traveler=traveler,
        status=status,
        budget_amount=Decimal(budget),
        estimated_cost=Decimal(cost),
        **kw,
    )


def test_budget_posture_counts_approved_and_completed_trips(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    _trip(db_session, emp, TripStatus.approved, "1000.00", "600.00")  # +400
    _trip(db_session, emp, TripStatus.completed, "500.00", "450.00")  # +50
    for status in (
        TripStatus.draft,
        TripStatus.pending_approval,
        TripStatus.rejected,
        TripStatus.cancelled,
    ):
        _trip(db_session, emp, status, "900.00", "100.00")  # never counted
    db_session.flush()
    assert _posture(finance_client) == pytest.approx(450.0)


def test_completing_an_elapsed_trip_does_not_move_the_posture(
    finance_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    start = date.today() - timedelta(days=10)
    trip = _trip(
        db_session,
        emp,
        TripStatus.approved,
        "800.00",
        "500.00",
        start_date=start,
        end_date=start + timedelta(days=3),
    )
    db_session.flush()
    before = _posture(finance_client)
    assert before == pytest.approx(300.0)

    done = finance_client.post(f"{API}/trips/{trip.id}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"
    assert _posture(finance_client) == pytest.approx(before)
