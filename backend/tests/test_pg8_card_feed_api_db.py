"""Product gap 8 (fxcard track): GET /card-transactions — the caller's card feed.

Pinned (DB-backed, real-JWT role clients, rollback-isolated):
  * owner-only for EVERY role: an employee sees their own swipes; a manager sees
    their own and never a direct report's; another org's swipes never appear;
  * finance_admin has no receipts access -> 403 (deny by default);
  * status (matched / unmatched) and date filters, pagination, newest first;
  * a matched swipe links its entry, report and first receipt — only when that
    report is the caller's own (no link into someone else's record);
  * foreign-currency swipes carry their base-currency conversion.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from database.models.identity import Organization, User
from database.models.receipt import OcrStatus

pytestmark = [pytest.mark.db]

URL = "/api/v1/card-transactions"
_T0 = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)


def _ids(resp: Any) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["items"]]


@pytest.fixture()
def feed(db_session: Session, users_a: dict[str, User], users_b: dict[str, User]) -> dict[str, Any]:
    emp, mgr = users_a["employee"], users_a["manager"]
    report = f.make_expense_report(db_session, owner=emp)
    entry = f.make_expense(db_session, report=report, amount="38.50", vendor="Joe's Pizza")
    # "First receipt" = earliest upload (created_at), ties -> lowest id.  Both rows
    # get explicit, distinct created_at values (the DB default is the transaction
    # start, identical for every row of this fixture) and the EARLIER upload has
    # the HIGHER id, so the expectation is fixed and proves created_at decides.
    first = f.make_receipt(
        db_session,
        uploaded_by=emp,
        ocr_status=OcrStatus.processed,
        expense_id=entry.id,
        id=uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff"),
        created_at=_T0,
    )
    f.make_receipt(
        db_session,
        uploaded_by=emp,
        ocr_status=OcrStatus.processed,
        expense_id=entry.id,
        id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        created_at=_T0 + timedelta(minutes=5),
    )
    matched = f.make_card_transaction(
        db_session,
        user=emp,
        merchant="Joe's Pizza NYC",
        amount="38.50",
        txn_date=date(2026, 6, 11),
        matched=True,
        expense_id=entry.id,
        last_four="4242",
    )
    unmatched = f.make_card_transaction(
        db_session,
        user=emp,
        merchant="United Airlines",
        amount="320.00",
        txn_date=date(2026, 6, 10),
        last_four="4242",
    )
    euro = f.make_card_transaction(
        db_session,
        user=emp,
        merchant="Hotel Paris",
        amount="220.00",
        currency="EUR",
        txn_date=date(2026, 5, 2),
    )
    mgr_txn = f.make_card_transaction(
        db_session, user=mgr, merchant="Manager Lunch", txn_date=date(2026, 6, 1)
    )
    other_org = f.make_card_transaction(
        db_session, user=users_b["employee"], merchant="Org B Taxi", txn_date=date(2026, 6, 12)
    )
    return {
        "entry": entry,
        "report": report,
        "receipt": first,
        "matched": str(matched.id),
        "unmatched": str(unmatched.id),
        "euro": str(euro.id),
        "mgr": str(mgr_txn.id),
        "other_org": str(other_org.id),
    }


def test_employee_sees_only_own_transactions_newest_first(
    employee_client: TestClient, feed: dict[str, Any]
) -> None:
    resp = employee_client.get(URL)
    assert _ids(resp) == [feed["matched"], feed["unmatched"], feed["euro"]]
    body = resp.json()
    assert body["total"] == 3 and body["page"] == 1


def test_manager_sees_only_own_never_direct_reports(
    manager_client: TestClient, feed: dict[str, Any]
) -> None:
    assert _ids(manager_client.get(URL)) == [feed["mgr"]]


def test_finance_has_no_card_feed_access(finance_client: TestClient, feed: dict[str, Any]) -> None:
    resp = finance_client.get(URL)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_other_tenant_never_sees_org_a_swipes(
    employee_client_b: TestClient, feed: dict[str, Any]
) -> None:
    assert _ids(employee_client_b.get(URL)) == [feed["other_org"]]


def test_unauthenticated_is_401(app: Any, feed: dict[str, Any]) -> None:
    resp = TestClient(app).get(URL)
    assert resp.status_code == 401, resp.text


def test_matched_row_links_entry_report_and_first_receipt(
    employee_client: TestClient, feed: dict[str, Any]
) -> None:
    rows = {r["id"]: r for r in employee_client.get(URL).json()["items"]}
    m = rows[feed["matched"]]
    assert m["status"] == "matched"
    assert m["expense_id"] == str(feed["entry"].id)
    assert m["report_id"] == str(feed["report"].id)
    assert m["receipt_id"] == str(feed["receipt"].id)
    assert m["merchant"] == "Joe's Pizza NYC" and m["amount"] == "38.50"
    assert m["last_four"] == "4242"
    u = rows[feed["unmatched"]]
    assert u["status"] == "unmatched"
    assert u["expense_id"] is None and u["report_id"] is None and u["receipt_id"] is None


def test_status_filters(employee_client: TestClient, feed: dict[str, Any]) -> None:
    assert _ids(employee_client.get(URL, params={"status": "matched"})) == [feed["matched"]]
    assert _ids(employee_client.get(URL, params={"status": "unmatched"})) == [
        feed["unmatched"],
        feed["euro"],
    ]
    bad = employee_client.get(URL, params={"status": "everything"})
    assert bad.status_code == 422, bad.text


def test_expense_id_alone_counts_as_matched(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    feed: dict[str, Any],
) -> None:
    linked = f.make_card_transaction(
        db_session,
        user=users_a["employee"],
        merchant="Linked only",
        txn_date=date(2026, 6, 20),
        expense_id=feed["entry"].id,
    )
    assert str(linked.id) in _ids(employee_client.get(URL, params={"status": "matched"}))


def test_date_filters_are_inclusive(employee_client: TestClient, feed: dict[str, Any]) -> None:
    resp = employee_client.get(URL, params={"date_from": "2026-06-10", "date_to": "2026-06-10"})
    assert _ids(resp) == [feed["unmatched"]]
    assert _ids(employee_client.get(URL, params={"date_from": "2026-06-01"})) == [
        feed["matched"],
        feed["unmatched"],
    ]
    bad = employee_client.get(URL, params={"date_from": "2026-06-11", "date_to": "2026-06-01"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "BAD_REQUEST"


def test_pagination(employee_client: TestClient, feed: dict[str, Any]) -> None:
    page1 = employee_client.get(URL, params={"page": 1, "size": 2}).json()
    page2 = employee_client.get(URL, params={"page": 2, "size": 2}).json()
    assert [r["id"] for r in page1["items"]] == [feed["matched"], feed["unmatched"]]
    assert [r["id"] for r in page2["items"]] == [feed["euro"]]
    assert page1["total"] == page2["total"] == 3 and page1["pages"] == 2


def test_foreign_swipe_carries_base_currency_conversion(
    employee_client: TestClient, feed: dict[str, Any]
) -> None:
    rows = {r["id"]: r for r in employee_client.get(URL).json()["items"]}
    conv = rows[feed["euro"]]["amount_conversion"]
    assert conv["original_currency"] == "EUR" and conv["currency"] == "USD"
    assert conv["converted_amount"] == "238.70" and conv["rate"] == "1.085"
    assert rows[feed["unmatched"]]["amount_conversion"] is None  # already USD


def test_link_into_someone_elses_report_is_never_exposed(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
    org_a: Organization,
) -> None:
    """A swipe of mine that points at another user's entry (data anomaly) shows
    as matched, but without any id of that other user's records."""
    other = f.make_user(db_session, org=org_a)
    their_report = f.make_expense_report(db_session, owner=other)
    their_entry = f.make_expense(db_session, report=their_report)
    txn = f.make_card_transaction(
        db_session,
        user=users_a["employee"],
        merchant="Anomaly",
        matched=True,
        expense_id=their_entry.id,
    )
    rows = {r["id"]: r for r in employee_client.get(URL).json()["items"]}
    row = rows[str(txn.id)]
    assert row["status"] == "matched"
    assert row["expense_id"] is None and row["report_id"] is None and row["receipt_id"] is None


def test_first_receipt_tie_on_created_at_is_the_lowest_id(
    employee_client: TestClient,
    db_session: Session,
    users_a: dict[str, User],
) -> None:
    """Two receipts uploaded in the same instant: the lowest id is "first" — a
    fixed rule, so the link never depends on row order."""
    emp = users_a["employee"]
    report = f.make_expense_report(db_session, owner=emp)
    entry = f.make_expense(db_session, report=report, amount="12.00", vendor="Tie Cafe")
    ids = [
        uuid.UUID("00000000-0000-4000-8000-0000000000b2"),
        uuid.UUID("00000000-0000-4000-8000-0000000000a1"),
        uuid.UUID("00000000-0000-4000-8000-0000000000c3"),
    ]
    for rid in ids:
        f.make_receipt(
            db_session,
            uploaded_by=emp,
            ocr_status=OcrStatus.processed,
            expense_id=entry.id,
            id=rid,
            created_at=_T0,
        )
    txn = f.make_card_transaction(
        db_session, user=emp, merchant="Tie Cafe", matched=True, expense_id=entry.id
    )
    rows = {r["id"]: r for r in employee_client.get(URL).json()["items"]}
    assert rows[str(txn.id)]["receipt_id"] == str(min(ids))
