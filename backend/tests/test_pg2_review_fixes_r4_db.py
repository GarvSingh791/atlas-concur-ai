"""Product gap 2 — review fixes, round 4, end to end on Postgres.

* The approval agent never auto-approves a report (<= $500) past an entry-rule
  "could not verify" advisory whose text promises an approver will check: MEAL-04 alcohol
  found only by keyword on an un-itemized receipt, a GRND-05 tip the receipt does not
  itemize.  A control report without the unverifiable item still auto-approves, so the
  escalation is caused by the advisory and nothing else.
* LOD-02 on the expense path: a hotel segment the traveler typed in (POST /trips — no
  planner ``recommendation_context``) is not a stay booked through the travel tool, even
  with a receipt naming that hotel in that city: 13% over the cap stays a manager
  HOTEL-CAP exception.  The same stay on a PLANNER-booked segment keeps the advisory.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.context import AgentContext
from backend.tests.test_pg2_entry_rules_db import (
    _approved_trip,
    _by_clause,
    _dt,
    _entry,
    _planner_booked,
    _run,
    _seed_rules,
    _segment,
)
from database.models.approval import ApprovalSubjectType
from database.models.expense import ExpenseCategory, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus
from database.models.trip_segment import SegmentType

pytestmark = pytest.mark.db

DET = "deterministic"
DAY = date(2026, 6, 10)


def _report_with_line(
    db: Session,
    owner: User,
    *,
    category: ExpenseCategory,
    amount: str,
    vendor: str,
    description: str,
    receipt_fields: dict,
) -> ExpenseReport:
    report = f.make_expense_report(
        db, owner=owner, title="Client visit", status=ExpenseReportStatus.submitted
    )
    entry = f.make_expense(
        db,
        report=report,
        category=category,
        amount=amount,
        vendor=vendor,
        description=description,
        expense_date=DAY,
        match_source=ExpenseMatchSource.manual,
    )
    f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        image_url=f"{owner.organization_id}/{entry.id}.jpg",
        extracted_fields={"currency": "USD", "date": DAY.isoformat(), **receipt_fields},
        field_provenance={k: DET for k in ("currency", "date", *receipt_fields)},
    )
    report.total_amount = Decimal(amount)
    db.flush()
    return report


def _route(db: Session, report: ExpenseReport, actor: User) -> tuple[list[dict], str]:
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=actor.id,
    )
    db.expire_all()
    fresh = db.get(ExpenseReport, report.id)
    assert fresh is not None
    a = assess_risk(fresh, ApprovalSubjectType.expense_report, context=ctx, session=db)
    return list(a.flags_snapshot), decide_route(a)


def _clause(flags: list[dict], ref: str) -> list[dict]:
    return [x for x in flags if x.get("source_clause_ref") == ref]


class TestApproverCheckAdvisoriesEscalate:
    def test_dinner_with_wine_unitemized_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        receipt = {"vendor": "Corner Bistro", "amount": 60.0}
        control = _report_with_line(
            db_session,
            emp,
            category=ExpenseCategory.meals,
            amount="60.00",
            vendor="Corner Bistro",
            description="Dinner",
            receipt_fields=receipt,
        )
        flags, route = _route(db_session, control, mgr)
        assert route == "auto_approve", [x.get("reason") for x in flags]

        wine = _report_with_line(
            db_session,
            emp,
            category=ExpenseCategory.meals,
            amount="60.00",
            vendor="Corner Bistro",
            description="Dinner with wine",
            receipt_fields=receipt,
        )
        flags, route = _route(db_session, wine, mgr)
        (meal04,) = _clause(flags, "MEAL-04")
        assert meal04["flag_type"] == "not_evaluated" and meal04["severity"] == "advisory"
        assert meal04["requires_approver"] == "manager"
        assert route == "escalate"

    def test_unverified_rideshare_tip_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        receipt = {"vendor": "Uber", "amount": 30.0}
        control = _report_with_line(
            db_session,
            emp,
            category=ExpenseCategory.ground_transport,
            amount="30.00",
            vendor="Uber",
            description="Ride to client office",
            receipt_fields=receipt,
        )
        flags, route = _route(db_session, control, mgr)
        assert route == "auto_approve", [x.get("reason") for x in flags]

        tipped = _report_with_line(
            db_session,
            emp,
            category=ExpenseCategory.ground_transport,
            amount="30.00",
            vendor="Uber",
            description="Ride to client office incl. tip",
            receipt_fields=receipt,
        )
        flags, route = _route(db_session, tipped, mgr)
        (tip,) = _clause(flags, "GRND-05")
        assert tip["flag_type"] == "not_evaluated" and tip["requires_approver"] == "manager"
        assert route == "escalate"


class TestLod02ExpensePathNeedsAPlannerBooking:
    def _hotel_case(self, db: Session, org: Organization, emp: User, *, planner: bool) -> list:
        _seed_rules(db, org)
        d1, d3 = date(2026, 4, 6), date(2026, 4, 8)
        trip = _approved_trip(db, emp, d1, d3)
        hotel = _segment(
            db,
            trip,
            SegmentType.hotel,
            _dt(d1, 15),
            _dt(d3, 11),
            details={"nights": 2},
            supplier="Hilton",
        )
        if planner:
            _planner_booked(db, trip, hotel)
        receipt = f.make_receipt(
            db,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={"amount": 680.0, "vendor": "HILTON", "location": "New York, NY"},
            field_provenance={"amount": DET, "vendor": DET, "location": DET},
        )
        return _run(
            db,
            org,
            emp,
            _entry("lodging", 680.0, vendor="Hilton", segment=hotel, receipt_id=receipt.id),
        )

    def test_typed_in_hotel_segment_stays_a_manager_exception(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        flags = self._hotel_case(db_session, org_a, users_a["employee"], planner=False)
        (cap,) = _by_clause(flags, "HOTEL-CAP")
        assert cap.flag_type == "policy_violation" and cap.requires_approver == "manager"
        assert not _by_clause(flags, "LOD-02")

    def test_planner_booked_hotel_segment_keeps_the_advisory(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        flags = self._hotel_case(db_session, org_a, users_a["employee"], planner=True)
        (adv,) = _by_clause(flags, "LOD-02")
        assert adv.flag_type == "policy_advisory" and adv.severity == "advisory"
        assert not _by_clause(flags, "HOTEL-CAP")
