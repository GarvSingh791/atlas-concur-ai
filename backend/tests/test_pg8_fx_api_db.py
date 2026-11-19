"""Product gap 8 (fxcard track): currencies through the API (DB-backed).

* report totals are in the report's currency: a EUR entry on a USD report is
  converted (single-currency totals unchanged);
* entry / receipt responses carry the original + converted amount and rate;
* an unsupported currency is refused at every write boundary (422): report /
  entry create, entry update, receipt currency correction;
* an OCR currency outside the table is never auto-drafted (review reason); picking
  a supported one in receipt review drafts it then;
* a EUR receipt is checked against the USD cap after conversion;
* the advisory currency_unverified flag keeps a report out of auto-approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.modules.expense.fx import report_total
from backend.app.modules.expense.service import _compute_total
from backend.tests.receipt_images import scenario_of, scenario_upload
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization, User
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleType
from database.models.receipt import Receipt
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
    "eur_meal": {
        "amount": 60.00,
        "currency": "EUR",
        "date": "2026-06-11",
        "vendor": "Cafe de Flore",
        "location": "Paris, France",
        "expense_type": "meals",
    },
    "brl_meal": {
        "amount": 120.00,
        "currency": "BRL",
        "date": "2026-06-11",
        "vendor": "Churrascaria",
        "location": "Sao Paulo, Brazil",
        "expense_type": "meals",
    },
    "usd_meal": {
        "amount": 17.20,
        "currency": "USD",
        "date": "2026-06-11",
        "vendor": "Corner Deli",
        "location": "New York, NY",
        "expense_type": "meals",
    },
}


class _ScenarioOcr(OcrProvider):
    def extract(self, image: bytes | str) -> OcrResult:
        key = scenario_of(image)
        return OcrResult(fields=dict(_SCENARIOS[key]), field_confidence=dict(_HIGH))


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
    return client.post("/api/v1/receipts", files={"file": scenario_upload(scenario)})


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def test_single_currency_total_is_unchanged(db_session: Session, users_a: dict[str, User]) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"])
    f.make_expense(db_session, report=report, amount="10.10")
    f.make_expense(db_session, report=report, amount="5.25")
    assert _compute_total(db_session, report.id) == Decimal("15.35")
    empty = f.make_expense_report(db_session, owner=users_a["employee"])
    assert _compute_total(db_session, empty.id) == Decimal("0")


def test_mixed_currency_total_is_in_report_currency(
    db_session: Session, users_a: dict[str, User]
) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"], currency="USD")
    f.make_expense(db_session, report=report, amount="100.00")
    f.make_expense(db_session, report=report, amount="220.00", currency="EUR")
    f.make_expense(db_session, report=report, amount="25000", currency="JPY")
    # 100 + 238.70 + 167.50
    assert _compute_total(db_session, report.id) == Decimal("506.20")


def test_unsupported_legacy_entry_makes_total_uncomputable(
    db_session: Session, users_a: dict[str, User]
) -> None:
    from backend.app.core.errors import UnprocessableEntityError

    report = f.make_expense_report(db_session, owner=users_a["employee"])
    f.make_expense(db_session, report=report, amount="5.00", currency="BRL")
    with pytest.raises(UnprocessableEntityError) as exc:
        report_total(db_session, report.id, raw_total=Decimal("5.00"))
    assert exc.value.code == "CURRENCY_NOT_SUPPORTED"


def test_api_create_mixed_report_total_and_entry_conversion(
    employee_client: TestClient,
) -> None:
    resp = employee_client.post(
        "/api/v1/expense-reports",
        json={
            "title": "Paris trip",
            "currency": "usd",
            "entries": [
                {
                    "category": "meals",
                    "amount": "40.00",
                    "currency": "USD",
                    "expense_date": "2026-06-11",
                },
                {
                    "category": "meals",
                    "amount": "220.00",
                    "currency": "eur",
                    "expense_date": "2026-06-11",
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    report = resp.json()
    assert report["currency"] == "USD"
    assert Decimal(report["total_amount"]) == Decimal("278.70")
    entries = employee_client.get(f"/api/v1/expense-reports/{report['id']}/entries").json()
    by_ccy = {e["currency"]: e for e in entries}
    assert by_ccy["USD"]["amount_conversion"] is None
    conv = by_ccy["EUR"]["amount_conversion"]
    assert conv["original_amount"] == "220.00" and conv["converted_amount"] == "238.70"
    assert conv["currency"] == "USD" and conv["rate"] == "1.085" and conv["supported"] is True


# ---------------------------------------------------------------------------
# Write boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "currency": "BRL", "entries": []},
        {
            "title": "x",
            "currency": "USD",
            "entries": [
                {
                    "category": "meals",
                    "amount": "1",
                    "currency": "XYZ",
                    "expense_date": "2026-06-11",
                }
            ],
        },
    ],
)
def test_create_refuses_unsupported_currency(employee_client: TestClient, body: dict) -> None:
    resp = employee_client.post("/api/v1/expense-reports", json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_entry_update_refuses_unsupported_currency(
    employee_client: TestClient, db_session: Session, users_a: dict[str, User]
) -> None:
    report = f.make_expense_report(db_session, owner=users_a["employee"])
    entry = f.make_expense(db_session, report=report)
    resp = employee_client.patch(f"/api/v1/expenses/{entry.id}", json={"currency": "BRL"})
    assert resp.status_code == 422, resp.text
    ok = employee_client.patch(f"/api/v1/expenses/{entry.id}", json={"currency": "gbp"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["currency"] == "GBP"
    db_session.refresh(report)
    assert report.total_amount == Decimal("31.75")  # 25.00 GBP x 1.27


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def test_eur_receipt_shows_conversion_and_is_capped_after_conversion(
    employee_client: TestClient,
    storage: _MemStorage,
    db_session: Session,
    org_a: Organization,
) -> None:
    # Meals per diem 62 USD: EUR 60.00 passes UNconverted (the old bug) but is
    # USD 65.10 after conversion -> a policy violation quoting the conversion.
    policy = f.make_policy(db_session, org=org_a)
    f.make_policy_rule(
        db_session,
        policy=policy,
        rule_type=PolicyRuleType.per_diem,
        applies_to=PolicyAppliesTo.meals,
        definition={"rates": [{"region": "domestic", "amount": 62}], "currency": "USD"},
        source_clause_ref="MEALS-PD",
    )
    resp = _upload(employee_client, "eur_meal")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    conv = body["amount_conversion"]
    assert conv["original_currency"] == "EUR" and conv["converted_amount"] == "65.10"
    assert body["policy_verdict"]["in_policy"] is False
    assert "policy_violation" in body["policy_verdict"]["flag_types"]
    entry = db_session.get(Expense, body["expense_id"])
    assert entry is not None and entry.currency == "EUR" and entry.amount == Decimal("60.00")
    report = db_session.get(ExpenseReport, entry.report_id)
    assert report is not None and report.currency == "USD"
    assert report.total_amount == Decimal("65.10")
    flags = (report.pre_submit_flags or {}).get("flags") or []
    violation = next(fl for fl in flags if fl.get("source_clause_ref") == "MEALS-PD")
    assert "EUR 60.00 ≈ USD 65.10 at 1.085" in violation["reason"]
    assert violation["observed"]["fx"]["converted_amount"] == "65.10"


def test_usd_receipt_has_no_conversion(employee_client: TestClient, storage: _MemStorage) -> None:
    body = _upload(employee_client, "usd_meal").json()
    assert body["amount_conversion"] is None
    assert body["expense_id"] is not None


def test_unsupported_ocr_currency_is_not_drafted_until_corrected(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    up = _upload(employee_client, "brl_meal")
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["expense_id"] is None  # never drafted in an unconvertible currency
    assert body["needs_review"] is True
    assert any("Currency BRL is not supported" in r for r in body["review_reasons"])
    assert body["amount_conversion"]["supported"] is False
    assert body["amount_conversion"]["converted_amount"] is None

    bad = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "currency", "value": "BRL"}
    )
    assert bad.status_code == 422, bad.text

    fixed = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "currency", "value": "eur"}
    )
    assert fixed.status_code == 200, fixed.text
    out = fixed.json()
    assert out["linked_expense_sync"] == "drafted"
    assert out["extracted_fields"]["currency"] == "EUR"
    assert not any("Currency" in r for r in out["review_reasons"])
    entry = db_session.get(Expense, out["expense_id"])
    assert entry is not None and entry.currency == "EUR" and entry.amount == Decimal("120.00")
    receipt = db_session.get(Receipt, body["id"])
    assert receipt is not None and receipt.expense_id == entry.id


def test_currency_correction_on_linked_entry_reconverts_total(
    employee_client: TestClient, storage: _MemStorage, db_session: Session
) -> None:
    body = _upload(employee_client, "usd_meal").json()
    resp = employee_client.patch(
        f"/api/v1/receipts/{body['id']}", json={"field": "currency", "value": "GBP"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["linked_expense_sync"] == "updated"
    entry = db_session.get(Expense, body["expense_id"])
    assert entry is not None
    db_session.refresh(entry)
    report = db_session.get(ExpenseReport, entry.report_id)
    assert report is not None
    db_session.refresh(report)
    assert entry.currency == "GBP"
    assert report.total_amount == Decimal("21.84")  # 17.20 GBP x 1.27 = 21.844
