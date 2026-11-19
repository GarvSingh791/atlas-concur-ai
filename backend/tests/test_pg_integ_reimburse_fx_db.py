"""Product-gaps integration — reimbursing a report that holds a foreign-currency line.

The lifecycle track's reimburse guard compares the stored total with the plain
SUM of the lines (fail closed on drift).  The fxcard track made the stored total
the report-currency total, converting foreign lines.  Combined, every mixed-
currency report would have been refused with REIMBURSEMENT_TOTAL_MISMATCH.  The
guard now recomputes the total with the same converting rule; drift still fails.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.modules.expense.service import _compute_total
from backend.tests.test_pg_lifecycle_db import API, _client, _code
from database.models.approval import ApprovalStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User

pytestmark = pytest.mark.db


def _approved_mixed_report(db: Session, owner: User) -> ExpenseReport:
    report = f.make_expense_report(db, owner=owner, currency="USD", title="Lisbon")
    f.make_expense(db, report=report, amount="100.00")
    f.make_expense(db, report=report, amount="220.00", currency="EUR")
    db.flush()
    report.total_amount = _compute_total(db, report.id)  # 100 + 238.70
    report.status = ExpenseReportStatus.approved
    f.make_approval(
        db, submitter=owner, subject_id=report.id, current_status=ApprovalStatus.approved
    )
    db.flush()
    return report


def test_mixed_currency_report_can_be_reimbursed_at_its_converted_total(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _approved_mixed_report(db_session, users_a["employee"])
    assert report.total_amount == Decimal("338.70")
    r = _client(app, users_a["finance"]).post(f"{API}/expense-reports/{report.id}/reimburse")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reimbursed"
    assert Decimal(r.json()["reimbursement_amount"]) == Decimal("338.70")


def test_mixed_currency_report_with_a_drifted_total_still_fails_closed(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _approved_mixed_report(db_session, users_a["employee"])
    report.total_amount = Decimal("320.00")  # the raw, unconverted sum
    db_session.flush()
    r = _client(app, users_a["finance"]).post(f"{API}/expense-reports/{report.id}/reimburse")
    assert r.status_code == 409 and _code(r) == "REIMBURSEMENT_TOTAL_MISMATCH", r.text
