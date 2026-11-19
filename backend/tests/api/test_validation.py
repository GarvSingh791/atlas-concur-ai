"""P7-T2 — request validation contract.

Every body-bearing endpoint rejects a malformed body with 422 + the standard
envelope (VALIDATION_ERROR). Bad enum values are rejected the same way.

RECONCILE / FLAG: the brief asks for "422 with field+reason" in the body. The
live ``validation_exception_handler`` returns a GENERIC message ("Request
validation failed.") and logs the field-level errors SERVER-SIDE ONLY (no field
names / values in the response — deliberate, to avoid schema/info leakage). These
tests therefore assert the standard envelope + VALIDATION_ERROR code, NOT a
field+reason body. See the FSR.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.app.core.errors import ErrorCode
from backend.tests.api._helpers import assert_error_envelope, dummy_uuid
from backend.tests.api.endpoints import API

pytestmark = pytest.mark.db


def _fill(path: str) -> str:
    while "{" in path:
        s, e = path.index("{"), path.index("}")
        path = path[:s] + dummy_uuid() + path[e + 1 :]
    return path


# (label, method, path_template, role, json_body) — each body is malformed for
# its schema so FastAPI raises RequestValidationError -> 422.
_MALFORMED_BODY_CASES = [
    ("trips.create.missing_required", "POST", f"{API}/trips", "employee", {}),
    ("trips.transition.bad_enum", "PATCH", f"{API}/trips/{{id}}", "employee", {"status": "bogus"}),
    ("expense.create.missing_required", "POST", f"{API}/expense-reports", "employee", {}),
    (
        "expense.transition.bad_enum",
        "PATCH",
        f"{API}/expense-reports/{{id}}",
        "employee",
        {"status": "not-a-status"},
    ),
    (
        "expense.update_entry.bad_type",
        "PATCH",
        f"{API}/expenses/{{id}}",
        "employee",
        {"amount": "not-a-decimal"},
    ),
    (
        "ai.create_conversation.bad_enum",
        "POST",
        f"{API}/conversations",
        "employee",
        {"context_type": "bogus_context"},
    ),
    (
        "ai.send_message.empty_content",
        "POST",
        f"{API}/conversations/{{id}}/messages",
        "employee",
        {"content": ""},
    ),
    ("policy.query.missing_question", "POST", f"{API}/policy/query", "employee", {}),
    ("policy.evaluate.missing_subject", "POST", f"{API}/policy/evaluate", "employee", {}),
    (
        "policy.create_rule.missing_title",
        "POST",
        f"{API}/policies/{{id}}/rules",
        "finance_admin",
        {},
    ),
    (
        "policy.patch_rule.bad_enum",
        "PATCH",
        f"{API}/policy-rules/{{id}}",
        "finance_admin",
        {"status": "draft"},  # only 'active'|'archived' permitted by the Literal
    ),
    (
        "receipts.correct.missing_field",
        "PATCH",
        f"{API}/receipts/{{id}}",
        "employee",
        {"value": "x"},
    ),
    (
        "approvals.trigger.bad_enum",
        "POST",
        f"{API}/approvals",
        "manager",
        {"subject_type": "bogus", "subject_id": dummy_uuid()},
    ),
    ("approvals.decide.missing_decision", "PATCH", f"{API}/approvals/{{id}}", "manager", {}),
]


@pytest.mark.parametrize(
    ("label", "method", "path_tmpl", "role", "body"),
    _MALFORMED_BODY_CASES,
    ids=[c[0] for c in _MALFORMED_BODY_CASES],
)
def test_malformed_body_is_422_envelope(
    role_clients: dict[str, TestClient],
    label: str,
    method: str,
    path_tmpl: str,
    role: str,
    body: dict,
) -> None:
    client = role_clients[role]
    resp = getattr(client, method.lower())(_fill(path_tmpl), json=body)
    assert (
        resp.status_code == 422
    ), f"{label}: expected 422, got {resp.status_code}: {resp.text[:200]}"
    cid = assert_error_envelope(resp, 422)
    assert resp.json()["error"]["code"] == ErrorCode.VALIDATION
    # FLAG: message is generic — field-level detail is logged server-side only.
    assert resp.json()["error"]["message"] == "Request validation failed."
    assert cid


def test_receipt_upload_missing_file_is_422(
    employee_client: TestClient, local_providers: None
) -> None:
    """The multipart upload endpoint rejects a request with no file part -> 422."""
    resp = employee_client.post(f"{API}/receipts", json={})
    assert resp.status_code == 422
    assert_error_envelope(resp, 422)


def test_path_uuid_malformed_is_422(employee_client: TestClient) -> None:
    """A non-UUID path param is a request-validation error (422), not a 500."""
    resp = employee_client.get(f"{API}/expense-reports/not-a-uuid")
    assert resp.status_code == 422
    assert_error_envelope(resp, 422)
