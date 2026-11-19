"""Follow-up (UI polish) — GET /mileage-rate for the Add-mileage live estimate.

The Add-mileage dialog shows "about $X = distance x rate (MIL-01)" before the
claimant confirms.  The rate it shows is read from this tiny read-only endpoint,
which returns exactly what the server would charge (``current_mileage_rate``:
the caller's organisation's active MIL-01 rule, lowest wins, canonical figure
when none).  The amount itself is still computed server-side on POST.

Asserted: org-scoped (another tenant's rule never leaks), follows a rule
change, falls back to the canonical figure, permission-gated like the POST
(``expense.update.own``: finance 403), Decimal as a string, and read-only (no
audit row).

DB-backed (shared conftest: rolled-back session, real-JWT clients).
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import backend.tests.factories as f
from database.models.audit_log import AuditLog
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleStatus, PolicyRuleType
from database.seeds.corpus import figures as F

pytestmark = pytest.mark.db

URL = "/api/v1/mileage-rate"


def _rate_rule(db: Session, org: Any, rate: str, status: PolicyRuleStatus) -> Any:
    policy = f.make_policy(db, org=org, is_active=True)
    return f.make_policy_rule(
        db,
        policy=policy,
        rule_type=PolicyRuleType.mileage,
        applies_to=PolicyAppliesTo.all,
        definition={"check": "rate", "rate_per_mile": rate, "expense_categories": ["mileage"]},
        source_clause_ref="MIL-01",
        status=status,
    )


def test_returns_the_org_rate_the_server_charges(
    db_session: Session, org_a: Any, org_b: Any, employee_client: TestClient
) -> None:
    _rate_rule(db_session, org_a, "0.725", PolicyRuleStatus.active)
    _rate_rule(db_session, org_a, "0.10", PolicyRuleStatus.archived)  # inactive: ignored
    _rate_rule(db_session, org_b, "0.01", PolicyRuleStatus.active)  # other tenant: ignored
    db_session.flush()

    r = employee_client.get(URL)

    assert r.status_code == 200, r.text
    assert r.json() == {"rate_per_mile": "0.725", "currency": "USD", "rate_clause": "MIL-01"}


def test_falls_back_to_the_canonical_figure_and_is_read_only(
    db_session: Session, employee_client: TestClient
) -> None:
    before = db_session.scalar(sa.select(sa.func.count(AuditLog.id)))
    r = employee_client.get(URL)
    assert r.status_code == 200, r.text
    assert r.json()["rate_per_mile"] == F.MILEAGE_RATE_USD_PER_MILE
    assert db_session.scalar(sa.select(sa.func.count(AuditLog.id))) == before


def test_permission_gated_like_add_mileage(
    app: Any, finance_client: TestClient, employee_client_b: TestClient
) -> None:
    assert finance_client.get(URL).status_code == 403
    assert employee_client_b.get(URL).status_code == 200
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get(URL).status_code == 401
