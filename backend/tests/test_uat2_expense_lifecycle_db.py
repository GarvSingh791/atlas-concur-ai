"""UAT2 Issues 1+2 — attach a receipt to an entry; delete entries / receipts / drafts.

Old behaviour (UAT round 2):
  * a returned report could never be resubmitted: the UI had no way to attach a
    receipt, and the backend attach path (POST /receipts?expense_id=) neither
    re-ran the pre-submit check nor bumped the version, and checked "draft"
    without a lock before a long OCR call;
  * duplicates deadlocked reports: there was no DELETE anywhere, and a duplicate
    flag on report B kept naming a line of report A after A's line changed;
  * a returned report showed the seeded / submit-time verdict, not the engine's.

Governance asserted here: deletes are owner-only and draft-only; a report with
approval history is never deleted; a reviewed entry is deletable only while its
return froze a snapshot (legacy NULL snapshot → 409); receipts that are or were
evidence on a reviewed report are kept for audit (never deleted / reused); one
receipt backs one claim, a re-uploaded copy of a receipt backing another claim is
a blocking duplicate, a receipt dated far from the claim and an unverifiable
receipt are advisory but escalate (never auto-approved).

DB-backed (shared conftest: rolled-back session, real-JWT clients).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
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
from ai_agents.receipt_pipeline.inbox import editable_inbox_expense_ids
from ai_agents.receipt_pipeline.retention import RECEIPT_RETAINED_ACTION
from backend.app.core.security import create_access_token
from backend.app.modules.expense import service as expense_service
from backend.tests.receipt_images import scenario_of, scenario_upload
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision
from database.models.audit_log import AuditLog
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import OcrStatus, Receipt
from database.seeds.policy_content import RULES
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

API = "/api/v1"
DET = "deterministic"
DAY = date(2026, 6, 10)

_CONF = {
    "amount": 0.95,
    "currency": 0.99,
    "date": 0.95,
    "vendor": 0.93,
    "location": 0.9,
    "expense_type": 0.92,
}
_MEAL = {
    "currency": "USD",
    "date": "2026-06-10",
    "vendor": "Corner Deli",
    "location": "New York, NY",
    "expense_type": "meals",
}
_SCENARIOS: dict[str, dict[str, Any]] = {
    "meal60": {**_MEAL, "amount": 60.0, "total_legible": True},
    "coffee": {**_MEAL, "amount": 3.0, "total_legible": True},
    "lodging750": {
        **_MEAL,
        "amount": 750.0,
        "vendor": "Hilton Chicago",
        "location": "Chicago, IL",
        "expense_type": "hotel",
        "total_legible": True,
    },
}


class _Ocr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        return OcrResult(fields=dict(_SCENARIOS[key]), field_confidence=dict(_CONF))


class _MemStorage(StorageProvider):
    def __init__(self, *, fail_delete: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_delete = fail_delete

    def put_object(self, key: str, data: bytes, *, content_type: str = "") -> None:  # type: ignore[override]
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("bucket unavailable")
        self.deleted.append(key)
        self.objects.pop(key, None)

    def generate_presigned_url(self, key: str, *, expires_in: int = 300) -> PresignedUrl:
        return PresignedUrl(url=f"https://example.invalid/{key}", expires_in=expires_in)


@pytest.fixture()
def storage(app: Any) -> Iterator[_MemStorage]:
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _Ocr()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    yield mem


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


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _report(
    db: Session,
    owner: User,
    lines: list[tuple[str, str | None, date]] | None = None,
    *,
    title: str = "Trip",
    category: ExpenseCategory = ExpenseCategory.meals,
    status: ExpenseReportStatus = ExpenseReportStatus.draft,
) -> tuple[ExpenseReport, list[Expense]]:
    report = f.make_expense_report(db, owner=owner, title=title, status=status)
    entries = [
        f.make_expense(
            db,
            report=report,
            category=category,
            amount=amount,
            vendor=vendor,
            expense_date=day,
            match_source=ExpenseMatchSource.manual,
        )
        for amount, vendor, day in (lines if lines is not None else [("60.00", "Corner Deli", DAY)])
    ]
    report.total_amount = sum((Decimal(str(e.amount)) for e in entries), Decimal("0"))
    db.flush()
    return report, entries


def _receipt(
    db: Session,
    owner: User,
    entry: Expense | None = None,
    *,
    amount: float | None = 60.0,
    day: str = "2026-06-10",
    vendor: str = "Corner Deli",
    amount_provenance: str = DET,
    storage: _MemStorage | None = None,
    status: OcrStatus = OcrStatus.processed,
) -> Receipt:
    key = f"{owner.organization_id}/{datetime.now(timezone.utc).timestamp()}-{vendor}-{amount}"
    receipt = f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id if entry is not None else None,
        ocr_status=status,
        image_url=key,
        extracted_fields={"vendor": vendor, "amount": amount, "date": day, "currency": "USD"},
        field_provenance={"vendor": DET, "amount": amount_provenance, "date": DET},
    )
    if storage is not None:
        storage.objects[key] = b"img"
    return receipt


def _flags(db: Session, report: ExpenseReport) -> list[dict]:
    db.expire_all()
    fresh = db.get(ExpenseReport, report.id)
    assert fresh is not None
    return [
        fl for fl in ((fresh.pre_submit_flags or {}).get("flags") or []) if isinstance(fl, dict)
    ]


def _of_type(flags: list[dict], flag_type: str, entry: Expense | None = None) -> list[dict]:
    return [
        fl
        for fl in flags
        if fl.get("flag_type") == flag_type
        and (entry is None or fl.get("entry_id") == str(entry.id))
    ]


def _version(db: Session, report: ExpenseReport) -> int:
    db.expire_all()
    fresh = db.get(ExpenseReport, report.id)
    assert fresh is not None
    return int(fresh.version)


def _audits(db: Session, action: str, entity_id: Any) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(AuditLog.action == action, AuditLog.entity_id == entity_id)
        ).all()
    )


def _submit(client: TestClient, report: ExpenseReport) -> dict:
    r = client.patch(f"{API}/expense-reports/{report.id}", json={"status": "submitted"})
    assert r.status_code == 200, r.text
    return r.json()


def _approval(db: Session, report: ExpenseReport) -> Approval:
    approval = db.scalar(sa.select(Approval).where(Approval.subject_id == report.id))
    assert approval is not None
    return approval


def _decide(client: TestClient, approval: Approval, decision: str) -> None:
    r = client.patch(
        f"{API}/approvals/{approval.id}",
        json={"decision": decision, "reason": "Attach the receipts, please."},
    )
    assert r.status_code == 200, r.text


def _returned(db: Session, app: Any, users: dict[str, User], report: ExpenseReport) -> None:
    """Submit (escalated: no receipts) then the manager requests changes."""
    _submit(_client(app, users["employee"]), report)
    _decide(_client(app, users["manager"]), _approval(db, report), "request_changes")
    db.expire_all()


def _upload_to(
    client: TestClient, entry: Expense, scenario: str, *, version: int | None = None
) -> Any:
    headers = {"If-Match": str(version)} if version is not None else {}
    return client.post(
        f"{API}/receipts",
        params={"expense_id": str(entry.id)},
        files={"file": scenario_upload(scenario)},
        headers=headers,
    )


def _assess(db: Session, report: ExpenseReport, actor: User) -> tuple[list[dict], str]:
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


# ---------------------------------------------------------------------------
# Issue 1 — attach a receipt
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_attach_covering_receipt_to_returned_report_entry_clears_receipt_req(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: the UI could not attach; the API linked without re-checking, so the
    stored RECEIPT-REQ stayed blocking and the returned report was stuck."""
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    _returned(db_session, app, users_a, report)
    # M1: returning the report re-ran the engine — RECEIPT-REQ is visible now.
    assert _of_type(_flags(db_session, report), "missing_receipt", entry)
    before = _version(db_session, report)

    resp = _upload_to(_client(app, emp), entry, "meal60", version=before)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["expense_id"] == str(entry.id)
    assert body["linked_expense_sync"] == "attached"
    assert body["policy_verdict"]["in_policy"] is True, body["policy_verdict"]
    assert not _of_type(_flags(db_session, report), "missing_receipt", entry)
    assert _version(db_session, report) == before + 1
    (audit,) = _audits(db_session, "expense.receipt_attached", entry.id)
    assert audit.after_state["receipt_id"] == body["id"]  # type: ignore[index]
    assert audit.after_state["source"] == "upload"  # type: ignore[index]


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_non_covering_receipt_keeps_blocking_flag_and_a_second_correct_one_clears_it(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: receipt support used the EARLIEST receipt, so a correct receipt
    attached after a wrong one never cleared RECEIPT-REQ."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry,) = _report(db_session, emp)

    first = _upload_to(client, entry, "coffee")
    assert first.status_code == 201, first.text
    assert first.json()["policy_verdict"]["blocking_count"] >= 1
    (flag,) = _of_type(_flags(db_session, report), "missing_receipt", entry)
    assert flag["severity"] == "blocking"
    assert "does not cover the claimed amount" in flag["reason"]

    second = _upload_to(client, entry, "meal60")
    assert second.status_code == 201, second.text
    assert not _of_type(_flags(db_session, report), "missing_receipt", entry)
    entries = client.get(f"{API}/expense-reports/{report.id}/entries").json()
    assert len(entries[0]["receipt_ids"]) == 2


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_attach_rechecks_draft_under_lock_after_ocr_and_stores_nothing(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    storage: _MemStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old: the draft check ran unlocked BEFORE OCR, so a submit that landed
    while OCR ran left the receipt on a submitted report the submit never saw.
    Also: the receipt row is inserted UNLINKED (no FK lock on the entry during
    OCR — the deadlock the review found)."""
    from backend.app.modules.receipt import router as receipt_router

    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    real = receipt_router.run_receipt_pipeline
    seen: list[Any] = []

    def ocr_then_concurrent_submit(db: Session, **kwargs: Any) -> Any:
        receipt = db.get(Receipt, kwargs["receipt_id"])
        seen.append(receipt.expense_id if receipt is not None else "missing")
        out = real(db, **kwargs)
        db.execute(
            sa.update(ExpenseReport)
            .where(ExpenseReport.id == report.id)
            .values(status=ExpenseReportStatus.submitted)
            .execution_options(synchronize_session=False)
        )
        return out

    monkeypatch.setattr(receipt_router, "run_receipt_pipeline", ocr_then_concurrent_submit)

    resp = _upload_to(_client(app, emp), entry, "meal60")

    assert seen == [None]  # unlinked while OCR ran
    assert resp.status_code == 409, resp.text
    assert "draft" in resp.json()["error"]["message"]
    assert storage.objects == {}  # stored image removed
    assert (
        db_session.scalar(sa.select(sa.func.count(Receipt.id)).where(Receipt.uploaded_by == emp.id))
        == 0
    )


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_attach_with_stale_if_match_is_409_and_stores_nothing(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    resp = _upload_to(_client(app, emp), entry, "meal60", version=_version(db_session, report) + 5)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert storage.objects == {}


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_link_existing_receipt_and_its_guards(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry, other) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("40.00", "Bistro", DAY + timedelta(days=1))],
    )
    free = _receipt(db_session, emp)
    pending = _receipt(db_session, emp, status=OcrStatus.pending, amount=None)
    linked = _receipt(db_session, emp, other, amount=40.0, vendor="Bistro", day="2026-06-11")
    foreign = _receipt(db_session, users_a["manager"])

    listed = client.get(f"{API}/receipts")
    assert listed.status_code == 200, listed.text
    assert {r["id"] for r in listed.json()} == {str(free.id)}  # own, unlinked, read

    def link(receipt: Receipt) -> Any:
        return client.post(
            f"{API}/expenses/{entry.id}/receipts", json={"receipt_id": str(receipt.id)}
        )

    assert link(foreign).status_code == 404
    r = link(pending)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_NOT_PROCESSED"
    r = link(linked)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_ALREADY_LINKED"

    ok = client.post(
        f"{API}/expenses/{entry.id}/receipts",
        json={"receipt_id": str(free.id)},
        headers={"If-Match": str(_version(db_session, report))},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["policy_verdict"]["in_policy"] is True
    assert ok.json()["entry"]["receipt_ids"] == [str(free.id)]
    assert not _of_type(_flags(db_session, report), "missing_receipt", entry)
    (audit,) = _audits(db_session, "expense.receipt_attached", entry.id)
    assert audit.after_state["source"] == "existing"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Evidence integrity — reuse, date drift, unverifiable totals
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_same_receipt_reuploaded_for_a_retyped_entry_is_a_blocking_duplicate(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """H3: the same METRO receipt uploaded again as a new row, its entry retyped
    (vendor / date) — the entry-level duplicate check missed it and the covering
    receipt satisfied RECEIPT-REQ, so it could auto-approve."""
    emp = users_a["employee"]
    _first, (a,) = _report(db_session, emp, title="June trip", status=ExpenseReportStatus.approved)
    _receipt(db_session, emp, a)
    second, (b,) = _report(
        db_session,
        emp,
        [("60.00", "SkyPark", DAY + timedelta(days=2))],
        title="Later trip",
        status=ExpenseReportStatus.submitted,
    )
    _receipt(db_session, emp, b)  # same vendor / date / total as a's receipt

    flags, route = _assess(db_session, second, users_a["manager"])

    dup = _of_type(flags, "possible_duplicate", b)
    assert dup and dup[0]["severity"] == "blocking", flags
    assert "'June trip'" in dup[0]["reason"]
    assert dup[0]["observed"]["duplicate_check"] == "receipt date+total+vendor"
    assert route == "escalate"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_receipt_dated_far_from_the_claim_is_advisory_but_escalates(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    _receipt(db_session, emp, entry, day="2026-07-20")
    expense_service.refresh_presubmit_flags(db_session, report)

    (flag,) = _of_type(_flags(db_session, report), "receipt_mismatch", entry)
    assert flag["severity"] == "advisory"
    assert "40 days" in flag["reason"]
    assert not [fl for fl in _flags(db_session, report) if fl.get("severity") == "blocking"]
    _flags_now, route = _assess(db_session, report, users_a["manager"])
    assert route == "escalate"  # never auto-approved


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_hotel_folio_dated_at_checkout_is_not_a_mismatch(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    report, (stay,) = _report(
        db_session, emp, [("750.00", "Hilton Chicago", DAY)], category=ExpenseCategory.lodging
    )
    _receipt(db_session, emp, stay, amount=750.0, vendor="Hilton Chicago", day="2026-06-13")
    expense_service.refresh_presubmit_flags(db_session, report)
    assert not _of_type(_flags(db_session, report), "receipt_mismatch")


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_unverifiable_receipt_total_is_advisory_and_escalates(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: a receipt whose total the claimant typed (or OCR could not read)
    stayed a BLOCKING, non-justifiable missing_receipt — a permanent dead-end."""
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    _receipt(db_session, emp, entry, amount_provenance="user_edited")
    expense_service.refresh_presubmit_flags(db_session, report)

    (flag,) = _of_type(_flags(db_session, report), "missing_receipt", entry)
    assert flag["severity"] == "advisory"
    assert "could not be verified" in flag["reason"] and "approver will review" in flag["reason"]
    assert _submit(_client(app, emp), report)["status"] == "submitted"  # escalated
    assert _approval(db_session, report).current_status == ApprovalStatus.pending


# ---------------------------------------------------------------------------
# Issue 2 — deletes
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_delete_entry_on_draft_removes_entry_receipts_and_audits(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    report, (gone, kept) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("40.00", "Bistro", DAY + timedelta(days=1))],
    )
    receipt = _receipt(db_session, emp, gone, storage=storage)
    txn = f.make_card_transaction(db_session, user=emp, expense_id=gone.id, matched=True)
    before = _version(db_session, report)

    resp = _client(app, emp).delete(f"{API}/expenses/{gone.id}", headers={"If-Match": str(before)})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_receipt_ids"] == [str(receipt.id)]
    assert body["retained_receipt_ids"] == []
    assert Decimal(body["report"]["total_amount"]) == Decimal("40.00")
    assert body["report"]["version"] == before + 1
    db_session.expire_all()
    assert db_session.get(Expense, gone.id) is None
    assert db_session.get(Receipt, receipt.id) is None
    assert storage.deleted == [receipt.image_url]  # purged after the commit
    freed = db_session.get(CardTransaction, txn.id)
    assert freed is not None and freed.expense_id is None and freed.matched is False
    (audit,) = _audits(db_session, "expense.deleted", gone.id)
    assert audit.after_state is None
    assert audit.before_state["vendor"] == "Corner Deli"  # type: ignore[index]
    assert audit.before_state["receipts"][0]["amount"] == 60.0  # type: ignore[index]
    assert audit.before_state["card_transaction_ids"] == [str(txn.id)]  # type: ignore[index]
    assert _audits(db_session, "receipt.deleted", receipt.id)
    assert db_session.get(Expense, kept.id) is not None


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
@pytest.mark.parametrize(
    "status",
    [
        ExpenseReportStatus.submitted,
        ExpenseReportStatus.approved,
        ExpenseReportStatus.rejected,
        ExpenseReportStatus.reimbursed,
    ],
)
def test_delete_entry_refused_unless_draft(
    app: Any, db_session: Session, users_a: dict[str, User], status: ExpenseReportStatus
) -> None:
    emp = users_a["employee"]
    _report_, (entry,) = _report(db_session, emp, status=status)
    resp = _client(app, emp).delete(f"{API}/expenses/{entry.id}")
    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert db_session.get(Expense, entry.id) is not None


@pytest.mark.usefixtures("rbac_catalog")
def test_delete_entry_is_owner_only(
    app: Any, db_session: Session, users_a: dict[str, User], users_b: dict[str, User]
) -> None:
    _r, (entry,) = _report(db_session, users_a["employee"])
    assert _client(app, users_a["manager"]).delete(f"{API}/expenses/{entry.id}").status_code == 403
    assert _client(app, users_a["finance"]).delete(f"{API}/expenses/{entry.id}").status_code == 403
    assert _client(app, users_b["employee"]).delete(f"{API}/expenses/{entry.id}").status_code == 404
    db_session.expire_all()
    assert db_session.get(Expense, entry.id) is not None


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_deleting_one_twin_clears_the_duplicate_flag_on_the_peer_draft(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: report B kept its blocking duplicate flag (naming report A) after the
    twin on A was gone — a deadlock only an unrelated edit of B broke."""
    emp = users_a["employee"]
    report_a, (twin_a,) = _report(db_session, emp, title="Inbox copy")
    report_b, (twin_b,) = _report(db_session, emp, title="Chicago visit")
    expense_service.refresh_presubmit_flags(db_session, report_a)
    expense_service.refresh_presubmit_flags(db_session, report_b)
    assert _of_type(_flags(db_session, report_b), "possible_duplicate", twin_b)
    b_version = _version(db_session, report_b)

    resp = _client(app, emp).delete(f"{API}/expenses/{twin_a.id}")

    assert resp.status_code == 200, resp.text
    assert not _of_type(_flags(db_session, report_b), "possible_duplicate")
    assert _version(db_session, report_b) == b_version + 1  # a stale tab gets a 409


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_editing_a_twin_refreshes_the_peer_and_wording_is_honest(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: the peer kept "the same vendor, date and amount ..." although one line
    had no vendor at all, and kept it after the twin was edited away."""
    emp = users_a["employee"]
    report_a, (twin_a,) = _report(db_session, emp, title="Inbox copy")
    report_b, (twin_b,) = _report(db_session, emp, [("60.00", None, DAY)], title="Chicago visit")
    expense_service.refresh_presubmit_flags(db_session, report_b)
    (dup,) = _of_type(_flags(db_session, report_b), "possible_duplicate", twin_b)
    assert "same vendor" not in dup["reason"]
    assert "names no vendor" in dup["reason"] and "'Inbox copy'" in dup["reason"]

    resp = _client(app, emp).patch(f"{API}/expenses/{twin_a.id}", json={"amount": "61.00"})

    assert resp.status_code == 200, resp.text
    assert not _of_type(_flags(db_session, report_b), "possible_duplicate")


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_delete_reviewed_entry_with_snapshot_keeps_its_receipt_and_refile_escalates(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """A returned report's entry may be deleted while the return step froze the
    report (0014): its receipt is kept for audit (never deleted / reused) and the
    re-file check still catches the line typed into a fresh report."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (gone, _stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("45.00", "Bistro", DAY + timedelta(days=1))],
    )
    receipt = _receipt(db_session, emp, gone, storage=storage)
    _returned(db_session, app, users_a, report)

    resp = client.delete(f"{API}/expenses/{gone.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["retained_receipt_ids"] == [str(receipt.id)]
    db_session.expire_all()
    kept = db_session.get(Receipt, receipt.id)
    assert kept is not None and kept.expense_id is None
    assert storage.deleted == [] and receipt.image_url in storage.objects
    assert _audits(db_session, RECEIPT_RETAINED_ACTION, receipt.id)
    # Kept for audit: never deleted, never re-linked.
    r = client.delete(f"{API}/receipts/{receipt.id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_RETAINED"
    assert "kept for audit" in r.json()["error"]["message"]
    fresh, (copy,) = _report(db_session, emp, [("45.00", "Other", DAY)], title="Fresh")
    r = client.post(f"{API}/expenses/{copy.id}/receipts", json={"receipt_id": str(receipt.id)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_RETAINED"
    assert str(receipt.id) not in {x["id"] for x in client.get(f"{API}/receipts").json()}

    # The deleted line re-filed in a fresh report → a human sees it.
    refile, _ = _report(db_session, emp, [("60.00", "Different name", DAY)], title="Refile")
    flags, route = _assess(db_session, refile, users_a["manager"])
    assert route == "escalate"
    assert [fl for fl in flags if fl.get("flag_type") == "refiled_rejected_claim"], flags


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_reviewed_entry_under_a_legacy_null_snapshot_is_not_deletable(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Pre-0014 return steps froze nothing, so deleting a line the reviewer saw
    would erase the only evidence (Q3 in the browser DB): 409, fail closed.  A
    line added AFTER that return was never reviewed and may be deleted."""
    emp, mgr = users_a["employee"], users_a["manager"]
    report, (reviewed,) = _report(db_session, emp)
    approval = f.make_approval(
        db_session,
        submitter=emp,
        subject_id=report.id,
        current_status=ApprovalStatus.changes_requested,
    )
    f.make_approval_step(
        db_session,
        approval=approval,
        decision=ApprovalDecision.request_changes,
        approver_id=mgr.id,
        reason="Legacy return",
        decided_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        subject_snapshot=None,
    )
    client = _client(app, emp)

    entries = client.get(f"{API}/expense-reports/{report.id}/entries").json()
    assert entries[0]["deletable"] is False and entries[0]["delete_blocked_reason"]
    resp = client.delete(f"{API}/expenses/{reviewed.id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "REPORT_UNDER_REVIEW"

    later = f.make_expense(db_session, report=report, amount="5.00", vendor="Later")
    later.created_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db_session.flush()
    assert client.delete(f"{API}/expenses/{later.id}").status_code == 200


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_delete_receipt_rules(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry,) = _report(db_session, emp)
    loose = _receipt(db_session, emp, storage=storage)
    attached = _receipt(db_session, emp, entry, storage=storage)
    expense_service.refresh_presubmit_flags(db_session, report)
    assert not _of_type(_flags(db_session, report), "missing_receipt", entry)
    before = _version(db_session, report)

    # Not the uploader: the manager can READ the receipt but not delete it.
    assert _client(app, users_a["manager"]).delete(f"{API}/receipts/{loose.id}").status_code == 403

    r = client.delete(f"{API}/receipts/{loose.id}")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "deleted" and r.json()["report"] is None
    r = client.delete(f"{API}/receipts/{attached.id}")
    assert r.status_code == 200, r.text
    assert r.json()["expense_id"] == str(entry.id)
    db_session.expire_all()
    assert db_session.get(Receipt, loose.id) is None
    assert db_session.get(Receipt, attached.id) is None
    assert db_session.get(Expense, entry.id) is not None  # the entry stays
    assert set(storage.deleted) == {loose.image_url, attached.image_url}
    assert _of_type(_flags(db_session, report), "missing_receipt", entry)  # RECEIPT-REQ back
    assert _version(db_session, report) == before + 1
    assert _audits(db_session, "expense.receipt_removed", entry.id)

    submitted, (s_entry,) = _report(db_session, emp, status=ExpenseReportStatus.submitted)
    in_use = _receipt(db_session, emp, s_entry)
    r = client.delete(f"{API}/receipts/{in_use.id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_IN_USE"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_receipt_on_a_returned_report_is_kept_for_audit(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    receipt = _receipt(db_session, emp, entry, amount=3.0, storage=storage)
    _returned(db_session, app, users_a, report)
    r = _client(app, emp).delete(f"{API}/receipts/{receipt.id}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "RECEIPT_RETAINED"
    assert r.json()["error"]["message"] == (
        "This receipt was part of a submitted report and is kept for audit."
    )
    assert storage.deleted == []


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_discarding_an_upload_also_removes_its_auto_drafted_inbox_entry(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    up = client.post(f"{API}/receipts", files={"file": scenario_upload("meal60")})
    assert up.status_code == 201, up.text
    drafted = up.json()["expense_id"]
    assert drafted is not None

    r = client.delete(f"{API}/receipts/{up.json()['id']}", params={"discard_drafted_entry": "true"})

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "deleted_with_entry"
    db_session.expire_all()
    assert db_session.get(Expense, drafted) is None
    assert storage.objects == {}


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_delete_never_submitted_report_cascades_with_audit(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    report, (a, b) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("40.00", "Bistro", DAY + timedelta(days=1))],
    )
    receipt = _receipt(db_session, emp, a, storage=storage)
    client = _client(app, emp)

    stale = client.delete(f"{API}/expense-reports/{report.id}", headers={"If-Match": "999"})
    assert stale.status_code == 409
    resp = client.delete(
        f"{API}/expense-reports/{report.id}",
        headers={"If-Match": str(_version(db_session, report))},
    )

    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    assert db_session.get(ExpenseReport, report.id) is None
    assert db_session.get(Expense, a.id) is None and db_session.get(Expense, b.id) is None
    assert db_session.get(Receipt, receipt.id) is None
    assert storage.deleted == [receipt.image_url]
    (audit,) = _audits(db_session, "expense_report.deleted", report.id)
    assert set(audit.before_state["entry_ids"]) == {str(a.id), str(b.id)}  # type: ignore[index]
    assert _audits(db_session, "expense.deleted", a.id) and _audits(
        db_session, "expense.deleted", b.id
    )
    assert client.get(f"{API}/expense-reports/{report.id}").status_code == 404


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_report_with_approval_history_cannot_be_deleted(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    report, _ = _report(db_session, emp)
    _returned(db_session, app, users_a, report)
    r = _client(app, emp).delete(f"{API}/expense-reports/{report.id}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "REPORT_HAS_APPROVAL"
    assert "Submitted reports can't be deleted" in r.json()["error"]["message"]
    db_session.expire_all()
    assert db_session.get(ExpenseReport, report.id) is not None

    submitted, _ = _report(db_session, emp, status=ExpenseReportStatus.submitted)
    assert _client(app, emp).delete(f"{API}/expense-reports/{submitted.id}").status_code == 409
    assert (
        _client(app, users_a["manager"]).delete(f"{API}/expense-reports/{report.id}").status_code
        == 403
    )


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_storage_delete_failure_never_fails_the_request(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    broken = _MemStorage(fail_delete=True)
    app.dependency_overrides[get_storage_provider] = lambda: broken
    emp = users_a["employee"]
    _r, (entry,) = _report(db_session, emp)
    receipt = _receipt(db_session, emp, entry, storage=broken)
    resp = _client(app, emp).delete(f"{API}/expenses/{entry.id}")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Receipt, receipt.id) is None  # committed; object orphaned


# ---------------------------------------------------------------------------
# M1 / M12 — the returned report shows the engine's verdict; empty submits
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_request_changes_replaces_seeded_flags_with_the_engine_verdict(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """M1: the Q3 fixture carried one hand-written HOTEL-CAP flag; the employee saw
    it until the first edit, then six engine flags appeared."""
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    report.pre_submit_flags = {
        "flags": [{"flag_type": "policy_violation", "severity": "blocking", "reason": "seed"}],
        "blocking_count": 1,
        "advisory_count": 0,
    }
    db_session.flush()

    _returned(db_session, app, users_a, report)

    flags = _flags(db_session, report)
    assert not [fl for fl in flags if fl.get("reason") == "seed"]
    assert _of_type(flags, "missing_receipt", entry)


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_reopening_a_rejected_report_refreshes_its_flags(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    report, (entry,) = _report(db_session, emp)
    _submit(_client(app, emp), report)
    _decide(_client(app, users_a["manager"]), _approval(db_session, report), "reject")
    db_session.expire_all()
    stale = db_session.get(ExpenseReport, report.id)
    assert stale is not None
    stale.pre_submit_flags = {"flags": [], "blocking_count": 0, "advisory_count": 0}
    db_session.flush()

    r = _client(app, emp).patch(f"{API}/expense-reports/{report.id}", json={"status": "draft"})

    assert r.status_code == 200, r.text
    assert _of_type(_flags(db_session, report), "missing_receipt", entry)


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_empty_report_submit_is_refused_unless_it_has_approval_history(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    empty = f.make_expense_report(db_session, owner=emp, title="Nothing yet")
    r = client.patch(f"{API}/expense-reports/{empty.id}", json={"status": "submitted"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "REPORT_EMPTY"

    # A returned report whose every line was deleted can't be deleted (approval
    # history) — it must stay submittable so a human can close it.
    report, (only,) = _report(db_session, emp)
    _returned(db_session, app, users_a, report)
    assert client.delete(f"{API}/expenses/{only.id}").status_code == 200
    assert _submit(client, report)["status"] == "submitted"


# ---------------------------------------------------------------------------
# Inbox release never touches reviewed evidence
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog")
def test_a_reviewed_receipt_inbox_entry_is_never_released(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    inbox, (entry,) = _report(db_session, emp, title="Receipt Inbox")
    entry.match_source = ExpenseMatchSource.receipt
    db_session.flush()
    ids = editable_inbox_expense_ids(
        db_session, organization_id=emp.organization_id, user_id=emp.id, expense_ids=[entry.id]
    )
    assert ids == {entry.id}
    f.make_approval(
        db_session,
        submitter=emp,
        subject_id=inbox.id,
        current_status=ApprovalStatus.changes_requested,
    )
    ids = editable_inbox_expense_ids(
        db_session, organization_id=emp.organization_id, user_id=emp.id, expense_ids=[entry.id]
    )
    assert ids == set()


# ---------------------------------------------------------------------------
# Review round 2 — evidence that the claimant typed, and evidence kept for audit
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_correcting_a_reused_receipts_date_never_hides_the_duplicate(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: PATCH /receipts {date} on a re-uploaded copy of an approved claim's
    receipt made the reuse check (which matched on the CURRENT receipt date)
    miss it; the next refresh cleared the flags and the double claim
    auto-approved.  A typed receipt date is not evidence."""
    emp = users_a["employee"]
    client = _client(app, emp)
    _first, (a,) = _report(db_session, emp, title="June trip", status=ExpenseReportStatus.approved)
    _receipt(db_session, emp, a)  # Corner Deli 60.00 2026-06-10, read by OCR
    second, (b,) = _report(
        db_session, emp, [("60.00", "Corner Deli", DAY + timedelta(days=5))], title="Later trip"
    )
    up = _upload_to(client, b, "meal60")  # the SAME paper receipt, a new row
    assert up.status_code == 201, up.text
    assert _of_type(_flags(db_session, second), "possible_duplicate", b)
    version = _version(db_session, second)

    p = client.patch(
        f"{API}/receipts/{up.json()['id']}", json={"field": "date", "value": "2026-06-15"}
    )
    assert p.status_code == 200, p.text
    assert p.json()["field_provenance"]["date"] == "user_edited"
    # The correction left the entry unchanged but still re-ran the check.
    assert _version(db_session, second) > version
    dup = _of_type(_flags(db_session, second), "possible_duplicate", b)
    assert dup and dup[0]["severity"] == "blocking", _flags(db_session, second)
    assert "'June trip'" in dup[0]["reason"]

    # Any later refresh keeps it (the typed date is never trusted).
    e = client.patch(f"{API}/expenses/{b.id}", json={"description": "team lunch"})
    assert e.status_code == 200, e.text
    dup = _of_type(_flags(db_session, second), "possible_duplicate", b)
    assert dup and dup[0]["severity"] == "blocking"
    # The double claim reaches a human; it is never auto-approved.
    assert _submit(client, second)["status"] == "submitted"
    assert _approval(db_session, second).current_status == ApprovalStatus.pending


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_hand_typed_receipt_date_is_advisory_and_escalates(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """A receipt date the claimant typed cannot be checked against the claim:
    advisory (submit allowed), but the report always reaches a human."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry,) = _report(db_session, emp, [("60.00", "Corner Deli", DAY)])
    up = _upload_to(client, entry, "meal60")
    assert up.status_code == 201, up.text
    assert not _of_type(_flags(db_session, report), "receipt_mismatch", entry)

    p = client.patch(
        f"{API}/receipts/{up.json()['id']}", json={"field": "date", "value": "2026-06-11"}
    )
    assert p.status_code == 200, p.text

    flags = _flags(db_session, report)
    (flag,) = _of_type(flags, "receipt_mismatch", entry)
    assert flag["severity"] == "advisory"
    assert "entered by hand" in flag["reason"]
    assert not [fl for fl in flags if fl.get("severity") == "blocking"], flags
    assert _submit(client, report)["status"] == "submitted"  # escalated, not auto-approved
    assert _approval(db_session, report).current_status == ApprovalStatus.pending


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_reuploading_a_receipt_kept_for_audit_escalates_the_refiled_line(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: a line deleted from a returned report kept its receipt for audit,
    but the reuse check only compared receipts LINKED to live entries, so the
    same paper receipt re-uploaded on a fresh line (dated 2 days off, outside
    the ±1-day re-file window) auto-approved."""
    emp, mgr = users_a["employee"], users_a["manager"]
    client = _client(app, emp)
    report, (gone, _stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("45.00", "Bistro", DAY + timedelta(days=1))],
        title="Returned trip",
    )
    _receipt(db_session, emp, gone, storage=storage)
    _returned(db_session, app, users_a, report)
    assert client.delete(f"{API}/expenses/{gone.id}").status_code == 200
    _submit(client, report)
    db_session.expire_all()
    _decide(_client(app, mgr), _approval(db_session, report), "approve")
    db_session.expire_all()
    assert db_session.get(ExpenseReport, report.id).status == ExpenseReportStatus.approved  # type: ignore[union-attr]

    fresh, (copy,) = _report(
        db_session, emp, [("60.00", "Corner Deli", DAY + timedelta(days=2))], title="Fresh"
    )
    up = _upload_to(client, copy, "meal60")
    assert up.status_code == 201, up.text

    flags = _flags(db_session, fresh)
    kept = [
        fl
        for fl in _of_type(flags, "receipt_mismatch", copy)
        if fl["observed"].get("receipt_check") == "kept_for_audit"
    ]
    assert kept and kept[0]["severity"] == "advisory", flags
    assert "'Returned trip'" in kept[0]["reason"]
    body = _submit(client, fresh)
    assert body["status"] == "submitted"  # escalated to a human
    assert _approval(db_session, fresh).current_status == ApprovalStatus.pending


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_refile_window_for_returned_lines_covers_the_receipt_date_tolerance(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """A deleted reviewed line re-typed 3 days off (no receipt) still matches
    the frozen snapshot (the window equals the receipt-date tolerance)."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (gone, _stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("45.00", "Bistro", DAY + timedelta(days=1))],
    )
    _returned(db_session, app, users_a, report)
    assert client.delete(f"{API}/expenses/{gone.id}").status_code == 200

    refile, _ = _report(
        db_session, emp, [("60.00", "Elsewhere", DAY + timedelta(days=3))], title="Refile"
    )
    flags, route = _assess(db_session, refile, users_a["manager"])
    assert route == "escalate"
    assert [fl for fl in flags if fl.get("flag_type") == "refiled_rejected_claim"], flags


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_receipts_screen_upload_can_be_picked_and_its_inbox_line_is_released(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: every Receipts-screen upload is auto-drafted into a Receipt Inbox line,
    so the picker (unlinked receipts only) was empty, and re-uploading the file
    on the entry flagged both lines as duplicates."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry,) = _report(db_session, emp)
    up = client.post(f"{API}/receipts", files={"file": scenario_upload("meal60")})
    assert up.status_code == 201, up.text
    rid, inbox_line = up.json()["id"], up.json()["expense_id"]
    assert inbox_line is not None
    db_session.expire_all()
    inbox_id = db_session.get(Expense, inbox_line).report_id  # type: ignore[union-attr]

    assert rid in {r["id"] for r in client.get(f"{API}/receipts").json()}
    ok = client.post(f"{API}/expenses/{entry.id}/receipts", json={"receipt_id": rid})

    assert ok.status_code == 200, ok.text
    assert ok.json()["entry"]["receipt_ids"] == [rid]
    db_session.expire_all()
    assert db_session.get(Expense, inbox_line) is None  # the upload's own line is gone
    moved = db_session.get(Receipt, uuid.UUID(rid))
    assert moved is not None and moved.expense_id == entry.id
    (gone,) = _audits(db_session, "expense.deleted", uuid.UUID(inbox_line))
    assert gone.after_state["receipt_id"] == rid  # type: ignore[index]
    assert gone.before_state["receipts"][0]["receipt_id"] == rid  # type: ignore[index]
    assert not _of_type(_flags(db_session, report), "possible_duplicate")
    inbox = db_session.get(ExpenseReport, inbox_id)
    assert inbox is not None and Decimal(str(inbox.total_amount)) == Decimal("0")
    assert rid not in {r["id"] for r in client.get(f"{API}/receipts").json()}


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_an_inbox_line_that_is_more_than_its_upload_is_never_released(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    _report_, (entry,) = _report(db_session, emp)
    up = client.post(f"{API}/receipts", files={"file": scenario_upload("meal60")})
    assert up.status_code == 201, up.text
    rid, inbox_line = up.json()["id"], up.json()["expense_id"]
    db_session.expire_all()
    line = db_session.get(Expense, inbox_line)
    assert line is not None
    _receipt(db_session, emp, line)  # a second receipt on that inbox line

    assert rid not in {r["id"] for r in client.get(f"{API}/receipts").json()}
    r = client.post(f"{API}/expenses/{entry.id}/receipts", json={"receipt_id": rid})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_ALREADY_LINKED"
    db_session.expire_all()
    assert db_session.get(Expense, inbox_line) is not None
