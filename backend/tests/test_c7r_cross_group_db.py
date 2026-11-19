"""C7R cross-group review — DB-backed regressions for defects found between groups.

Real router + receipt pipeline (stub OCR / LLM / storage), real orchestrator with
the deterministic policy handler, real seeded rule definitions.

  X-HIGH   an amount the gap-fill LLM INFERRED (it never sees the image) is never
           drafted into an expense nor reported "in policy"; entering the total
           creates the deferred draft.
  X-MED-1  that deferred draft (a total the claimant TYPED) reports a blocking
           ``missing_receipt`` — the same verdict /policy/evaluate and approval
           time give — and its inbox snapshot carries the flag.
  X-MED-2  /policy/evaluate names WHY an attached receipt does not satisfy
           RECEIPT-REQ (approval-time wording), not "no receipt attached".
  X-LOW-1  every persisted entry's flag carries its entry_id from the handler, so
           colliding labels can never mis-attribute a flag.
  X-LOW-2  a second claim for the same purchase (same owner, vendor, date, amount)
           is a blocking ``possible_duplicate`` — never auto-approved.
  X-LOW-3  a Receipt Inbox that went through an approval (returned for changes) is
           not reused for new uploads.

Regression proof (old behaviour, each verified failing): see the module's
companion notes in the C7R phase log; summarised per test docstring.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents.approval_agent.evaluate import assess_risk
from ai_agents.agents.approval_agent.routing import decide_route
from ai_agents.context import AgentContext
from backend.app.modules.ai.runtime import get_orchestrator
from backend.tests.receipt_images import scenario_of, scenario_upload
from database.models.approval import ApprovalStatus, ApprovalSubjectType
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import OcrStatus, Receipt
from database.seeds.policy_content import RULES
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

API = "/api/v1"
DET = "deterministic"

_CONF = {
    "amount": 0.95,
    "currency": 0.99,
    "date": 0.95,
    "vendor": 0.93,
    "location": 0.9,
    "expense_type": 0.92,
}
_BASE: dict[str, Any] = {
    "currency": "USD",
    "date": "2026-06-11",
    "vendor": "Corner Deli",
    "location": "New York, NY",
    "expense_type": "meals",
}
_SCENARIOS: dict[str, tuple[dict[str, Any], dict[str, float]]] = {
    # Blurry receipt, NO total / subtotal / tax / legibility signal: the amount is
    # routed to gap-fill (not withheld) and the LLM "infers" it.
    "unread_total": ({**_BASE, "amount": None}, {**_CONF, "amount": 0.0}),
    "clean": ({**_BASE, "amount": 18.73, "total_legible": True}, _CONF),
}


class _ScenarioOcr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        fields, conf = _SCENARIOS[key]
        return OcrResult(fields=dict(fields), field_confidence=dict(conf))


class _GuessingLlm(LlmProvider):
    """Gap-fill stub that invents a total (it never sees the image)."""

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        return LlmResponse(
            text=json.dumps({"amount": 60.0}),
            model="stub",
            usage=LlmUsage(input_tokens=1, output_tokens=1),
            tool_calls=[],
        )

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


class _MemStorage(StorageProvider):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, *, content_type: str = "") -> None:  # type: ignore[override]
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"https://example.invalid/{key}", expires_in=expires_in)


@pytest.fixture()
def real_orchestrator(app: Any, local_ai: None) -> Iterator[None]:
    app.dependency_overrides.pop(get_orchestrator, None)
    yield


@pytest.fixture()
def providers(app: Any, real_orchestrator: None) -> Iterator[None]:
    """Stub OCR / storage / gap-fill LLM for the upload path — installed AFTER
    ``local_ai`` (via ``real_orchestrator``), which would otherwise replace the
    gap-fill LLM override with the local stub."""
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _ScenarioOcr()
    app.dependency_overrides[get_llm_provider] = lambda: _GuessingLlm()
    yield


@pytest.fixture()
def seeded_policy(db_session: Session, org_a: Any) -> None:
    policy = f.make_policy(db_session, org=org_a, is_active=True)
    for r in RULES:
        f.make_policy_rule(
            db_session,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )


def _upload(client: TestClient, scenario: str) -> dict[str, Any]:
    r = client.post("/api/v1/receipts", files={"file": scenario_upload(scenario)})
    assert r.status_code == 201, r.text
    return r.json()


def _evaluate(client: TestClient, report_id: Any) -> dict[str, Any]:
    r = client.post(
        f"{API}/policy/evaluate",
        json={"subject_type": "expense_report", "subject_id": str(report_id)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _assess(db: Session, report: ExpenseReport, actor: User) -> tuple[list[dict], str]:
    ctx = AgentContext(
        organization_id=report.organization_id,
        granted_permission_keys=frozenset(),
        user_id=actor.id,
    )
    a = assess_risk(report, ApprovalSubjectType.expense_report, context=ctx, session=db)
    return list(a.flags_snapshot), decide_route(a)


# ---------------------------------------------------------------------------
# X-HIGH + X-MED-1 + X-MED-2: inferred total, deferred draft, consistent verdicts
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("providers", "seeded_policy")
def test_inferred_total_is_never_drafted_and_a_typed_total_is_not_receipt_evidence(
    employee_client: TestClient, manager_client: TestClient, db_session: Session
) -> None:
    """Old behaviour: the upload drafted a 60.00 entry from the LLM's guess and
    returned policy_verdict in_policy=true; the PATCH-created draft returned
    in_policy=true / flag_types=[] with an empty inbox snapshot; /policy/evaluate
    said "no receipt attached" for the attached receipt."""
    body = _upload(employee_client, "unread_total")
    # The guess is kept on the receipt as a suggestion (M-A1), but never drafted.
    assert body["field_provenance"]["amount"] == "ai_inferred"
    assert body["expense_id"] is None
    assert body["policy_verdict"] is None
    assert body["needs_review"] is True
    assert (
        "Amount was not read with confidence — enter the receipt total" in (body["review_reasons"])
    )

    patch = employee_client.patch(
        f"{API}/receipts/{body['id']}", json={"field": "amount", "value": 60.0}
    )
    assert patch.status_code == 200, patch.text
    out = patch.json()
    assert out["linked_expense_sync"] == "drafted"
    verdict = out["policy_verdict"]
    assert verdict is not None
    assert "missing_receipt" in verdict["flag_types"], verdict
    assert verdict["in_policy"] is False
    # UAT2: an attached receipt whose total is typed is ADVISORY for submit (not a
    # dead-end) — but it still escalates to a human (asserted below).
    assert verdict["advisory_count"] >= 1

    expense = db_session.get(Expense, out["expense_id"])
    assert expense is not None
    report = db_session.get(ExpenseReport, expense.report_id)
    assert report is not None
    snapshot_flags = (report.pre_submit_flags or {}).get("flags") or []
    missing = [fl for fl in snapshot_flags if fl.get("flag_type") == "missing_receipt"]
    assert missing, snapshot_flags
    assert missing[0]["entry_id"] == str(expense.id)
    assert "no machine-read amount" in missing[0]["reason"]
    assert "could not be verified" in missing[0]["reason"]
    assert missing[0]["severity"] == "advisory"

    # /policy/evaluate and approval time agree, with the precise reason (X-MED-2).
    ev = _evaluate(employee_client, report.id)
    ev_missing = [fl for fl in ev["flags"] if fl["flag_type"] == "missing_receipt"]
    assert ev_missing, ev["flags"]
    assert "no machine-read amount" in ev_missing[0]["reason"]
    assert "no receipt attached" not in ev_missing[0]["reason"]
    assert ev_missing[0]["entry_id"] == str(expense.id)
    flags, route = _assess(db_session, report, _manager_of(db_session, report))
    assert route == "escalate"
    approval_missing = [fl for fl in flags if fl.get("flag_type") == "missing_receipt"]
    assert approval_missing and approval_missing[0]["reason"] == ev_missing[0]["reason"]


def _manager_of(db: Session, report: ExpenseReport) -> User:
    owner = db.get(User, report.owner_id)
    assert owner is not None and owner.manager_id is not None
    manager = db.get(User, owner.manager_id)
    assert manager is not None
    return manager


@pytest.mark.usefixtures("seeded_policy", "real_orchestrator")
def test_policy_evaluate_names_a_receipt_that_does_not_cover_the_claim(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old behaviour: "Receipt required for Corner Deli but no receipt attached"."""
    emp = users_a["employee"]
    report = f.make_expense_report(db_session, owner=emp)
    report.total_amount = Decimal("320.00")
    entry = f.make_expense(
        db_session,
        report=report,
        category=ExpenseCategory.meals,
        amount="320.00",
        vendor="Corner Deli",
        expense_date=date(2026, 6, 10),
    )
    f.make_receipt(
        db_session,
        uploaded_by=emp,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={"vendor": "Corner Deli", "amount": 30.0, "date": "2026-06-10"},
        field_provenance={"vendor": DET, "amount": DET, "date": DET},
    )
    db_session.flush()

    ev = _evaluate(employee_client, report.id)
    missing = [fl for fl in ev["flags"] if fl["flag_type"] == "missing_receipt"]
    assert missing, ev["flags"]
    assert "does not cover the claimed amount" in missing[0]["reason"]
    assert "no receipt attached" not in missing[0]["reason"]

    flags, _route = _assess(db_session, report, users_a["manager"])
    approval_missing = [fl for fl in flags if fl.get("flag_type") == "missing_receipt"]
    assert approval_missing[0]["reason"] == missing[0]["reason"]


# ---------------------------------------------------------------------------
# X-LOW-2: duplicate claims
# ---------------------------------------------------------------------------


def _united_report(
    db: Session, owner: User, *, status: ExpenseReportStatus, title: str
) -> tuple[ExpenseReport, Expense]:
    report = f.make_expense_report(db, owner=owner, title=title, status=status)
    report.total_amount = Decimal("320.00")
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.airfare,
        amount="320.00",
        vendor="United Airlines",
        expense_date=date(2026, 6, 10),
    )
    f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={"vendor": "UNITED", "amount": 320.0, "date": "2026-06-10"},
        field_provenance={"vendor": DET, "amount": DET, "date": DET},
    )
    db.flush()
    return report, entry


@pytest.mark.usefixtures("seeded_policy", "real_orchestrator")
@pytest.mark.parametrize(
    ("first_status", "duplicate"),
    [
        (ExpenseReportStatus.approved, True),
        (ExpenseReportStatus.submitted, True),
        (ExpenseReportStatus.draft, True),
        # A rejected claim may be re-filed (no pre-submit duplicate block); the
        # approval-time re-file check (approval_agent.evaluate) escalates it.
        (ExpenseReportStatus.rejected, False),
    ],
)
def test_same_purchase_claimed_twice_is_flagged_and_escalated(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    first_status: ExpenseReportStatus,
    duplicate: bool,
) -> None:
    """Old behaviour: the second (receipt-corroborated) claim was in policy and
    auto-approved — nothing detected duplicate claims."""
    emp = users_a["employee"]
    _united_report(db_session, emp, status=first_status, title="Q2 Summit")
    second, entry = _united_report(
        db_session, emp, status=ExpenseReportStatus.submitted, title="Q2 Summit (again)"
    )

    flags, route = _assess(db_session, second, users_a["manager"])
    dup = [fl for fl in flags if fl.get("flag_type") == "possible_duplicate"]
    ev = _evaluate(employee_client, second.id)
    ev_dup = [fl for fl in ev["flags"] if fl["flag_type"] == "possible_duplicate"]
    if duplicate:
        assert dup and dup[0]["severity"] == "blocking", flags
        assert "'Q2 Summit'" in dup[0]["reason"]
        assert str(entry.id) not in dup[0]["reason"]  # never an internal id in text
        assert route == "escalate"
        assert ev_dup and ev_dup[0]["entry_id"] == str(entry.id)
        assert ev["verdict"] == "violation"
    elif first_status == ExpenseReportStatus.rejected:
        # No pre-submit block (the claimant may re-file a rejected claim), but the
        # approval-time re-file check routes it to a human (C7R SoD).
        assert not dup and not ev_dup
        assert route == "escalate"
        refiled = [fl for fl in flags if fl.get("flag_type") == "refiled_rejected_claim"]
        assert refiled and "'Q2 Summit'" in refiled[0]["reason"], flags
    else:
        assert not dup and not ev_dup
        assert route == "auto_approve"


def _airfare_line(
    db: Session,
    report: ExpenseReport,
    owner: User,
    *,
    vendor: str | None,
    receipt_vendor: str = "UNITED",
) -> Expense:
    entry = f.make_expense(
        db,
        report=report,
        category=ExpenseCategory.airfare,
        amount="320.00",
        vendor=vendor,
        expense_date=date(2026, 6, 10),
    )
    f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        extracted_fields={"vendor": receipt_vendor, "amount": 320.0, "date": "2026-06-10"},
        field_provenance={"vendor": DET, "amount": DET, "date": DET},
    )
    db.flush()
    return entry


@pytest.mark.usefixtures("seeded_policy", "real_orchestrator")
@pytest.mark.parametrize("second_vendor", [None, "", "United Airlines Inc", "  UNITED AIRLINES "])
def test_duplicate_is_caught_with_a_blank_or_respelled_vendor(
    db_session: Session, users_a: dict[str, User], second_vendor: str | None
) -> None:
    """C7R review: blanking the vendor (PATCH vendor=null — the receipt's OCR vendor
    still verifies the supplier for policy) or re-spelling it ('United Airlines
    Inc') skipped the exact lower(trim(vendor)) duplicate check, so the same
    purchase auto-approved twice.  Old behaviour: route 'auto_approve', no flag."""
    emp = users_a["employee"]
    _united_report(db_session, emp, status=ExpenseReportStatus.approved, title="Q2 Summit")
    second = f.make_expense_report(
        db_session, owner=emp, title="Q2 again", status=ExpenseReportStatus.submitted
    )
    second.total_amount = Decimal("320.00")
    _airfare_line(db_session, second, emp, vendor=second_vendor)

    flags, route = _assess(db_session, second, users_a["manager"])
    dup = [fl for fl in flags if fl.get("flag_type") == "possible_duplicate"]
    assert dup and dup[0]["severity"] == "blocking", flags
    assert "'Q2 Summit'" in dup[0]["reason"]
    assert route == "escalate"


@pytest.mark.usefixtures("seeded_policy", "real_orchestrator")
def test_two_identical_lines_on_one_report_are_flagged(
    db_session: Session, users_a: dict[str, User]
) -> None:
    """C7R review: lines on ONE report were never compared, but an auto-approved
    report (< $500) is never seen by an approver, so the same receipt uploaded
    twice into one inbox auto-approved as two claims.  Old: 'auto_approve'."""
    emp = users_a["employee"]
    report = f.make_expense_report(
        db_session, owner=emp, title="Inbox", status=ExpenseReportStatus.submitted
    )
    report.total_amount = Decimal("640.00")
    a = _airfare_line(db_session, report, emp, vendor="United Airlines")
    b = _airfare_line(db_session, report, emp, vendor=None)

    flags, route = _assess(db_session, report, users_a["manager"])
    dup = [fl for fl in flags if fl.get("flag_type") == "possible_duplicate"]
    assert {fl["entry_id"] for fl in dup} == {str(a.id), str(b.id)}, flags
    assert all("this report" in fl["reason"] for fl in dup)
    assert route == "escalate"


@pytest.mark.usefixtures("seeded_policy", "real_orchestrator")
def test_same_date_and_amount_from_another_supplier_is_not_a_duplicate(
    db_session: Session, users_a: dict[str, User]
) -> None:
    """Control: a different supplier (claim AND receipt) on the same date and
    amount is a different purchase — no duplicate flag."""
    emp = users_a["employee"]
    _united_report(db_session, emp, status=ExpenseReportStatus.approved, title="Q2 Summit")
    second = f.make_expense_report(
        db_session, owner=emp, title="Delta trip", status=ExpenseReportStatus.submitted
    )
    second.total_amount = Decimal("320.00")
    _airfare_line(db_session, second, emp, vendor="Delta Air Lines", receipt_vendor="DELTA")

    flags, _route = _assess(db_session, second, users_a["manager"])
    assert not [fl for fl in flags if fl.get("flag_type") == "possible_duplicate"], flags


# ---------------------------------------------------------------------------
# X-LOW-3: a returned Receipt Inbox is not reused
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("providers")
def test_upload_never_lands_on_a_returned_receipt_inbox(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old behaviour: the upload picked the returned (changes_requested) inbox."""
    emp = users_a["employee"]
    returned = f.make_expense_report(db_session, owner=emp, title="Receipt Inbox")
    f.make_approval(
        db_session,
        submitter=emp,
        subject_id=returned.id,
        current_status=ApprovalStatus.changes_requested,
    )
    db_session.flush()

    body = _upload(employee_client, "clean")
    assert body["expense_id"] is not None
    expense = db_session.get(Expense, body["expense_id"])
    assert expense is not None
    assert expense.report_id != returned.id
    inboxes = db_session.scalars(
        sa.select(ExpenseReport).where(
            ExpenseReport.owner_id == emp.id, ExpenseReport.title == "Receipt Inbox"
        )
    ).all()
    assert len(inboxes) == 2

    # A second upload reuses the NEW (unreviewed) inbox, not the returned one.
    body2 = _upload(employee_client, "clean")
    expense2 = db_session.get(Expense, body2["expense_id"])
    assert expense2 is not None and expense2.report_id == expense.report_id
    assert db_session.scalar(
        sa.select(sa.func.count(Receipt.id)).where(Receipt.expense_id == expense2.id)
    )
