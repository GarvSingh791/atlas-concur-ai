"""C7-05 live-DB regression: the Approval Agent re-evaluates policy at approval time.

Mirrors the staging repro exactly: a report created through the API/manual path
has ``pre_submit_flags = NULL``; the manager triggers the approval agent; the
report must ESCALATE on the seeded AIR-PREF / HOTEL-CAP rules instead of being
auto-approved because "no flags" was read as "in policy".

Requires Postgres + the seeded demo org (self-skips when unreachable).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.approval_agent.agent import ApprovalAgent
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from config.constants.permissions import APPROVAL_DECIDE_REPORTS
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def seeded():
    try:
        from backend.app.core.database import get_engine
    except ImportError:
        pytest.skip("backend not importable")
    engine = get_engine()
    try:
        engine.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    if "approvals" not in sa.inspect(engine).get_table_names():
        pytest.skip("schema not migrated")
    from backend.app.core.database import get_sessionmaker
    from database.models.identity import Organization, User
    from database.seeds.seed_all import seed

    seed()
    with get_sessionmaker()() as s:
        org = s.scalar(sa.select(Organization).where(Organization.name == "Atlas Demo Corp"))
        employee = s.scalar(sa.select(User).where(User.email == "employee@atlas-demo.internal"))
        manager = s.scalar(sa.select(User).where(User.email == "manager@atlas-demo.internal"))
    return {"org_id": org.id, "employee_id": employee.id, "manager_id": manager.id}


@pytest.fixture()
def db_session(seeded) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


_DECISION = RoutingDecision(
    target_agent=AiAgent.approval,
    route=AiRoute.deterministic,
    task_type="approval_decision",
    confidence=0.95,
    rationale="c7-05-db",
)


def _run(session: Session, seeded: dict, report_id: uuid.UUID):
    ctx = AgentContext(
        organization_id=seeded["org_id"],
        user_id=seeded["manager_id"],
        granted_permission_keys=frozenset({APPROVAL_DECIDE_REPORTS}),
        correlation_id="c7-05-db",
    )
    task = Task(
        context_type="approval",
        payload={"subject_type": "expense_report", "subject_id": str(report_id)},
        signals={"approval_required": True},
    )
    result = ApprovalAgent(audit_writer=None).handle(task, _DECISION, context=ctx, session=session)
    approval = session.scalar(
        sa.select(Approval).where(
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id == report_id,
        )
    )
    return result, approval


def _manual_report(
    session: Session, seeded: dict, *, category: ExpenseCategory, amount: str, vendor: str
) -> ExpenseReport:
    """Exactly what POST /expense-reports + submit produce: pre_submit_flags NULL."""
    org_id, owner = seeded["org_id"], seeded["employee_id"]
    report = ExpenseReport(
        organization_id=org_id,
        owner_id=owner,
        title="C7-05 manual report",
        status=ExpenseReportStatus.submitted,
        total_amount=Decimal(amount),
        currency="USD",
        pre_submit_flags=None,
        created_by=owner,
        updated_by=owner,
    )
    session.add(report)
    session.flush()
    session.add(
        Expense(
            organization_id=org_id,
            report_id=report.id,
            category=category,
            amount=Decimal(amount),
            currency="USD",
            expense_date=date(2026, 11, 1),
            vendor=vendor,
            status=ExpenseStatus.draft,
            match_source=ExpenseMatchSource.manual,
            created_by=owner,
            updated_by=owner,
        )
    )
    session.flush()
    return report


def _clauses(approval: Approval) -> set[str]:
    return {
        f.get("source_clause_ref")
        for f in (approval.risk_summary or {}).get("flags", [])
        if f.get("flag_type") == "policy_violation"
    }


def test_skybridge_air_450_escalates(seeded: dict, db_session: Session) -> None:
    report = _manual_report(
        db_session,
        seeded,
        category=ExpenseCategory.airfare,
        amount="450.00",
        vendor="SKYBRIDGE AIR",
    )
    result, approval = _run(db_session, seeded, report.id)

    assert result.status == AgentResultStatus.ok
    assert result.output["route"] == "escalate"
    assert approval.current_status == ApprovalStatus.pending
    assert approval.is_auto_approved is False
    assert approval.risk_summary["is_policy_compliant"] is False
    assert approval.risk_summary["subject_in_policy"] is False
    assert "AIR-PREF" in _clauses(approval)
    assert approval.risk_summary["policy_rationale"]


def test_nyc_hotel_389_escalates(seeded: dict, db_session: Session) -> None:
    report = _manual_report(
        db_session,
        seeded,
        category=ExpenseCategory.lodging,
        amount="389.00",
        vendor="C7 Grand Hotel New York",
    )
    result, approval = _run(db_session, seeded, report.id)

    assert result.output["route"] == "escalate"
    assert approval.current_status == ApprovalStatus.pending
    assert "HOTEL-CAP" in _clauses(approval)


def test_clean_meal_still_auto_approves(seeded: dict, db_session: Session) -> None:
    report = _manual_report(
        db_session, seeded, category=ExpenseCategory.meals, amount="20.00", vendor="Cafe"
    )
    result, approval = _run(db_session, seeded, report.id)

    assert result.output["route"] == "auto_approve"
    assert approval.current_status == ApprovalStatus.auto_approved
    assert approval.risk_summary["is_policy_compliant"] is True
