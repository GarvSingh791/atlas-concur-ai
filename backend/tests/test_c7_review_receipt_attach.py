"""C7 whole-branch review, round 2 — POST /receipts?expense_id= on a non-draft report.

A receipt attached to an entry AFTER its report was submitted used to be stored
and linked (only org + report-owner were checked), so the approval-time policy
re-evaluation saw a receipt that nobody reviewed with the claim.  Attaching is
now limited to entries of a DRAFT report (409 otherwise), and the refusal happens
before anything is stored or OCR'd.

Uses the shared conftest (rollback-isolated session, real-JWT role clients) and
the in-memory OCR / storage / LLM stubs from the G3 receipts suite — no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.tests.receipt_images import scenario_upload
from backend.tests.test_c7_g3_receipts_api import _MemStorage, _ScenarioOcr
from database.models.expense import ExpenseCategory
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import User
from database.models.receipt import Receipt
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.local import LocalLlmProvider
from services.ocr_provider.factory import get_ocr_provider
from services.storage_provider.factory import get_storage_provider

pytestmark = [pytest.mark.db]


@pytest.fixture()
def storage(app: Any) -> Iterator[_MemStorage]:
    mem = _MemStorage()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _ScenarioOcr()
    app.dependency_overrides[get_llm_provider] = lambda: LocalLlmProvider()
    yield mem


def _attach(client: TestClient, expense_id: Any, scenario: str = "meal") -> Any:
    return client.post(
        "/api/v1/receipts",
        params={"expense_id": str(expense_id)},
        files={"file": scenario_upload(scenario)},
    )


def _entry(db: Session, owner: User, status: ExpenseReportStatus) -> Any:
    report = f.make_expense_report(db, owner=owner, status=status)
    return f.make_expense(db, report=report, category=ExpenseCategory.lodging, amount="450.00")


@pytest.mark.parametrize(
    "status",
    [
        ExpenseReportStatus.submitted,
        ExpenseReportStatus.approved,
        ExpenseReportStatus.rejected,
    ],
)
def test_attach_to_non_draft_report_entry_is_refused(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
    status: ExpenseReportStatus,
) -> None:
    entry = _entry(db_session, users_a["employee"], status)

    resp = _attach(employee_client, entry.id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert storage.objects == {}
    assert (
        db_session.scalar(
            sa.select(sa.func.count(Receipt.id)).where(Receipt.expense_id == entry.id)
        )
        == 0
    )


def test_attach_to_draft_report_entry_still_works(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    entry = _entry(db_session, users_a["employee"], ExpenseReportStatus.draft)

    resp = _attach(employee_client, entry.id)

    assert resp.status_code == 201, resp.text
    assert resp.json()["expense_id"] == str(entry.id)


def test_attach_to_someone_elses_entry_is_still_404_and_stores_nothing(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    entry = _entry(db_session, users_a["manager"], ExpenseReportStatus.draft)

    resp = _attach(employee_client, entry.id)

    assert resp.status_code == 404, resp.text
    assert storage.objects == {}
