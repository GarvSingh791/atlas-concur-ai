"""Product gap 2 — review fixes, round 5, end to end on Postgres.

* GRND-01: an approved rail segment that says "Standard" does not clear a ticket the
  receipt reads as First Class — the receipt is what was bought (2h journey -> violation).
* MEAL-04: a receipt that itemizes a grape varietal / a beer brand / "Gin & tonic" has the
  drink excluded and flagged.
* Ground sub-type trust, through the real approval path (assess_risk + decide_route): a
  $180 ground line whose receipt vendor the claimant edited ("AMTRAK" -> "Uber"), or that
  has no trusted evidence at all, escalates to the approver; it is never auto-approved
  past the kind rules it might fall under.  A control line whose receipt reads "UBER"
  still auto-approves.
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


class TestRailReceiptClassBeatsSegment:
    def test_standard_segment_first_class_receipt_is_a_grnd01_violation(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        d1, d3 = date(2026, 5, 1), date(2026, 5, 3)
        trip = _approved_trip(db_session, emp, d1, d3)
        rail = _segment(
            db_session,
            trip,
            SegmentType.rail,
            _dt(d1, 8),
            _dt(d1, 10),  # 2h journey
            details={"travel_class": "standard"},
        )
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": 180.0,
                "vendor": "AMTRAK",
                "line_items": [{"description": "Acela First Class seat 2C", "amount": 180.0}],
            },
            field_provenance={"amount": DET, "vendor": DET, "line_items": DET},
        )
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry(
                "ground_transport",
                180.0,
                vendor="Amtrak",
                description="train to DC",
                segment=rail,
                receipt_id=receipt.id,
            ),
        )
        (grnd01,) = _by_clause(flags, "GRND-01")
        assert grnd01.flag_type == "policy_violation", grnd01.reason
        assert "under 3 hours" in grnd01.reason and grnd01.justifiable


class TestAlcoholLineItems:
    @pytest.mark.parametrize(
        "drink", ["Cabernet Sauvignon", "Pinot Noir glass", "Heineken", "Gin & tonic"]
    )
    def test_itemized_drink_is_excluded_and_flagged(
        self,
        db_session: Session,
        org_a: Organization,
        users_a: dict[str, User],
        drink: str,
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        receipt = f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            extracted_fields={
                "amount": 34.0,
                "vendor": "Corner Bistro",
                "line_items": [
                    {"description": "Burger", "amount": 20.0},
                    {"description": drink, "amount": 14.0},
                ],
            },
            field_provenance={"amount": DET, "vendor": DET, "line_items": DET},
        )
        flags = _run(
            db_session,
            org_a,
            emp,
            _entry("meals", 34.0, vendor="Corner Bistro", receipt_id=receipt.id),
        )
        (meal04,) = _by_clause(flags, "MEAL-04")
        assert meal04.flag_type == "policy_violation"
        assert meal04.observed["excluded_usd"] == 14.0
        assert meal04.observed["reimbursable_usd"] == 20.0


def _ground_report(
    db: Session,
    owner: User,
    *,
    vendor: str,
    receipt_fields: dict | None,
    provenance: dict | None = None,
    grounding_context: dict | None = None,
) -> ExpenseReport:
    report = f.make_expense_report(
        db, owner=owner, title="Client visit", status=ExpenseReportStatus.submitted
    )
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.ground_transport,
        amount="180.00",
        vendor=vendor,
        description="Ride",
        expense_date=DAY,
        match_source=ExpenseMatchSource.manual,
    )
    if receipt_fields is not None:
        f.make_receipt(
            db,
            uploaded_by=owner,
            expense_id=entry.id,
            ocr_status=OcrStatus.processed,
            image_url=f"{owner.organization_id}/{entry.id}.jpg",
            extracted_fields={"currency": "USD", "date": DAY.isoformat(), **receipt_fields},
            field_provenance=provenance or {k: DET for k in ("currency", "date", *receipt_fields)},
            grounding_context=grounding_context,
        )
    report.total_amount = Decimal("180.00")
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


def _refs(flags: list[dict]) -> set[str]:
    return {str(x.get("source_clause_ref")) for x in flags}


class TestGroundLinesNeverAutoApproveUnverified:
    def test_control_receipt_naming_uber_auto_approves(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _ground_report(
            db_session, emp, vendor="Uber", receipt_fields={"vendor": "UBER", "amount": 180.0}
        )
        flags, route = _route(db_session, report, mgr)
        assert route == "auto_approve", [x.get("reason") for x in flags]

    def test_reviewer_case_b_edited_receipt_vendor_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """OCR read AMTRAK (frozen in the snapshot); the claimant retyped it to Uber."""
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _ground_report(
            db_session,
            emp,
            vendor="Uber",
            receipt_fields={"vendor": "Uber", "amount": 180.0},
            provenance={"currency": DET, "date": DET, "amount": DET, "vendor": "user_edited"},
            grounding_context={"ocr_snapshot": {"vendor": "AMTRAK", "amount": 180.0}},
        )
        flags, route = _route(db_session, report, mgr)
        assert "GRND-01" in _refs(flags)
        assert route == "escalate"

    def test_edited_vendor_on_a_legacy_row_without_snapshot_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _ground_report(
            db_session,
            emp,
            vendor="Uber",
            receipt_fields={"vendor": "Uber", "amount": 180.0},
            provenance={"currency": DET, "date": DET, "amount": DET, "vendor": "user_edited"},
        )
        flags, route = _route(db_session, report, mgr)
        assert {"GRND-01", "GRND-03"} <= _refs(flags)
        assert route == "escalate"

    def test_reviewer_case_c_no_trusted_vendor_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _ground_report(db_session, emp, vendor="Uber", receipt_fields={"amount": 180.0})
        flags, route = _route(db_session, report, mgr)
        grnd = [x for x in flags if x.get("source_clause_ref") in ("GRND-01", "GRND-03")]
        assert grnd and all(x["flag_type"] == "not_evaluated" for x in grnd)
        assert all(x.get("requires_approver") == "manager" for x in grnd)
        assert route == "escalate"
