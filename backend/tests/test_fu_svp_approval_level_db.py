"""Follow-up (grades): a real SVP approval level, end to end on Postgres.

Org chart (grades as in the demo seed):
    svp   (manager, SVP)  <- ic        (IC)
    svp   (manager, SVP)  <- vp_mid    (manager, VP) <- ic_deep (IC)
    vp    (manager, VP)   <- senior    (Senior IC)
    finance (finance_admin, Director)

* an SVP-required flag routes up the manager chain to the first SVP+ approver, else a
  finance admin of sufficient grade, else a finance admin with an explicit
  "no approver of the required grade" rationale (audited);
* a VP (or an ungraded manager / Director finance admin) cannot decide it:
  403 INSUFFICIENT_APPROVER_GRADE — the SVP manager can;
* the SVP routed further up the chain sees the approval (list, detail) and decides it;
* nothing changes for approvals whose flags name no grade; SoD is unchanged.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.agents import process_checks
from backend.app.modules.approval.lifecycle import open_approval_on_submission
from backend.tests.test_pg2_process_rules_db import _seed
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.expense import ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization, RoleKey, User

pytestmark = pytest.mark.db

API = "/api/v1"
TODAY = date(2026, 11, 14)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_checks, "today_utc", lambda: TODAY)


@pytest.fixture()
def org(db_session: Session) -> Organization:
    org = f.make_org(db_session)
    _seed(db_session, org)
    return org


@pytest.fixture()
def people(db_session: Session, org: Organization, rbac_catalog: None) -> dict[str, User]:
    mk = f.make_user
    svp = mk(db_session, org=org, role=RoleKey.manager, grade="SVP", name="Sam SVP")
    vp = mk(db_session, org=org, role=RoleKey.manager, grade="VP", name="Val VP")
    vp_mid = mk(db_session, org=org, role=RoleKey.manager, grade="VP", manager_id=svp.id)
    finance = mk(db_session, org=org, role=RoleKey.finance_admin, grade="DIRECTOR")
    return {
        "svp": svp,
        "vp": vp,
        "vp_mid": vp_mid,
        "finance": finance,
        "ic": mk(db_session, org=org, grade="IC", manager_id=svp.id),
        "ic_deep": mk(db_session, org=org, grade="IC", manager_id=vp_mid.id),
        "senior": mk(db_session, org=org, grade="SENIOR_IC", manager_id=vp.id),
    }


def _submit(
    db: Session, owner: User, *, description: str = "lounge day pass", **entry: Any
) -> Approval:
    """A submitted report with one $20 line, opened exactly as the owner's submit does."""
    report = f.make_expense_report(
        db, owner=owner, status=ExpenseReportStatus.submitted, total_amount="20.00"
    )
    f.make_expense(
        db,
        report=report,
        category=entry.pop("category", ExpenseCategory.other),
        amount="20.00",
        expense_date=TODAY,
        vendor=entry.pop("vendor", "Airport"),
        description=description,
        justification=entry.pop("justification", "Flight delayed 5 hours"),
    )
    return open_approval_on_submission(
        db,
        organization_id=owner.organization_id,
        subject_type=ApprovalSubjectType.expense_report,
        subject_id=report.id,
        actor=owner,
    )


def _decide(client: Any, approval: Approval, decision: str = "approve") -> Any:
    body: dict[str, Any] = {"decision": decision}
    if decision != "approve":
        body["reason"] = "Not covered"
    return client.patch(f"{API}/approvals/{approval.id}", json=body)


def _code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_svp_required_item_routes_to_the_svp_manager(
    db_session: Session, people: dict[str, User]
) -> None:
    approval = _submit(db_session, people["ic"])
    s = approval.risk_summary or {}
    assert approval.current_status == ApprovalStatus.pending
    assert s["required_approver_grade"] == "SVP"
    assert s["routed_to_approver_id"] == str(people["svp"].id)
    assert s["grade_routing"]["routed_by"] == "manager_chain"
    assert s["grade_routing"]["clauses"] == ["AIR-07"]


def test_chain_walk_skips_a_vp_to_the_svp_above(
    db_session: Session, people: dict[str, User]
) -> None:
    approval = _submit(db_session, people["ic_deep"])
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(people["svp"].id)
    audit = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_id == approval.id, AuditLog.action == "approval.routed_by_grade"
        )
    ).one()
    assert audit.before_state == {"routed_to_approver_id": str(people["vp_mid"].id)}
    assert audit.actor_id is None


def test_no_svp_anywhere_falls_back_to_finance_with_a_rationale(
    db_session: Session, people: dict[str, User]
) -> None:
    approval = _submit(db_session, people["senior"])
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(people["finance"].id)
    assert s["grade_routing"]["routed_by"] == "fallback_no_approver_of_grade"
    assert (
        "No approver of grade Senior Vice President (SVP) or above"
        in (s["grade_routing"]["rationale"])
    )


def test_finance_admin_of_sufficient_grade_is_preferred_over_the_fallback(
    db_session: Session, org: Organization, people: dict[str, User]
) -> None:
    csuite_fin = f.make_user(db_session, org=org, role=RoleKey.finance_admin, grade="CSUITE")
    approval = _submit(db_session, people["senior"])
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(csuite_fin.id)
    assert s["grade_routing"]["routed_by"] == "finance_admin"


def test_no_grade_flag_keeps_the_usual_routing(
    db_session: Session, people: dict[str, User]
) -> None:
    approval = _submit(db_session, people["senior"], description="Taxi", vendor="Yellow Cab")
    s = approval.risk_summary or {}
    assert "required_approver_grade" not in s
    assert s.get("routed_to_approver_id") in (None, str(people["vp"].id))


# ---------------------------------------------------------------------------
# Enforcement (API)
# ---------------------------------------------------------------------------


def test_vp_cannot_approve_an_svp_item_but_the_svp_manager_can(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    # senior's line manager is the VP; ic's is the SVP — both SVP-required.
    vp_item = _submit(db_session, people["senior"])
    resp = _decide(auth_as(people["vp"]), vp_item)
    assert resp.status_code == 403 and _code(resp) == "INSUFFICIENT_APPROVER_GRADE"
    for decision in ("reject", "request_changes"):
        assert _code(_decide(auth_as(people["vp"]), vp_item, decision)) == (
            "INSUFFICIENT_APPROVER_GRADE"
        )
    # Director finance admin: also below SVP
    assert _code(_decide(auth_as(people["finance"]), vp_item)) == "INSUFFICIENT_APPROVER_GRADE"
    db_session.refresh(vp_item)
    assert vp_item.current_status == ApprovalStatus.pending
    svp_item = _submit(db_session, people["ic"])
    ok = _decide(auth_as(people["svp"]), svp_item)
    assert ok.status_code == 200, ok.text
    assert ok.json()["current_status"] == "approved"


def test_svp_further_up_the_chain_sees_and_decides(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    approval = _submit(db_session, people["ic_deep"])
    client = auth_as(people["svp"])
    listed = client.get(f"{API}/approvals").json()["items"]
    row = next(r for r in listed if r["id"] == str(approval.id))
    assert row["required_approver_grade"] == "SVP"
    detail = client.get(f"{API}/approvals/{approval.id}").json()
    req = detail["grade_requirement"]
    assert req["required_short"] == "SVP" and req["viewer_can_decide"] is True
    assert req["routed_to_name"] == "Sam SVP" and req["clauses"] == ["AIR-07"]
    # the VP direct manager sees it but may not decide; the detail says so
    vp_view = auth_as(people["vp_mid"]).get(f"{API}/approvals/{approval.id}").json()
    assert vp_view["grade_requirement"]["viewer_can_decide"] is False
    assert "Vice President" in vp_view["grade_requirement"]["message"]
    assert _decide(auth_as(people["vp_mid"]), approval).status_code == 403
    assert _decide(auth_as(people["svp"]), approval).status_code == 200


def test_routed_scope_does_not_leak_other_approvals(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    # The VP never sees ic's approval (not a report, not routed to them).
    approval = _submit(db_session, people["ic"])
    resp = auth_as(people["vp"]).get(f"{API}/approvals/{approval.id}")
    assert resp.status_code == 404


def test_ungraded_manager_refused_on_a_legacy_vp_flag(
    db_session: Session, org: Organization, auth_as: Any, rbac_catalog: None
) -> None:
    mgr = f.make_user(db_session, org=org, role=RoleKey.manager)
    emp = f.make_user(db_session, org=org, manager_id=mgr.id)
    report = f.make_expense_report(db_session, owner=emp, status=ExpenseReportStatus.submitted)
    flag = {
        "flag_type": "policy_violation",
        "requires_approver": "VP",
        "severity": "blocking",
        "source_clause_ref": "AIR-CABIN",
    }
    approval = f.make_approval(
        db_session,
        submitter=emp,
        subject_id=report.id,
        risk_summary={"route": "escalate", "flags": [flag], "routed_to_approver_id": str(mgr.id)},
    )
    resp = _decide(auth_as(mgr), approval)
    assert resp.status_code == 403 and _code(resp) == "INSUFFICIENT_APPROVER_GRADE"
    mgr.grade = "VP"
    db_session.flush()
    assert _decide(auth_as(mgr), approval).status_code == 200


def test_sod_unchanged_nobody_decides_their_own(
    db_session: Session, org: Organization, people: dict[str, User], auth_as: Any
) -> None:
    # An SVP-graded finance admin files a VP-level item: routed to someone else, and
    # their own grade never lets them decide it (SoD is checked first).
    own = f.make_user(db_session, org=org, role=RoleKey.finance_admin, grade="CSUITE")
    approval = _submit(
        db_session, own, description="lounge day pass", category=ExpenseCategory.other
    )
    s = approval.risk_summary or {}
    assert s.get("routed_to_approver_id") != str(own.id)
    resp = _decide(auth_as(own), approval)
    assert resp.status_code == 403 and _code(resp) == "FORBIDDEN"
    # The VP's own SVP-required item: never routed to the VP, refused for SoD.
    vp_own = _submit(db_session, people["vp"])
    assert (vp_own.risk_summary or {})["routed_to_approver_id"] != str(people["vp"].id)
    assert _code(_decide(auth_as(people["vp"]), vp_own)) == "FORBIDDEN"


def test_entitled_owner_needs_no_svp(db_session: Session, people: dict[str, User]) -> None:
    # The SVP's OWN lounge fee is in policy (advisory note): no grade requirement.
    report_owner = people["svp"]
    approval = _submit(db_session, report_owner, justification=None)
    assert "required_approver_grade" not in (approval.risk_summary or {})
    assert db_session.get(ExpenseReport, approval.subject_id) is not None


def test_svp_up_the_chain_is_notified(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    approval = _submit(db_session, people["ic_deep"])
    items = auth_as(people["svp"]).get(f"{API}/notifications").json()["items"]
    assert ("approval_pending", str(approval.id)) in [(i["kind"], i["subject_id"]) for i in items]


def test_a_self_managed_approver_is_never_routed_their_own_item(
    db_session: Session, org: Organization
) -> None:
    from backend.app.modules.approval.lifecycle import ensure_independent_approver

    boss = f.make_user(db_session, org=org, role=RoleKey.manager, grade="SVP")
    boss.manager_id = boss.id  # legacy data: their own manager
    db_session.flush()
    report = f.make_expense_report(db_session, owner=boss, status=ExpenseReportStatus.submitted)
    flag = {
        "flag_type": "late_submission",
        "requires_approver": "VP",
        "source_clause_ref": "EXP-01",
    }
    approval = f.make_approval(
        db_session,
        submitter=boss,
        subject_id=report.id,
        risk_summary={"route": "escalate", "flags": [flag], "routed_to_approver_id": str(boss.id)},
    )
    ensure_independent_approver(db_session, approval)
    assert (approval.risk_summary or {})["routed_to_approver_id"] != str(boss.id)


# ---------------------------------------------------------------------------
# Review fixes: every routing / scope guard is red-when-removed
# ---------------------------------------------------------------------------

_SVP_FLAG = {
    "flag_type": "policy_violation",
    "requires_approver": "SVP",
    "severity": "blocking",
    "source_clause_ref": "AIR-07",
}


def _routing_audits(db: Session, approval: Approval) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(
                AuditLog.entity_id == approval.id, AuditLog.action == "approval.routed_by_grade"
            )
        )
    )


def test_inactive_svp_in_the_chain_is_skipped(
    db_session: Session, org: Organization, people: dict[str, User]
) -> None:
    from database.models.identity import UserStatus

    gone = f.make_user(
        db_session,
        org=org,
        role=RoleKey.manager,
        grade="SVP",
        manager_id=people["svp"].id,
        status=UserStatus.inactive,
    )
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=gone.id)
    approval = _submit(db_session, ic)
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] == str(people["svp"].id)
    assert s["grade_routing"]["routed_by"] == "manager_chain"


def test_inactive_only_svp_falls_through_to_the_finance_step(
    db_session: Session, org: Organization, people: dict[str, User]
) -> None:
    from database.models.identity import UserStatus

    gone = f.make_user(
        db_session, org=org, role=RoleKey.manager, grade="SVP", status=UserStatus.inactive
    )
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=gone.id)
    s = _submit(db_session, ic).risk_summary or {}
    assert s["routed_to_approver_id"] == str(people["finance"].id)
    assert s["grade_routing"]["routed_by"] == "fallback_no_approver_of_grade"


@pytest.mark.parametrize("status", [ApprovalStatus.approved, ApprovalStatus.rejected])
def test_a_decided_approval_is_never_rerouted(
    db_session: Session, people: dict[str, User], status: ApprovalStatus
) -> None:
    from backend.app.modules.approval.grade_routing import route_by_grade

    report = f.make_expense_report(
        db_session, owner=people["ic"], status=ExpenseReportStatus.submitted
    )
    summary = {"route": "escalate", "flags": [_SVP_FLAG], "routed_to_approver_id": "x"}
    approval = f.make_approval(
        db_session,
        submitter=people["ic"],
        subject_id=report.id,
        current_status=status,
        risk_summary=dict(summary),
    )
    route_by_grade(db_session, approval)
    assert approval.risk_summary == summary
    assert _routing_audits(db_session, approval) == []


def test_stored_required_grade_survives_rewritten_flags(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    # The flags no longer name a grade, but the server-written requirement says SVP:
    # the more senior one wins (fail closed) — the VP line manager is refused.
    senior = people["senior"]
    report = f.make_expense_report(db_session, owner=senior, status=ExpenseReportStatus.submitted)
    approval = f.make_approval(
        db_session,
        submitter=senior,
        subject_id=report.id,
        risk_summary={
            "route": "escalate",
            "flags": [{"flag_type": "policy_violation", "source_clause_ref": "AIR-07"}],
            "required_approver_grade": "SVP",
            "routed_to_approver_id": str(people["vp"].id),
        },
    )
    resp = _decide(auth_as(people["vp"]), approval)
    assert resp.status_code == 403 and _code(resp) == "INSUFFICIENT_APPROVER_GRADE"
    db_session.refresh(approval)
    assert approval.current_status == ApprovalStatus.pending


def _foreign_routed_approval(db: Session, target: User) -> Approval:
    """An approval in ANOTHER org whose (corrupt / forged) summary routes it by grade to
    *target*."""
    other = f.make_org(db)
    owner = f.make_user(db, org=other, grade="IC")
    report = f.make_expense_report(db, owner=owner, status=ExpenseReportStatus.submitted)
    return f.make_approval(
        db,
        submitter=owner,
        subject_id=report.id,
        risk_summary={
            "route": "escalate",
            "flags": [_SVP_FLAG],
            "required_approver_grade": "SVP",
            "grade_routing": {"required_grade": "SVP", "routed_by": "manager_chain"},
            "routed_to_approver_id": str(target.id),
        },
    )


def test_routed_scope_never_crosses_tenants(
    db_session: Session, people: dict[str, User], auth_as: Any
) -> None:
    svp = people["svp"]
    foreign = _foreign_routed_approval(db_session, svp)
    client = auth_as(svp)
    listed = client.get(f"{API}/approvals").json()["items"]
    assert str(foreign.id) not in [r["id"] for r in listed]
    assert client.get(f"{API}/approvals/{foreign.id}").status_code == 404
    items = client.get(f"{API}/notifications").json()["items"]
    assert str(foreign.id) not in [i["subject_id"] for i in items]
    assert _decide(client, foreign).status_code == 404
    db_session.refresh(foreign)
    assert foreign.current_status == ApprovalStatus.pending


def test_chain_crossing_into_another_org_falls_back_in_tenant(
    db_session: Session, org: Organization, people: dict[str, User], auth_as: Any
) -> None:
    # Legacy data: a VP whose manager_id points at a C-suite manager in ANOTHER org.
    other = f.make_org(db_session)
    outsider = f.make_user(db_session, org=other, role=RoleKey.manager, grade="CSUITE")
    vp = f.make_user(db_session, org=org, role=RoleKey.manager, grade="VP", manager_id=outsider.id)
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=vp.id)
    approval = _submit(db_session, ic)
    s = approval.risk_summary or {}
    assert s["routed_to_approver_id"] != str(outsider.id)
    assert s["routed_to_approver_id"] == str(people["finance"].id)
    assert s["grade_routing"]["routed_by"] == "fallback_no_approver_of_grade"
    assert auth_as(outsider).get(f"{API}/approvals/{approval.id}").status_code == 404


def test_chain_never_walks_through_another_org(
    db_session: Session, org: Organization, people: dict[str, User]
) -> None:
    # VP (org A) -> outsider (org B, not eligible) -> SVP (org A).  The walk stops at the
    # tenant boundary: an out-of-tenant link is never followed back in.
    other = f.make_org(db_session)
    outsider = f.make_user(db_session, org=other, grade="IC", manager_id=people["svp"].id)
    vp = f.make_user(db_session, org=org, role=RoleKey.manager, grade="VP", manager_id=outsider.id)
    ic = f.make_user(db_session, org=org, grade="IC", manager_id=vp.id)
    s = _submit(db_session, ic).risk_summary or {}
    assert s["routed_to_approver_id"] == str(people["finance"].id)
    assert s["grade_routing"]["routed_by"] == "fallback_no_approver_of_grade"
