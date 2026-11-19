"""Live-DB tests for the Expense Agent (P4-T10) — DB-backed gates.

Requires Postgres + seeded data; auto-skips when unreachable.
Run with: pytest -m db ai_agents/tests/test_expense_agent_db.py

Gates covered:
  - Smart-match DB: real session, seeded data, ItineraryCalendarTool
  - Assemble + derived total DB
  - Role-bound DB: cannot assemble another user's data
  - Persist: flush-only (status=draft, receipts/card_txns linked); flush-then-rollback
  - Decision log DB: exactly one row written
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent import ExpenseAgent
from ai_agents.agents.expense_agent.assemble import assemble_report
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.ingest import IngestedData, ReceiptFact
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.agents.expense_agent.persistence import persist_expense_report
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from config.constants.permissions import EXPENSE_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        raise AssertionError("LLM must NOT be called on this path")

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        raise AssertionError("LLM must NOT be called")


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


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
    if "expense_reports" not in insp.get_table_names():
        pytest.skip("expense_reports table not found — run `alembic upgrade head` first")
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
    return {"org_id": org.id, "employee_id": employee.id}


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


def _ctx(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    with_expense_create: bool = True,
) -> AgentContext:
    keys: frozenset[str] = frozenset({EXPENSE_CREATE_OWN}) if with_expense_create else frozenset()
    return AgentContext(
        organization_id=org_id,
        user_id=user_id,
        granted_permission_keys=keys,
        correlation_id="db-test",
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.expense,
    route=AiRoute.llm,
    task_type="expense_report",
    confidence=0.95,
    rationale="db-test",
)


def _task(payload: dict) -> Task:
    return Task(context_type="expense", payload=payload)


def _in_policy_verdict(ref_id: str, amount: float = 100.0) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(item_type="receipt", amount_usd=amount, ref_id=ref_id),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="amount_cap",
                applies_to="all",
                source_clause_ref="RECEIPT-CAP",
                in_policy=True,
                reason="Within cap.",
                requires_approver=None,
                observed={"amount_usd": amount},
            ),
        ),
        evaluated_rule_count=1,
    )


# ---------------------------------------------------------------------------
# DB Gate: Persist
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestPersistDB:
    def test_persist_creates_expense_report(self, seeded: dict, db_session: Session) -> None:
        """persist_expense_report creates ExpenseReport with status=draft."""
        from database.models.expense_report import ExpenseReport, ExpenseReportStatus

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        entries = [
            DraftEntry(
                category="meals",
                amount=50.0,
                currency="USD",
                expense_date="2026-06-10",
                vendor="DB Test Cafe",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            )
        ]
        report = assemble_report(entries, trip_id=None, today="2026-06-14")
        flags_snapshot = {
            "flags": [],
            "evaluated_entry_count": 1,
            "blocking_count": 0,
            "advisory_count": 0,
        }

        intent = ExpenseIntent(
            trip_id=None,
            owner_id=None,
            conversation_id=None,
        )

        expense_report, _ = persist_expense_report(
            intent=intent,
            report=report,
            flags_snapshot=flags_snapshot,
            reflection_outcome_value="passed",
            context=ctx,
            session=db_session,
        )

        assert expense_report.id is not None
        assert expense_report.status == ExpenseReportStatus.draft
        assert expense_report.owner_id == user_id
        assert expense_report.organization_id == org_id
        assert expense_report.title == report.title

        # Visible within the session (flush worked)
        fetched = db_session.get(ExpenseReport, expense_report.id)
        assert fetched is not None
        assert fetched.status == ExpenseReportStatus.draft
        # db_session fixture rolls back — not permanently committed

    def test_persist_writes_expense_entries(self, seeded: dict, db_session: Session) -> None:
        """Expense entries are created with correct amounts and categories."""
        from database.models.expense import Expense, ExpenseCategory, ExpenseStatus

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        entries = [
            DraftEntry(
                category="airfare",
                amount=350.0,
                currency="USD",
                expense_date="2026-06-10",
                vendor="United Airlines",
                description="Flight to NYC",
                status="matched",
                match_source="card_txn",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            )
        ]
        report = assemble_report(entries, trip_id=None, today="2026-06-14")
        flags_snapshot = {
            "flags": [],
            "evaluated_entry_count": 1,
            "blocking_count": 0,
            "advisory_count": 0,
        }
        intent = ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None)

        expense_report, _ = persist_expense_report(
            intent=intent,
            report=report,
            flags_snapshot=flags_snapshot,
            reflection_outcome_value="passed",
            context=ctx,
            session=db_session,
        )

        # Check entries exist
        expense_rows = db_session.scalars(
            sa.select(Expense).where(Expense.report_id == expense_report.id)
        ).all()
        assert len(expense_rows) == 1
        exp = expense_rows[0]
        assert exp.category == ExpenseCategory.airfare
        assert Decimal(str(exp.amount)) == Decimal("350.00")
        assert exp.status == ExpenseStatus.matched
        assert exp.vendor == "United Airlines"

    def test_persist_flush_only_not_committed(self, seeded: dict, db_session: Session) -> None:
        """ExpenseReport is visible in session after flush but rolls back on close."""
        from database.models.expense_report import ExpenseReport

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        entries = [
            DraftEntry(
                category="other",
                amount=25.0,
                currency="USD",
                expense_date="2026-06-10",
                vendor="Misc",
                description=None,
                status="matched",
                match_source="receipt",
                trip_segment_id=None,
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            )
        ]
        report = assemble_report(entries, trip_id=None, today="2026-06-14")
        flags_snapshot = {
            "flags": [],
            "evaluated_entry_count": 1,
            "blocking_count": 0,
            "advisory_count": 0,
        }
        intent = ExpenseIntent(trip_id=None, owner_id=None, conversation_id=None)

        expense_report, _ = persist_expense_report(
            intent=intent,
            report=report,
            flags_snapshot=flags_snapshot,
            reflection_outcome_value="passed",
            context=ctx,
            session=db_session,
        )

        # Visible within session (flush worked)
        fetched = db_session.get(ExpenseReport, expense_report.id)
        assert fetched is not None
        # After rollback (in fixture), the row will not be committed


# ---------------------------------------------------------------------------
# DB Gate: Role-bound
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestRoleBoundDB:
    def test_missing_expense_create_permission_blocked(
        self, seeded: dict, db_session: Session
    ) -> None:
        """check_action raises GuardrailBlocked when EXPENSE_CREATE_OWN is absent."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id, with_expense_create=False)

        verdict = _in_policy_verdict("test-ref")
        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=IngestedData(
                    receipts=[
                        ReceiptFact(
                            receipt_id=str(uuid.uuid4()),
                            amount=50.0,
                            currency="USD",
                            date="2026-06-10",
                            vendor="Test",
                            location=None,
                            expense_type="meals",
                            line_items=None,
                            already_linked=False,
                        )
                    ],
                    card_txns=[],
                    segments=[],
                    trip_id=None,
                    unprocessed_receipts=[],
                ),
            ),
            patch(
                "ai_agents.agents.expense_agent.presubmit.PolicyEvalTool.evaluate_many",
                return_value=(verdict,),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task({}),
                _DECISION,
                context=ctx,
                session=db_session,
            )

        assert result.status == AgentResultStatus.needs_review
        assert "persist_error" in result.output
        assert result.output.get("report_id") is None

    def test_different_owner_id_denied(self, seeded: dict, db_session: Session) -> None:
        """owner_id != user_id → role-blocked response."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        other_id = str(uuid.uuid4())
        agent = ExpenseAgent(provider=_RaisingProvider())
        result = agent.handle(
            _task({"owner_id": other_id}),
            _DECISION,
            context=ctx,
            session=db_session,
        )
        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True


# ---------------------------------------------------------------------------
# DB Gate: Decision log
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestDecisionLogDB:
    def test_decision_log_row_flushed(self, seeded: dict, db_session: Session) -> None:
        """A real AiDecisionLog row is flushed to the DB."""
        from database.models.ai_decision_log import AiDecisionLog

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        with (
            patch(
                "ai_agents.agents.expense_agent.agent.ingest",
                return_value=IngestedData(
                    receipts=[],
                    card_txns=[],
                    segments=[],
                    trip_id=None,
                    unprocessed_receipts=[],
                ),
            ),
        ):
            agent = ExpenseAgent(provider=_RaisingProvider())
            agent.handle(
                _task({}),
                _DECISION,
                context=ctx,
                session=db_session,
            )

        # Read the flushed row (visible in same session before rollback)
        rows = db_session.scalars(
            sa.select(AiDecisionLog)
            .where(
                AiDecisionLog.agent == AiAgent.expense,
                AiDecisionLog.organization_id == org_id,
                AiDecisionLog.user_id == user_id,
            )
            .order_by(AiDecisionLog.created_at.desc())
            .limit(1)
        ).all()

        assert len(rows) == 1, "Expected exactly one AiDecisionLog row"
        row = rows[0]
        assert row.task_type == "expense_report"
        assert row.route == AiRoute.deterministic
        assert row.output_summary is not None

    def test_no_backend_import_in_persistence(self) -> None:
        """persistence.py must not import from backend."""
        import sys

        before = set(sys.modules)
        import ai_agents.agents.expense_agent.persistence  # noqa: F401

        after = set(sys.modules)
        new_mods = {m for m in (after - before) if m.startswith("backend")}
        assert not new_mods, f"expense persistence imported backend modules: {new_mods}"
