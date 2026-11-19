"""QA round fixes (browser QA of the product-gaps build) — API contract, DB-backed.

1. CARD-03 / EXP-01 process-rule flags no longer count as "Rule: category not allowed"
   (list filter, exception-type counts, CSV) — new flags carry their own rule type and
   flags stored with the old shape are mapped at read time.
4. Analytics "Spend by department" groups by ``users.department`` (NULL/blank ->
   "Unassigned"), not by the e-mail local part.
7. A withdrawn subject is shown as "withdrawn" in the approvals queue (list item flag,
   ``current_status=withdrawn`` filter, CSV Status column), not "changes_requested".
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.api.endpoints import API
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import User
from database.models.trip import TripStatus

pytestmark = pytest.mark.db

APPROVALS = f"{API}/approvals"
APPROVALS_CSV = f"{API}/approvals/export.csv"
EXCEPTION_TYPES = f"{API}/approvals/exception-types"

LEGACY_CARD03 = {
    "flag_type": "unreconciled_card",
    "rule_type": "category_allow",
    "source_clause_ref": "CARD-03",
    "severity": "advisory",
    "observed": {"overdue_count": 1, "process_rule": True},
}
NEW_CARD03 = {**LEGACY_CARD03, "rule_type": "card_reconciliation"}
LEGACY_EXP01 = {
    "flag_type": "late_submission",
    "rule_type": "category_allow",
    "source_clause_ref": "EXP-01",
    "severity": "advisory",
    "observed": {"process_rule": True},
}
GROUND_CLASS = {
    "flag_type": "policy_violation",
    "rule_type": "category_allow",
    "source_clause_ref": "GROUND-CLASS",
    "severity": "advisory",
}


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, tzinfo=timezone.utc)


def _ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text[:300]
    return {i["id"] for i in resp.json()["items"]}


def _csv(resp: Any) -> list[dict[str, str]]:
    assert resp.status_code == 200, resp.text[:300]
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8")[1:])))


def _approval(db: Session, owner: User, flags: list[dict], **report_kw: Any) -> Any:
    report = f.make_expense_report(
        db,
        owner=owner,
        status=report_kw.pop("status", ExpenseReportStatus.submitted),
        total_amount=Decimal("40.00"),
        pre_submit_flags={"flags": flags},
        **report_kw,
    )
    return f.make_approval(db, submitter=owner, subject_id=report.id, risk_summary={"flags": flags})


# ---------------------------------------------------------------------------
# 1. CARD-03 is not "category not allowed"
# ---------------------------------------------------------------------------


@pytest.fixture()
def card_data(db_session: Session, users_a: dict[str, User]) -> dict[str, Any]:
    emp = users_a["employee"]
    out = {
        "legacy_card": _approval(db_session, emp, [LEGACY_CARD03]),
        "new_card": _approval(db_session, emp, [NEW_CARD03]),
        "legacy_late": _approval(db_session, emp, [LEGACY_EXP01]),
        "ground": _approval(db_session, emp, [GROUND_CLASS]),
    }
    db_session.flush()
    return out


def _sid(data: dict[str, Any], *keys: str) -> set[str]:
    return {str(data[k].id) for k in keys}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("category_allow", {"ground"}),
        ("card_reconciliation", {"legacy_card", "new_card"}),
        ("unreconciled_card", {"legacy_card", "new_card"}),
        ("submission_deadline", {"legacy_late"}),
        ("late_submission", {"legacy_late"}),
    ],
)
def test_process_flags_filter_under_their_own_type(
    card_data: dict[str, Any], finance_client: TestClient, value: str, expected: set[str]
) -> None:
    resp = finance_client.get(APPROVALS, params={"exception_type": value, "size": 100})
    assert _ids(resp) == _sid(card_data, *expected)


def test_exception_type_counts_and_row_labels_agree(
    card_data: dict[str, Any], finance_client: TestClient
) -> None:
    options = {o["value"]: o for o in finance_client.get(EXCEPTION_TYPES).json()}
    assert options["category_allow"]["count"] == 1
    assert options["card_reconciliation"]["count"] == 2
    assert options["card_reconciliation"]["label"] == "Rule: card reconciliation"
    assert options["submission_deadline"]["count"] == 1

    items = {
        i["id"]: i["exception_types"]
        for i in finance_client.get(APPROVALS, params={"size": 100}).json()["items"]
    }
    for key in ("legacy_card", "new_card"):
        assert items[str(card_data[key].id)] == ["unreconciled_card", "card_reconciliation"]
    assert "category_allow" in items[str(card_data["ground"].id)]


def test_csv_exception_column_names_card03_correctly(
    card_data: dict[str, Any], finance_client: TestClient
) -> None:
    rows = _csv(finance_client.get(APPROVALS_CSV, params={"exception_type": "category_allow"}))
    assert len(rows) == 1
    rows = _csv(finance_client.get(APPROVALS_CSV, params={"exception_type": "card_reconciliation"}))
    assert len(rows) == 2
    for row in rows:
        assert row["Exception types"] == (
            "Unreconciled card transactions; Rule: card reconciliation"
        )
        assert "category" not in row["Exception types"]


# ---------------------------------------------------------------------------
# 4. Department analytics use users.department
# ---------------------------------------------------------------------------


def test_spend_by_department_groups_by_user_department(
    db_session: Session, finance_client: TestClient, users_a: dict[str, User], org_a: Any
) -> None:
    eng1 = users_a["employee"]
    eng1.department = "Engineering"
    eng2 = f.make_user(db_session, org=org_a, department="Engineering")
    ops = f.make_user(db_session, org=org_a, department="Operations")
    nodept = f.make_user(db_session, org=org_a, department=None)
    blank = f.make_user(db_session, org=org_a, department="  ")
    for owner, amount in ((eng1, "100.00"), (eng2, "50.50"), (ops, "20.00"), (nodept, "7.00")):
        f.make_expense_report(
            db_session,
            owner=owner,
            status=ExpenseReportStatus.approved,
            total_amount=Decimal(amount),
            submitted_at=_utc(2026, 10, 1),
        )
    f.make_trip(
        db_session,
        traveler=blank,
        status=TripStatus.approved,
        budget_amount=Decimal("300.00"),
        estimated_cost=Decimal("10.00"),
    )
    f.make_trip(
        db_session,
        traveler=ops,
        status=TripStatus.approved,
        budget_amount=Decimal("80.00"),
        estimated_cost=Decimal("10.00"),
    )
    db_session.flush()

    r = finance_client.get(f"{API}/analytics/overview")
    assert r.status_code == 200, r.text
    rows = {d["department"]: d for d in r.json()["finance"]["spend_by_department"]}
    assert set(rows) == {"Engineering", "Operations", "Unassigned"}
    assert rows["Engineering"]["amount"] == pytest.approx(150.5)
    assert rows["Operations"] == {"department": "Operations", "amount": 20.0, "budget": 80.0}
    assert rows["Unassigned"]["amount"] == pytest.approx(7.0)
    assert rows["Unassigned"]["budget"] == pytest.approx(300.0)
    # Never the e-mail local part.
    assert not any("@" in k or k == eng1.email.split("@", 1)[0] for k in rows)


# ---------------------------------------------------------------------------
# 7. Withdrawn subjects show as withdrawn in the queue
# ---------------------------------------------------------------------------


@pytest.fixture()
def withdraw_data(db_session: Session, users_a: dict[str, User]) -> dict[str, Any]:
    emp = users_a["employee"]
    withdrawn_report = f.make_expense_report(
        db_session,
        owner=emp,
        status=ExpenseReportStatus.draft,
        withdrawn_at=_utc(2026, 11, 14),
        withdrawn_by_id=emp.id,
    )
    returned_report = f.make_expense_report(db_session, owner=emp, status=ExpenseReportStatus.draft)
    withdrawn_trip = f.make_trip(
        db_session,
        traveler=emp,
        status=TripStatus.cancelled,
        withdrawn_at=_utc(2026, 11, 14),
        withdrawn_by_id=emp.id,
    )
    out = {
        "withdrawn": f.make_approval(
            db_session,
            submitter=emp,
            subject_id=withdrawn_report.id,
            current_status=ApprovalStatus.changes_requested,
        ),
        "returned": f.make_approval(
            db_session,
            submitter=emp,
            subject_id=returned_report.id,
            current_status=ApprovalStatus.changes_requested,
        ),
        "withdrawn_trip": f.make_approval(
            db_session,
            submitter=emp,
            subject_type=ApprovalSubjectType.trip,
            subject_id=withdrawn_trip.id,
            current_status=ApprovalStatus.changes_requested,
        ),
        "pending": f.make_approval(
            db_session,
            submitter=emp,
            subject_id=f.make_expense_report(
                db_session, owner=emp, status=ExpenseReportStatus.submitted
            ).id,
        ),
    }
    db_session.flush()
    return out


def test_withdrawn_subject_is_flagged_on_the_list_item(
    withdraw_data: dict[str, Any], finance_client: TestClient
) -> None:
    items = {
        i["id"]: i for i in finance_client.get(APPROVALS, params={"size": 100}).json()["items"]
    }
    for key in ("withdrawn", "withdrawn_trip"):
        item = items[str(withdraw_data[key].id)]
        assert item["subject_withdrawn"] is True
        assert item["display_status"] == "withdrawn"
    returned = items[str(withdraw_data["returned"].id)]
    assert returned["subject_withdrawn"] is False
    assert returned["display_status"] == "changes_requested"
    assert items[str(withdraw_data["pending"].id)]["display_status"] == "pending"


def test_status_filter_separates_withdrawn_from_changes_requested(
    withdraw_data: dict[str, Any], finance_client: TestClient
) -> None:
    def ids(status: str) -> set[str]:
        return _ids(finance_client.get(APPROVALS, params={"current_status": status}))

    assert ids("withdrawn") == _sid(withdraw_data, "withdrawn", "withdrawn_trip")
    assert ids("changes_requested") == _sid(withdraw_data, "returned")
    assert ids("pending") == _sid(withdraw_data, "pending")


def test_invalid_status_still_400(finance_client: TestClient) -> None:
    resp = finance_client.get(APPROVALS, params={"current_status": "nope"})
    assert resp.status_code == 400
    assert "withdrawn" in resp.json()["error"]["message"]


def test_csv_status_column_says_withdrawn(
    withdraw_data: dict[str, Any], finance_client: TestClient
) -> None:
    rows = _csv(finance_client.get(APPROVALS_CSV))
    statuses = sorted(r["Status"] for r in rows)
    assert statuses == ["changes_requested", "pending", "withdrawn", "withdrawn"]
    rows = _csv(finance_client.get(APPROVALS_CSV, params={"current_status": "withdrawn"}))
    assert [r["Status"] for r in rows] == ["withdrawn", "withdrawn"]


def test_inverted_date_range_is_a_readable_400(finance_client: TestClient) -> None:
    """Defect 2 server half: the message the UI now surfaces verbatim."""
    resp = finance_client.get(
        APPROVALS, params={"submitted_from": "2026-11-20", "submitted_to": "2026-11-10"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "'submitted_from' must not be after 'submitted_to'."


# ---------------------------------------------------------------------------
# 8. Manager dashboard recent activity — readable, no raw keys / UUIDs
# ---------------------------------------------------------------------------


def test_recent_activity_summary_is_readable_without_ids(
    db_session: Session, manager_client: TestClient, users_a: dict[str, User], org_a: Any
) -> None:
    import re
    import uuid

    from backend.app.core.audit import record_audit

    report = f.make_expense_report(db_session, owner=users_a["employee"])
    record_audit(
        db_session,
        organization_id=org_a.id,
        action="expense_report.withdrawn",
        entity_type="expense_report",
        entity_id=report.id,
        actor_id=users_a["manager"].id,
    )
    record_audit(
        db_session,
        organization_id=org_a.id,
        action="approval.decided",
        entity_type="approval",
        entity_id=uuid.uuid4(),
        actor_id=users_a["manager"].id,
    )
    db_session.flush()
    r = manager_client.get(f"{API}/analytics/overview")
    assert r.status_code == 200, r.text
    items = r.json()["manager"]["recent_activity"]
    summaries = {i["action"]: i["summary"] for i in items}
    assert summaries["expense_report.withdrawn"] == "Expense report withdrawn"
    assert summaries["approval.decided"] == "Approval decided"
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-")
    for item in items:
        assert not uuid_re.search(item["summary"]), item["summary"]
        assert "_" not in item["summary"], item["summary"]


# ---------------------------------------------------------------------------
# 8. /team marks a deactivated member
# ---------------------------------------------------------------------------


def test_team_members_carry_their_account_status(
    db_session: Session, manager_client: TestClient, users_a: dict[str, User], org_a: Any
) -> None:
    from database.models.identity import UserStatus

    gone = f.make_user(
        db_session,
        org=org_a,
        manager_id=users_a["manager"].id,
        status=UserStatus.inactive,
        name="Deactivated Report",
    )
    db_session.flush()
    r = manager_client.get(f"{API}/team/members", params={"size": 100})
    assert r.status_code == 200, r.text
    by_id = {m["id"]: m for m in r.json()["items"]}
    assert by_id[str(gone.id)]["status"] == "inactive"
    assert by_id[str(users_a["employee"].id)]["status"] == "active"
    detail = manager_client.get(f"{API}/team/members/{gone.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["member"]["status"] == "inactive"


# ---------------------------------------------------------------------------
# Detail pane follow-ups: withdrawn status in the detail, consistent AI summary
# ---------------------------------------------------------------------------


def test_withdrawn_subject_is_flagged_on_the_detail(
    withdraw_data: dict[str, Any], finance_client: TestClient
) -> None:
    for key in ("withdrawn", "withdrawn_trip"):
        resp = finance_client.get(f"{APPROVALS}/{withdraw_data[key].id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["current_status"] == "changes_requested"
        assert body["subject_withdrawn"] is True
        assert body["display_status"] == "withdrawn"
    for key, status in (("returned", "changes_requested"), ("pending", "pending")):
        body = finance_client.get(f"{APPROVALS}/{withdraw_data[key].id}").json()
        assert body["subject_withdrawn"] is False
        assert body["display_status"] == status


def test_blocking_review_flag_means_not_in_policy(
    db_session: Session,
    users_a: dict[str, User],
    employee_client: TestClient,
    manager_client: TestClient,
) -> None:
    """A refiled-rejected claim adds a blocking flag; the summary must not say "in policy"."""
    employee = users_a["employee"]
    rejected = f.make_expense_report(db_session, owner=employee, title="Original")
    rejected.currency = "USD"
    line = f.make_expense(
        db_session,
        report=rejected,
        amount="40.00",
        vendor="Corner Cafe",
        expense_date=date(2026, 6, 10),
    )
    f.make_expense(  # large line: the first submission goes to a human
        db_session,
        report=rejected,
        amount="710.00",
        vendor="Vendor Big",
        expense_date=date(2026, 6, 11),
    )
    rejected.total_amount = Decimal("750.00")
    db_session.flush()
    submit = employee_client.patch(
        f"{API}/expense-reports/{rejected.id}", json={"status": "submitted"}
    )
    assert submit.status_code == 200, submit.text
    approval = db_session.scalar(sa.select(Approval).where(Approval.subject_id == rejected.id))
    assert approval is not None
    assert approval.current_status == ApprovalStatus.pending
    decided = manager_client.patch(
        f"{APPROVALS}/{approval.id}", json={"decision": "reject", "reason": "Duplicate."}
    )
    assert decided.status_code == 200, decided.text

    created = employee_client.post(
        f"{API}/expense-reports",
        json={
            "title": "Re-filed",
            "currency": "USD",
            "entries": [
                {
                    "category": line.category.value,
                    "amount": "40.00",
                    "currency": "USD",
                    "expense_date": "2026-06-10",
                    "vendor": "Corner Cafe",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    resubmit = employee_client.patch(
        f"{API}/expense-reports/{created.json()['id']}", json={"status": "submitted"}
    )
    assert resubmit.status_code == 200, resubmit.text
    refiled = db_session.scalar(
        sa.select(Approval).where(Approval.subject_id == uuid.UUID(created.json()["id"]))
    )
    assert refiled is not None
    summary = refiled.risk_summary or {}
    assert summary.get("blocking_flag_count", 0) >= 1, summary
    assert summary.get("subject_in_policy") is False, summary
