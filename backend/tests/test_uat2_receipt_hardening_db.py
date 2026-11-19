"""UAT2 Issues 1+2 hardening — receipt evidence can't be retyped to launder a double claim.

Old behaviour (2nd governance review + security review of the expense lifecycle):
  * PATCH /receipts accepted corrections on ANY receipt the caller uploaded —
    including the ORIGINAL receipt backing an already-submitted / approved claim,
    one on a returned report and one kept for audit.  The receipt-reuse check
    then compared the RETYPED values, so the claimant could retype the original
    (vendor / date / total) and re-file a copy of the same paper receipt;
  * the reuse check failed OPEN when the vendor / date were user_edited, and a
    user_edited TOTAL was still trusted by both the signature and the SQL
    candidate filter;
  * itemized siblings (one hotel folio split into room + meals) dead-ended: only
    the first sibling could hold the receipt, linking it to the second was
    refused and a re-uploaded copy was a blocking duplicate;
  * the reviewer of a resubmitted returned report could not see that lines had
    been deleted since their return.

Asserted here:
  * relied-on evidence is FROZEN: PATCH /receipts → 409 RECEIPT_LOCKED when the
    receipt backs an entry on a non-draft report or one with approval history, or
    is kept for audit; never-submitted drafts stay correctable;
  * OCR's deterministic reading is snapshotted at upload and never rewritten;
    the reuse check compares snapshots (STRONG → blocking duplicate) and a
    total-only match where fields are typed / unread / legacy is WEAK → an
    advisory flag that always escalates (never silently passes);
  * one receipt row can back several lines of ONE report up to its total.

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
from ai_agents.agents.expense_agent import ExpenseAgent
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.agents.expense_agent.persistence import persist_expense_report
from ai_agents.agents.expense_agent.presubmit import build_flags_snapshot, run_presubmit
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import RoutingDecision, Task
from ai_agents.receipt_pipeline.evidence import OCR_SNAPSHOT_KEY, shared_expense_ids
from backend.app.core.security import create_access_token
from backend.app.modules.expense import service as expense_service
from backend.tests.receipt_images import scenario_of, scenario_upload
from config.constants.permissions import EXPENSE_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
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
EDITED = "user_edited"
DAY = date(2026, 6, 10)
LOCKED = "This receipt is part of a submitted report and can't be changed"

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
    "folio900": {
        **_MEAL,
        "amount": 900.0,
        "date": "2026-06-13",
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
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def put_object(self, key: str, data: bytes, *, content_type: str = "") -> None:  # type: ignore[override]
        self.objects[key] = data

    def get_object(self, key: str) -> bytes:
        return self.objects[key]

    def delete_object(self, key: str) -> None:
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
    categories: list[ExpenseCategory] | None = None,
    status: ExpenseReportStatus = ExpenseReportStatus.draft,
) -> tuple[ExpenseReport, list[Expense]]:
    report = f.make_expense_report(db, owner=owner, title=title, status=status)
    rows = lines if lines is not None else [("60.00", "Corner Deli", DAY)]
    entries = [
        f.make_expense(
            db,
            report=report,
            category=(categories[i] if categories else category),
            amount=amount,
            vendor=vendor,
            expense_date=day,
            match_source=ExpenseMatchSource.manual,
        )
        for i, (amount, vendor, day) in enumerate(rows)
    ]
    report.total_amount = sum((Decimal(str(e.amount)) for e in entries), Decimal("0"))
    db.flush()
    return report, entries


def _legacy_receipt(
    db: Session,
    owner: User,
    entry: Expense | None = None,
    *,
    amount: float = 60.0,
    day: str = "2026-06-10",
    vendor: str = "Corner Deli",
    provenance: dict[str, str] | None = None,
) -> Receipt:
    """A receipt processed BEFORE snapshots existed (no grounding_context)."""
    return f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id if entry is not None else None,
        ocr_status=OcrStatus.processed,
        image_url=f"{owner.organization_id}/{datetime.now(timezone.utc).timestamp()}-{vendor}",
        extracted_fields={"vendor": vendor, "amount": amount, "date": day, "currency": "USD"},
        field_provenance={"vendor": DET, "amount": DET, "date": DET, **(provenance or {})},
    )


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


def _of_type_any(flags: list[dict], entry: Expense) -> list[dict]:
    return [fl for fl in flags if fl.get("entry_id") == str(entry.id)]


def _version(db: Session, report: ExpenseReport) -> int:
    db.expire_all()
    fresh = db.get(ExpenseReport, report.id)
    assert fresh is not None
    return int(fresh.version)


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
        json={"decision": decision, "reason": "Please fix the receipts."},
    )
    assert r.status_code == 200, r.text


def _returned(db: Session, app: Any, users: dict[str, User], report: ExpenseReport) -> None:
    _submit(_client(app, users["employee"]), report)
    _decide(_client(app, users["manager"]), _approval(db, report), "request_changes")
    db.expire_all()


def _upload_to(client: TestClient, entry: Expense, scenario: str) -> Any:
    return client.post(
        f"{API}/receipts",
        params={"expense_id": str(entry.id)},
        files={"file": scenario_upload(scenario)},
    )


def _patch(client: TestClient, receipt_id: Any, field: str, value: Any) -> Any:
    return client.patch(f"{API}/receipts/{receipt_id}", json={"field": field, "value": value})


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


def _set_status(db: Session, report: ExpenseReport, status: ExpenseReportStatus) -> None:
    db.expire_all()
    fresh = db.get(ExpenseReport, report.id)
    assert fresh is not None
    fresh.status = status
    db.flush()


_RETYPES = [("vendor", "SkyPark"), ("date", "2026-06-15"), ("amount", 61.0)]


# ---------------------------------------------------------------------------
# Decision 2 — the machine reading is frozen at OCR time
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_ocr_snapshot_is_frozen_at_upload_and_never_rewritten_by_a_correction(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    _report_, (entry,) = _report(db_session, emp)
    up = _upload_to(client, entry, "meal60")
    assert up.status_code == 201, up.text
    assert up.json()["correction_locked"] is False
    rid = up.json()["id"]
    expected = {"amount": 60.0, "date": "2026-06-10", "vendor": "Corner Deli"}

    db_session.expire_all()
    receipt = db_session.get(Receipt, uuid.UUID(rid))
    assert receipt is not None
    assert (receipt.grounding_context or {})[OCR_SNAPSHOT_KEY] == expected

    # A never-submitted draft's receipt stays correctable (owner decision 1) …
    for field, value in _RETYPES:
        r = _patch(client, rid, field, value)
        assert r.status_code == 200, r.text
    db_session.expire_all()
    receipt = db_session.get(Receipt, uuid.UUID(rid))
    assert receipt is not None
    assert (receipt.extracted_fields or {})["vendor"] == "SkyPark"
    # … but the correction never touches the machine reading.
    assert (receipt.grounding_context or {})[OCR_SNAPSHOT_KEY] == expected


# ---------------------------------------------------------------------------
# Decision 1 — relied-on evidence is frozen (409 RECEIPT_LOCKED)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), _RETYPES)
@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_retyping_the_original_receipt_after_submit_is_refused(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    storage: _MemStorage,
    field: str,
    value: Any,
) -> None:
    """The reviewer's repro: retype the ORIGINAL receipt backing a submitted /
    approved claim, so a later copy no longer reads the same — now 409."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (entry,) = _report(db_session, emp, title="June trip")
    up = _upload_to(client, entry, "meal60")
    assert up.status_code == 201, up.text
    rid = up.json()["id"]
    assert _submit(client, report)["status"] in {"submitted", "approved"}

    r = _patch(client, rid, field, value)

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "RECEIPT_LOCKED"
    assert LOCKED in r.json()["error"]["message"]
    db_session.expire_all()
    receipt = db_session.get(Receipt, uuid.UUID(rid))
    assert receipt is not None
    assert (receipt.extracted_fields or {})["vendor"] == "Corner Deli"
    assert (receipt.extracted_fields or {})["amount"] == 60.0
    assert (receipt.field_provenance or {})[field] == DET
    assert not db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.entity_id == uuid.UUID(rid), AuditLog.action == "receipt.corrected"
        )
    )
    got = client.get(f"{API}/receipts/{rid}")
    assert got.status_code == 200
    assert got.json()["correction_locked"] is True
    assert LOCKED in got.json()["correction_locked_reason"]

    # The same holds on an APPROVED report.
    _set_status(db_session, report, ExpenseReportStatus.approved)
    assert _patch(client, rid, field, value).status_code == 409


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_returned_reports_reviewed_receipt_is_locked_but_a_new_one_is_editable_until_resubmit(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    up = _upload_to(client, meal, "meal60")
    assert up.status_code == 201, up.text
    _returned(db_session, app, users_a, report)  # escalated (the stay has no receipt)
    db_session.expire_all()
    assert db_session.get(ExpenseReport, report.id).status == ExpenseReportStatus.draft  # type: ignore[union-attr]

    r = _patch(client, up.json()["id"], "date", "2026-06-11")

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "RECEIPT_LOCKED"
    # A receipt attached AFTER the return has not been seen by any reviewer yet:
    # the employee may still correct it (and delete a wrong one) until resubmit.
    wrong = _upload_to(client, stay, "folio900")
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["correction_locked"] is False
    assert client.delete(f"{API}/receipts/{wrong.json()['id']}").status_code == 200
    new = _upload_to(client, stay, "folio900")
    assert new.status_code == 201, new.text
    assert new.json()["correction_locked"] is False
    fixed = _patch(client, new.json()["id"], "amount", 750.0)
    assert fixed.status_code == 200, fixed.text
    # The ORIGINAL (reviewed) receipt stays locked and kept for audit.
    assert client.delete(f"{API}/receipts/{up.json()['id']}").json()["error"]["code"] == (
        "RECEIPT_RETAINED"
    )
    # Once resubmitted, the new receipt is evidence too — frozen.
    _submit(client, report)
    assert _patch(client, new.json()["id"], "amount", 700.0).status_code == 409
    db_session.expire_all()
    assert db_session.get(ExpenseReport, report.id).status != ExpenseReportStatus.draft  # type: ignore[union-attr]


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_receipt_kept_for_audit_cannot_be_corrected(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (gone, _stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    up = _upload_to(client, gone, "meal60")
    assert up.status_code == 201, up.text
    _returned(db_session, app, users_a, report)
    deleted = client.delete(f"{API}/expenses/{gone.id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["retained_receipt_ids"] == [up.json()["id"]]

    r = _patch(client, up.json()["id"], "vendor", "SkyPark")

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "RECEIPT_LOCKED"
    got = client.get(f"{API}/receipts/{up.json()['id']}")
    assert got.json()["correction_locked"] is True


# ---------------------------------------------------------------------------
# Decision 2 — the reuse check compares what the receipts READ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), _RETYPES)
@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_retyping_a_copy_on_a_never_submitted_draft_still_flags_it_as_a_duplicate(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    storage: _MemStorage,
    field: str,
    value: Any,
) -> None:
    """A copy of an approved claim's receipt, uploaded on a fresh draft and then
    retyped (vendor / date / TOTAL) — the snapshots still read the same."""
    emp = users_a["employee"]
    client = _client(app, emp)
    first, (a,) = _report(db_session, emp, title="June trip")
    assert _upload_to(client, a, "meal60").status_code == 201
    _set_status(db_session, first, ExpenseReportStatus.approved)
    second, (b,) = _report(
        db_session, emp, [("60.00", "Corner Deli", DAY + timedelta(days=5))], title="Later trip"
    )
    copy = _upload_to(client, b, "meal60")
    assert copy.status_code == 201, copy.text

    r = _patch(client, copy.json()["id"], field, value)
    assert r.status_code == 200, r.text  # a never-submitted draft: allowed

    dup = _of_type(_flags(db_session, second), "possible_duplicate", b)
    assert dup and dup[0]["severity"] == "blocking", _flags(db_session, second)
    assert "'June trip'" in dup[0]["reason"]
    flags, route = _assess(db_session, second, users_a["manager"])
    assert _of_type(flags, "possible_duplicate", b)
    assert route == "escalate"
    assert _submit(client, second)["status"] == "submitted"
    assert _approval(db_session, second).current_status == ApprovalStatus.pending


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_candidates_are_found_by_the_snapshot_total_when_the_current_total_was_typed(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: the SQL prefilter matched only the CURRENT total, so an original whose
    total had been retyped (while it was still a draft) was never a candidate."""
    emp = users_a["employee"]
    client = _client(app, emp)
    first, (a,) = _report(db_session, emp, [("61.00", "Corner Deli", DAY)], title="June trip")
    original = _upload_to(client, a, "meal60")
    assert original.status_code == 201
    assert _patch(client, original.json()["id"], "amount", 61.0).status_code == 200
    _set_status(db_session, first, ExpenseReportStatus.approved)
    second, (b,) = _report(db_session, emp, title="Later trip")

    assert _upload_to(client, b, "meal60").status_code == 201

    dup = _of_type(_flags(db_session, second), "possible_duplicate", b)
    assert dup and dup[0]["severity"] == "blocking", _flags(db_session, second)
    assert "'June trip'" in dup[0]["reason"]


@pytest.mark.parametrize(
    ("original_prov", "copy_receipt"),
    [
        # retyped vendor + dates: nothing machine-read left to compare but the total
        ({"vendor": EDITED, "date": EDITED}, ("SkyPark", "2026-06-19", {"date": EDITED})),
        # the original's TOTAL was typed: the same read receipt, total untrusted
        ({"amount": EDITED}, ("Corner Deli", "2026-06-10", {})),
        # nothing left to confirm or rule out
        ({"date": EDITED}, ("SkyPark", "2026-06-19", {"vendor": EDITED})),
    ],
)
@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_legacy_pair_with_edited_fields_is_an_advisory_reuse_that_escalates(
    app: Any,
    db_session: Session,
    users_a: dict[str, User],
    original_prov: dict[str, str],
    copy_receipt: tuple[str, str, dict[str, str]],
) -> None:
    """Old (security review): a user_edited vendor / date made the reuse check
    fail OPEN; a typed total was trusted.  Without snapshots a total-only match
    is WEAK: advisory (submit allowed) but never auto-approved."""
    emp = users_a["employee"]
    _first, (a,) = _report(db_session, emp, title="June trip", status=ExpenseReportStatus.approved)
    _legacy_receipt(db_session, emp, a, provenance=original_prov)
    second, (b,) = _report(
        db_session, emp, [("60.00", "SkyPark", DAY + timedelta(days=9))], title="Later trip"
    )
    vendor, day, copy_prov = copy_receipt
    _legacy_receipt(db_session, emp, b, vendor=vendor, day=day, provenance=copy_prov)
    expense_service.refresh_presubmit_flags(db_session, second)

    flags = _flags(db_session, second)
    assert not _of_type(flags, "possible_duplicate", b), flags
    weak = [
        fl
        for fl in _of_type(flags, "receipt_mismatch", b)
        if fl["observed"].get("receipt_check") == "possible_reuse"
    ]
    assert weak and weak[0]["severity"] == "advisory", flags
    assert "may be the same receipt" in weak[0]["reason"]
    assert "'June trip'" in weak[0]["reason"]
    _flags_now, route = _assess(db_session, second, users_a["manager"])
    assert route == "escalate"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_misread_copy_retyped_to_the_originals_total_is_a_weak_reuse(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Revision (review): OCR misread the copy's total (59.00), the claimant typed
    the original's 60.00, the supplier read the same and the copy's date was not
    read.  A differing machine total used to rule the match out silently; now it
    is a WEAK match naming the other report, and the text says the TOTAL was typed."""
    emp = users_a["employee"]
    _first, (a,) = _report(db_session, emp, title="June trip", status=ExpenseReportStatus.approved)
    _legacy_receipt(db_session, emp, a)
    second, (b,) = _report(
        db_session, emp, [("60.00", "Corner Deli", DAY + timedelta(days=9))], title="Later trip"
    )
    f.make_receipt(
        db_session,
        uploaded_by=emp,
        expense_id=b.id,
        ocr_status=OcrStatus.processed,
        image_url=f"{emp.organization_id}/misread-copy",
        extracted_fields={"vendor": "Corner Deli", "amount": 60.0, "currency": "USD"},
        field_provenance={"vendor": DET, "amount": EDITED},
        grounding_context={OCR_SNAPSHOT_KEY: {"amount": 59.0, "vendor": "Corner Deli"}},
    )
    expense_service.refresh_presubmit_flags(db_session, second)

    weak = [
        fl
        for fl in _of_type(_flags(db_session, second), "receipt_mismatch", b)
        if fl["observed"].get("receipt_check") == "possible_reuse"
    ]
    assert weak and weak[0]["severity"] == "advisory", _flags(db_session, second)
    assert "'June trip'" in weak[0]["reason"]
    assert "a total was typed by hand or not read" in weak[0]["reason"]
    _flags_now, route = _assess(db_session, second, users_a["manager"])
    assert route == "escalate"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_weak_reuse_flag_on_a_peer_draft_clears_when_the_other_side_is_deleted(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Revision (review): refresh_duplicate_peers re-checked only peers carrying a
    blocking possible_duplicate, so a WEAK possible_reuse flag naming a line that
    was then deleted stayed stale on the peer draft."""
    emp = users_a["employee"]
    client = _client(app, emp)
    first, (a,) = _report(db_session, emp, title="Draft A")
    _legacy_receipt(db_session, emp, a, provenance={"vendor": EDITED, "date": EDITED})
    second, (b,) = _report(
        db_session, emp, [("60.00", "SkyPark", DAY + timedelta(days=9))], title="Draft B"
    )
    _legacy_receipt(db_session, emp, b, vendor="SkyPark", day="2026-06-19")
    expense_service.refresh_presubmit_flags(db_session, second)

    def weak_on_b() -> list[dict]:
        return [
            fl
            for fl in _of_type(_flags(db_session, second), "receipt_mismatch", b)
            if fl["observed"].get("receipt_check") == "possible_reuse"
        ]

    assert weak_on_b(), _flags(db_session, second)

    assert client.delete(f"{API}/expenses/{a.id}").status_code == 200

    assert not weak_on_b(), _flags(db_session, second)


@pytest.mark.parametrize("original_prov", [{}, {"amount": EDITED}])
@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_same_total_receipts_that_read_differently_are_not_flagged(
    app: Any, db_session: Session, users_a: dict[str, User], original_prov: dict[str, str]
) -> None:
    """Noise control: two receipts whose machine-read dates differ (two real $60
    lunches) are two purchases — even when one total was typed.  Only the
    machine reading can rule a match out; a typed value never can."""
    emp = users_a["employee"]
    _first, (a,) = _report(db_session, emp, title="June trip", status=ExpenseReportStatus.approved)
    _legacy_receipt(db_session, emp, a, provenance=original_prov)
    second, (b,) = _report(
        db_session, emp, [("60.00", "Corner Deli", DAY + timedelta(days=2))], title="Later"
    )
    _legacy_receipt(db_session, emp, b, day="2026-06-12")
    expense_service.refresh_presubmit_flags(db_session, second)

    flags = _flags(db_session, second)
    assert not _of_type(flags, "possible_duplicate", b), flags
    assert not _of_type(flags, "receipt_mismatch", b), flags


# ---------------------------------------------------------------------------
# Decision 4 — one receipt row can back itemized siblings on ONE report
# ---------------------------------------------------------------------------


def _folio_report(db: Session, emp: User, meals: str = "150.00") -> tuple[Any, Expense, Expense]:
    report, (stay, food) = _report(
        db,
        emp,
        [("750.00", "Hilton Chicago", DAY), (meals, "Hilton Chicago", DAY)],
        title="Chicago",
        categories=[ExpenseCategory.lodging, ExpenseCategory.meals],
    )
    return report, stay, food


def _link(client: TestClient, entry: Expense, receipt: Receipt) -> Any:
    return client.post(f"{API}/expenses/{entry.id}/receipts", json={"receipt_id": str(receipt.id)})


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_one_folio_can_back_its_itemized_siblings_on_one_report(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: linking the folio to the second sibling 409'd (RECEIPT_ALREADY_LINKED)
    and uploading a copy was a blocking duplicate — a dead end."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )

    listed = client.get(f"{API}/receipts", params={"for_expense_id": str(food.id)})
    assert listed.status_code == 200, listed.text
    assert str(folio.id) in {r["id"] for r in listed.json()}

    r = _link(client, food, folio)

    assert r.status_code == 200, r.text
    assert r.json()["entry"]["receipt_ids"] == [str(folio.id)]
    db_session.expire_all()
    shared = db_session.get(Receipt, folio.id)
    assert shared is not None and shared.expense_id == stay.id
    assert shared_expense_ids(shared) == [food.id]
    flags = _flags(db_session, report)
    assert not _of_type(flags, "missing_receipt"), flags
    assert not _of_type(flags, "possible_duplicate"), flags
    (audit,) = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "expense.receipt_attached", AuditLog.entity_id == food.id
        )
    ).all()
    assert audit.after_state["shared_with_expense_id"] == str(stay.id)  # type: ignore[index]
    entries = client.get(f"{API}/expense-reports/{report.id}/entries").json()
    assert {e["id"]: e["receipt_id"] for e in entries} == {
        str(stay.id): str(folio.id),
        str(food.id): str(folio.id),
    }


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_shared_receipt_is_refused_beyond_its_total_or_across_reports(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    _report_, stay, food = _folio_report(db_session, emp, meals="200.00")  # 950 > 900
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    r = _link(client, food, folio)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_ALREADY_LINKED"
    assert "950.00" in r.json()["error"]["message"]

    _other, (elsewhere,) = _report(
        db_session, emp, [("100.00", "Hilton Chicago", DAY)], title="Other"
    )
    r = _link(client, elsewhere, folio)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECEIPT_ALREADY_LINKED"
    assert "another report" in r.json()["error"]["message"]

    typed = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton", provenance={"amount": EDITED}
    )
    _r2, (meal2,) = _report(db_session, emp, [("50.00", "Hilton", DAY)], title="x")
    # A typed total can't be split (nothing machine-read to check the split against).
    r = _link(client, meal2, typed)
    assert r.status_code == 409


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_split_that_stops_fitting_or_crosses_reports_is_flagged(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    assert _link(client, food, folio).status_code == 200

    # Raising a sibling above the folio total: the receipt is used beyond it.
    e = client.patch(f"{API}/expenses/{food.id}", json={"amount": "300.00"})
    assert e.status_code == 200, e.text
    dup = _of_type(_flags(db_session, report), "possible_duplicate", food)
    assert dup and dup[0]["severity"] == "blocking"
    assert "more than the receipt's total 900.00" in dup[0]["reason"]
    assert dup[0]["observed"]["duplicate_check"] == "shared receipt split"

    # Moving one sibling alone would split the receipt across reports → refused.
    target, _ = _report(db_session, emp, [], title="Target")
    moved = client.post(
        f"{API}/expense-reports/{report.id}/entries/move",
        json={"entry_ids": [str(food.id)], "target_report_id": str(target.id)},
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "RECEIPT_SHARED"

    # Data that crosses reports anyway (legacy / out-of-band) is still caught.
    db_session.execute(sa.update(Expense).where(Expense.id == food.id).values(report_id=target.id))
    db_session.flush()
    expense_service.refresh_presubmit_flags(db_session, db_session.get(ExpenseReport, target.id))  # type: ignore[arg-type]
    dup = _of_type(_flags(db_session, target), "possible_duplicate", food)
    assert dup and "also backs a line on report 'Chicago'" in dup[0]["reason"], _flags(
        db_session, target
    )


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_copy_of_a_shared_folio_on_another_report_is_a_blocking_duplicate(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    _report_, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-13"
    )
    assert _link(client, food, folio).status_code == 200
    other, (again,) = _report(
        db_session, emp, [("900.00", "Hilton", DAY + timedelta(days=20))], title="Refile"
    )

    assert _upload_to(client, again, "folio900").status_code == 201

    dup = _of_type(_flags(db_session, other), "possible_duplicate", again)
    assert dup and dup[0]["severity"] == "blocking"
    assert "'Chicago'" in dup[0]["reason"]


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_removing_or_deleting_one_sibling_keeps_the_receipt_for_the_other(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    assert _link(client, food, folio).status_code == 200

    # "Remove receipt" on the meals line lets go of it there only.
    r = client.delete(f"{API}/receipts/{folio.id}", params={"from_expense_id": str(food.id)})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unshared" and r.json()["expense_id"] == str(food.id)
    db_session.expire_all()
    kept = db_session.get(Receipt, folio.id)
    assert kept is not None and kept.expense_id == stay.id and not shared_expense_ids(kept)
    assert _of_type(_flags(db_session, report), "missing_receipt", food)

    # Revision (review): a stale / wrong line id is refused — it never falls
    # through to deleting the receipt from every line it backs.
    assert _link(client, food, folio).status_code == 200
    stale = client.delete(
        f"{API}/receipts/{folio.id}", params={"from_expense_id": str(uuid.uuid4())}
    )
    assert stale.status_code == 409, stale.text
    db_session.expire_all()
    still = db_session.get(Receipt, folio.id)
    assert still is not None and still.expense_id == stay.id
    assert shared_expense_ids(still) == [food.id]
    assert folio.image_url not in storage.deleted
    r = client.delete(f"{API}/receipts/{folio.id}", params={"from_expense_id": str(food.id)})
    assert r.status_code == 200, r.text

    # Share again, then delete the PRIMARY line: the receipt moves to the sibling.
    assert _link(client, food, folio).status_code == 200
    d = client.delete(f"{API}/expenses/{stay.id}")
    assert d.status_code == 200, d.text
    assert d.json()["deleted_receipt_ids"] == []
    db_session.expire_all()
    kept = db_session.get(Receipt, folio.id)
    assert kept is not None and kept.expense_id == food.id
    assert folio.image_url not in storage.deleted
    assert not _of_type(_flags(db_session, report), "missing_receipt", food)


def _sibling(folio: Receipt, group: str, amount: float, category: str, primary: bool) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-06-10",
        vendor="Hilton Chicago",
        description=None,
        status="matched",
        match_source="receipt",
        trip_segment_id=None,
        is_itemized=True,
        primary_receipt_id=str(folio.id) if primary else None,
        source_card_txn_id=None,
        sibling_group_id=group,
    )


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_expense_agent_shares_an_itemized_folio_with_its_siblings(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """categorize gives only the first sibling primary_receipt_id; persistence
    uses the explicit sibling marker to back the others with the same row.

    Revision (governance review): the agent computes its pre-submit flags on the
    DraftReport BEFORE persisting — when only the first sibling held the folio —
    and persisted that snapshot, so the second sibling kept a STORED blocking
    missing_receipt flag (Submit disabled) although it was backed.  Persisted
    here with the REAL agent-time snapshot and asserted with no manual refresh.
    """
    emp = users_a["employee"]
    folio = _legacy_receipt(db_session, emp, amount=900.0, vendor="Hilton Chicago")
    group = str(uuid.uuid4())
    ctx = AgentContext(
        organization_id=emp.organization_id,
        granted_permission_keys=frozenset(),
        user_id=emp.id,
    )
    draft = DraftReport(
        entries=[
            _sibling(folio, group, 750.0, "lodging", True),
            _sibling(folio, group, 150.0, "meals", False),
        ],
        total_amount=900.0,
        currency="USD",
        trip_id=None,
        title="Agent report",
    )
    agent_time, _ = run_presubmit(draft, context=ctx, session=db_session)
    # The precondition the reviewer reproduced: at agent time the meals sibling
    # has no receipt, so its flag is a blocking missing_receipt.
    assert [
        fl for fl in agent_time if fl.flag_type == "missing_receipt" and fl.severity == "blocking"
    ], agent_time

    report, notes = persist_expense_report(
        intent=ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None),
        report=draft,
        flags_snapshot=build_flags_snapshot(agent_time, len(draft.entries)),
        reflection_outcome_value=None,
        context=ctx,
        session=db_session,
    )
    entries = db_session.scalars(
        sa.select(Expense).where(Expense.report_id == report.id).order_by(Expense.amount)
    ).all()
    food, stay = entries
    assert not notes, notes
    db_session.expire_all()
    shared = db_session.get(Receipt, folio.id)
    assert shared is not None and shared.expense_id == stay.id
    assert shared_expense_ids(shared) == [food.id]
    flags = _flags(db_session, report)  # STORED flags — no manual refresh
    assert not _of_type(flags, "missing_receipt"), flags
    assert not _of_type(flags, "possible_duplicate"), flags
    # Each line's flagged status follows its own STORED blocking flags.
    for line in (food, stay):
        fresh = db_session.get(Expense, line.id)
        blocking = [fl for fl in _of_type_any(flags, line) if fl.get("severity") == "blocking"]
        assert fresh is not None and (fresh.status == ExpenseStatus.flagged) == bool(blocking)


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_expense_agent_run_stores_no_dead_end_flag_on_an_itemized_folio(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """End to end through ExpenseAgent.handle: a real itemized folio (line items)
    becomes two sibling lines, both backed by the one folio row, and the STORED
    flags (what the submit gate reads) carry no missing_receipt / duplicate."""
    emp = users_a["employee"]
    folio = f.make_receipt(
        db_session,
        uploaded_by=emp,
        ocr_status=OcrStatus.processed,
        image_url=f"{emp.organization_id}/folio-agent",
        extracted_fields={
            "vendor": "Hilton Chicago",
            "amount": 900.0,
            "date": "2026-06-10",
            "currency": "USD",
            "line_items": [
                {"description": "Room charge", "amount": 750.0},
                {"description": "Restaurant dinner", "amount": 150.0},
            ],
        },
        field_provenance={"vendor": DET, "amount": DET, "date": DET},
    )
    ctx = AgentContext(
        organization_id=emp.organization_id,
        granted_permission_keys=frozenset({EXPENSE_CREATE_OWN}),
        user_id=emp.id,
        correlation_id="uat2-hardening",
    )
    decision = RoutingDecision(
        target_agent=AiAgent.expense,
        route=AiRoute.deterministic,
        task_type="expense_report",
        confidence=0.95,
        rationale="db-test",
    )

    result = ExpenseAgent(provider=LocalLlmProvider()).handle(
        Task(context_type="expense", payload={}), decision, context=ctx, session=db_session
    )

    report_id = result.output.get("report_id")
    assert report_id, result.output
    report = db_session.get(ExpenseReport, uuid.UUID(report_id))
    assert report is not None
    entries = db_session.scalars(
        sa.select(Expense).where(Expense.report_id == report.id).order_by(Expense.amount)
    ).all()
    assert len(entries) == 2, entries
    food, stay = entries
    db_session.expire_all()
    shared = db_session.get(Receipt, folio.id)
    assert shared is not None and shared.expense_id == stay.id
    assert shared_expense_ids(shared) == [food.id]
    flags = _flags(db_session, report)
    assert not _of_type(flags, "missing_receipt"), flags
    assert not _of_type(flags, "possible_duplicate"), flags
    # The agent's own summary reflects the stored flags, not the stale ones.
    stored_blocking = sum(1 for fl in flags if fl.get("severity") == "blocking")
    assert result.output["blocking_count"] == stored_blocking


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_attach_picker_offers_a_folio_already_split_onto_the_line(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """A stale flag must be clearable from the flag's own "Attach receipt" action:
    the folio already split onto the line is offered (marked as such) and
    re-attaching it is idempotent — it re-runs the check, no second share."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    assert _link(client, food, folio).status_code == 200

    listed = client.get(f"{API}/receipts", params={"for_expense_id": str(food.id)})
    assert listed.status_code == 200, listed.text
    (offered,) = [r for r in listed.json() if r["id"] == str(folio.id)]
    assert offered["shared_expense_ids"] == [str(food.id)]

    again = _link(client, food, folio)

    assert again.status_code == 200, again.text
    db_session.expire_all()
    shared = db_session.get(Receipt, folio.id)
    assert shared is not None and shared_expense_ids(shared) == [food.id]
    assert not _of_type(_flags(db_session, report), "missing_receipt"), _flags(db_session, report)


# ---------------------------------------------------------------------------
# Decision 5 — the reviewer sees lines removed since their return
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_reviewer_sees_how_many_lines_were_removed_since_the_return(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (gone, _stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("45.00", "Bistro", DAY + timedelta(days=1))],
        title="Returned trip",
    )
    flags, _route = _assess(db_session, report, users_a["manager"])
    assert not [fl for fl in flags if fl.get("flag_type") == "lines_removed_since_return"]
    _returned(db_session, app, users_a, report)
    assert client.delete(f"{API}/expenses/{gone.id}").status_code == 200

    flags, route = _assess(db_session, report, users_a["manager"])

    (note,) = [fl for fl in flags if fl.get("flag_type") == "lines_removed_since_return"]
    assert note["reason"].startswith("1 line removed since this report was returned")
    assert route == "escalate"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_discarding_a_drafted_line_via_receipt_delete_is_refused_once_reviewed(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Lock-scope review: DELETE /receipts?discard_drafted_entry=true must never delete
    a line of a report with approval history (only DELETE /expenses may, behind its
    review-history guard).  The unreviewed receipt itself is still deletable."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    stay.match_source = ExpenseMatchSource.receipt  # looks like a receipt-drafted line
    db_session.flush()
    assert _upload_to(client, meal, "meal60").status_code == 201
    _returned(db_session, app, users_a, report)

    new = _upload_to(client, stay, "folio900")
    assert new.status_code == 201, new.text
    r = client.delete(
        f"{API}/receipts/{new.json()['id']}", params={"discard_drafted_entry": "true"}
    )

    assert r.status_code == 200, r.text
    assert r.json().get("outcome") != "deleted_with_entry"
    db_session.expire_all()
    assert db_session.get(Expense, stay.id) is not None  # the reviewed line survives
    assert db_session.get(Receipt, uuid.UUID(new.json()["id"])) is None


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_deleting_a_returned_line_keeps_only_receipts_the_reviewer_saw(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay, extra) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY), ("8.75", "Cafe", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging, ExpenseCategory.meals],
    )
    seen = _upload_to(client, meal, "meal60")
    assert seen.status_code == 201
    _returned(db_session, app, users_a, report)
    unseen = _upload_to(client, extra, "meal60")
    assert unseen.status_code == 201, unseen.text

    gone = client.delete(f"{API}/expenses/{extra.id}")

    assert gone.status_code == 200, gone.text
    assert gone.json()["retained_receipt_ids"] == []  # nobody saw it: deleted, not kept
    db_session.expire_all()
    assert db_session.get(Receipt, uuid.UUID(unseen.json()["id"])) is None
    kept = client.delete(f"{API}/expenses/{meal.id}")
    if kept.status_code == 200:  # deletable when the return froze a snapshot
        assert kept.json()["retained_receipt_ids"] == [seen.json()["id"]]


# ---------------------------------------------------------------------------
# UAT2 browser retest (defect B) — a wrong receipt attached after a return can
# be removed: the entry says per receipt whether it may go (same rule as DELETE
# /receipts), and a LEGACY return step (no frozen receipt ids) falls back to
# time: a receipt first attached to the report after the return is unreviewed.
# ---------------------------------------------------------------------------

_T_RETURN = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _removal(client: TestClient, report: ExpenseReport) -> dict[str, dict[str, Any]]:
    entries = client.get(f"{API}/expense-reports/{report.id}/entries")
    assert entries.status_code == 200, entries.text
    return {r["receipt_id"]: r for e in entries.json() for r in e["receipt_removal"]}


def _return_step(db: Session, report: ExpenseReport) -> ApprovalStep:
    step = db.scalars(
        sa.select(ApprovalStep)
        .where(
            ApprovalStep.approval_id == _approval(db, report).id,
            ApprovalStep.decision.in_([ApprovalDecision.request_changes, ApprovalDecision.reject]),
        )
        .order_by(ApprovalStep.step_order.desc())
    ).first()
    assert step is not None
    return step


def _make_legacy_return(
    db: Session, report: ExpenseReport, *, snapshot: dict | None = None
) -> ApprovalStep:
    """Turn the report's return step into a pre-0014 one (no receipt ids) at _T_RETURN."""
    step = _return_step(db, report)
    step.subject_snapshot = snapshot
    step.decided_at = _T_RETURN
    db.flush()
    return step


def _attached_at(db: Session, receipt_id: Any, when: datetime) -> None:
    """Pin the receipt's attach audit rows' time (one test transaction freezes now())."""
    db.execute(
        sa.update(AuditLog)
        .where(
            AuditLog.action == "expense.receipt_attached",
            AuditLog.after_state["receipt_id"].astext == str(receipt_id),
        )
        .values(created_at=when)
    )
    db.flush()


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_entries_say_per_receipt_whether_it_can_be_removed(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: the editor hid Remove for EVERY receipt of a report with approval
    history, though the server allows deleting one attached after the return."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    seen = _upload_to(client, meal, "meal60")
    assert seen.status_code == 201, seen.text
    draft_state = _removal(client, report)[seen.json()["id"]]
    assert draft_state["removable"] is True  # never-submitted draft
    assert draft_state["kept_for_audit"] is False
    _returned(db_session, app, users_a, report)
    wrong = _upload_to(client, stay, "folio900")
    assert wrong.status_code == 201, wrong.text

    states = _removal(client, report)

    assert states[seen.json()["id"]]["removable"] is False
    assert states[seen.json()["id"]]["kept_for_audit"] is True
    assert "kept for audit" in states[seen.json()["id"]]["removal_blocked_reason"]
    assert states[wrong.json()["id"]]["removable"] is True
    assert states[wrong.json()["id"]]["kept_for_audit"] is False
    assert states[wrong.json()["id"]]["removal_blocked_reason"] is None
    # A reader who is not the owner is never offered a Remove.
    as_manager = _removal(_client(app, users_a["manager"]), report)
    assert not any(s["removable"] for s in as_manager.values())
    # The flags match what DELETE does.
    assert client.delete(f"{API}/receipts/{seen.json()['id']}").status_code == 409
    gone = client.delete(
        f"{API}/receipts/{wrong.json()['id']}", params={"from_expense_id": str(stay.id)}
    )
    assert gone.status_code == 200, gone.text


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_legacy_return_lets_the_owner_remove_a_receipt_attached_after_it(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: a return step from before receipt snapshots (seeded Q3) failed closed for
    EVERY receipt, so a wrong receipt attached after it could never be removed."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    seen = _upload_to(client, meal, "meal60")
    assert seen.status_code == 201, seen.text
    _returned(db_session, app, users_a, report)
    _make_legacy_return(db_session, report)
    _attached_at(db_session, seen.json()["id"], _T_RETURN - timedelta(hours=1))
    wrong = _upload_to(client, meal, "folio900")
    assert wrong.status_code == 201, wrong.text
    _attached_at(db_session, wrong.json()["id"], _T_RETURN + timedelta(hours=1))

    states = _removal(client, report)
    assert states[wrong.json()["id"]]["removable"] is True
    assert states[seen.json()["id"]]["removable"] is False
    assert states[seen.json()["id"]]["kept_for_audit"] is True
    got = client.get(f"{API}/receipts/{wrong.json()['id']}")
    assert got.json()["correction_locked"] is False

    kept = client.delete(
        f"{API}/receipts/{seen.json()['id']}", params={"from_expense_id": str(meal.id)}
    )
    assert kept.status_code == 409, kept.text
    assert kept.json()["error"]["code"] == "RECEIPT_RETAINED"
    r = client.delete(
        f"{API}/receipts/{wrong.json()['id']}", params={"from_expense_id": str(meal.id)}
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Receipt, uuid.UUID(wrong.json()["id"])) is None
    assert db_session.get(Receipt, uuid.UUID(seen.json()["id"])) is not None
    assert db_session.get(Expense, meal.id) is not None  # the reviewed line stays


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_legacy_snapshot_without_receipt_ids_also_falls_back_to_time(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("60.00", "Corner Deli", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    assert _upload_to(client, meal, "meal60").status_code == 201
    _returned(db_session, app, users_a, report)
    _make_legacy_return(db_session, report, snapshot={"entries": []})
    wrong = _upload_to(client, stay, "folio900")
    assert wrong.status_code == 201, wrong.text
    _attached_at(db_session, wrong.json()["id"], _T_RETURN + timedelta(minutes=5))

    assert _removal(client, report)[wrong.json()["id"]]["removable"] is True
    assert client.delete(f"{API}/receipts/{wrong.json()['id']}").status_code == 200


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_a_legacy_return_never_makes_a_reviewed_receipt_look_unreviewed(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """The time fallback is fail-closed: a receipt on the report at the return
    (seeded, no attach row) stays reviewed even when it is SHARED with another
    line after the return (a newer attach row); an attach older than the return,
    or a return step with no decision time, keeps it reviewed too."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    _returned(db_session, app, users_a, report)
    step = _make_legacy_return(db_session, report)
    shared = _link(client, food, folio)
    assert shared.status_code == 200, shared.text
    _attached_at(db_session, folio.id, _T_RETURN + timedelta(hours=1))

    states = _removal(client, report)
    assert states[str(folio.id)]["removable"] is False
    assert states[str(folio.id)]["kept_for_audit"] is True
    for params in ({"from_expense_id": str(food.id)}, {"from_expense_id": str(stay.id)}, {}):
        r = client.delete(f"{API}/receipts/{folio.id}", params=params)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "RECEIPT_RETAINED"
    assert _patch(client, folio.id, "amount", 800.0).status_code == 409

    # A receipt attached BEFORE the (legacy) return was seen: kept.
    old = _legacy_receipt(db_session, emp, None, amount=60.0)
    assert _link(client, food, old).status_code == 200
    _attached_at(db_session, old.id, _T_RETURN - timedelta(minutes=1))
    assert _removal(client, report)[str(old.id)]["removable"] is False
    assert client.delete(f"{API}/receipts/{old.id}").status_code == 409

    # No decision time on the legacy step: nothing can be proven newer — fail closed.
    late = _legacy_receipt(db_session, emp, None, amount=61.0)
    assert _link(client, stay, late).status_code == 200
    _attached_at(db_session, late.id, _T_RETURN + timedelta(hours=2))
    assert _removal(client, report)[str(late.id)]["removable"] is True
    step.decided_at = None
    db_session.flush()
    assert _removal(client, report)[str(late.id)]["removable"] is False
    assert client.delete(f"{API}/receipts/{late.id}").status_code == 409


# ---------------------------------------------------------------------------
# UAT2 defect F — the approver sees EVERY receipt backing a line
# ---------------------------------------------------------------------------

_SCENARIOS["coffee875"] = {
    **_MEAL,
    "amount": 8.75,
    "vendor": "Blue Bottle Coffee",
    "total_legible": True,
}
_SCENARIOS["meal205"] = {
    **_MEAL,
    "amount": 205.5,
    "vendor": "Gramercy Tavern",
    "total_legible": True,
}


def _created_at(db: Session, receipt_id: Any, when: datetime) -> None:
    """Pin a receipt's created_at (one test transaction freezes now())."""
    db.execute(
        sa.update(Receipt).where(Receipt.id == uuid.UUID(str(receipt_id))).values(created_at=when)
    )
    db.flush()


def _approval_entries(client: TestClient, report: ExpenseReport, db: Session) -> list[dict]:
    r = client.get(f"{API}/approvals/{_approval(db, report).id}")
    assert r.status_code == 200, r.text
    return list(r.json()["expense_report"]["entries"])


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_approver_sees_every_receipt_on_a_line_with_its_machine_reading(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    """Old: the approval detail carried only the EARLIEST receipt per entry — a
    wrong $8.75 coffee receipt hid the correct $205.50 one attached after the
    return, so the approver could not see what actually supports the claim."""
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal, stay) = _report(
        db_session,
        emp,
        [("205.50", "Gramercy Tavern", DAY), ("750.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.meals, ExpenseCategory.lodging],
    )
    wrong = _upload_to(client, meal, "coffee875")
    assert wrong.status_code == 201, wrong.text
    _returned(db_session, app, users_a, report)
    right = _upload_to(client, meal, "meal205")
    assert right.status_code == 201, right.text
    _created_at(db_session, wrong.json()["id"], _T_RETURN - timedelta(days=1))
    _created_at(db_session, right.json()["id"], _T_RETURN + timedelta(hours=1))
    _submit(client, report)

    entries = _approval_entries(_client(app, users_a["manager"]), report, db_session)

    by_id = {e["id"]: e for e in entries}
    line = by_id[str(meal.id)]
    # Backward compatible: receipt_id is still the earliest receipt.
    assert line["receipt_id"] == wrong.json()["id"]
    assert line["receipt_ids"] == [wrong.json()["id"], right.json()["id"]]
    first, second = line["receipts"]
    assert first["receipt_id"] == wrong.json()["id"]
    assert first["link"] == "direct"
    assert first["vendor"] == "Blue Bottle Coffee"
    assert first["receipt_date"] == "2026-06-10"
    assert Decimal(first["amount"]) == Decimal("8.75")
    assert first["currency"] == "USD"
    assert first["covers_line"] is False
    assert first["attached_after_return"] is False
    assert second["receipt_id"] == right.json()["id"]
    assert second["vendor"] == "Gramercy Tavern"
    assert Decimal(second["amount"]) == Decimal("205.50")
    assert second["covers_line"] is True
    assert second["attached_after_return"] is True
    assert by_id[str(stay.id)]["receipts"] == []
    # The owner's own entries view carries the same list.
    own = client.get(f"{API}/expense-reports/{report.id}/entries").json()
    assert [r["receipt_id"] for r in {e["id"]: e for e in own}[str(meal.id)]["receipts"]] == [
        wrong.json()["id"],
        right.json()["id"],
    ]


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_approver_sees_a_folio_shared_onto_an_itemized_sibling(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, stay, food = _folio_report(db_session, emp)
    folio = _legacy_receipt(
        db_session, emp, stay, amount=900.0, vendor="Hilton Chicago", day="2026-06-10"
    )
    typed = _legacy_receipt(
        db_session, emp, food, amount=150.0, vendor="Room Service", provenance={"amount": EDITED}
    )
    _created_at(db_session, typed.id, _T_RETURN - timedelta(days=1))
    _created_at(db_session, folio.id, _T_RETURN)
    assert _link(client, food, folio).status_code == 200
    _submit(client, report)

    entries = {
        e["id"]: e for e in _approval_entries(_client(app, users_a["manager"]), report, db_session)
    }

    (own,) = entries[str(stay.id)]["receipts"]
    assert own["receipt_id"] == str(folio.id)
    assert own["link"] == "direct"
    assert own["covers_line"] is True
    direct, shared = entries[str(food.id)]["receipts"]
    assert direct["receipt_id"] == str(typed.id)
    assert direct["link"] == "direct"
    # A typed total is not a machine reading: no amount, coverage unknown.
    assert direct["amount"] is None
    assert direct["covers_line"] is None
    assert direct["vendor"] == "Room Service"
    assert shared["receipt_id"] == str(folio.id)
    assert shared["link"] == "shared"
    assert Decimal(shared["amount"]) == Decimal("900.00")
    assert shared["covers_line"] is True
    assert shared["attached_after_return"] is False  # never returned


# ---------------------------------------------------------------------------
# UAT2 browser retest (defect H) — the entry editor listed receipts as
# "Receipt 1/2/3" with nothing to tell them apart.  The editor now reads F's
# per-receipt ``receipts`` (machine reading) next to B's ``receipt_removal``
# from the owner's entries view; these pin what the editor relies on there.
# ---------------------------------------------------------------------------


def _owner_entries(client: TestClient, report: ExpenseReport) -> dict[str, dict[str, Any]]:
    entries = client.get(f"{API}/expense-reports/{report.id}/entries")
    assert entries.status_code == 200, entries.text
    return {e["id"]: e for e in entries.json()}


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_owner_editor_view_describes_each_receipt_by_its_machine_reading(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (meal,) = _report(db_session, emp)
    read = _upload_to(client, meal, "meal60")
    assert read.status_code == 201, read.text
    # A typed vendor never hides what the paper says.
    assert _patch(client, read.json()["id"], "vendor", "SkyPark").status_code == 200
    legacy = _legacy_receipt(db_session, emp, meal, amount=42.5, vendor="Old Diner")
    partial = f.make_receipt(
        db_session,
        uploaded_by=emp,
        expense_id=meal.id,
        ocr_status=OcrStatus.processed,
        image_url=f"{emp.organization_id}/partial.jpg",
        extracted_fields={
            "vendor": "Typed Cafe",
            "amount": 12,
            "date": "2026-06-11",
            "currency": "eur",
        },
        field_provenance={"vendor": EDITED, "amount": EDITED, "date": DET},
        # The snapshot froze only the date (vendor / total were not machine-read).
        grounding_context={OCR_SNAPSHOT_KEY: {"date": "2026-06-09"}},
    )

    line = _owner_entries(client, report)[str(meal.id)]

    # One row per receipt, in receipt_ids order, each with its removal state.
    assert [r["receipt_id"] for r in line["receipts"]] == line["receipt_ids"]
    assert {r["receipt_id"] for r in line["receipt_removal"]} == set(line["receipt_ids"])
    by_id = {r["receipt_id"]: r for r in line["receipts"]}
    snapped = by_id[read.json()["id"]]
    assert (snapped["vendor"], snapped["receipt_date"]) == ("Corner Deli", "2026-06-10")
    assert Decimal(snapped["amount"]) == Decimal("60.00")
    assert snapped["currency"] == "USD"
    old = by_id[str(legacy.id)]
    assert (old["vendor"], old["receipt_date"]) == ("Old Diner", "2026-06-10")
    assert Decimal(old["amount"]) == Decimal("42.50")
    # Typed values are never shown as what the receipt reads.
    half = by_id[str(partial.id)]
    assert (half["vendor"], half["receipt_date"], half["amount"]) == (None, "2026-06-09", None)
    assert half["currency"] == "EUR"


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy")
def test_the_owner_editor_view_tolerates_unreadable_fields_on_a_shared_receipt(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    report, (stay, meal) = _report(
        db_session,
        emp,
        [("450.00", "Hilton Chicago", DAY), ("450.00", "Hilton Chicago", DAY)],
        categories=[ExpenseCategory.lodging, ExpenseCategory.meals],
    )
    blank = f.make_receipt(
        db_session,
        uploaded_by=emp,
        expense_id=stay.id,
        ocr_status=OcrStatus.failed,
        image_url=f"{emp.organization_id}/blank.jpg",
        extracted_fields={"vendor": "  ", "amount": "n/a", "date": "soon"},
        # Shared onto the sibling line (an itemized folio).
        grounding_context={"shared_expense_ids": [str(meal.id)]},
    )

    entries = _owner_entries(client, report)

    for eid, link in ((stay.id, "direct"), (meal.id, "shared")):
        (row,) = entries[str(eid)]["receipts"]
        assert row["receipt_id"] == str(blank.id)
        assert row["link"] == link
        assert (row["vendor"], row["receipt_date"], row["amount"], row["currency"]) == (
            None,
            None,
            None,
            None,
        )
        assert row["covers_line"] is None
