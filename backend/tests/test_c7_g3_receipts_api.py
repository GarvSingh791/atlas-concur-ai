"""C7 G3 (receipts) — DB-backed API regression tests.

C7-19  POST /receipts requires expense.create.own (finance → 403, nothing stored).
C7-16  PATCH /receipts/{id} validates field + value (422 via the envelope).
C7-17  A correction reaches the linked draft entry + report total; never a
       submitted report's entry; never another user's entry.
C7-18  An ambiguous (subtotal-as-total) amount is never asserted deterministically.
C7-21  An OCR "transportation" receipt drafts a ground_transport entry.
C7-22  Ingest keeps an inbox-linked receipt matchable; release_inbox_draft removes
       the duplicate inbox entry (and only an editable, own, inbox entry).

Uses the shared conftest (rollback-isolated session, real-JWT role clients) and
in-memory OCR/storage/LLM stubs — no network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.agents.expense_agent.ingest import ingest
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline import RECEIPT_INBOX_TITLE, release_inbox_draft
from backend.tests.receipt_images import scenario_of, scenario_upload
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.receipt import OcrStatus, Receipt
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = [pytest.mark.db]

_HIGH = {
    "amount": 0.95,
    "currency": 0.99,
    "date": 0.95,
    "vendor": 0.93,
    "location": 0.9,
    "expense_type": 0.92,
}

_SCENARIOS: dict[str, dict[str, Any]] = {
    "meal": {
        "amount": 17.20,
        "currency": "USD",
        "date": "2026-06-11",
        "vendor": "Corner Deli",
        "location": "New York, NY",
        "expense_type": "meals",
    },
    "taxi": {
        "amount": 93.44,
        "currency": "USD",
        "date": "2026-06-10",
        "vendor": "METRO YELLOW CAB 1731",
        "location": "Manhattan, NY",
        "expense_type": "transportation",
    },
    # Over the NYC hotel cap (one night, no per-night extra → amount is the rate).
    "hotel": {
        "amount": 495.00,
        "currency": "USD",
        "date": "2026-06-10",
        "vendor": "Marriott New York",
        "location": "New York, NY",
        "expense_type": "hotel",
    },
    # R5: the TOTAL is blurred; the model returned the readable subtotal as amount.
    "blurred": {
        "amount": 17.20,
        "currency": "USD",
        "date": "2026-06-11",
        "vendor": "Corner Deli",
        "location": "New York, NY",
        "expense_type": "meals",
        "subtotal": 17.20,
        "tax": 1.53,
        "total_legible": False,
    },
}


class _ScenarioOcr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        fields = dict(_SCENARIOS[key])
        return OcrResult(fields=fields, field_confidence=dict(_HIGH))


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
def storage(app: Any) -> Iterator[_MemStorage]:
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _ScenarioOcr()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    yield mem


def _upload(client: TestClient, scenario: str) -> Any:
    return client.post(
        "/api/v1/receipts",
        files={"file": scenario_upload(scenario)},
    )


def _ctx(user: User) -> AgentContext:
    return AgentContext(
        organization_id=user.organization_id,
        granted_permission_keys=frozenset(),
        user_id=user.id,
        role=user.role.value,
    )


# ---------------------------------------------------------------------------
# C7-19
# ---------------------------------------------------------------------------


def test_finance_cannot_upload_receipt_and_nothing_is_stored(
    finance_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    resp = _upload(finance_client, "meal")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    finance = users_a["finance"]
    assert storage.objects == {}
    assert (
        db_session.scalar(
            sa.select(sa.func.count(Receipt.id)).where(Receipt.uploaded_by == finance.id)
        )
        == 0
    )
    assert (
        db_session.scalar(
            sa.select(sa.func.count(ExpenseReport.id)).where(ExpenseReport.owner_id == finance.id)
        )
        == 0
    )


def test_employee_upload_still_works(employee_client: TestClient, storage: _MemStorage) -> None:
    resp = _upload(employee_client, "meal")
    assert resp.status_code == 201, resp.text
    assert resp.json()["expense_id"] is not None


# ---------------------------------------------------------------------------
# C7-21
# ---------------------------------------------------------------------------


def test_transportation_receipt_drafts_ground_transport_entry(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    resp = _upload(employee_client, "taxi")
    assert resp.status_code == 201, resp.text
    entry = db_session.get(Expense, resp.json()["expense_id"])
    assert entry is not None
    assert entry.category == ExpenseCategory.ground_transport


# ---------------------------------------------------------------------------
# C7-18
# ---------------------------------------------------------------------------


def test_blurred_subtotal_is_not_asserted_as_total(
    employee_client: TestClient, storage: _MemStorage
) -> None:
    resp = _upload(employee_client, "blurred")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["processing_path"] == "agent_gap_fill"
    assert body["field_provenance"]["amount"] != "deterministic"
    assert body["field_confidence"]["amount"] < 0.70
    # The subtotal is never silently drafted as the claim amount.
    assert body["extracted_fields"].get("amount") != 17.20 or body["expense_id"] is None


# ---------------------------------------------------------------------------
# C7-16
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"field": "not_a_field", "value": "x"},
        {"field": "amount", "value": "abc"},
        {"field": "amount", "value": -999999},
        {"field": "amount", "value": 0},
        {"field": "amount", "value": 12.345},
        {"field": "amount", "value": 99999999},
        {"field": "currency", "value": "dollars"},
        {"field": "date", "value": "2999-01-01"},
        {"field": "date", "value": "not-a-date"},
        {"field": "vendor", "value": ""},
        {"field": "vendor", "value": "x" * 500},
        {"field": "expense_type", "value": "yacht"},
        {"field": "expense_id", "value": "00000000-0000-0000-0000-000000000000"},
        {"field": "ocr_status", "value": "processed"},
        # Review finding: NUL / control chars used to reach Postgres → 500.
        {"field": "vendor", "value": "a\u0000b"},
        {"field": "location", "value": " \u0000 "},
        {"field": "vendor", "value": "\u200b"},
    ],
)
def test_invalid_correction_rejected_with_422(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    payload: dict,
) -> None:
    up = _upload(employee_client, "meal")
    assert up.status_code == 201, up.text
    rid = up.json()["id"]
    before = dict(up.json()["extracted_fields"])

    resp = employee_client.patch(f"/api/v1/receipts/{rid}", json=payload)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    receipt = db_session.get(Receipt, rid)
    assert receipt is not None
    db_session.refresh(receipt)
    assert receipt.extracted_fields == before
    assert "not_a_field" not in (receipt.extracted_fields or {})


# ---------------------------------------------------------------------------
# C7-17
# ---------------------------------------------------------------------------


def test_correction_propagates_to_linked_draft_entry_and_total(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    up = _upload(employee_client, "meal")
    assert up.status_code == 201, up.text
    rid, eid = up.json()["id"], up.json()["expense_id"]
    entry = db_session.get(Expense, eid)
    assert entry is not None and entry.amount == Decimal("17.20")
    report = db_session.get(ExpenseReport, entry.report_id)
    assert report is not None
    total_before = Decimal(str(report.total_amount))

    resp = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 18.73}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked_expense_sync"] == "updated"
    assert resp.json()["extracted_fields"]["amount"] == 18.73

    db_session.refresh(entry)
    db_session.refresh(report)
    assert entry.amount == Decimal("18.73")
    assert Decimal(str(report.total_amount)) == total_before + Decimal("1.53")
    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == entry.id, AuditLog.action == "expense.updated"
        )
    )
    assert audit is not None

    # Other mapped fields propagate too.
    r2 = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "expense_type", "value": "transportation"}
    )
    assert r2.status_code == 200 and r2.json()["linked_expense_sync"] == "updated"
    r3 = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "date", "value": "2026-06-12"}
    )
    assert r3.status_code == 200 and r3.json()["linked_expense_sync"] == "updated"
    r4 = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "location", "value": "Boston, MA"}
    )
    assert r4.status_code == 200 and r4.json()["linked_expense_sync"] == "not_applicable"
    db_session.refresh(entry)
    assert entry.category == ExpenseCategory.ground_transport
    assert str(entry.expense_date) == "2026-06-12"


def test_correction_never_touches_submitted_report_entry(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    up = _upload(employee_client, "meal")
    rid, eid = up.json()["id"], up.json()["expense_id"]
    entry = db_session.get(Expense, eid)
    assert entry is not None
    report = db_session.get(ExpenseReport, entry.report_id)
    assert report is not None
    report.status = ExpenseReportStatus.submitted
    db_session.flush()
    total_before = Decimal(str(report.total_amount))

    resp = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 999.00}
    )
    # UAT2 hardening: evidence on a submitted report is frozen — 409, nothing changes.
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "RECEIPT_LOCKED"
    db_session.refresh(entry)
    db_session.refresh(report)
    assert entry.amount == Decimal("17.20")
    assert Decimal(str(report.total_amount)) == total_before


def test_cannot_correct_someone_elses_receipt(
    app: Any,
    employee_client: TestClient,
    manager_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    up = _upload(employee_client, "meal")
    rid, eid = up.json()["id"], up.json()["expense_id"]
    # Manager of the uploader: readable via GET scope, but never correctable.
    resp = manager_client.patch(f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 1.00})
    assert resp.status_code == 404, resp.text
    # Finance lacks expense.update.own entirely.
    from backend.tests.conftest import _bearer_client

    fin = _bearer_client(app, users_a["finance"])
    resp = fin.patch(f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 1.00})
    assert resp.status_code == 403, resp.text
    entry = db_session.get(Expense, eid)
    assert entry is not None
    db_session.refresh(entry)
    assert entry.amount == Decimal("17.20")


def test_correction_does_not_reach_entry_owned_by_another_user(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    """A receipt (somehow) linked to another user's draft entry must not edit it."""
    employee, manager = users_a["employee"], users_a["manager"]
    foreign_report = f.make_expense_report(db_session, owner=manager, total_amount=Decimal("50"))
    foreign_entry = f.make_expense(db_session, report=foreign_report, amount="50.00")
    receipt = f.make_receipt(
        db_session,
        uploaded_by=employee,
        ocr_status=OcrStatus.processed,
        extracted_fields={"amount": 50.0},
        field_provenance={"amount": "deterministic"},
        expense_id=foreign_entry.id,
    )
    resp = employee_client.patch(
        f"/api/v1/receipts/{receipt.id}", json={"field": "amount", "value": 1.00}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked_expense_sync"] == "skipped_not_owner"
    db_session.refresh(foreign_entry)
    assert foreign_entry.amount == Decimal("50.00")


def _seed_hotel_cap(db_session: Session, org: Any) -> None:
    from database.models.policy_rule import PolicyAppliesTo, PolicyRuleType

    policy = f.make_policy(db_session, org=org, effective_from=date(2026, 1, 1), is_active=True)
    f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.hotel,
        source_clause_ref="HOTEL-CAP",
        definition={
            "currency": "USD",
            "per": "night",
            "tiers": [
                {"tier": "tier1", "cities": ["New York"], "cap": 250},
                {"tier": "domestic", "cap": 180},
            ],
            "exception_approver": "manager",
        },
    )


def _blocking_refs(report: ExpenseReport) -> list[str]:
    snap = report.pre_submit_flags or {}
    return [fl["entry_ref"] for fl in snap.get("flags") or [] if fl.get("severity") == "blocking"]


def test_correction_refreshes_entry_policy_status_and_flags(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    org_a: Any,
) -> None:
    """Review finding (C7-17): a corrected amount must re-run the single-entry
    policy check — the flagged badge and the pre-submit blocking flag must follow
    the corrected value, in both directions, and a vendor correction must not
    leave the old vendor-keyed flag behind. On the pre-review code the entry
    stayed 'flagged' with a blocking flag after the $495 → $200 correction."""
    _seed_hotel_cap(db_session, org_a)
    up = _upload(employee_client, "hotel")
    assert up.status_code == 201, up.text
    assert up.json()["policy_verdict"]["blocking_count"] == 1
    rid, eid = up.json()["id"], up.json()["expense_id"]
    entry = db_session.get(Expense, eid)
    assert entry is not None
    report = db_session.get(ExpenseReport, entry.report_id)
    assert report is not None
    assert entry.status.value == "flagged"
    assert _blocking_refs(report) == ["Marriott New York"]

    # Over cap → in policy: badge + blocking flag cleared.
    r1 = employee_client.patch(f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 200})
    assert r1.status_code == 200 and r1.json()["linked_expense_sync"] == "updated"
    db_session.refresh(entry)
    db_session.refresh(report)
    assert entry.status.value == "draft"
    assert _blocking_refs(report) == []
    assert (report.pre_submit_flags or {})["blocking_count"] == 0

    # In policy → over cap: flagged again (no stale "in policy").
    r2 = employee_client.patch(f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 4999})
    assert r2.status_code == 200
    db_session.refresh(entry)
    db_session.refresh(report)
    assert entry.status.value == "flagged"
    assert _blocking_refs(report) == ["Marriott New York"]

    # Vendor correction re-keys the flag; the old vendor-keyed flag is gone.
    r3 = employee_client.patch(
        f"/api/v1/receipts/{rid}", json={"field": "vendor", "value": "Hilton Midtown"}
    )
    assert r3.status_code == 200
    db_session.refresh(report)
    assert _blocking_refs(report) == ["Hilton Midtown"]
    assert (report.pre_submit_flags or {})["blocking_count"] == 1


def test_correction_audit_matches_expense_service_snapshot_shape(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    """Review finding: one action ('expense.updated') must have one audit shape."""
    up = _upload(employee_client, "meal")
    rid, eid = up.json()["id"], up.json()["expense_id"]
    resp = employee_client.patch(f"/api/v1/receipts/{rid}", json={"field": "amount", "value": 19})
    assert resp.status_code == 200
    audit = db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == uuid.UUID(eid), AuditLog.action == "expense.updated"
        )
    )
    assert audit is not None
    service_keys = {
        "report_id",
        "category",
        "amount",
        "currency",
        "expense_date",
        "status",
        "is_itemized",
    }
    after, before = audit.after_state or {}, audit.before_state or {}
    assert service_keys <= set(after)
    assert service_keys <= set(before)
    assert before["amount"] == "17.20" and after["amount"] == "19.00"


def test_idempotency_key_of_another_user_never_replays_their_receipt(
    employee_client: TestClient,
    manager_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    """Review finding: the replay was org-scoped, so a second user in the same org
    sending the first user's Idempotency-Key got the first user's receipt back."""
    headers = {"Idempotency-Key": "g3-shared-key-1"}
    first = employee_client.post(
        "/api/v1/receipts",
        files={"file": scenario_upload("meal")},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    replay = employee_client.post(
        "/api/v1/receipts",
        files={"file": scenario_upload("meal")},
        headers=headers,
    )
    assert replay.status_code == 201 and replay.json()["id"] == first.json()["id"]

    other = manager_client.post(
        "/api/v1/receipts",
        files={"file": scenario_upload("taxi")},
        headers=headers,
    )
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "CONFLICT"
    assert first.json()["id"] not in other.text
    assert "Corner Deli" not in other.text
    n_mgr = db_session.scalar(
        sa.select(sa.func.count(Receipt.id)).where(Receipt.uploaded_by == users_a["manager"].id)
    )
    assert n_mgr == 0


# ---------------------------------------------------------------------------
# C7-22
# ---------------------------------------------------------------------------


def _inbox_with_receipt(
    db_session: Session, user: User, *, status: ExpenseReportStatus = ExpenseReportStatus.draft
) -> tuple[ExpenseReport, Expense, Receipt]:
    report = f.make_expense_report(
        db_session,
        owner=user,
        title=RECEIPT_INBOX_TITLE,
        status=status,
        total_amount=Decimal("345.00"),
        pre_submit_flags={
            "flags": [
                {"entry_ref": "UNITED", "flag_type": "policy_violation", "severity": "advisory"},
                {"entry_ref": "Corner Deli", "flag_type": "x", "severity": "blocking"},
            ],
            "evaluated_entry_count": 2,
            "blocking_count": 1,
            "advisory_count": 1,
        },
    )
    entry = f.make_expense(
        db_session,
        report=report,
        amount="320.00",
        category=ExpenseCategory.airfare,
        vendor="UNITED",
        match_source=ExpenseMatchSource.receipt,
    )
    f.make_expense(
        db_session,
        report=report,
        amount="25.00",
        vendor="Corner Deli",
        match_source=ExpenseMatchSource.receipt,
    )
    receipt = f.make_receipt(
        db_session,
        uploaded_by=user,
        ocr_status=OcrStatus.processed,
        extracted_fields={
            "amount": 320.0,
            "currency": "USD",
            "date": "2026-06-10",
            "vendor": "UNITED",
            "expense_type": "airfare",
        },
        expense_id=entry.id,
    )
    return report, entry, receipt


def _enable_inbox_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the persistence.py half of C7-22 having landed (contract flag)."""
    from ai_agents.agents.expense_agent import persistence

    monkeypatch.setattr(persistence, "RELEASES_INBOX_DRAFTS_ON_RELINK", True, raising=False)


def _assemble_and_persist(db_session: Session, user: User) -> list[Any]:
    """Run the real assembly path: ingest → smart_match → draft → assemble → persist."""
    from ai_agents.agents.expense_agent.assemble import assemble_report
    from ai_agents.agents.expense_agent.categorize import build_draft_entries
    from ai_agents.agents.expense_agent.matching import smart_match
    from ai_agents.agents.expense_agent.persistence import persist_expense_report
    from ai_agents.agents.expense_agent.presubmit import build_flags_snapshot

    intent = ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None)
    data = ingest(intent, context=_ctx(user), session=db_session)
    entries = build_draft_entries(smart_match(data))
    report = assemble_report(entries, trip_id=data.trip_id)
    if report.entries:
        persist_expense_report(
            intent=intent,
            report=report,
            flags_snapshot=build_flags_snapshot([], len(report.entries)),
            reflection_outcome_value=None,
            context=_ctx(user),
            session=db_session,
        )
    db_session.flush()
    return entries


def test_ingest_keeps_inbox_receipt_linked_until_persistence_releases(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    """Review finding: without the persistence release, an inbox receipt must
    stay already_linked (HEAD behaviour) so a match cannot orphan the inbox entry."""
    from ai_agents.agents.expense_agent import persistence
    from ai_agents.agents.expense_agent.ingest import persistence_releases_inbox_drafts

    if getattr(persistence, "RELEASES_INBOX_DRAFTS_ON_RELINK", False) is True:
        pytest.skip("persistence.py already releases inbox drafts (C7-22 complete)")
    assert persistence_releases_inbox_drafts() is False
    employee = users_a["employee"]
    _report, _entry, receipt = _inbox_with_receipt(db_session, employee)
    data = ingest(
        ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None),
        context=_ctx(employee),
        session=db_session,
    )
    (fact,) = [r for r in data.receipts if r.receipt_id == str(receipt.id)]
    assert fact.already_linked is True
    assert fact.inbox_expense_id is None


def test_assembly_never_orphans_the_inbox_entry(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    """End-to-end (ingest → match → persist) with the persistence code AS IT IS.

    Review finding: with the ingest change active and persistence unchanged, the
    receipt was re-pointed at the card entry and the inbox entry was left with no
    receipt (and the charge still counted twice). Whatever persistence does, the
    inbox entry must never lose its receipt while it still exists.
    """
    employee = users_a["employee"]
    inbox, entry, receipt = _inbox_with_receipt(db_session, employee)
    f.make_card_transaction(
        db_session,
        user=employee,
        amount="320.00",
        merchant="UNITED",
        txn_date=date(2026, 6, 10),
    )
    _assemble_and_persist(db_session, employee)
    db_session.refresh(receipt)
    inbox_entry = db_session.get(Expense, entry.id)
    if inbox_entry is not None:
        # Inbox entry kept → the receipt must still be linked to it.
        assert receipt.expense_id == entry.id
    else:
        # Inbox entry released → the receipt now backs exactly one entry.
        assert receipt.expense_id is not None


def test_assembly_with_release_has_exactly_one_entry_for_the_charge(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    """Full C7-22 contract — runs once persistence.py releases the inbox draft."""
    from ai_agents.agents.expense_agent import persistence

    if getattr(persistence, "RELEASES_INBOX_DRAFTS_ON_RELINK", False) is not True:
        pytest.skip("persistence.py release (C7-22 cross-group request) not landed yet")
    employee = users_a["employee"]
    _inbox, entry, receipt = _inbox_with_receipt(db_session, employee)
    txn = f.make_card_transaction(
        db_session,
        user=employee,
        amount="320.00",
        merchant="UNITED",
        txn_date=date(2026, 6, 10),
    )
    _assemble_and_persist(db_session, employee)
    db_session.refresh(receipt)
    db_session.refresh(txn)
    assert db_session.get(Expense, entry.id) is None
    assert receipt.expense_id is not None and receipt.expense_id == txn.expense_id
    charges = db_session.scalars(
        sa.select(Expense)
        .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
        .where(ExpenseReport.owner_id == employee.id, Expense.amount == Decimal("320.00"))
    ).all()
    assert len(charges) == 1


def test_ingest_keeps_inbox_linked_receipt_matchable(
    db_session: Session,
    rbac_catalog: None,
    users_a: dict[str, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_inbox_release(monkeypatch)
    employee = users_a["employee"]
    _report, entry, receipt = _inbox_with_receipt(db_session, employee)
    data = ingest(
        ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None),
        context=_ctx(employee),
        session=db_session,
    )
    (fact,) = [r for r in data.receipts if r.receipt_id == str(receipt.id)]
    assert fact.already_linked is False
    assert fact.inbox_expense_id == str(entry.id)


def test_ingest_treats_submitted_inbox_receipt_as_linked(
    db_session: Session, rbac_catalog: None, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    _r, _e, receipt = _inbox_with_receipt(
        db_session, employee, status=ExpenseReportStatus.submitted
    )
    data = ingest(
        ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None),
        context=_ctx(employee),
        session=db_session,
    )
    (fact,) = [r for r in data.receipts if r.receipt_id == str(receipt.id)]
    assert fact.already_linked is True
    assert fact.inbox_expense_id is None


def test_release_inbox_draft_removes_duplicate_and_recomputes(
    db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    report, entry, receipt = _inbox_with_receipt(db_session, employee)
    entry_id = entry.id
    # The prune pass alone (synthetic flags): by default the release then re-runs
    # the inbox's pre-submit check (UAT2 defect D, test_uat2_inbox_duplicate_db).
    assert (
        release_inbox_draft(
            db_session, context=_ctx(employee), receipt=receipt, refresh_flags=False
        )
        is True
    )
    assert receipt.expense_id is None
    assert db_session.get(Expense, entry_id) is None
    db_session.refresh(report)
    assert Decimal(str(report.total_amount)) == Decimal("25.00")
    flags = report.pre_submit_flags
    assert flags is not None
    assert [fl["entry_ref"] for fl in flags["flags"]] == ["Corner Deli"]
    assert flags["evaluated_entry_count"] == 1
    assert flags["blocking_count"] == 1 and flags["advisory_count"] == 0


@pytest.mark.parametrize("status", [ExpenseReportStatus.submitted, ExpenseReportStatus.approved])
def test_release_inbox_draft_never_touches_submitted_report(
    db_session: Session, users_a: dict[str, User], status: ExpenseReportStatus
) -> None:
    employee = users_a["employee"]
    report, entry, receipt = _inbox_with_receipt(db_session, employee, status=status)
    assert release_inbox_draft(db_session, context=_ctx(employee), receipt=receipt) is False
    assert receipt.expense_id == entry.id
    assert db_session.get(Expense, entry.id) is not None
    assert Decimal(str(report.total_amount)) == Decimal("345.00")


def test_release_inbox_draft_refuses_other_users_receipt(
    db_session: Session, users_a: dict[str, User]
) -> None:
    employee, manager = users_a["employee"], users_a["manager"]
    _report, entry, receipt = _inbox_with_receipt(db_session, employee)
    assert release_inbox_draft(db_session, context=_ctx(manager), receipt=receipt) is False
    assert db_session.get(Expense, entry.id) is not None


def test_release_inbox_draft_refuses_non_inbox_entry(
    db_session: Session, users_a: dict[str, User]
) -> None:
    employee = users_a["employee"]
    report = f.make_expense_report(db_session, owner=employee, title="Trip to NYC")
    entry = f.make_expense(
        db_session, report=report, amount="320.00", match_source=ExpenseMatchSource.receipt
    )
    receipt = f.make_receipt(
        db_session, uploaded_by=employee, ocr_status=OcrStatus.processed, expense_id=entry.id
    )
    assert release_inbox_draft(db_session, context=_ctx(employee), receipt=receipt) is False
    assert db_session.get(Expense, entry.id) is not None
