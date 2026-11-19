"""Known limits — unconfirmed ground lines, end to end on Postgres.

Through the real pre-submit evaluation and the approval agent (``assess_risk`` +
``decide_route``) with the seeded policy: a $14 typed-only "Uber" with no receipt is
within every ground-transport limit whatever its kind, so it gets one low-key advisory
(with the "add a receipt or card match" hint) and the report auto-approves.  A typed
"Amtrak" of the same size, or a $25 ride whose receipt names nothing, still escalates.
The round-5 security cases (``test_pg2_review_fixes_r5_db``) are unchanged.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.tools import ground_worst_case as gwc
from backend.tests.test_pg2_entry_rules_db import _by_clause, _entry, _run, _seed_rules
from backend.tests.test_pg2_review_fixes_r5_db import _route
from database.models.expense import ExpenseCategory, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus

pytestmark = pytest.mark.db

DET = "deterministic"
DAY = date(2026, 6, 10)


def _report(
    db: Session,
    owner: User,
    *,
    amount: str,
    vendor: str,
    description: str = "Ride",
    receipt_fields: dict | None = None,
) -> ExpenseReport:
    report = f.make_expense_report(
        db, owner=owner, title="Client visit", status=ExpenseReportStatus.submitted
    )
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.ground_transport,
        amount=amount,
        vendor=vendor,
        description=description,
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
            field_provenance={k: DET for k in ("currency", "date", *receipt_fields)},
        )
    report.total_amount = Decimal(amount)
    db.flush()
    return report


class TestEntryTimeHint:
    def test_small_typed_uber_gets_one_advisory_with_the_hint(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        flags = _run(db_session, org_a, emp, _entry("ground_transport", 14.0, vendor="Uber"))
        grnd = [x for x in flags if str(x.source_clause_ref).startswith("GRND")]
        (note,) = grnd
        assert note.flag_type == "policy_advisory" and note.requires_approver is None
        assert gwc.HINT_TEXT in note.reason and note.observed[gwc.CONFIRM_HINT] is True

    def test_typed_amtrak_escalating_flag_carries_the_hint(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp = users_a["employee"]
        _seed_rules(db_session, org_a)
        flags = _run(db_session, org_a, emp, _entry("ground_transport", 14.0, vendor="Amtrak"))
        (rail,) = _by_clause(flags, "GRND-01")
        assert rail.flag_type == "not_evaluated" and rail.requires_approver == "manager"
        assert gwc.HINT_TEXT in rail.reason and rail.observed[gwc.CONFIRM_HINT] is True
        assert not _by_clause(flags, "GRND-03")  # cleared: it cannot be breached


class TestApprovalRouting:
    def test_small_typed_uber_auto_approves(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _report(db_session, emp, amount="14.00", vendor="Uber")
        flags, route = _route(db_session, report, mgr)
        assert route == "auto_approve", [x.get("reason") for x in flags]

    def test_small_typed_amtrak_still_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _report(db_session, emp, amount="14.00", vendor="Amtrak")
        _, route = _route(db_session, report, mgr)
        assert route == "escalate"

    def test_receipt_naming_nothing_at_the_threshold_escalates(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _report(
            db_session,
            emp,
            amount="25.00",
            vendor="Uber",
            receipt_fields={"vendor": "PAYMENT RECEIPT", "amount": 25.0},
        )
        flags, route = _route(db_session, report, mgr)
        assert route == "escalate"
        grnd = [x for x in flags if x.get("source_clause_ref") in ("GRND-01", "GRND-03")]
        assert grnd and all(x.get("requires_approver") == "manager" for x in grnd)

    def test_receipt_naming_nothing_below_the_threshold_auto_approves(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _report(
            db_session,
            emp,
            amount="24.99",
            vendor="Uber",
            receipt_fields={"vendor": "PAYMENT RECEIPT", "amount": 24.99},
        )
        flags, route = _route(db_session, report, mgr)
        assert route == "auto_approve", [x.get("reason") for x in flags]


class TestClearedTotalPerReport:
    def test_two_small_typed_rides_on_one_report_escalate(
        self, db_session: Session, org_a: Organization, users_a: dict[str, User]
    ) -> None:
        """Integration security fix: each $14 typed "Uber" alone would clear, but
        together they reach the $25 receipt threshold, so the report escalates."""
        emp, mgr = users_a["employee"], users_a["manager"]
        _seed_rules(db_session, org_a)
        report = _report(db_session, emp, amount="14.00", vendor="Uber")
        f.make_expense(
            db_session,
            report=report,
            category=ExpenseCategory.ground_transport,
            amount="14.00",
            vendor="Lyft",
            description="Ride",
            expense_date=DAY,
            match_source=ExpenseMatchSource.manual,
        )
        report.total_amount = Decimal("28.00")
        db_session.flush()
        flags, route = _route(db_session, report, mgr)
        assert route == "escalate", [x.get("reason") for x in flags]
        at_risk = [x for x in flags if (x.get("observed") or {}).get(gwc.AT_RISK)]
        assert at_risk and "receipt threshold" in str(at_risk[0]["observed"][gwc.AT_RISK])
