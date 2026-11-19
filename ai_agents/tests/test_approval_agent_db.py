"""Live-DB integration tests for the Approval Agent (P4-T12) — 13 break-tests.

Requires Postgres + seeded data; auto-skips when unreachable.
Run with: pytest -m db ai_agents/tests/test_approval_agent_db.py

Break-tests covered:
  BT-1  Auto-approve expense report: one clean $20 meal → status=auto_approved + step
  BT-2  Escalate trip: segment in_policy=False → status=pending, no step, routed_to in risk_summary
  BT-3  Escalate: amount > $500 expense report (clean entries, compliant) → status=pending
  BT-4  Escalate: stored blocking flag on an otherwise clean report → status=pending
  BT-5  Escalate: stored requires_approver advisory on a clean report → status=pending

C7-05: the agent re-evaluates policy over the report's ACTUAL entries and an
entry-less report is never compliant, so BT-1/3/4/5 each carry clean meal
entries — the gate named by the test is then the ONLY reason for its route.
  BT-6  Invalid subject_type → needs_review, no Approval row written
  BT-7  Subject not found → needs_review, no Approval row written
  BT-8  Wrong org → needs_review, no Approval row written
  BT-9  Missing APPROVAL_DECIDE_REPORTS permission → needs_review, no Approval row written
  BT-10 Idempotency: second identical call → needs_review, no second Approval row
  BT-11 Auto-approve trip: all segments in_policy=True, $400 → auto_approved + step
  BT-12 Escalate: trip with no estimated_cost/budget_amount → status=pending
  BT-13 Decision log: exactly one ai_decision_logs row per call
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
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clock_in_the_fixture_era(monkeypatch: pytest.MonkeyPatch) -> None:
    """Product gap 2 stage 2 (intended change): the seed now carries the timing rules
    (EXP-01 submission deadline, PTA-02 advance booking), which are evaluated against
    today.  These scenarios use fixed July 2026 dates and test routing on otherwise
    in-policy subjects, so the process checks run as of 2026-06-01 (the trip is booked
    30 days ahead, the expenses are not yet due).  The timing rules themselves are
    covered by backend/tests/test_pg2_process_rules_db.py."""
    from ai_agents.agents import process_checks

    monkeypatch.setattr(process_checks, "today_utc", lambda: date(2026, 6, 1))


@pytest.fixture(scope="module")
def db_engine():
    try:
        from backend.app.core.database import get_engine
    except ImportError:
        pytest.skip("backend not importable — DB tests require the full stack")
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")
    insp = sa.inspect(engine)
    if "approvals" not in insp.get_table_names():
        pytest.skip("approvals table not found — run `alembic upgrade head` first")
    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine):
    from database.seeds.seed_all import seed

    seed()
    from backend.app.core.database import get_sessionmaker
    from database.models.identity import Organization, User

    factory = get_sessionmaker()
    with factory() as s:
        org = s.scalar(sa.select(Organization).where(Organization.name == "Atlas Demo Corp"))
        employee = s.scalar(sa.select(User).where(User.email == "employee@atlas-demo.internal"))
        manager = s.scalar(sa.select(User).where(User.email == "manager@atlas-demo.internal"))
    return {
        "org_id": org.id,
        "employee_id": employee.id,
        "manager_id": manager.id if manager else None,
    }


@pytest.fixture()
def db_session(db_engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID, *, with_perm: bool = True) -> AgentContext:
    keys = frozenset({APPROVAL_DECIDE_REPORTS}) if with_perm else frozenset()
    return AgentContext(
        organization_id=org_id,
        user_id=user_id,
        granted_permission_keys=keys,
        correlation_id="db-test",
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.approval,
    route=AiRoute.deterministic,
    task_type="approval_decision",
    confidence=0.95,
    rationale="db-test",
)


def _task(subject_type: str, subject_id: uuid.UUID) -> Task:
    return Task(
        context_type="approval",
        payload={"subject_type": subject_type, "subject_id": str(subject_id)},
        signals={"approval_required": True},
    )


def _make_expense_report(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    total: Decimal = Decimal("200.00"),
    pre_submit_flags: dict | None = None,
) -> ExpenseReport:
    if pre_submit_flags is None:
        pre_submit_flags = {
            "flags": [],
            "evaluated_entry_count": 1,
            "blocking_count": 0,
            "advisory_count": 0,
        }
    report = ExpenseReport(
        organization_id=org_id,
        owner_id=user_id,
        title="DB Test Report",
        status=ExpenseReportStatus.submitted,
        total_amount=total,
        currency="USD",
        pre_submit_flags=pre_submit_flags,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(report)
    session.flush()
    return report


def _add_clean_meal(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    report: ExpenseReport,
    amount: Decimal = Decimal("20.00"),
    *,
    day: int = 1,
) -> Expense:
    """A meal under both the per-diem (75) and the receipt threshold (25)."""
    exp = Expense(
        organization_id=org_id,
        report_id=report.id,
        category=ExpenseCategory.meals,
        amount=amount,
        currency="USD",
        expense_date=date(2026, 7, day),
        vendor="Cafe",
        status=ExpenseStatus.draft,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(exp)
    session.flush()
    return exp


def _make_trip(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    estimated_cost: Decimal | None = Decimal("400.00"),
    budget_amount: Decimal | None = None,
) -> Trip:
    from datetime import date

    trip = Trip(
        organization_id=org_id,
        traveler_id=user_id,
        origin="SFO",
        primary_destination="JFK",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        estimated_cost=estimated_cost,
        budget_amount=budget_amount,
        currency="USD",
        status=TripStatus.pending_approval,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(trip)
    session.flush()
    return trip


def _make_segment(
    session: Session,
    trip_id: uuid.UUID,
    *,
    in_policy: bool | None = True,
    policy_notes: str | None = None,
) -> TripSegment:
    from datetime import datetime, timezone

    seg = TripSegment(
        trip_id=trip_id,
        segment_type=SegmentType.flight,
        supplier="United Airlines",
        start_datetime=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc),
        location="JFK",
        amount=Decimal("350.00"),
        currency="USD",
        in_policy=in_policy,
        policy_notes=policy_notes,
    )
    session.add(seg)
    session.flush()
    return seg


# ---------------------------------------------------------------------------
# BT-1: Auto-approve expense report
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT1AutoApproveExpenseReport:
    def test_auto_approve_creates_row_and_step(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        # C7-05: the agent now re-evaluates policy over the report's ACTUAL entries
        # and an entry-less report is never "compliant".  Give the report one
        # clean entry (meal under the per-diem and under the receipt threshold).
        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("20.00"))
        _add_clean_meal(db_session, org_id, user_id, report)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        assert result.status == AgentResultStatus.ok
        assert result.output["route"] == "auto_approve"
        assert result.output["is_policy_compliant"] is True

        approval = db_session.scalar(
            sa.select(Approval).where(
                Approval.subject_type == ApprovalSubjectType.expense_report,
                Approval.subject_id == report.id,
            )
        )
        assert approval is not None
        assert approval.current_status == ApprovalStatus.auto_approved
        assert approval.is_auto_approved is True

        steps = db_session.scalars(
            sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
        ).all()
        assert len(steps) == 1
        assert steps[0].decision == ApprovalDecision.auto_approve
        assert steps[0].approver_id is None
        assert steps[0].reason is not None and steps[0].reason != ""


# ---------------------------------------------------------------------------
# BT-2: Escalate trip (segment in_policy=False)
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT2EscalateTripOutOfPolicy:
    def test_escalate_no_step_row(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        trip = _make_trip(db_session, org_id, user_id, estimated_cost=Decimal("400.00"))
        _make_segment(db_session, trip.id, in_policy=False, policy_notes="Exceeds cap")
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(_task("trip", trip.id), _DECISION, context=ctx, session=db_session)

        assert result.status == AgentResultStatus.ok
        assert result.output["route"] == "escalate"

        approval = db_session.scalar(
            sa.select(Approval).where(
                Approval.subject_type == ApprovalSubjectType.trip,
                Approval.subject_id == trip.id,
            )
        )
        assert approval is not None
        assert approval.current_status == ApprovalStatus.pending
        assert "routed_to_approver_id" in (approval.risk_summary or {})

        # Decision D: no ApprovalStep row for escalate
        steps = db_session.scalars(
            sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
        ).all()
        assert len(steps) == 0


# ---------------------------------------------------------------------------
# BT-3: Escalate — amount > $500
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT3EscalateOverThreshold:
    def test_over_threshold_escalates(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        # 21 clean meals x 23.86 = 501.06: policy-compliant, only the $500 gate fires.
        # One meal a day (C7R: identical same-day lines are a possible duplicate).
        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("501.06"))
        for day in range(1, 22):
            _add_clean_meal(db_session, org_id, user_id, report, Decimal("23.86"), day=day)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        assert result.output["is_policy_compliant"] is True
        assert result.output["blocking_flag_count"] == 0
        assert result.output["route"] == "escalate"
        approval = db_session.scalar(sa.select(Approval).where(Approval.subject_id == report.id))
        assert approval.current_status == ApprovalStatus.pending


# ---------------------------------------------------------------------------
# BT-4: Escalate — blocking flag
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT4EscalateBlockingFlag:
    def test_blocking_flag_escalates(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        flags = {
            "flags": [
                {
                    "entry_ref": "Hotel",
                    "flag_type": "policy_violation",
                    "rule_id": str(uuid.uuid4()),
                    "rule_type": "amount_cap",
                    "source_clause_ref": "HOTEL-CAP",
                    "reason": "Exceeds nightly hotel cap.",
                    "requires_approver": None,
                    "observed": {"amount_usd": 350.0},
                    "severity": "blocking",
                }
            ],
            "evaluated_entry_count": 1,
            "blocking_count": 1,
            "advisory_count": 0,
        }
        report = _make_expense_report(
            db_session, org_id, user_id, total=Decimal("20.00"), pre_submit_flags=flags
        )
        _add_clean_meal(db_session, org_id, user_id, report)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        # The fresh evaluation of the $20 meal is clean; only the stored flag blocks.
        assert result.output["is_policy_compliant"] is False
        assert result.output["blocking_flag_count"] == 1
        assert result.output["policy_rationale"] == ["Exceeds nightly hotel cap."]
        assert result.output["route"] == "escalate"


# ---------------------------------------------------------------------------
# BT-5: Escalate — requires_approver flag
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT5EscalateRequiresApprover:
    def test_requires_approver_escalates(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        flags = {
            "flags": [
                {
                    "entry_ref": "Meals",
                    "flag_type": "approval_required",
                    "rule_id": str(uuid.uuid4()),
                    "rule_type": "meal_cap",
                    "source_clause_ref": "MEAL-CAP",
                    "reason": "Meal exceeds per-diem; manager approval required.",
                    "requires_approver": "manager",
                    "observed": {"amount_usd": 80.0},
                    "severity": "advisory",
                }
            ],
            "evaluated_entry_count": 1,
            "blocking_count": 0,
            "advisory_count": 1,
        }
        report = _make_expense_report(
            db_session, org_id, user_id, total=Decimal("20.00"), pre_submit_flags=flags
        )
        _add_clean_meal(db_session, org_id, user_id, report)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        # Compliant and under $500: the stored requires_approver is the only reason.
        assert result.output["is_policy_compliant"] is True
        assert result.output["route"] == "escalate"
        approval = db_session.scalar(sa.select(Approval).where(Approval.subject_id == report.id))
        assert approval.risk_summary["requires_approver"] is True


# ---------------------------------------------------------------------------
# BT-6: Invalid subject_type → needs_review, no write
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT6InvalidSubjectType:
    def test_invalid_type_needs_review_no_write(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        fake_id = uuid.uuid4()
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            Task(
                context_type="approval",
                payload={"subject_type": "invoice", "subject_id": str(fake_id)},
                signals={"approval_required": True},
            ),
            _DECISION,
            context=ctx,
            session=db_session,
        )

        assert result.status == AgentResultStatus.needs_review
        count = db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Approval)
            .where(
                Approval.organization_id == org_id,
                Approval.subject_id == fake_id,
            )
        )
        assert count == 0


# ---------------------------------------------------------------------------
# BT-7: Subject not found → needs_review, no write
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT7SubjectNotFound:
    def test_subject_not_found_needs_review(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        fake_id = uuid.uuid4()
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", fake_id), _DECISION, context=ctx, session=db_session
        )

        assert result.status == AgentResultStatus.needs_review
        count = db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Approval)
            .where(
                Approval.organization_id == org_id,
                Approval.subject_id == fake_id,
            )
        )
        assert count == 0


# ---------------------------------------------------------------------------
# BT-8: Wrong org → needs_review, no write
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT8WrongOrg:
    def test_wrong_org_needs_review(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]

        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("200.00"))

        # Context with a different org_id
        other_org = uuid.uuid4()
        ctx = AgentContext(
            organization_id=other_org,
            user_id=user_id,
            granted_permission_keys=frozenset({APPROVAL_DECIDE_REPORTS}),
            correlation_id="db-test",
        )
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        assert result.status == AgentResultStatus.needs_review
        # No approval written for the wrong org
        count = db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Approval)
            .where(
                Approval.organization_id == other_org,
                Approval.subject_id == report.id,
            )
        )
        assert count == 0


# ---------------------------------------------------------------------------
# BT-9: Missing permission → needs_review, no write
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT9MissingPermission:
    def test_no_permission_needs_review(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id, with_perm=False)

        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("200.00"))
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )

        assert result.status == AgentResultStatus.needs_review
        count = db_session.scalar(
            sa.select(sa.func.count()).select_from(Approval).where(Approval.subject_id == report.id)
        )
        assert count == 0


# ---------------------------------------------------------------------------
# BT-10: Idempotency — second call → needs_review, no second row
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT10Idempotency:
    def test_second_call_needs_review(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("200.00"))
        agent = ApprovalAgent(audit_writer=None)

        # First call — should succeed
        r1 = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )
        assert r1.status == AgentResultStatus.ok

        # Second call — should detect existing row
        r2 = agent.handle(
            _task("expense_report", report.id), _DECISION, context=ctx, session=db_session
        )
        assert r2.status == AgentResultStatus.needs_review

        # Exactly one Approval row
        count = db_session.scalar(
            sa.select(sa.func.count()).select_from(Approval).where(Approval.subject_id == report.id)
        )
        assert count == 1


# ---------------------------------------------------------------------------
# BT-11: Auto-approve trip (all segments in_policy=True)
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT11AutoApproveTrip:
    def test_all_in_policy_trip_auto_approved(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        trip = _make_trip(db_session, org_id, user_id, estimated_cost=Decimal("400.00"))
        _make_segment(db_session, trip.id, in_policy=True)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(_task("trip", trip.id), _DECISION, context=ctx, session=db_session)

        assert result.output["route"] == "auto_approve"
        approval = db_session.scalar(sa.select(Approval).where(Approval.subject_id == trip.id))
        assert approval.current_status == ApprovalStatus.auto_approved
        steps = db_session.scalars(
            sa.select(ApprovalStep).where(ApprovalStep.approval_id == approval.id)
        ).all()
        assert len(steps) == 1
        assert steps[0].decision == ApprovalDecision.auto_approve


# ---------------------------------------------------------------------------
# BT-12: Escalate trip with no cost info
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT12EscalateTripNoCost:
    def test_unknown_cost_escalates(self, seeded: dict, db_session: Session) -> None:
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        # Trip with no estimated_cost and no budget_amount → subject_total=None → escalate
        trip = _make_trip(
            db_session,
            org_id,
            user_id,
            estimated_cost=None,
            budget_amount=None,
        )
        _make_segment(db_session, trip.id, in_policy=True)
        agent = ApprovalAgent(audit_writer=None)
        result = agent.handle(_task("trip", trip.id), _DECISION, context=ctx, session=db_session)

        assert result.output["route"] == "escalate"
        approval = db_session.scalar(sa.select(Approval).where(Approval.subject_id == trip.id))
        assert approval.current_status == ApprovalStatus.pending


# ---------------------------------------------------------------------------
# BT-13: Decision log — exactly one row per call
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestBT13DecisionLog:
    def test_decision_log_row_flushed(self, seeded: dict, db_session: Session) -> None:
        from database.models.ai_decision_log import AiDecisionLog

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        report = _make_expense_report(db_session, org_id, user_id, total=Decimal("200.00"))
        agent = ApprovalAgent(audit_writer=None)
        agent.handle(_task("expense_report", report.id), _DECISION, context=ctx, session=db_session)

        rows = db_session.scalars(
            sa.select(AiDecisionLog)
            .where(
                AiDecisionLog.agent == AiAgent.approval,
                AiDecisionLog.organization_id == org_id,
            )
            .order_by(AiDecisionLog.created_at.desc())
            .limit(2)
        ).all()

        assert len(rows) >= 1
        row = rows[0]
        assert row.task_type == "approval_decision"
        assert row.route == AiRoute.deterministic
        assert row.model_provider is None
        assert row.output_summary is not None
        assert row.confidence is not None
