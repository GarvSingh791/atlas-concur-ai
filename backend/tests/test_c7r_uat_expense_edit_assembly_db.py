"""C7R UAT — fixing a returned report: entry edit + justification, report assembly.

Browser UAT found the request-changes loop impossible to finish:

  (A) the report detail had no edit control and there was no justification field,
      although the HOTEL-CAP flag says "Adjust the amount to within policy, or add
      a justification" — so 'Submit report' stayed disabled forever;
  (B) there was no way to create a report or move entries out of the auto-drafted
      'Receipt Inbox'.

Backend contract proven here (DB-backed, shared rolled-back ``db_session``):

  * ``PATCH /expenses/{id}`` accepts ``justification``; every edit re-runs the
    pre-submit check, so a JUSTIFIABLE violation (amount_cap …) becomes advisory
    (still listed, with the justification, for the approver) while hard blocks
    (missing receipt) stay blocking; the edit is audited; If-Match carries the
    parent report version (stale → 409) and the version bumps.
  * Approval-time evaluation still treats a justified violation as an exception
    (never auto-approved).
  * ``PATCH /expense-reports/{id}/details`` renames a draft (owner, draft, If-Match).
  * ``POST /expense-reports/{id}/entries/move`` moves entries between the caller's
    draft reports: totals, flags and versions of BOTH reports follow, audited;
    not-owned → 403/404, non-draft → 409, foreign entry → 404, stale → 409.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.context import AgentContext
from database.models.approval import ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import OcrStatus
from database.seeds.policy_content import RULES

pytestmark = pytest.mark.db

API = "/api/v1"
DET = "deterministic"
JUSTIFICATION = "Conference hotel; every capped property within 5 miles was sold out."


@pytest.fixture()
def hotel_policy(db_session: Session, org_a: Any) -> None:
    """The real HOTEL-CAP (amount_cap, justifiable) + RECEIPT-REQ (hard) rules."""
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    for r in RULES:
        if r["clause_ref"] not in ("HOTEL-CAP", "RECEIPT-REQ"):
            continue
        f.make_policy_rule(
            db_session,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )


def _report(db: Session, owner: User, *, title: str = "Receipt Inbox", **kw: Any) -> ExpenseReport:
    report = f.make_expense_report(db, owner=owner, title=title, **kw)
    report.currency = "USD"
    db.flush()
    return report


def _hotel_entry(
    db: Session, report: ExpenseReport, owner: User, amount: str = "600.00"
) -> Expense:
    """A $600 hotel stay backed by the owner's machine-read receipt (RECEIPT-REQ met)."""
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.lodging,
        amount=amount,
        vendor="Grand Hotel",
        expense_date=date(2026, 6, 10),
    )
    f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={"amount": float(amount), "vendor": "Grand Hotel"},
        field_provenance={"amount": DET, "vendor": DET},
    )
    report.total_amount = Decimal(amount)
    db.flush()
    return entry


def _patch_entry(client: TestClient, entry: Expense, body: dict, version: int | None = None) -> Any:
    headers = {"If-Match": str(version)} if version is not None else {}
    return client.patch(f"{API}/expenses/{entry.id}", json=body, headers=headers)


def _get_report(client: TestClient, report: ExpenseReport) -> dict:
    r = client.get(f"{API}/expense-reports/{report.id}")
    assert r.status_code == 200, r.text
    return r.json()


def _flags(report_json: dict, clause: str) -> list[dict]:
    snap = report_json["pre_submit_flags"] or {}
    return [fl for fl in snap.get("flags", []) if fl.get("source_clause_ref") == clause]


# ---------------------------------------------------------------------------
# (A) entry edit + justification → pre-submit check
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("hotel_policy")
def test_justification_turns_hotel_cap_advisory_and_back(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report = _report(db_session, owner)
    entry = _hotel_entry(db_session, report, owner)

    # An edit without a justification: HOTEL-CAP is a blocking, justifiable flag.
    r = _patch_entry(employee_client, entry, {"description": "3 nights"}, report.version)
    assert r.status_code == 200, r.text
    body = _get_report(employee_client, report)
    (cap,) = _flags(body, "HOTEL-CAP")
    assert cap["flag_type"] == "policy_violation"
    assert cap["severity"] == "blocking"
    assert cap["justifiable"] is True
    assert cap["justification"] is None
    assert cap["entry_id"] == str(entry.id)
    assert body["pre_submit_flags"]["blocking_count"] == 1
    db_session.refresh(entry)
    assert entry.status == ExpenseStatus.flagged

    # Add a justification: the flag stays visible but is advisory → submit allowed.
    r = _patch_entry(
        employee_client, entry, {"justification": f"  {JUSTIFICATION}  "}, body["version"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["justification"] == JUSTIFICATION
    body = _get_report(employee_client, report)
    (cap,) = _flags(body, "HOTEL-CAP")
    assert cap["severity"] == "advisory"
    assert cap["flag_type"] == "policy_violation"
    assert cap["justification"] == JUSTIFICATION
    assert body["pre_submit_flags"]["blocking_count"] == 0
    db_session.refresh(entry)
    assert entry.status == ExpenseStatus.draft

    # Clearing it (blank) restores the hard gate.
    r = _patch_entry(employee_client, entry, {"justification": "   "}, body["version"])
    assert r.status_code == 200, r.text
    assert r.json()["justification"] is None
    body = _get_report(employee_client, report)
    (cap,) = _flags(body, "HOTEL-CAP")
    assert cap["severity"] == "blocking"
    assert body["pre_submit_flags"]["blocking_count"] == 1


@pytest.mark.usefixtures("hotel_policy")
def test_amount_within_cap_clears_the_flag(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report = _report(db_session, owner)
    entry = _hotel_entry(db_session, report, owner)
    r = _patch_entry(employee_client, entry, {"amount": "150.00"}, report.version)
    assert r.status_code == 200, r.text
    body = _get_report(employee_client, report)
    assert _flags(body, "HOTEL-CAP") == []
    assert body["pre_submit_flags"]["blocking_count"] == 0
    assert Decimal(body["total_amount"]) == Decimal("150.00")


@pytest.mark.usefixtures("hotel_policy")
def test_justification_never_clears_a_missing_receipt(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """Hard block: a justification is not evidence — RECEIPT-REQ stays blocking."""
    owner = users_a["employee"]
    report = _report(db_session, owner)
    entry = f.make_expense(
        db_session,
        report=report,
        category=ExpenseCategory.meals,
        amount="80.00",
        vendor="Bistro",
        expense_date=date(2026, 6, 10),
    )
    r = _patch_entry(employee_client, entry, {"justification": "Lost the receipt."})
    assert r.status_code == 200, r.text
    body = _get_report(employee_client, report)
    (missing,) = _flags(body, "RECEIPT-REQ")
    assert missing["flag_type"] == "missing_receipt"
    assert missing["severity"] == "blocking"
    assert missing["justifiable"] is False
    assert missing["justification"] is None
    db_session.refresh(entry)
    assert entry.status == ExpenseStatus.flagged


@pytest.mark.usefixtures("hotel_policy")
def test_justified_violation_is_still_an_exception_at_approval_time(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report = _report(db_session, owner)
    entry = _hotel_entry(db_session, report, owner)
    r = _patch_entry(employee_client, entry, {"justification": JUSTIFICATION})
    assert r.status_code == 200, r.text
    db_session.refresh(report)

    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=users_a["manager"].id,
    )
    risk = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db_session)
    assert risk.subject_in_policy is False
    assert risk.blocking_flag_count >= 1
    justified = [fl for fl in risk.flags_snapshot if fl.get("justification") == JUSTIFICATION]
    assert justified, risk.flags_snapshot


def test_entry_edit_is_audited_and_version_guarded(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    report = _report(db_session, owner, title="June")
    entry = f.make_expense(db_session, report=report, amount="20.00", vendor="Cafe")
    version = report.version

    stale = _patch_entry(employee_client, entry, {"amount": "25.00"}, version - 1)
    assert stale.status_code == 409, stale.text
    db_session.refresh(entry)
    assert entry.amount == Decimal("20.00")

    ok = _patch_entry(
        employee_client,
        entry,
        {"amount": "25.00", "vendor": "Cafe Roma", "justification": "Team lunch"},
        version,
    )
    assert ok.status_code == 200, ok.text
    db_session.refresh(report)
    assert report.version == version + 1
    assert report.total_amount == Decimal("25.00")

    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == entry.id, AuditLog.action == "expense.updated"
        )
    )
    assert audit is not None
    assert (audit.before_state or {})["justification"] is None
    assert (audit.after_state or {})["justification"] == "Team lunch"
    assert (audit.after_state or {})["vendor"] == "Cafe Roma"


@pytest.mark.usefixtures("hotel_policy")
def test_refresh_failure_keeps_prior_flags_and_says_so(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the engine cannot re-evaluate, the edit still lands; the prior flags are
    kept (a blocking flag is never silently dropped) plus an advisory note."""
    from backend.app.modules.expense import service as expense_service

    owner = users_a["employee"]
    report = _report(db_session, owner)
    entry = _hotel_entry(db_session, report, owner)
    assert _patch_entry(employee_client, entry, {"description": "x"}).status_code == 200

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("policy engine down")

    monkeypatch.setattr(expense_service, "run_presubmit", boom)
    for _ in range(2):  # the note is not duplicated
        r = _patch_entry(employee_client, entry, {"justification": JUSTIFICATION})
        assert r.status_code == 200, r.text
    body = _get_report(employee_client, report)
    (cap,) = _flags(body, "HOTEL-CAP")
    assert cap["severity"] == "blocking"
    notes = [
        fl for fl in body["pre_submit_flags"]["flags"] if fl.get("entry_ref") == "presubmit_refresh"
    ]
    assert len(notes) == 1 and notes[0]["severity"] == "advisory"
    assert body["pre_submit_flags"]["blocking_count"] == 1


def test_entry_edit_rejects_null_for_required_fields(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"], title="June")
    entry = f.make_expense(db_session, report=report, amount="20.00")
    for body in ({"category": None}, {"amount": None}, {"amount": "-1"}):
        r = _patch_entry(employee_client, entry, body)
        assert r.status_code == 422, (body, r.text)
    long = _patch_entry(employee_client, entry, {"justification": "x" * 2001})
    assert long.status_code == 422


def test_non_owner_cannot_edit_or_justify(
    manager_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"], title="June")
    entry = f.make_expense(db_session, report=report, amount="20.00")
    r = _patch_entry(manager_client, entry, {"justification": "approved by me"})
    assert r.status_code == 403, r.text
    db_session.refresh(entry)
    assert entry.justification is None


# ---------------------------------------------------------------------------
# (B) report assembly: rename + move
# ---------------------------------------------------------------------------


def test_rename_draft_report(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = _report(db_session, users_a["employee"])
    version = report.version

    stale = employee_client.patch(
        f"{API}/expense-reports/{report.id}/details",
        json={"title": "Chicago conference"},
        headers={"If-Match": str(version + 5)},
    )
    assert stale.status_code == 409, stale.text

    blank = employee_client.patch(
        f"{API}/expense-reports/{report.id}/details", json={"title": "   "}
    )
    assert blank.status_code == 422, blank.text

    r = employee_client.patch(
        f"{API}/expense-reports/{report.id}/details",
        json={"title": "  Chicago conference "},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Chicago conference"
    assert r.json()["version"] == version + 1
    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == report.id, AuditLog.action == "expense_report.updated"
        )
    )
    assert audit is not None
    assert (audit.before_state or {})["title"] == "Receipt Inbox"
    assert (audit.after_state or {})["title"] == "Chicago conference"


def test_rename_requires_owned_draft(
    employee_client: TestClient,
    manager_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    submitted = _report(
        db_session, users_a["employee"], title="Sent", status=ExpenseReportStatus.submitted
    )
    r = employee_client.patch(
        f"{API}/expense-reports/{submitted.id}/details", json={"title": "New"}
    )
    assert r.status_code == 409, r.text

    draft = _report(db_session, users_a["employee"], title="Mine")
    r = manager_client.patch(f"{API}/expense-reports/{draft.id}/details", json={"title": "New"})
    assert r.status_code == 403, r.text
    r = employee_client.patch(
        f"{API}/expense-reports/{uuid.uuid4()}/details", json={"title": "New"}
    )
    assert r.status_code == 404, r.text


@pytest.mark.usefixtures("hotel_policy")
def test_create_report_and_move_entries_out_of_the_inbox(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    owner = users_a["employee"]
    inbox = _report(db_session, owner)
    hotel = _hotel_entry(db_session, inbox, owner)
    coffee = f.make_expense(
        db_session, report=inbox, amount="12.00", vendor="Cafe", expense_date=date(2026, 6, 11)
    )
    inbox.total_amount = Decimal("612.00")
    db_session.flush()
    inbox_version = inbox.version

    created = employee_client.post(
        f"{API}/expense-reports", json={"title": "Chicago conference", "currency": "USD"}
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]
    target_version = created.json()["version"]

    r = employee_client.post(
        f"{API}/expense-reports/{inbox.id}/entries/move",
        json={"entry_ids": [str(hotel.id)], "target_report_id": target_id},
        headers={"If-Match": str(inbox_version)},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["moved_entry_ids"] == [str(hotel.id)]
    assert Decimal(out["source"]["total_amount"]) == Decimal("12.00")
    assert Decimal(out["target"]["total_amount"]) == Decimal("600.00")
    assert out["source"]["version"] == inbox_version + 1
    assert out["target"]["version"] == target_version + 1
    # The HOTEL-CAP flag moved with its entry.
    assert _flags(out["source"], "HOTEL-CAP") == []
    (cap,) = _flags(out["target"], "HOTEL-CAP")
    assert cap["entry_id"] == str(hotel.id)

    db_session.refresh(hotel)
    db_session.refresh(coffee)
    assert str(hotel.report_id) == target_id
    assert coffee.report_id == inbox.id
    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == hotel.id, AuditLog.action == "expense.moved"
        )
    )
    assert audit is not None
    assert (audit.before_state or {})["report_id"] == str(inbox.id)
    assert (audit.after_state or {})["report_id"] == target_id
    # Audited after the re-evaluation: after_state carries the entry's status on
    # its new report (HOTEL-CAP over cap, unjustified → flagged).
    assert (audit.after_state or {})["status"] == ExpenseStatus.flagged.value

    # The receipt stays linked to the moved entry.
    entries = employee_client.get(f"{API}/expense-reports/{target_id}/entries").json()
    assert [e["id"] for e in entries] == [str(hotel.id)]
    assert entries[0]["receipt_id"] is not None


def test_edit_racing_a_move_never_touches_an_entry_now_on_a_submitted_report(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C7R review TOCTOU: PATCH reads the entry (unlocked), then a concurrent
    move re-parents it to report B and B is submitted, then PATCH locks the OLD
    parent A (still draft).  The edit must be refused (409) — never written to an
    entry that now belongs to a submitted report — and A must be untouched."""
    from backend.app.modules.expense import service as expense_service

    owner = users_a["employee"]
    report_a = _report(db_session, owner, title="A")
    report_b = _report(db_session, owner, title="B")
    entry = f.make_expense(db_session, report=report_a, amount="100.00", vendor="Cafe")
    report_a.total_amount = Decimal("100.00")
    db_session.flush()
    version_a = report_a.version
    total_a = report_a.total_amount

    real_require_owner = expense_service.require_owner
    fired: list[bool] = []

    def move_then_submit_concurrently(*args: Any, **kwargs: Any) -> Any:
        # Runs after the unlocked entry read and the parent (A) lock.  Simulate
        # the other transaction's committed effects WITHOUT syncing the ORM
        # identity map — exactly what the stale in-memory entry looks like.
        if not fired:
            fired.append(True)
            db_session.execute(
                sa.update(Expense)
                .where(Expense.id == entry.id)
                .values(report_id=report_b.id)
                .execution_options(synchronize_session=False)
            )
            db_session.execute(
                sa.update(ExpenseReport)
                .where(ExpenseReport.id == report_b.id)
                .values(status=ExpenseReportStatus.submitted, total_amount=Decimal("100.00"))
                .execution_options(synchronize_session=False)
            )
        return real_require_owner(*args, **kwargs)

    monkeypatch.setattr(expense_service, "require_owner", move_then_submit_concurrently)

    r = _patch_entry(employee_client, entry, {"amount": "900.00"}, version_a)
    assert fired, "the interleaving hook did not run"
    assert r.status_code == 409, r.text

    db_session.expire_all()
    moved = db_session.get(Expense, entry.id)
    assert moved is not None
    assert moved.report_id == report_b.id
    assert moved.amount == Decimal("100.00")  # submitted report's entry unchanged
    a = db_session.get(ExpenseReport, report_a.id)
    assert a is not None
    assert a.version == version_a
    assert a.total_amount == total_a
    assert (
        db_session.scalar(
            sa.select(AuditLog).where(
                AuditLog.entity_id == entry.id, AuditLog.action == "expense.updated"
            )
        )
        is None
    )


def test_move_guards(
    employee_client: TestClient,
    manager_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    source = _report(db_session, employee, title="Inbox")
    entry = f.make_expense(db_session, report=source, amount="10.00")
    other_draft = _report(db_session, employee, title="Other")
    stray = f.make_expense(db_session, report=other_draft, amount="5.00")
    submitted = _report(db_session, employee, title="Sent", status=ExpenseReportStatus.submitted)
    managers_draft = _report(db_session, manager, title="Manager's own")

    def move(client: TestClient, src: Any, ids: list, target: Any, version: int | None = None):
        headers = {"If-Match": str(version)} if version is not None else {}
        return client.post(
            f"{API}/expense-reports/{src.id}/entries/move",
            json={"entry_ids": [str(i.id) for i in ids], "target_report_id": str(target.id)},
            headers=headers,
        )

    # Same report → 400; target not draft → 409; stale source version → 409.
    assert move(employee_client, source, [entry], source).status_code == 400
    assert move(employee_client, source, [entry], submitted).status_code == 409
    assert move(employee_client, submitted, [entry], source).status_code == 409
    assert (
        move(employee_client, source, [entry], other_draft, source.version + 3).status_code == 409
    )
    # An entry that is not on the source report → 404 (not disclosed).
    assert move(employee_client, source, [stray], other_draft).status_code == 404
    # Another user's report: unreadable → 404; readable but not owned → 403.
    assert move(employee_client, source, [entry], managers_draft).status_code == 404
    assert move(manager_client, source, [entry], managers_draft).status_code == 403
    # Empty selection → 422.
    r = employee_client.post(
        f"{API}/expense-reports/{source.id}/entries/move",
        json={"entry_ids": [], "target_report_id": str(other_draft.id)},
    )
    assert r.status_code == 422

    db_session.refresh(entry)
    assert entry.report_id == source.id
    assert (
        db_session.scalar(
            sa.select(sa.func.count(AuditLog.id)).where(AuditLog.action == "expense.moved")
        )
        == 0
    )
