"""Follow-up (grades), review round 3: the backfill of pre-existing pending approvals and
the three untested routing / evaluation guards.

* ``database.seeds.backfill_grade_routing`` routes a pending approval that already
  carried a grade flag before grade routing existed (no ``grade_routing`` key);
* ``_eligible`` refuses an ``employee``-role user even with a qualifying grade;
* a grade-entitlement rule whose definition the validator never saw (written straight
  to the DB) yields a ``not_evaluated`` flag, never a silently dropped line;
* ``_fallback_finance`` never routes an approval to its own submitter.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents import process_checks
from backend.app.modules.approval.grade_routing import (
    ROUTED_FALLBACK,
    ROUTED_MANAGER_CHAIN,
    _eligible,
    route_by_grade,
)
from backend.tests.test_fu_grade_entitlements_db import _entry, _grade_flags
from backend.tests.test_pg2_process_rules_db import _seed
from database.models.approval import Approval, ApprovalStatus
from database.models.audit_log import AuditLog
from database.models.expense_report import ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User, UserStatus
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule
from database.seeds import backfill_grade_routing as bf

pytestmark = pytest.mark.db

API = "/api/v1"
TODAY = date(2026, 11, 14)

_SVP_FLAG = {
    "flag_type": "policy_violation",
    "requires_approver": "SVP",
    "severity": "blocking",
    "source_clause_ref": "AIR-07",
}


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


@pytest.fixture()
def org(db_session: Session) -> Organization:
    org = f.make_org(db_session)
    _seed(db_session, org)
    return org


def _pending(db: Session, submitter: User, routed_to: User | None) -> Approval:
    """A pending approval as it existed BEFORE grade routing: an SVP flag, a plain routed
    id, and no ``grade_routing`` key."""
    report = f.make_expense_report(db, owner=submitter, status=ExpenseReportStatus.submitted)
    summary: dict[str, Any] = {"route": "escalate", "flags": [dict(_SVP_FLAG)]}
    if routed_to is not None:
        summary["routed_to_approver_id"] = str(routed_to.id)
    return f.make_approval(db, submitter=submitter, subject_id=report.id, risk_summary=summary)


def _audits(db: Session, approval: Approval, action: str) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(AuditLog.entity_id == approval.id, AuditLog.action == action)
        )
    )


def _factory(db: Session) -> Any:
    return lambda: contextlib.nullcontext(db)


# ---------------------------------------------------------------------------
# 1. Backfill of approvals that were already pending
# ---------------------------------------------------------------------------


def test_backfill_routes_a_pre_existing_svp_flag_to_the_svp_and_is_idempotent(
    db_session: Session, org: Organization, rbac_catalog: None, auth_as: Any
) -> None:
    svp = f.make_user(db_session, org=org, role=RoleKey.manager, grade="SVP", name="Sam SVP")
    vp = f.make_user(
        db_session, org=org, role=RoleKey.manager, grade="VP", manager_id=svp.id, name="Val VP"
    )
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=vp.id)
    approval = _pending(db_session, ic, vp)
    db_session.flush()

    # Before: stuck. The VP manager is refused, the qualified SVP cannot even see it.
    assert (
        auth_as(vp)
        .patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
        .status_code
        == 403
    )
    assert auth_as(svp).get(f"{API}/approvals/{approval.id}").status_code == 404
    assert approval.id in bf._stranded(db_session)

    assert bf.backfill(_factory(db_session)) == {"routed": 1, "failed": 0}
    db_session.refresh(approval)
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(svp.id)
    assert s["required_approver_grade"] == "SVP"
    assert s["grade_routing"]["routed_by"] == ROUTED_MANAGER_CHAIN
    routed = _audits(db_session, approval, "approval.routed_by_grade")
    assert len(routed) == 1 and routed[0].before_state == {"routed_to_approver_id": str(vp.id)}
    assert len(_audits(db_session, approval, "approval.grade_routing_backfilled")) == 1

    # After: the SVP sees it and decides it; the VP manager is no longer the owner.
    assert auth_as(svp).get(f"{API}/approvals/{approval.id}").status_code == 200
    assert s["routed_to_approver_id"] != str(vp.id)

    # Idempotent: a second run selects nothing and writes nothing.
    summary_before = dict(approval.risk_summary or {})
    assert bf._stranded(db_session) == []
    assert bf.backfill(_factory(db_session)) == {"routed": 0, "failed": 0}
    db_session.refresh(approval)
    assert approval.risk_summary == summary_before
    assert len(_audits(db_session, approval, "approval.routed_by_grade")) == 1
    assert len(_audits(db_session, approval, "approval.grade_routing_backfilled")) == 1

    ok = auth_as(svp).patch(f"{API}/approvals/{approval.id}", json={"decision": "approve"})
    assert ok.status_code == 200, ok.text


def test_backfill_leaves_decided_and_grade_free_approvals_alone(
    db_session: Session, org: Organization
) -> None:
    svp = f.make_user(db_session, org=org, role=RoleKey.manager, grade="SVP")
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=svp.id)
    decided = _pending(db_session, ic, None)
    decided.current_status = ApprovalStatus.approved
    plain = _pending(db_session, ic, None)
    plain.risk_summary = {"route": "escalate", "flags": [{"requires_approver": "manager"}]}
    db_session.flush()
    before = (dict(decided.risk_summary or {}), dict(plain.risk_summary or {}))
    assert bf._stranded(db_session) == []
    assert bf.backfill(_factory(db_session)) == {"routed": 0, "failed": 0}
    assert (dict(decided.risk_summary or {}), dict(plain.risk_summary or {})) == before


def test_backfill_main_exits_1_when_a_row_fails(
    db_session: Session, org: Organization, monkeypatch: pytest.MonkeyPatch
) -> None:
    svp = f.make_user(db_session, org=org, role=RoleKey.manager, grade="SVP")
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=svp.id)
    _pending(db_session, ic, None)
    db_session.flush()

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bf, "route_by_grade", boom)
    monkeypatch.setattr(bf, "get_sessionmaker", lambda: _factory(db_session))
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    assert bf.main() == 1


# ---------------------------------------------------------------------------
# 2. _eligible: only deciding roles
# ---------------------------------------------------------------------------


def test_employee_role_svp_in_the_chain_is_never_routed_to(
    db_session: Session, org: Organization
) -> None:
    svp_mgr = f.make_user(db_session, org=org, role=RoleKey.manager, grade="SVP")
    emp_svp = f.make_user(
        db_session, org=org, role=RoleKey.employee, grade="SVP", manager_id=svp_mgr.id
    )
    vp_mgr = f.make_user(
        db_session, org=org, role=RoleKey.manager, grade="VP", manager_id=emp_svp.id
    )
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=vp_mgr.id)
    approval = _pending(db_session, ic, vp_mgr)
    route_by_grade(db_session, approval)
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(svp_mgr.id)
    assert s["routed_to_approver_id"] != str(emp_svp.id)
    assert s["grade_routing"]["routed_by"] == ROUTED_MANAGER_CHAIN


def test_an_employee_role_user_is_not_eligible_whatever_the_grade() -> None:
    org_id = uuid.uuid4()
    approval = SimpleNamespace(organization_id=org_id, submitter_id=uuid.uuid4())

    def user(role: RoleKey) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            organization_id=org_id,
            status=UserStatus.active,
            role=role,
            grade="CSUITE",
        )

    assert _eligible(user(RoleKey.manager), approval=approval, need="SVP") is True  # type: ignore[arg-type]
    assert _eligible(user(RoleKey.finance_admin), approval=approval, need="SVP") is True  # type: ignore[arg-type]
    assert _eligible(user(RoleKey.employee), approval=approval, need="SVP") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Malformed rule definition: fail safe, never dropped
# ---------------------------------------------------------------------------


def test_a_malformed_grade_rule_yields_a_not_evaluated_flag(
    db_session: Session, org: Organization
) -> None:
    ic = f.make_user(db_session, org=org, grade="IC")
    rule = db_session.scalars(
        sa.select(PolicyRule)
        .join(Policy, Policy.id == PolicyRule.policy_id)
        .where(Policy.organization_id == org.id, PolicyRule.source_clause_ref == "AIR-07")
    ).first()
    assert rule is not None
    # Written straight to the DB: the activation validator never saw it.
    rule.definition = {**dict(rule.definition), "min_grade": "XYZ"}
    db_session.flush()
    flags = _grade_flags(db_session, ic, _entry("airfare", 20.0, description="lounge day pass"))
    assert len(flags) == 1, "the malformed rule's line must not be dropped"
    flag = flags[0]
    assert flag.flag_type == "not_evaluated"
    assert flag.observed["could_not_verify"] == "definition"
    assert flag.source_clause_ref == "AIR-07"


# ---------------------------------------------------------------------------
# 4. _fallback_finance never returns the submitter
# ---------------------------------------------------------------------------


def _earliest(db: Session, user: User, minutes: int) -> None:
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    user.created_at = base + dt.timedelta(minutes=minutes)
    db.flush()


def test_fallback_finance_never_picks_the_submitter(db_session: Session, org: Organization) -> None:
    fin_submitter = f.make_user(db_session, org=org, role=RoleKey.finance_admin, grade="DIRECTOR")
    other_fin = f.make_user(db_session, org=org, role=RoleKey.finance_admin, grade="DIRECTOR")
    _earliest(db_session, fin_submitter, 0)  # the EARLIEST finance admin is the submitter
    _earliest(db_session, other_fin, 5)
    approval = _pending(db_session, fin_submitter, None)
    route_by_grade(db_session, approval)
    s = approval.risk_summary or {}
    assert s["grade_routing"]["routed_by"] == ROUTED_FALLBACK
    assert s["routed_to_approver_id"] == str(other_fin.id)
    assert s["routed_to_approver_id"] != str(fin_submitter.id)


def test_fallback_finance_with_only_the_submitter_routes_to_nobody(
    db_session: Session, org: Organization
) -> None:
    fin_submitter = f.make_user(db_session, org=org, role=RoleKey.finance_admin, grade="DIRECTOR")
    approval = _pending(db_session, fin_submitter, None)
    route_by_grade(db_session, approval)
    s = approval.risk_summary or {}
    assert s["grade_routing"]["routed_by"] == ROUTED_FALLBACK
    assert s["routed_to_approver_id"] is None
