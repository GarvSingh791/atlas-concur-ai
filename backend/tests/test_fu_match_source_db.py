"""Follow-up (UI polish) — the entry's match source follows its CURRENT links.

Old behaviour: an entry auto-drafted from a receipt kept ``match_source =
receipt`` (badge "Auto · Receipt") after its receipt was removed — the stored
column records how the line was created, and the DTO echoed it verbatim.

Now ``entries_with_receipt_ids`` (the one serializer every entry view goes
through: owner, approver, PATCH / attach / mileage responses) derives the
reported source from the receipts that back the line right now: a
receipt-sourced line with no direct or shared receipt reports ``None``
("Unmatched").  The stored column is untouched (the receipt inbox and the
discard-upload path still read it), and other sources pass through as stored.

DB-backed (shared conftest: rolled-back session, real-JWT clients).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from ai_agents.receipt_pipeline.evidence import set_shared_expense_ids
from backend.app.core.security import create_access_token
from backend.app.modules.expense import service as expense_service
from database.models.expense import Expense, ExpenseMatchSource
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.receipt import OcrStatus
from services.storage_provider.factory import get_storage_provider
from services.storage_provider.interface import PresignedUrl, StorageProvider

pytestmark = pytest.mark.db

API = "/api/v1"


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
    yield mem


def _client(app: Any, user: User) -> TestClient:
    token = create_access_token(user.id, user.organization_id)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _line(db: Session, owner: User, source: ExpenseMatchSource | None) -> Expense:
    report = f.make_expense_report(db, owner=owner, title="Trip")
    return f.make_expense(db, report=report, vendor="Corner Deli", match_source=source)


def _attach(db: Session, owner: User, entry: Expense, storage: _MemStorage | None = None) -> Any:
    receipt = f.make_receipt(
        db,
        uploaded_by=owner,
        expense_id=entry.id,
        ocr_status=OcrStatus.processed,
        image_url=f"{owner.organization_id}/{entry.id}.png",
        extracted_fields={"vendor": "Corner Deli", "amount": 25.0, "currency": "USD"},
    )
    if storage is not None:
        storage.objects[receipt.image_url] = b"img"
    return receipt


def _entries(client: TestClient, entry: Expense) -> dict[str, Any]:
    r = client.get(f"{API}/expense-reports/{entry.report_id}/entries")
    assert r.status_code == 200, r.text
    (row,) = [e for e in r.json() if e["id"] == str(entry.id)]
    return row


@pytest.mark.usefixtures("rbac_catalog")
def test_removing_the_only_receipt_drops_the_auto_receipt_source(
    app: Any, db_session: Session, users_a: dict[str, User], storage: _MemStorage
) -> None:
    emp = users_a["employee"]
    client = _client(app, emp)
    entry = _line(db_session, emp, ExpenseMatchSource.receipt)
    receipt = _attach(db_session, emp, entry, storage)

    before = _entries(client, entry)
    assert before["match_source"] == "receipt"
    assert before["receipt_ids"] == [str(receipt.id)]

    r = client.delete(f"{API}/receipts/{receipt.id}")
    assert r.status_code == 200, r.text

    after = _entries(client, entry)
    assert after["receipt_ids"] == []
    assert after["match_source"] is None  # badge: "Unmatched", not "Auto · Receipt"
    # The stored column (how the line was created) is not rewritten.
    db_session.expire_all()
    stored = db_session.get(Expense, entry.id)
    assert stored is not None and stored.match_source == ExpenseMatchSource.receipt


@pytest.mark.usefixtures("rbac_catalog")
def test_reported_source_follows_current_links_and_other_sources_pass_through(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    bare_receipt = _line(db_session, emp, ExpenseMatchSource.receipt)
    linked_receipt = _line(db_session, emp, ExpenseMatchSource.receipt)
    _attach(db_session, emp, linked_receipt)
    manual = _line(db_session, emp, ExpenseMatchSource.manual)
    card = _line(db_session, emp, ExpenseMatchSource.card_txn)
    seeded = _line(db_session, emp, None)

    out = {
        r.id: r
        for r in expense_service.entries_with_receipt_ids(
            db_session, [bare_receipt, linked_receipt, manual, card, seeded]
        )
    }
    assert out[bare_receipt.id].match_source is None
    assert out[linked_receipt.id].match_source == ExpenseMatchSource.receipt
    assert out[manual.id].match_source == ExpenseMatchSource.manual
    assert out[card.id].match_source == ExpenseMatchSource.card_txn
    assert out[seeded.id].match_source is None


@pytest.mark.usefixtures("rbac_catalog")
def test_a_receipt_shared_from_an_itemized_sibling_still_counts_as_a_link(
    db_session: Session, users_a: dict[str, User]
) -> None:
    emp = users_a["employee"]
    owner_line = _line(db_session, emp, ExpenseMatchSource.receipt)
    sibling = f.make_expense(
        db_session,
        report=db_session.get(ExpenseReport, owner_line.report_id),
        vendor="Hilton",
        match_source=ExpenseMatchSource.receipt,
    )
    folio = _attach(db_session, emp, owner_line)
    set_shared_expense_ids(folio, [sibling.id])
    db_session.flush()

    (row,) = expense_service.entries_with_receipt_ids(db_session, [sibling])
    assert row.receipt_ids == [folio.id]
    assert row.match_source == ExpenseMatchSource.receipt
