"""UAT2 defect D — a re-upload into the Receipt Inbox is flagged as a duplicate at once.

Old behaviour (browser acceptance test): uploading ``taxi_metro_93_44.png`` twice
on /receipts answered the second upload with "Draft expense added to your Receipt
Inbox / In policy." and no duplicate warning.  The inbox then held two identical
"METRO YELLOW CAB $93.44" lines while its stored pre-submit check still described
the report as it was before the upload: the auto-draft only MERGED the new line's
single-entry verdict into the snapshot and never re-ran the report-wide check (the
one that compares lines), nor re-checked other drafts holding the twin.  Releasing
an inbox line (smart-match / attach) only pruned flags keyed by the line, so a
twin's duplicate flag naming the released line survived.

Asserted here:
  * the 2nd identical upload leaves the inbox carrying a BLOCKING
    ``possible_duplicate`` flag on both lines, right after the upload;
  * the 2nd upload's response says so (not in policy, ``possible_duplicate`` in
    its flag types, a duplicate reason), while the 1st stays in policy;
  * another draft already holding the twin is re-checked in the same request;
  * releasing one twin from the inbox re-runs the inbox check (the survivor's
    duplicate flag clears).

DB-backed (shared conftest: rolled-back session, real-JWT clients).
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
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.inbox import RECEIPT_INBOX_TITLE, release_inbox_draft
from backend.app.core.security import create_access_token
from backend.tests.receipt_images import scenario_upload
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import Receipt
from database.seeds.policy_content import RULES
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.ocr_provider.interface import OcrProvider, OcrResult
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

API = "/api/v1"
DUP = "possible_duplicate"

_CONF = {
    "amount": 0.95,
    "currency": 0.99,
    "date": 0.95,
    "vendor": 0.93,
    "location": 0.9,
    "expense_type": 0.92,
}
_TAXI: dict[str, Any] = {
    "amount": 93.44,
    "currency": "USD",
    "date": "2026-06-10",
    "vendor": "METRO YELLOW CAB",
    "location": "New York, NY",
    "expense_type": "transportation",
    "total_legible": True,
}


class _Ocr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        return OcrResult(fields=dict(_TAXI), field_confidence=dict(_CONF))


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
def providers(app: Any) -> Iterator[None]:
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _Ocr()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
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


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _upload(client: TestClient) -> dict[str, Any]:
    r = client.post(
        f"{API}/receipts",
        files={"file": scenario_upload("taxi", "taxi_metro_93_44.png")},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    assert body["expense_id"] is not None, body
    return body


def _fresh(db: Session, report_id: uuid.UUID) -> ExpenseReport:
    db.expire_all()
    report = db.get(ExpenseReport, report_id)
    assert report is not None
    return report


def _flags(report: ExpenseReport) -> list[dict]:
    snapshot = report.pre_submit_flags if isinstance(report.pre_submit_flags, dict) else {}
    return [fl for fl in (snapshot.get("flags") or []) if isinstance(fl, dict)]


def _dups(report: ExpenseReport, entry_id: Any | None = None) -> list[dict]:
    return [
        fl
        for fl in _flags(report)
        if fl.get("flag_type") == DUP and (entry_id is None or fl.get("entry_id") == str(entry_id))
    ]


def _inbox_id(db: Session, expense_id: Any) -> uuid.UUID:
    entry = db.get(Expense, uuid.UUID(str(expense_id)))
    assert entry is not None
    return entry.report_id


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy", "providers")
def test_second_identical_upload_flags_the_inbox_and_says_duplicate(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: the 2nd upload said 'In policy.' and the inbox kept its stale check."""
    client = _client(app, users_a["employee"])

    first = _upload(client)
    assert first["policy_verdict"]["in_policy"] is True, first["policy_verdict"]
    assert DUP not in first["policy_verdict"]["flag_types"]
    inbox_id = _inbox_id(db_session, first["expense_id"])
    assert not _dups(_fresh(db_session, inbox_id))

    second = _upload(client)
    assert _inbox_id(db_session, second["expense_id"]) == inbox_id

    # The response is honest: a possible duplicate, never "in policy".
    verdict = second["policy_verdict"]
    assert verdict["in_policy"] is False, verdict
    assert DUP in verdict["flag_types"], verdict
    assert verdict["blocking_count"] >= 1, verdict
    assert verdict["duplicate_reason"], verdict
    assert "METRO YELLOW CAB" in verdict["duplicate_reason"]

    # The inbox's stored check was re-run in the same request: both lines carry
    # a blocking possible_duplicate, and the counts describe both lines.
    inbox = _fresh(db_session, inbox_id)
    for expense_id in (first["expense_id"], second["expense_id"]):
        dup = _dups(inbox, expense_id)
        assert dup and dup[0]["severity"] == "blocking", _flags(inbox)
        entry = db_session.get(Expense, uuid.UUID(expense_id))
        assert entry is not None and entry.status == ExpenseStatus.flagged
    snapshot = inbox.pre_submit_flags or {}
    assert snapshot["evaluated_entry_count"] == 2
    assert snapshot["blocking_count"] == sum(
        1 for fl in _flags(inbox) if fl.get("severity") == "blocking"
    )


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy", "providers")
def test_upload_rechecks_another_draft_holding_the_twin(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: a draft already claiming the taxi ride kept its clean check after the
    same ride was uploaded into the inbox."""
    emp = users_a["employee"]
    other = f.make_expense_report(db_session, owner=emp, title="NYC visit")
    twin = f.make_expense(
        db_session,
        report=other,
        category=ExpenseCategory.ground_transport,
        amount="93.44",
        vendor="Metro Yellow Cab",
        expense_date=date(2026, 6, 10),
        match_source=ExpenseMatchSource.manual,
    )
    other.total_amount = Decimal("93.44")
    other.pre_submit_flags = {
        "flags": [],
        "evaluated_entry_count": 1,
        "blocking_count": 0,
        "advisory_count": 0,
    }
    db_session.flush()
    version_before = _fresh(db_session, other.id).version

    body = _upload(_client(app, emp))
    assert DUP in body["policy_verdict"]["flag_types"], body["policy_verdict"]
    inbox = _fresh(db_session, _inbox_id(db_session, body["expense_id"]))
    assert _dups(inbox, body["expense_id"]), _flags(inbox)

    peer = _fresh(db_session, other.id)
    dup = _dups(peer, twin.id)
    assert dup and dup[0]["severity"] == "blocking", _flags(peer)
    assert RECEIPT_INBOX_TITLE in dup[0]["reason"]
    assert peer.version > version_before  # a stale tab of the peer gets a 409


@pytest.mark.usefixtures("rbac_catalog", "seeded_policy", "providers")
def test_releasing_an_inbox_twin_rechecks_the_inbox(
    app: Any, db_session: Session, users_a: dict[str, User]
) -> None:
    """Old: release only pruned flags keyed by the released line; the survivor kept
    a duplicate flag naming a line that no longer exists."""
    emp = users_a["employee"]
    client = _client(app, emp)
    first = _upload(client)
    second = _upload(client)
    inbox_id = _inbox_id(db_session, first["expense_id"])
    assert _dups(_fresh(db_session, inbox_id), first["expense_id"])

    receipt = db_session.get(Receipt, uuid.UUID(second["id"]))
    assert receipt is not None
    ctx = AgentContext(
        organization_id=emp.organization_id,
        granted_permission_keys=frozenset(),
        user_id=emp.id,
    )
    assert release_inbox_draft(db_session, context=ctx, receipt=receipt) is True
    db_session.flush()

    inbox = _fresh(db_session, inbox_id)
    remaining = db_session.scalars(sa.select(Expense.id).where(Expense.report_id == inbox_id)).all()
    assert [str(e) for e in remaining] == [first["expense_id"]]
    assert not _dups(inbox), _flags(inbox)
    assert all(fl.get("entry_id") in (None, first["expense_id"]) for fl in _flags(inbox)), _flags(
        inbox
    )
    assert (inbox.pre_submit_flags or {})["evaluated_entry_count"] == 1
    survivor = db_session.get(Expense, uuid.UUID(first["expense_id"]))
    assert survivor is not None and survivor.status != ExpenseStatus.flagged


@pytest.mark.parametrize(
    ("field", "value"), [("vendor", "Metro Yellow Cab Co"), ("date", "2026-06-10")]
)
@pytest.mark.usefixtures("rbac_catalog", "seeded_policy", "providers")
def test_correcting_an_inbox_twin_keeps_the_duplicate_flagged(
    app: Any, db_session: Session, users_a: dict[str, User], field: str, value: str
) -> None:
    """Old: a correction of an inbox line re-ran only that line's single-entry
    check and dropped every flag keyed by its vendor — both twins' duplicate flags
    vanished although the two lines still claim the same ride (same receipt)."""
    client = _client(app, users_a["employee"])
    first = _upload(client)
    second = _upload(client)
    inbox_id = _inbox_id(db_session, first["expense_id"])

    r = client.patch(f"{API}/receipts/{second['id']}", json={"field": field, "value": value})
    assert r.status_code == 200, r.text

    inbox = _fresh(db_session, inbox_id)
    for expense_id in (first["expense_id"], second["expense_id"]):
        dup = _dups(inbox, expense_id)
        assert dup and dup[0]["severity"] == "blocking", _flags(inbox)
