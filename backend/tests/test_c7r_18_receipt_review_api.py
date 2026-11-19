"""C7R-18 residual — DB-backed API regression for a withheld (illegible) receipt total.

Staging re-acceptance: TOTAL 18.73 blurred, SUBTOTAL 17.20 readable ->
amount 17.2 @ 0.6 ai_inferred, no review signal, policy_verdict in_policy=true and
an auto-drafted entry at 17.20. The LLM gap-fill re-inferred the amount from the
subtotal after the C7-18 guard had demoted it.

Pinned here, through the real router + pipeline with an LLM stub that ALWAYS
answers with the subtotal (the worst case):
  * upload -> amount null, needs_review true, review_reasons names the total, no
    draft entry, no policy verdict (never "in policy" for an unknown amount);
  * the same for the "no amount + total_legible false" provider shape;
  * GET reports the same review signal;
  * PATCH amount (the user enters the total) -> the deferred draft entry is
    created at the entered amount with a policy verdict; the review clears;
  * a clean receipt reports needs_review false / review_reasons [].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from backend.tests.receipt_images import scenario_of, scenario_upload
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.receipt import Receipt
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

pytestmark = [pytest.mark.db]

TOTAL_MSG = "Total is not legible — enter the receipt total"

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
    # The staging receipt: the model returned the readable subtotal as amount.
    "blurred": (
        {**_BASE, "amount": 17.20, "subtotal": 17.20, "tax": 1.53, "total_legible": False},
        _CONF,
    ),
    # The other staging run shape: no amount at all, total flagged illegible,
    # location also unread so gap-fill DOES run (and would happily fill amount).
    "blurred_no_amount": (
        {
            **_BASE,
            "amount": None,
            "location": None,
            "subtotal": 17.20,
            "tax": 1.53,
            "total_legible": False,
        },
        {**_CONF, "amount": 0.0, "location": 0.0},
    ),
    "clean": ({**_BASE, "amount": 18.73, "subtotal": 17.20, "total_legible": True}, _CONF),
}


class _ScenarioOcr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        fields, conf = _SCENARIOS[key]
        return OcrResult(fields=dict(fields), field_confidence=dict(conf))


class _SubtotalCopyingLlm(LlmProvider):
    """Gap-fill stub that answers every request with the subtotal as the amount."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.extend(m.content for m in messages)
        return LlmResponse(
            text=json.dumps({"amount": 17.2, "location": "New York, NY"}),
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
def llm(app: Any) -> Iterator[_SubtotalCopyingLlm]:
    mem = _MemStorage()
    stub = _SubtotalCopyingLlm()
    app.dependency_overrides[get_storage_provider] = lambda: mem
    app.dependency_overrides[get_ocr_provider] = lambda: _ScenarioOcr()
    app.dependency_overrides[get_llm_provider] = lambda: stub
    yield stub


def _upload(client: TestClient, scenario: str) -> Any:
    return client.post("/api/v1/receipts", files={"file": scenario_upload(scenario)})


@pytest.mark.parametrize("scenario", ["blurred", "blurred_no_amount"])
def test_illegible_total_is_withheld_flagged_and_never_drafted(
    employee_client: TestClient,
    llm: _SubtotalCopyingLlm,
    db_session: Session,
    scenario: str,
) -> None:
    resp = _upload(employee_client, scenario)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extracted_fields"]["amount"] is None
    assert body["field_confidence"]["amount"] == 0.0
    assert body["field_provenance"]["amount"] == "ai_inferred"
    assert body["needs_review"] is True
    assert TOTAL_MSG in body["review_reasons"]
    # No draft and no verdict: an unknown amount is never drafted / "in policy".
    assert body["expense_id"] is None
    assert body["policy_verdict"] is None
    # The subtotal never reached the gap-fill prompt and amount was never requested.
    for prompt in llm.prompts:
        assert "subtotal" not in prompt
        assert "'amount'" not in prompt

    get = employee_client.get(f"/api/v1/receipts/{body['id']}")
    assert get.status_code == 200, get.text
    assert get.json()["needs_review"] is True
    assert TOTAL_MSG in get.json()["review_reasons"]


def test_entering_the_total_creates_the_deferred_draft_and_clears_review(
    employee_client: TestClient, llm: _SubtotalCopyingLlm, db_session: Session
) -> None:
    body = _upload(employee_client, "blurred").json()
    assert body["expense_id"] is None

    patch = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "amount", "value": 18.73}
    )
    assert patch.status_code == 200, patch.text
    out = patch.json()
    assert out["linked_expense_sync"] == "drafted"
    assert out["extracted_fields"]["amount"] == 18.73
    assert out["field_provenance"]["amount"] == "user_edited"
    assert out["needs_review"] is False
    assert out["review_reasons"] == []
    assert out["policy_verdict"] is not None
    assert out["expense_id"] is not None

    entry = db_session.get(Expense, out["expense_id"])
    assert entry is not None
    assert str(entry.amount) == "18.73"
    receipt = db_session.get(Receipt, body["id"])
    assert receipt is not None and str(receipt.expense_id) == out["expense_id"]
    audit = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.action == "receipt.corrected", AuditLog.entity_id == receipt.id
        )
    ).one()
    assert audit.after_state is not None
    assert audit.after_state["linked_expense_sync"] == "drafted"
    assert audit.after_state["expense_id"] == out["expense_id"]

    # A second correction updates that entry — no second draft.
    again = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "amount", "value": 19.00}
    )
    assert again.status_code == 200, again.text
    assert again.json()["linked_expense_sync"] == "updated"
    assert again.json()["expense_id"] == out["expense_id"]


def test_correcting_another_field_does_not_draft_with_unknown_amount(
    employee_client: TestClient, llm: _SubtotalCopyingLlm
) -> None:
    body = _upload(employee_client, "blurred").json()
    patch = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "vendor", "value": "Corner Deli NYC"}
    )
    assert patch.status_code == 200, patch.text
    out = patch.json()
    assert out["expense_id"] is None
    assert out["linked_expense_sync"] == "no_linked_expense"
    assert out["needs_review"] is True
    assert TOTAL_MSG in out["review_reasons"]


def test_clean_receipt_reports_no_review(
    employee_client: TestClient, llm: _SubtotalCopyingLlm
) -> None:
    body = _upload(employee_client, "clean").json()
    assert body["extracted_fields"]["amount"] == 18.73
    assert body["needs_review"] is False
    assert body["review_reasons"] == []
    assert body["expense_id"] is not None
    assert body["policy_verdict"] is not None
