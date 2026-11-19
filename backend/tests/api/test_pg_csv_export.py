"""Product gap 8 — finance CSV exports: GET /approvals/export.csv + GET /audit/export.csv.

Contract:
  * finance_admin only (403 envelope for manager / employee, nothing audit-logged);
  * same scope + filters as the list (tenant-bound); invalid filter → 400;
  * RFC-4180 (CRLF, quoting) with a UTF-8 BOM; dated attachment filename;
  * CSV-injection safe: text cells starting with = + - @ TAB CR get a leading ';
  * hard row cap → marker row + ``X-Export-Truncated: true``;
  * no internal ids in the approvals file (only on-screen fields);
  * every export is itself audit-logged (actor, filters, row count).
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from backend.app.core import csv_export
from backend.app.core.audit import record_audit
from backend.tests.api._helpers import assert_error_envelope
from backend.tests.api.endpoints import API
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, User

pytestmark = pytest.mark.db

APPROVALS_CSV = f"{API}/approvals/export.csv"
AUDIT_CSV = f"{API}/audit/export.csv"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

EVIL_TITLE = '=HYPERLINK("http://evil.example","click")'
EVIL_NAME = "+cmd|' /C calc'!A0"
EVIL_DEPT = "@SUM(1+1)"


def _parse(resp: Any) -> list[list[str]]:
    assert resp.status_code == 200, resp.text[:300]
    raw = resp.content
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM expected"
    text = raw.decode("utf-8")[1:]
    # RFC 4180: every record ends with CRLF (a bare LF only inside a quoted field).
    assert text.endswith("\r\n") and "\n" not in text.split("\r\n", 1)[0], "CRLF rows"
    return list(csv.reader(io.StringIO(text)))


def _exports(db_session: Session, org: Organization, action: str) -> list[AuditLog]:
    return list(
        db_session.scalars(
            sa.select(AuditLog).where(AuditLog.organization_id == org.id, AuditLog.action == action)
        )
    )


@pytest.fixture()
def data(
    db_session: Session, org_a: Organization, users_a: dict[str, User], users_b: dict[str, User]
) -> dict[str, Any]:
    evil = f.make_user(db_session, org=org_a, name=EVIL_NAME, department=EVIL_DEPT)
    plain = users_a["employee"]
    plain.department = "Engineering"
    r_evil = f.make_expense_report(
        db_session,
        owner=evil,
        title=EVIL_TITLE,
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("12.50"),
        pre_submit_flags={"flags": [{"flag_type": "missing_receipt"}]},
    )
    r_plain = f.make_expense_report(
        db_session,
        owner=plain,
        title='Client dinner, "Q3" review\nline two',
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal("80.00"),
    )
    a_evil = f.make_approval(
        db_session,
        submitter=evil,
        subject_id=r_evil.id,
        risk_summary={"flags": [{"flag_type": "missing_receipt"}]},
        created_at=datetime(2026, 6, 1, 9, tzinfo=timezone.utc),
    )
    a_plain = f.make_approval(db_session, submitter=plain, subject_id=r_plain.id)
    # Other tenant — never exported.
    other = f.make_expense_report(db_session, owner=users_b["employee"], title="OTHER TENANT")
    f.make_approval(db_session, submitter=users_b["employee"], subject_id=other.id)
    db_session.flush()
    return {"evil": evil, "plain": plain, "a_evil": a_evil, "a_plain": a_plain}


# ---------------------------------------------------------------------------
# Approvals export
# ---------------------------------------------------------------------------


def test_approvals_export_shape_and_filename(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    resp = finance_client.get(APPROVALS_CSV)
    rows = _parse(resp)
    assert resp.headers["content-type"].startswith("text/csv")
    today = datetime.now(timezone.utc).date().isoformat()
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="atlas-approvals-{today}.csv"'
    )
    assert tuple(rows[0]) == (
        "Submitted at (UTC)",
        "Status",
        "Subject type",
        "Subject",
        "Submitter",
        "Department",
        "Amount",
        "Currency",
        "Risk level",
        "Auto-approved",
        "Exception types",
        "Last decided at (UTC)",
    )
    body = rows[1:]
    assert len(body) == 2 and resp.headers["x-export-row-count"] == "2"
    assert resp.headers["x-export-truncated"] == "false"
    assert "OTHER TENANT" not in resp.text
    # RFC-4180 round trip of a comma / quote / newline title.
    assert 'Client dinner, "Q3" review\nline two' in [r[3] for r in body]


def test_approvals_export_neutralises_formula_injection(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    rows = _parse(finance_client.get(APPROVALS_CSV, params={"department": EVIL_DEPT}))
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["Subject"] == "'" + EVIL_TITLE
    assert row["Submitter"] == "'" + EVIL_NAME
    assert row["Department"] == "'" + EVIL_DEPT
    assert row["Exception types"] == "Missing receipt"
    assert row["Amount"] == "12.50"
    assert row["Submitted at (UTC)"] == "2026-06-01T09:00:00Z"
    for cell in rows[1]:
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), cell


def test_approvals_export_has_no_internal_ids(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    text = finance_client.get(APPROVALS_CSV).content.decode("utf-8")
    assert not _UUID_RE.search(text)
    assert "@test.atlas.internal" not in text  # no emails


def test_approvals_export_applies_the_list_filters(
    data: dict[str, Any], finance_client: TestClient
) -> None:
    rows = _parse(finance_client.get(APPROVALS_CSV, params={"exception_type": "missing_receipt"}))
    assert [r[4] for r in rows[1:]] == ["'" + EVIL_NAME]
    listed = finance_client.get(f"{API}/approvals", params={"department": "Engineering"}).json()
    exported = _parse(finance_client.get(APPROVALS_CSV, params={"department": "Engineering"}))
    assert listed["total"] == len(exported) - 1 == 1


def test_approvals_export_cap_and_marker(
    data: dict[str, Any], finance_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csv_export, "EXPORT_MAX_ROWS", 1)
    resp = finance_client.get(APPROVALS_CSV)
    rows = _parse(resp)
    assert len(rows) == 3  # header + 1 data row + marker
    assert rows[-1][0] == csv_export.TRUNCATION_MARKER
    assert "limited to 1 rows" in rows[-1][1]
    assert resp.headers["x-export-truncated"] == "true"
    assert resp.headers["x-export-row-count"] == "1"


def test_approvals_export_is_audit_logged(
    data: dict[str, Any],
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    finance_client: TestClient,
) -> None:
    assert _exports(db_session, org_a, "approvals.exported") == []
    finance_client.get(APPROVALS_CSV, params={"department": "Engineering"})
    logged = _exports(db_session, org_a, "approvals.exported")
    assert len(logged) == 1
    entry = logged[0]
    assert entry.actor_id == users_a["finance"].id
    assert entry.entity_type == "export"
    assert entry.after_state == {
        "export": "approvals",
        "filters": {"department": "Engineering"},
        "sort": None,
        "row_count": 1,
        "truncated": False,
    }


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_approvals_export_is_finance_only(
    data: dict[str, Any],
    db_session: Session,
    org_a: Organization,
    role_clients: dict[str, TestClient],
    role: str,
) -> None:
    assert_error_envelope(role_clients[role].get(APPROVALS_CSV), 403)
    assert _exports(db_session, org_a, "approvals.exported") == []


def test_approvals_export_bad_filter_is_400_and_not_logged(
    data: dict[str, Any], db_session: Session, org_a: Organization, finance_client: TestClient
) -> None:
    assert_error_envelope(finance_client.get(APPROVALS_CSV, params={"nope": "1"}), 400)
    assert_error_envelope(finance_client.get(APPROVALS_CSV, params={"exception_type": "x"}), 400)
    assert _exports(db_session, org_a, "approvals.exported") == []


# ---------------------------------------------------------------------------
# Audit export
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_rows(
    db_session: Session, org_a: Organization, users_a: dict[str, User], users_b: dict[str, User]
) -> dict[str, Any]:
    trip_id = uuid.uuid4()
    for _ in range(3):
        record_audit(
            db_session,
            organization_id=org_a.id,
            action="trip.created",
            entity_type="trip",
            entity_id=trip_id,
            actor_id=users_a["employee"].id,
        )
    record_audit(
        db_session,
        organization_id=org_a.id,
        action="=cmd|calc",
        entity_type="-evil",
        entity_id=uuid.uuid4(),
        actor_id=None,
    )
    record_audit(
        db_session,
        organization_id=users_b["employee"].organization_id,
        action="trip.created",
        entity_type="trip",
        entity_id=uuid.uuid4(),
        actor_id=users_b["employee"].id,
    )
    return {"trip_id": trip_id}


def test_audit_export_filters_scope_and_columns(
    audit_rows: dict[str, Any], users_a: dict[str, User], finance_client: TestClient
) -> None:
    resp = finance_client.get(AUDIT_CSV, params={"action": "trip.created"})
    rows = _parse(resp)
    assert tuple(rows[0]) == ("Time (UTC)", "Actor", "Action", "Entity type", "Entity ID")
    assert len(rows) == 4  # 3 org-A rows, tenant B excluded
    assert {r[1] for r in rows[1:]} == {users_a["employee"].name}
    assert {r[4] for r in rows[1:]} == {str(audit_rows["trip_id"])}
    today = datetime.now(timezone.utc).date().isoformat()
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="atlas-audit-log-{today}.csv"'
    )


def test_audit_export_injection_and_system_actor(
    audit_rows: dict[str, Any], finance_client: TestClient
) -> None:
    rows = _parse(finance_client.get(AUDIT_CSV, params={"entity_type": "-evil"}))
    assert rows[1][1:4] == ["System", "'=cmd|calc", "'-evil"]


def test_audit_export_cap_logging_and_self_exclusion(
    audit_rows: dict[str, Any],
    db_session: Session,
    org_a: Organization,
    users_a: dict[str, User],
    finance_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csv_export, "EXPORT_MAX_ROWS", 2)
    resp = finance_client.get(AUDIT_CSV)
    rows = _parse(resp)
    assert len(rows) == 4 and rows[-1][0] == csv_export.TRUNCATION_MARKER
    assert resp.headers["x-export-truncated"] == "true"
    assert all(r[2] != "audit.exported" for r in rows[1:-1])

    logged = _exports(db_session, org_a, "audit.exported")
    assert len(logged) == 1 and logged[0].actor_id == users_a["finance"].id
    assert logged[0].after_state is not None
    assert logged[0].after_state["row_count"] == 2
    assert logged[0].after_state["truncated"] is True


@pytest.mark.parametrize("role", ["manager", "employee"])
def test_audit_export_is_finance_only(
    audit_rows: dict[str, Any],
    db_session: Session,
    org_a: Organization,
    role_clients: dict[str, TestClient],
    role: str,
) -> None:
    assert_error_envelope(role_clients[role].get(AUDIT_CSV), 403)
    assert _exports(db_session, org_a, "audit.exported") == []


def test_audit_export_bad_filter_is_400(
    audit_rows: dict[str, Any], finance_client: TestClient
) -> None:
    assert_error_envelope(finance_client.get(AUDIT_CSV, params={"actor_id": "nope"}), 400)
    assert_error_envelope(finance_client.get(AUDIT_CSV, params={"sort": "action"}), 400)
