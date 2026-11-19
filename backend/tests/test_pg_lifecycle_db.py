"""Product gaps 1 / 4 / 8 — report & trip lifecycle (DB-backed, real JWT role clients).

Gap 1  simulated reimbursement: finance-only, approved + decided-approved only,
       idempotent-safe (409, no double record), no self-reimbursement, one audit
       row, Decimal amount + server reference; a reimbursed report is terminal
       and immutable (every write path refuses it).
Gap 4  owner withdrawal of a RETURNED report / trip: owner-only, returned-only,
       audited, read-only afterwards, hidden from the default list, lines
       released (moved) to other drafts with reviewed receipts kept for audit;
       the approval agent stops escalating the owner's other submissions; a
       line from a withdrawn-after-REJECT report is still flagged on re-file, one
       from a withdrawn-after-request-changes report is escalated as a re-filed
       RETURNED claim (integration security fix), never as a rejected one.
Gap 8  trip completion: traveler or finance, approved + ended only, audited;
       the elapsed-trip sweep completes approved trips ended > 1 day ago.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.errors import ConflictError
from backend.app.core.security import create_access_token
from backend.app.modules.expense import service as expense_service
from backend.tests.test_c7_g3_receipts_api import _MemStorage, _ScenarioOcr
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import RoleKey, User
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType
from database.seeds.complete_elapsed_trips import complete_elapsed
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.storage_provider.factory import get_storage_provider

pytestmark = pytest.mark.db

API = "/api/v1"


def _today() -> date:
    """The app's "today" (UTC: travel.service / complete_elapsed_trips).  The local
    date differs for part of every day, which made the trip-completion tests fail
    whenever the run happened between local and UTC midnight."""
    return datetime.now(timezone.utc).date()


FAR = _today() + timedelta(days=21)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _report_with(
    db: Session, owner: User, amounts: list[str], *, title: str = "Trip", day: int = 10
) -> tuple[ExpenseReport, list[Expense]]:
    report = f.make_expense_report(db, owner=owner, title=title)
    entries = [
        f.make_expense(
            db,
            report=report,
            amount=amount,
            vendor=f"Vendor {i}",
            expense_date=date(2026, 6, day + i),
        )
        for i, amount in enumerate(amounts)
    ]
    report.total_amount = sum((Decimal(a) for a in amounts), Decimal("0"))
    db.flush()
    return report, entries


def _approval(db: Session, subject_id: Any) -> Approval:
    db.expire_all()
    approval = db.scalar(sa.select(Approval).where(Approval.subject_id == subject_id))
    assert approval is not None
    return approval


def _submit(client: TestClient, report_id: Any) -> dict:
    r = client.patch(f"{API}/expense-reports/{report_id}", json={"status": "submitted"})
    assert r.status_code == 200, r.text
    return r.json()


def _decide(client: TestClient, approval: Approval, decision: str) -> None:
    r = client.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": decision, "reason": "Attach the folio for the hotel."},
    )
    assert r.status_code == 200, r.text


def _returned(
    db: Session, emp: TestClient, mgr: TestClient, owner: User, amounts: list[str], **kw: Any
) -> tuple[ExpenseReport, list[Expense]]:
    """A report the manager sent back (request changes) — a draft with history."""
    report, entries = _report_with(db, owner, amounts, **kw)
    assert _submit(emp, report.id)["approval_status"] == "pending"
    _decide(mgr, _approval(db, report.id), "request_changes")
    return report, entries


def _flags(db: Session, subject_id: Any) -> list[dict]:
    return list((_approval(db, subject_id).risk_summary or {}).get("flags") or [])


def _audits(db: Session, entity_id: Any, action: str) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(AuditLog.entity_id == entity_id, AuditLog.action == action)
        ).all()
    )


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


def _new_draft(client: TestClient, title: str = "Fresh report") -> str:
    r = client.post(f"{API}/expense-reports", json={"title": title, "currency": "USD"})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _move(client: TestClient, source_id: Any, entries: list[Expense], target_id: Any) -> Any:
    return client.post(
        f"{API}/expense-reports/{source_id}/entries/move",
        json={"entry_ids": [str(e.id) for e in entries], "target_report_id": str(target_id)},
    )


def _auto_approved_report(
    db: Session, emp: TestClient, owner: User, amount: str = "12.50", day: int = 20
) -> ExpenseReport:
    report, _ = _report_with(db, owner, [amount], title="Taxi", day=day)
    assert _submit(emp, report.id)["status"] == "approved"
    db.refresh(report)
    return report


# ===========================================================================
# Gap 1 — simulated reimbursement
# ===========================================================================


def test_finance_reimburses_an_approved_report_once(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, finance = users_a["employee"], users_a["finance"]
    emp, fin = _client(app, employee), _client(app, finance)
    report = _auto_approved_report(db_session, emp, employee)

    r = fin.post(f"{API}/expense-reports/{report.id}/reimburse", json={"note": "Batch 7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reimbursed"
    assert Decimal(body["reimbursement_amount"]) == Decimal("12.50")
    assert body["reimbursed_by_id"] == str(finance.id)
    assert re.fullmatch(r"REIMB-\d{8}-[0-9A-F]{12}", body["reimbursement_reference"])
    assert body["reimbursed_at"]

    db_session.expire_all()
    stored = db_session.get(ExpenseReport, report.id)
    assert stored is not None and stored.status == ExpenseReportStatus.reimbursed
    assert stored.reimbursement_amount == Decimal("12.50")
    rows = _audits(db_session, report.id, "expense_report.reimbursed")
    assert len(rows) == 1
    assert rows[0].actor_id == finance.id
    after = rows[0].after_state or {}
    assert after["note"] == "Batch 7" and after["simulated"] is True

    # Idempotent-safe: a second call is a clean 409 and records nothing new.
    again = fin.post(f"{API}/expense-reports/{report.id}/reimburse")
    assert again.status_code == 409 and _code(again) == "ALREADY_REIMBURSED"
    assert len(_audits(db_session, report.id, "expense_report.reimbursed")) == 1
    db_session.expire_all()
    reloaded = db_session.get(ExpenseReport, report.id)
    assert reloaded is not None
    assert reloaded.reimbursement_reference == body["reimbursement_reference"]

    # The owner sees status, date and reference on detail and list.
    detail = emp.get(f"{API}/expense-reports/{report.id}").json()
    assert detail["status"] == "reimbursed"
    assert detail["reimbursement_reference"] == body["reimbursement_reference"]
    listed = emp.get(f"{API}/expense-reports?mine=true&status=reimbursed").json()["items"]
    assert [i["id"] for i in listed] == [str(report.id)]
    assert listed[0]["reimbursement_reference"] == body["reimbursement_reference"]


def test_reimburse_is_finance_only_and_never_self(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager, finance = users_a["employee"], users_a["manager"], users_a["finance"]
    emp = _client(app, employee)
    report = _auto_approved_report(db_session, emp, employee)
    for role_client in (emp, _client(app, manager)):
        r = role_client.post(f"{API}/expense-reports/{report.id}/reimburse")
        assert r.status_code == 403, r.text

    # SoD: a finance user's own approved report.
    own, _ = _report_with(db_session, finance, ["30.00"], title="Finance own")
    own.status = ExpenseReportStatus.approved
    f.make_approval(
        db_session,
        submitter=finance,
        subject_id=own.id,
        current_status=ApprovalStatus.approved,
    )
    db_session.flush()
    r = _client(app, finance).post(f"{API}/expense-reports/{own.id}/reimburse")
    assert r.status_code == 403, r.text
    db_session.refresh(own)
    assert own.status == ExpenseReportStatus.approved
    assert _audits(db_session, own.id, "expense_report.reimbursed") == []
    # ...and the queue never offers it.
    queue = _client(app, finance).get(f"{API}/reimbursements/queue").json()["items"]
    by_id = {i["id"]: i for i in queue}
    assert by_id[str(own.id)]["can_reimburse"] is False
    assert by_id[str(report.id)]["can_reimburse"] is True
    assert by_id[str(report.id)]["owner_name"] == employee.name


def test_reimburse_cross_tenant_is_404(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    emp = _client(app, users_a["employee"])
    report = _auto_approved_report(db_session, emp, users_a["employee"])
    r = _client(app, users_b["finance"]).post(f"{API}/expense-reports/{report.id}/reimburse")
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("report_status", "approval_status", "code"),
    [
        (ExpenseReportStatus.approved, None, "APPROVAL_NOT_APPROVED"),
        (ExpenseReportStatus.approved, ApprovalStatus.pending, "APPROVAL_NOT_APPROVED"),
        (ExpenseReportStatus.approved, ApprovalStatus.changes_requested, "APPROVAL_NOT_APPROVED"),
        (ExpenseReportStatus.approved, ApprovalStatus.rejected, "APPROVAL_NOT_APPROVED"),
        (ExpenseReportStatus.submitted, ApprovalStatus.pending, "REPORT_NOT_APPROVED"),
        (ExpenseReportStatus.draft, None, "REPORT_NOT_APPROVED"),
        (ExpenseReportStatus.rejected, ApprovalStatus.rejected, "REPORT_NOT_APPROVED"),
    ],
)
def test_reimburse_requires_approved_report_with_approved_approval(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    report_status: ExpenseReportStatus,
    approval_status: ApprovalStatus | None,
    code: str,
) -> None:
    employee = users_a["employee"]
    report, _ = _report_with(db_session, employee, ["20.00"])
    report.status = report_status
    if approval_status is not None:
        f.make_approval(
            db_session, submitter=employee, subject_id=report.id, current_status=approval_status
        )
    db_session.flush()
    r = _client(app, users_a["finance"]).post(f"{API}/expense-reports/{report.id}/reimburse")
    assert r.status_code == 409 and _code(r) == code, r.text
    db_session.refresh(report)
    assert report.status == report_status and report.reimbursed_at is None
    assert _audits(db_session, report.id, "expense_report.reimbursed") == []


def test_reimburse_fails_closed_on_total_mismatch_and_stale_version(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = _client(app, users_a["employee"])
    fin = _client(app, users_a["finance"])
    report = _auto_approved_report(db_session, emp, users_a["employee"])
    stale = fin.post(f"{API}/expense-reports/{report.id}/reimburse", headers={"If-Match": "999"})
    assert stale.status_code == 409
    report.total_amount = Decimal("99.99")  # drifted from its single 12.50 line
    db_session.flush()
    r = fin.post(f"{API}/expense-reports/{report.id}/reimburse")
    assert r.status_code == 409 and _code(r) == "REIMBURSEMENT_TOTAL_MISMATCH"
    db_session.refresh(report)
    assert report.status == ExpenseReportStatus.approved


def test_reimbursed_report_is_terminal_and_immutable(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr, fin = (_client(app, u) for u in (employee, manager, users_a["finance"]))
    report = _auto_approved_report(db_session, emp, employee)
    entry = db_session.scalars(sa.select(Expense).where(Expense.report_id == report.id)).one()
    assert fin.post(f"{API}/expense-reports/{report.id}/reimburse").status_code == 200
    receipt = f.make_receipt(db_session, uploaded_by=employee, ocr_status=OcrStatus.processed)
    other = _new_draft(emp)

    attempts = {
        "edit entry": emp.patch(f"{API}/expenses/{entry.id}", json={"amount": "1.00"}),
        "delete entry": emp.delete(f"{API}/expenses/{entry.id}"),
        "delete report": emp.delete(f"{API}/expense-reports/{report.id}"),
        "rename": emp.patch(f"{API}/expense-reports/{report.id}/details", json={"title": "x"}),
        "move out": _move(emp, report.id, [entry], other),
        "attach": emp.post(
            f"{API}/expenses/{entry.id}/receipts", json={"receipt_id": str(receipt.id)}
        ),
        "withdraw": emp.post(f"{API}/expense-reports/{report.id}/withdraw"),
        "reopen": emp.patch(f"{API}/expense-reports/{report.id}", json={"status": "draft"}),
    }
    for label, resp in attempts.items():
        assert resp.status_code == 409, (label, resp.status_code, resp.text)
    # A human can no longer reverse the auto-approval of a paid report.
    reversal = mgr.patch(
        f"{API}/approvals/{_approval(db_session, report.id).id}",
        json={"decision": "request_changes", "reason": "too late"},
    )
    assert reversal.status_code == 400, reversal.text
    db_session.expire_all()
    stored = db_session.get(ExpenseReport, report.id)
    assert stored is not None and stored.status == ExpenseReportStatus.reimbursed
    assert db_session.get(Expense, entry.id).amount == Decimal("12.50")  # type: ignore[union-attr]
    assert _approval(db_session, report.id).current_status == ApprovalStatus.auto_approved


# ===========================================================================
# Gap 4 — withdrawing a returned report
# ===========================================================================


def test_withdrawing_the_returned_report_restores_auto_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, _ = _returned(db_session, emp, mgr, employee, ["40.00", "710.00"])

    # Blocked: while the return is open every new submission escalates.
    blocked, _ = _report_with(db_session, employee, ["12.50"], title="Blocked", day=20)
    assert _submit(emp, blocked.id)["status"] == "submitted"
    assert any(
        fl.get("flag_type") == "open_returned_report" for fl in _flags(db_session, blocked.id)
    )

    r = emp.post(
        f"{API}/expense-reports/{returned.id}/withdraw", json={"reason": "Trip was cancelled"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["withdrawn_at"] and body["withdrawal_reason"] == "Trip was cancelled"
    assert body["status"] == "draft" and body["approval_status"] == "changes_requested"
    rows = _audits(db_session, returned.id, "expense_report.withdrawn")
    assert len(rows) == 1 and rows[0].actor_id == employee.id
    withdrawn_state = rows[0].after_state or {}
    assert withdrawn_state["approval_status"] == "changes_requested"
    assert len(withdrawn_state["entries"]) == 2
    # Approval history is kept.
    approval = _approval(db_session, returned.id)
    assert approval.current_status == ApprovalStatus.changes_requested
    assert db_session.scalar(
        sa.select(sa.func.count(ApprovalStep.id)).where(ApprovalStep.approval_id == approval.id)
    )

    # Unblocked: the next small report auto-approves again.
    fresh, _ = _report_with(db_session, employee, ["13.75"], title="Fresh", day=24)
    assert _submit(emp, fresh.id)["status"] == "approved"

    # Hidden from the default list, reachable via the filter.
    default_ids = {i["id"] for i in emp.get(f"{API}/expense-reports?mine=true").json()["items"]}
    assert str(returned.id) not in default_ids and str(fresh.id) in default_ids
    only = emp.get(f"{API}/expense-reports?mine=true&withdrawn=true").json()["items"]
    assert [i["id"] for i in only] == [str(returned.id)]
    assert only[0]["withdrawn_at"]
    every = emp.get(f"{API}/expense-reports?mine=true&withdrawn=any").json()["items"]
    assert str(returned.id) in {i["id"] for i in every}
    assert emp.get(f"{API}/expense-reports?withdrawn=maybe").status_code == 400

    # Idempotent-safe.
    again = emp.post(f"{API}/expense-reports/{returned.id}/withdraw")
    assert again.status_code == 409 and _code(again) == "REPORT_WITHDRAWN"
    assert len(_audits(db_session, returned.id, "expense_report.withdrawn")) == 1


def test_another_employees_returned_report_never_blocks_me(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User], org_a: Any
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    colleague = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=manager.id)
    col, mgr = _client(app, colleague), _client(app, manager)
    _returned(db_session, col, mgr, colleague, ["40.00", "710.00"])

    mine, _ = _report_with(db_session, employee, ["12.50"], title="Mine", day=20)
    assert _submit(_client(app, employee), mine.id)["status"] == "approved"


def test_withdraw_is_owner_only(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, _ = _returned(db_session, emp, mgr, employee, ["40.00", "710.00"])
    assert mgr.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 403
    fin = _client(app, users_a["finance"])
    assert fin.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 403
    other_org = _client(app, users_b["employee"])
    assert other_org.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 404
    db_session.refresh(returned)
    assert returned.withdrawn_at is None
    assert _audits(db_session, returned.id, "expense_report.withdrawn") == []


def test_withdraw_refuses_reports_that_are_not_returned(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    never_submitted, _ = _report_with(db_session, employee, ["10.00"], title="Never")
    under_review, _ = _report_with(db_session, employee, ["40.00", "710.00"], title="Review")
    _submit(emp, under_review.id)
    approved = _auto_approved_report(db_session, emp, employee, day=25)
    finally_rejected, _ = _report_with(db_session, employee, ["40.00", "720.00"], day=1)
    _submit(emp, finally_rejected.id)
    _decide(mgr, _approval(db_session, finally_rejected.id), "reject")

    for report in (never_submitted, under_review, approved, finally_rejected):
        r = emp.post(f"{API}/expense-reports/{report.id}/withdraw")
        assert r.status_code == 409 and _code(r) == "REPORT_NOT_RETURNED", r.text
    # Reimbursed too.
    assert (
        _client(app, users_a["finance"])
        .post(f"{API}/expense-reports/{approved.id}/reimburse")
        .status_code
        == 200
    )
    r = emp.post(f"{API}/expense-reports/{approved.id}/withdraw")
    assert r.status_code == 409 and _code(r) == "REPORT_NOT_RETURNED"


class _RecordingStorage(_MemStorage):
    """In-memory storage that also remembers every key ever written (even if deleted)."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[str] = []

    def put_object(self, key: str, data: bytes, *, content_type: str = "") -> None:
        self.puts.append(key)
        super().put_object(key, data, content_type=content_type)


@pytest.fixture()
def mem_storage(app: Any) -> Any:
    """In-memory storage / OCR / LLM so the receipt upload path never leaves the process."""
    mem = _RecordingStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _ScenarioOcr()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    yield mem
    for dep in (get_storage_provider, get_ocr_provider, get_llm_provider):
        app.dependency_overrides.pop(dep, None)


def _receipts_on(db: Session, expense_id: Any) -> int:
    return int(
        db.scalar(sa.select(sa.func.count(Receipt.id)).where(Receipt.expense_id == expense_id)) or 0
    )


def test_withdrawn_report_is_read_only(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    mem_storage: _RecordingStorage,
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, (small, _big) = _returned(db_session, emp, mgr, employee, ["40.00", "710.00"])
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        extracted_fields={"amount": "40.00", "currency": "USD"},
    )
    stray, (stray_line,) = _report_with(db_session, employee, ["5.00"], title="Stray", day=27)

    attempts = {
        "submit": emp.patch(f"{API}/expense-reports/{returned.id}", json={"status": "submitted"}),
        "rename": emp.patch(f"{API}/expense-reports/{returned.id}/details", json={"title": "x"}),
        "edit entry": emp.patch(f"{API}/expenses/{small.id}", json={"amount": "1.00"}),
        "delete entry": emp.delete(f"{API}/expenses/{small.id}"),
        "delete report": emp.delete(f"{API}/expense-reports/{returned.id}"),
        "attach": emp.post(
            f"{API}/expenses/{small.id}/receipts", json={"receipt_id": str(receipt.id)}
        ),
        "move in": _move(emp, stray.id, [stray_line], returned.id),
        # Upload a NEW receipt straight onto a line (POST /receipts?expense_id=).
        "upload to entry": emp.post(
            f"{API}/receipts",
            params={"expense_id": str(small.id)},
            files={"file": ("r.jpg", b"meal", "image/jpeg")},
        ),
    }
    for label, resp in attempts.items():
        assert resp.status_code == 409, (label, resp.status_code, resp.text)
    for label in (
        "submit",
        "rename",
        "edit entry",
        "delete entry",
        "attach",
        "move in",
        "upload to entry",
    ):
        assert _code(attempts[label]) == "REPORT_WITHDRAWN", label
    db_session.expire_all()
    # The upload refusal happens BEFORE anything is stored or OCR'd (the router
    # pre-check) — not merely cleaned up after the locked link step refuses.
    assert mem_storage.puts == []
    assert mem_storage.objects == {}
    assert _receipts_on(db_session, small.id) == 0
    assert db_session.get(Expense, small.id).amount == Decimal("40.00")  # type: ignore[union-attr]
    assert db_session.get(Expense, stray_line.id).report_id == stray.id  # type: ignore[union-attr]
    # Entries say why they can't be deleted.
    lines = emp.get(f"{API}/expense-reports/{returned.id}/entries").json()
    assert all(not line["deletable"] for line in lines)


def test_link_receipt_locked_refuses_a_withdrawn_report_on_its_own(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """The locked link step re-checks ``withdrawn_at`` itself (defence in depth).

    Driven directly, bypassing the router pre-check, so this guard is proven
    independently of the upload / attach-existing pre-checks.
    """
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, (small, _big) = _returned(db_session, emp, mgr, employee, ["40.00", "710.00"])
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200
    db_session.expire_all()
    receipt = f.make_receipt(db_session, uploaded_by=employee, ocr_status=OcrStatus.processed)

    with pytest.raises(ConflictError) as exc:
        expense_service.link_receipt_locked(
            db_session,
            employee,
            receipt=receipt,
            entry_id=small.id,
            expected_version=None,
            source="upload",
        )
    assert exc.value.code == "REPORT_WITHDRAWN"
    db_session.expire_all()
    assert _receipts_on(db_session, small.id) == 0


def test_lines_released_after_request_changes_are_not_punished_as_rejected(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    # Product-gaps integration (security review): the track's original spec let a
    # released withdrawn-after-request-changes line auto-approve, which laundered
    # the reviewer's return.  Reconciled: the line is still MOVABLE and FILEABLE
    # and is never treated as a rejected claim, but it now goes to a human with a
    # ``refiled_returned_claim`` flag naming the original return
    # (backend/tests/test_pg_integ_lifecycle_sod_db.py has the full proofs).
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, (small, _big) = _returned(db_session, emp, mgr, employee, ["12.50", "710.00"])
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200

    target = _new_draft(emp)
    moved = _move(emp, returned.id, [small], target)
    assert moved.status_code == 200, moved.text
    assert moved.json()["source"]["total_amount"] == "710.00"
    body = _submit(emp, target)
    flags = _flags(db_session, uuid.UUID(target))
    assert body["status"] == "submitted", flags  # escalated, never auto-approved
    assert not any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags
    assert any(fl.get("flag_type") == "refiled_returned_claim" for fl in flags), flags


@pytest.mark.parametrize("legacy_reject_step", [False, True])
def test_lines_released_after_reject_are_still_flagged_on_refile(
    app: Any,
    rbac_catalog: None,
    db_session: Session,
    users_a: dict[str, User],
    legacy_reject_step: bool,
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    rejected, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    _submit(emp, rejected.id)
    _decide(mgr, _approval(db_session, rejected.id), "reject")
    if legacy_reject_step:
        # A pre-0014 reject step froze nothing: only the withdrawal row remembers.
        for step in db_session.scalars(
            sa.select(ApprovalStep).where(
                ApprovalStep.approval_id == _approval(db_session, rejected.id).id
            )
        ):
            step.subject_snapshot = None
        db_session.flush()
    assert (
        emp.patch(f"{API}/expense-reports/{rejected.id}", json={"status": "draft"}).status_code
        == 200
    )
    w = emp.post(f"{API}/expense-reports/{rejected.id}/withdraw")
    assert w.status_code == 200, w.text
    assert w.json()["approval_status"] == "rejected"

    target = _new_draft(emp)
    assert _move(emp, rejected.id, [small], target).status_code == 200
    body = _submit(emp, target)
    assert body["status"] == "submitted"  # escalated, never auto-approved
    flags = _flags(db_session, uuid.UUID(target))
    assert any(fl.get("flag_type") == "refiled_rejected_claim" for fl in flags), flags
    assert not any(fl.get("flag_type") == "open_returned_report" for fl in flags), flags


def test_released_lines_keep_reviewed_receipts_for_audit(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    report, (small, _big) = _report_with(db_session, employee, ["12.50", "710.00"])
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        expense_id=small.id,
        extracted_fields={"amount": "12.50", "currency": "USD", "date": "2026-06-10"},
    )
    _submit(emp, report.id)
    _decide(mgr, _approval(db_session, report.id), "request_changes")
    assert emp.post(f"{API}/expense-reports/{report.id}/withdraw").status_code == 200
    target = _new_draft(emp)
    assert _move(emp, report.id, [small], target).status_code == 200
    assert len(_audits(db_session, receipt.id, "receipt.retained_for_audit")) == 1

    corrected = emp.patch(
        f"{API}/receipts/{receipt.id}", json={"field": "vendor", "value": "Other"}
    )
    assert corrected.status_code == 409 and _code(corrected) == "RECEIPT_LOCKED", corrected.text
    deleted = emp.delete(f"{API}/receipts/{receipt.id}")
    assert deleted.status_code == 409 and _code(deleted) == "RECEIPT_RETAINED", deleted.text
    # Deleting the released line keeps (detaches) the receipt instead of deleting it.
    gone = emp.delete(f"{API}/expenses/{small.id}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["retained_receipt_ids"] == [str(receipt.id)]
    db_session.expire_all()
    kept = db_session.get(Receipt, receipt.id)
    assert kept is not None and kept.expense_id is None


# ===========================================================================
# Gap 4 — withdrawing a returned trip
# ===========================================================================


def _cheap_trip(db: Session, traveler: User, destination: str, start: date) -> Trip:
    trip = f.make_trip(
        db,
        traveler=traveler,
        primary_destination=destination,
        start_date=start,
        end_date=start + timedelta(days=2),
        estimated_cost=Decimal("150.00"),
    )
    f.make_trip_segment(
        db,
        trip=trip,
        segment_type=SegmentType.flight,
        supplier="Delta",
        amount=Decimal("150.00"),
        currency="USD",
        in_policy=True,
    )
    return trip


def _returned_trip(db: Session, traveler: User, mgr: TestClient) -> Trip:
    trip = f.make_trip(db, traveler=traveler, status=TripStatus.pending_approval)
    approval = f.make_approval(
        db, submitter=traveler, subject_type=ApprovalSubjectType.trip, subject_id=trip.id
    )
    r = mgr.patch(
        f"{API}/approvals/{approval.id}", json={"decision": "request_changes", "reason": "r"}
    )
    assert r.status_code == 200, r.text
    db.refresh(trip)
    assert trip.status == TripStatus.draft
    return trip


def test_withdrawing_a_returned_trip_closes_it_and_unblocks_auto_approval(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned = _returned_trip(db_session, employee, mgr)
    # The ordinary cancel is refused for a returned trip...
    cancel = emp.patch(f"{API}/trips/{returned.id}", json={"status": "cancelled"})
    assert cancel.status_code == 409 and _code(cancel) == "SUBJECT_UNDER_REVIEW"

    blocked = _cheap_trip(db_session, employee, "Denver", FAR)
    r = emp.patch(f"{API}/trips/{blocked.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "pending_approval", r.text
    assert any(
        fl.get("flag_type") == "open_returned_report" for fl in _flags(db_session, blocked.id)
    )

    # ...the explicit withdrawal is not, and is owner-only.
    assert mgr.post(f"{API}/trips/{returned.id}/withdraw").status_code == 403
    w = emp.post(f"{API}/trips/{returned.id}/withdraw", json={"reason": "Not travelling"})
    assert w.status_code == 200, w.text
    assert w.json()["status"] == "cancelled" and w.json()["withdrawn_at"]
    assert len(_audits(db_session, returned.id, "trip.withdrawn")) == 1
    assert _approval(db_session, returned.id).current_status == ApprovalStatus.changes_requested
    again = emp.post(f"{API}/trips/{returned.id}/withdraw")
    assert again.status_code == 409 and _code(again) == "TRIP_WITHDRAWN"

    fresh = _cheap_trip(db_session, employee, "Austin", FAR + timedelta(days=30))
    r = emp.patch(f"{API}/trips/{fresh.id}", json={"status": "pending_approval"})
    assert r.json()["status"] == "approved", _flags(db_session, fresh.id)


def test_withdraw_trip_refuses_trips_that_are_not_returned(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(app, employee)
    draft = f.make_trip(db_session, traveler=employee)
    pending = f.make_trip(db_session, traveler=employee, status=TripStatus.pending_approval)
    f.make_approval(
        db_session, submitter=employee, subject_type=ApprovalSubjectType.trip, subject_id=pending.id
    )
    for trip in (draft, pending):
        r = emp.post(f"{API}/trips/{trip.id}/withdraw")
        assert r.status_code == 409 and _code(r) == "TRIP_NOT_RETURNED", r.text


# ===========================================================================
# Gap 8 — trip completion
# ===========================================================================


def _approved_trip(db: Session, traveler: User, *, ends_in_days: int) -> Trip:
    end = _today() + timedelta(days=ends_in_days)
    return f.make_trip(
        db,
        traveler=traveler,
        status=TripStatus.approved,
        start_date=end - timedelta(days=2),
        end_date=end,
    )


def test_traveler_or_finance_complete_an_ended_approved_trip(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp, mgr, fin = (_client(app, users_a[k]) for k in ("employee", "manager", "finance"))
    ended_today = _approved_trip(db_session, employee, ends_in_days=0)
    ended_earlier = _approved_trip(db_session, employee, ends_in_days=-4)

    assert mgr.post(f"{API}/trips/{ended_today.id}/complete").status_code == 403
    r = emp.post(f"{API}/trips/{ended_today.id}/complete")
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    r = fin.post(f"{API}/trips/{ended_earlier.id}/complete")
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    rows = _audits(db_session, ended_earlier.id, "trip.completed")
    assert len(rows) == 1 and rows[0].actor_id == users_a["finance"].id

    again = emp.post(f"{API}/trips/{ended_today.id}/complete")
    assert again.status_code == 409 and _code(again) == "TRIP_NOT_APPROVED"
    assert len(_audits(db_session, ended_today.id, "trip.completed")) == 1
    # Completed is terminal and read-only.
    assert (
        emp.patch(f"{API}/trips/{ended_today.id}", json={"status": "cancelled"}).status_code == 409
    )


def test_trip_cannot_be_completed_before_it_ends_or_unless_approved(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp = _client(app, employee)
    future = _approved_trip(db_session, employee, ends_in_days=1)
    r = emp.post(f"{API}/trips/{future.id}/complete")
    assert r.status_code == 409 and _code(r) == "TRIP_NOT_ENDED", r.text
    patched = emp.patch(f"{API}/trips/{future.id}", json={"status": "completed"})
    assert patched.status_code == 409 and _code(patched) == "TRIP_NOT_ENDED"
    draft = f.make_trip(
        db_session,
        traveler=employee,
        start_date=_today() - timedelta(days=5),
        end_date=_today() - timedelta(days=3),
    )
    r = emp.post(f"{API}/trips/{draft.id}/complete")
    assert r.status_code == 409 and _code(r) == "TRIP_NOT_APPROVED"
    db_session.refresh(future)
    assert future.status == TripStatus.approved


def test_elapsed_trip_sweep_is_idempotent_and_audited(
    db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    long_ago = _approved_trip(db_session, employee, ends_in_days=-3)
    yesterday = _approved_trip(db_session, employee, ends_in_days=-1)
    upcoming = _approved_trip(db_session, employee, ends_in_days=2)
    pending = f.make_trip(
        db_session,
        traveler=employee,
        status=TripStatus.pending_approval,
        start_date=_today() - timedelta(days=9),
        end_date=_today() - timedelta(days=7),
    )

    def factory() -> Any:
        return contextlib.nullcontext(db_session)

    first = complete_elapsed(factory)
    assert first["failed"] == 0 and first["completed"] >= 1
    db_session.expire_all()
    assert db_session.get(Trip, long_ago.id).status == TripStatus.completed  # type: ignore[union-attr]
    for untouched, status in (
        (yesterday, TripStatus.approved),
        (upcoming, TripStatus.approved),
        (pending, TripStatus.pending_approval),
    ):
        assert db_session.get(Trip, untouched.id).status == status  # type: ignore[union-attr]
    rows = _audits(db_session, long_ago.id, "trip.completed")
    assert len(rows) == 1 and rows[0].actor_id is None
    assert (rows[0].after_state or {})["trigger"] == "sweep"

    second = complete_elapsed(factory)
    # Phase 10: the result also reports the dry-run count and whether the run was capped.
    assert second == {"completed": 0, "skipped": 0, "failed": 0, "would_complete": 0, "capped": 0}
    assert len(_audits(db_session, long_ago.id, "trip.completed")) == 1


def test_withdrawing_freezes_receipts_no_reviewer_saw_yet(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    """A receipt attached AFTER the return is correctable while the report is
    being fixed — once the report is withdrawn (closed) it is frozen too."""
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, (small, _big) = _returned(db_session, emp, mgr, employee, ["12.50", "710.00"])
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        extracted_fields={"amount": "12.50", "currency": "USD", "date": "2026-06-10"},
    )
    attached = emp.post(f"{API}/expenses/{small.id}/receipts", json={"receipt_id": str(receipt.id)})
    assert attached.status_code == 200, attached.text
    ok = emp.patch(f"{API}/receipts/{receipt.id}", json={"field": "vendor", "value": "Cafe"})
    assert ok.status_code == 200, ok.text

    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200
    locked = emp.patch(f"{API}/receipts/{receipt.id}", json={"field": "vendor", "value": "Bar"})
    assert locked.status_code == 409 and _code(locked) == "RECEIPT_LOCKED", locked.text
    gone = emp.delete(f"{API}/receipts/{receipt.id}")
    assert gone.status_code == 409, gone.text


# ===========================================================================
# Analytics — cycle time is submitted -> reimbursed (PRD US-F3 AC2)
# ===========================================================================


def _kpis(client: TestClient) -> dict[str, dict]:
    body = client.get(f"{API}/analytics/overview").json()
    return {k["label"]: k for k in body["finance"]["kpis"]}


def test_finance_cycle_time_is_submission_to_reimbursement(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    emp, fin = _client(app, employee), _client(app, users_a["finance"])
    report = _auto_approved_report(db_session, emp, employee)

    before = _kpis(fin)
    assert before["Avg Cycle Time"]["value"] is None  # nothing reimbursed: "—", not 0
    assert before["Avg Cycle Time"]["unit"] == "days"
    assert "reimburse" in before["Avg Cycle Time"]["description"].lower()
    assert "Avg Time to Decision" in before

    assert fin.post(f"{API}/expense-reports/{report.id}/reimburse").status_code == 200
    db_session.refresh(report)
    # Make the timeline measurable: submitted 3 days before the reimbursement.
    report.submitted_at = report.reimbursed_at - timedelta(days=3)  # type: ignore[operator]
    db_session.flush()
    after = _kpis(fin)
    assert after["Avg Cycle Time"]["value"] == pytest.approx(3.0, abs=0.01)


def test_employee_draft_count_ignores_withdrawn_reports(
    app: Any, rbac_catalog: None, db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    emp, mgr = _client(app, employee), _client(app, manager)
    returned, _ = _returned(db_session, emp, mgr, employee, ["40.00", "710.00"])

    def drafts() -> list[dict]:
        recs = emp.get(f"{API}/analytics/overview").json()["employee"]["ai_recommendations"]
        return [r for r in recs if r["id"] == "rec_draft_reports"]

    assert drafts() and drafts()[0]["body"].startswith("1 ")
    assert emp.post(f"{API}/expense-reports/{returned.id}/withdraw").status_code == 200
    assert drafts() == []
