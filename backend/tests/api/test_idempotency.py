"""P7-T2 — idempotency contract on the live idempotent endpoints.

Idempotent endpoints (live): POST /expense-reports (scope expense_report.create),
POST /receipts (scope receipt.upload), POST /conversations/{id}/messages (scope
conversation.message).

Design contract (docs/phase_logs/P3-T4): an Idempotency-Key REPLAYS the stored
entity — "same key -> one row + same id; different keys -> two rows" — keyed on
``(organization_id, scope, idempotency_key)``.

FLAGGED DIVERGENCES from the P7-T2 brief item (e) — these tests assert the ACTUAL
behavior and document the gap (NO production change made):
  * "same key + different body -> 409 Conflict": NOT implemented. There is no
    body hash; the stored entity is replayed and the new body is ignored. See
    ``test_same_key_different_body_replays_first_no_409``.
  * "key scoped to operation+caller": the key is scoped to operation + ORG, NOT
    caller. Two different users in the SAME org sharing a key cause the second to
    replay the FIRST user's entity. See
    ``test_same_org_different_caller_replays_first_callers_entity`` — flagged as a
    potential cross-user exposure. (Cross-ORG isolation DOES hold.)
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core.security import create_access_token
from backend.tests.api._helpers import assert_error_envelope, sse_done_message_id
from backend.tests.api.endpoints import API
from backend.tests.receipt_images import scenario_upload
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization, RoleKey, User
from database.models.receipt import Receipt

pytestmark = pytest.mark.db

_REPORT_BODY = {"title": "Q1 Travel", "currency": "USD"}


def _report_count(db_session, owner_id) -> int:
    return db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ExpenseReport)
        .where(ExpenseReport.owner_id == owner_id)
    )


# ---------------------------------------------------------------------------
# Expense-report creation
# ---------------------------------------------------------------------------


def test_same_key_same_body_replays_no_double_write(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    employee = users_a["employee"]
    headers = {"Idempotency-Key": "exprep-key-AAA"}

    r1 = employee_client.post(f"{API}/expense-reports", json=_REPORT_BODY, headers=headers)
    assert r1.status_code == 201, r1.text[:200]
    id1 = r1.json()["id"]

    r2 = employee_client.post(f"{API}/expense-reports", json=_REPORT_BODY, headers=headers)
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    # Same stored response, exactly ONE row written (no double-write).
    assert id1 == id2
    assert _report_count(db_session, employee.id) == 1


def test_different_keys_create_distinct_rows(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    employee = users_a["employee"]
    r1 = employee_client.post(
        f"{API}/expense-reports", json=_REPORT_BODY, headers={"Idempotency-Key": "k-1"}
    )
    r2 = employee_client.post(
        f"{API}/expense-reports", json=_REPORT_BODY, headers={"Idempotency-Key": "k-2"}
    )
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert _report_count(db_session, employee.id) == 2


def test_no_key_always_writes(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """Absent Idempotency-Key -> every call writes (no dedup)."""
    employee = users_a["employee"]
    employee_client.post(f"{API}/expense-reports", json=_REPORT_BODY)
    employee_client.post(f"{API}/expense-reports", json=_REPORT_BODY)
    assert _report_count(db_session, employee.id) == 2


def test_same_key_different_body_replays_first_no_409(
    db_session, users_a: dict[str, User], employee_client: TestClient
) -> None:
    """FLAG: brief item (e) expects 409 on same-key+different-body. The live
    design has no body hash, so the FIRST entity is replayed and the new body is
    silently ignored — asserted here as the ACTUAL behavior."""
    employee = users_a["employee"]
    headers = {"Idempotency-Key": "exprep-key-BODY"}

    r1 = employee_client.post(
        f"{API}/expense-reports", json={"title": "First", "currency": "USD"}, headers=headers
    )
    assert r1.status_code == 201
    id1, title1 = r1.json()["id"], r1.json()["title"]

    r2 = employee_client.post(
        f"{API}/expense-reports",
        json={"title": "TOTALLY DIFFERENT", "currency": "EUR"},
        headers=headers,
    )
    # NOT 409: replay of the original, different body discarded.
    assert r2.status_code == 201
    assert r2.json()["id"] == id1
    assert r2.json()["title"] == title1 == "First"
    assert r2.json()["currency"] == "USD"  # the EUR override never persisted
    assert _report_count(db_session, employee.id) == 1


def test_cross_org_same_key_isolated(
    db_session,
    users_a: dict[str, User],
    employee_client: TestClient,
    employee_client_b: TestClient,
) -> None:
    """Same key string in two different orgs -> independent entities (the
    (organization_id, scope, key) uniqueness scopes per-tenant)."""
    shared = {"Idempotency-Key": "shared-across-orgs"}
    ra = employee_client.post(f"{API}/expense-reports", json=_REPORT_BODY, headers=shared)
    rb = employee_client_b.post(f"{API}/expense-reports", json=_REPORT_BODY, headers=shared)
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"]
    assert ra.json()["organization_id"] != rb.json()["organization_id"]


def test_same_org_different_caller_does_not_replay_first_callers_entity(
    db_session, org_a: Organization, rbac_catalog: None, app, users_a: dict[str, User]
) -> None:
    """Idempotency rows are keyed (org, scope, key), but a replay is only served to
    the record's OWNER.  A second user in the SAME org reusing the key gets 409 —
    never the first user's report (formerly FLAGged cross-user exposure, closed in
    the C7-01 write-scope sweep)."""
    user1 = users_a["employee"]
    user2 = f.make_user(db_session, org=org_a, role=RoleKey.employee)

    def _client(u: User) -> TestClient:
        c = TestClient(app, raise_server_exceptions=False)
        token = create_access_token(u.id, u.organization_id)
        c.headers.update({"Authorization": f"Bearer {token}"})
        return c

    headers = {"Idempotency-Key": "cross-caller-key"}
    r1 = _client(user1).post(f"{API}/expense-reports", json=_REPORT_BODY, headers=headers)
    assert r1.status_code == 201
    first_id, first_owner = r1.json()["id"], r1.json()["owner_id"]
    assert first_owner == str(user1.id)

    r2 = _client(user2).post(f"{API}/expense-reports", json=_REPORT_BODY, headers=headers)
    assert_error_envelope(r2, 409)
    assert first_id not in r2.text  # user1's report is not disclosed
    # Only one row exists (user1's); user2 created nothing.
    assert _report_count(db_session, user1.id) == 1
    assert _report_count(db_session, user2.id) == 0


# ---------------------------------------------------------------------------
# Receipt upload (multipart)
# ---------------------------------------------------------------------------


def _receipt_file() -> dict:
    return {"file": scenario_upload("fake-image-bytes", "receipt.png")}


def test_receipt_upload_idempotent_no_double_write(
    db_session, local_providers: None, users_a: dict[str, User], employee_client: TestClient
) -> None:
    employee = users_a["employee"]
    headers = {"Idempotency-Key": "receipt-key-XYZ"}

    r1 = employee_client.post(f"{API}/receipts", files=_receipt_file(), headers=headers)
    assert r1.status_code == 201, r1.text[:300]
    id1 = r1.json()["id"]

    r2 = employee_client.post(f"{API}/receipts", files=_receipt_file(), headers=headers)
    assert r2.status_code == 201
    assert r2.json()["id"] == id1

    count = db_session.scalar(
        sa.select(sa.func.count()).select_from(Receipt).where(Receipt.uploaded_by == employee.id)
    )
    assert count == 1


# ---------------------------------------------------------------------------
# Conversation messages (SSE streaming + idempotency)
# ---------------------------------------------------------------------------


def test_send_message_idempotent_replays_same_message(
    db_session, local_providers: None, employee_client: TestClient
) -> None:
    conv = employee_client.post(f"{API}/conversations", json={"context_type": "general"})
    assert conv.status_code == 201
    conv_id = conv.json()["id"]
    url = f"{API}/conversations/{conv_id}/messages"
    headers = {"Idempotency-Key": "msg-key-1"}
    body = {"content": "What is the per-diem limit?"}

    r1 = employee_client.post(url, json=body, headers=headers)
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("text/event-stream")
    mid1 = sse_done_message_id(r1.text)
    assert mid1 is not None

    r2 = employee_client.post(url, json=body, headers=headers)
    assert r2.status_code == 200
    mid2 = sse_done_message_id(r2.text)
    assert mid2 == mid1  # replay streams the same stored message id

    # No double-write: exactly one USER message persisted for this conversation.
    import uuid as _uuid

    user_msgs = db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == _uuid.UUID(conv_id),
            ConversationMessage.role == MessageRole.user,
        )
    )
    assert user_msgs == 1
