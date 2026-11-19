"""Live-DB tests for the Orchestrator (P4-T7) — requires Postgres + seeded data.

Marked @pytest.mark.db — self-skips when Postgres unreachable or the
ai_decision_logs table is missing.

Proves:
  - Exactly ONE ai_decision_logs row with agent='orchestrator' persists per routed task.
  - The row has the correct task_type, route, model_provider=NULL, and latency_ms.
  - A mix of tasks produces the correct M-R4 ratio (deterministic vs llm).
  - Cross-org isolation: logs from org A are not visible under org B.
  - Scoped-context org/user appears on the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.orchestrator import (
    AgentRegistry,
    AgentResult,
    AgentResultStatus,
    Orchestrator,
    RoutingDecision,
    Task,
)
from database.models import AiDecisionLog
from database.models.ai_decision_log import AiAgent, AiRoute

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    from backend.app.core.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping orchestrator DB tests")

    insp = sa.inspect(engine)
    if "ai_decision_logs" not in insp.get_table_names():
        pytest.skip("ai_decision_logs table not found — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def org_a_id(db_engine: Engine) -> Iterator[uuid.UUID]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models import Organization  # noqa: PLC0415
    from database.models.identity import User  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        org = Organization(name="OrchestratorTest OrgA", base_currency="USD")
        session.add(org)
        session.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with factory() as session:
            # Committed decision-log rows reference the org (FK) — remove first.
            session.execute(sa.delete(AiDecisionLog).where(AiDecisionLog.organization_id == oid))
            session.execute(sa.delete(User).where(User.organization_id == oid))
            org = session.get(Organization, oid)
            if org:
                session.delete(org)
            session.commit()


@pytest.fixture(scope="module")
def org_b_id(db_engine: Engine) -> Iterator[uuid.UUID]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models import Organization  # noqa: PLC0415
    from database.models.identity import User  # noqa: PLC0415

    factory = get_sessionmaker()
    with factory() as session:
        org = Organization(name="OrchestratorTest OrgB", base_currency="USD")
        session.add(org)
        session.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with factory() as session:
            # Committed decision-log rows reference the org (FK) — remove first.
            session.execute(sa.delete(AiDecisionLog).where(AiDecisionLog.organization_id == oid))
            session.execute(sa.delete(User).where(User.organization_id == oid))
            org = session.get(Organization, oid)
            if org:
                session.delete(org)
            session.commit()


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _ctx(org_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=frozenset(),
        user_id=user_id,
        correlation_id="test-corr-orch",
    )


def _make_user(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    from database.models.identity import RoleKey, User  # noqa: PLC0415

    user = User(
        organization_id=org_id,
        name="Orchestrator Test User",
        email=f"orch-{uuid.uuid4().hex[:8]}@example.test",
        role=RoleKey.employee,
        password_hash="!",  # not a usable hash; this user never logs in
    )
    session.add(user)
    session.commit()
    return user.id


def _task(context_type: str | None, *, signals: dict | None = None) -> Task:
    return Task(
        context_type=context_type,
        payload={"question": "test"},
        signals=signals or {},
    )


class _FakeDeterministicHandler:
    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Any,
    ) -> AgentResult:
        return AgentResult(status=AgentResultStatus.ok, output={"from": "fake_det_handler"})


def _make_orch(*, threshold: float = 0.75) -> Orchestrator:
    registry = AgentRegistry()
    # Register a deterministic handler for the paths that take it
    det = _FakeDeterministicHandler()
    registry.register_deterministic_handler("expense_categorize_simple", det)
    registry.register_deterministic_handler("policy_grounded_lookup", det)
    return Orchestrator(registry, confidence_threshold=threshold)


def _count_orch_logs(session: Session, org_id: uuid.UUID) -> int:
    from database.models import AiDecisionLog  # noqa: PLC0415

    return (
        session.query(AiDecisionLog)
        .filter(
            AiDecisionLog.organization_id == org_id,
            AiDecisionLog.agent == AiAgent.orchestrator,
        )
        .count()
    )


def _get_orch_logs(session: Session, org_id: uuid.UUID) -> list[Any]:
    from database.models import AiDecisionLog  # noqa: PLC0415

    return (
        session.query(AiDecisionLog)
        .filter(
            AiDecisionLog.organization_id == org_id,
            AiDecisionLog.agent == AiAgent.orchestrator,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# One row per task
# ---------------------------------------------------------------------------


class TestOneRowPerTask:
    def test_exactly_one_row_for_deterministic_task(
        self, db_session: Session, org_a_id: uuid.UUID
    ) -> None:
        orch = _make_orch()
        before = _count_orch_logs(db_session, org_a_id)

        outcome = orch.handle_task(
            _task("expense", signals={"expense_simple": True}),
            context=_ctx(org_a_id),
            session=db_session,
        )
        db_session.commit()

        after = _count_orch_logs(db_session, org_a_id)
        assert after == before + 1
        assert outcome.log_id is not None

    def test_exactly_one_row_for_llm_task(self, db_session: Session, org_a_id: uuid.UUID) -> None:
        orch = _make_orch()
        before = _count_orch_logs(db_session, org_a_id)

        outcome = orch.handle_task(
            _task("travel_planning"),
            context=_ctx(org_a_id),
            session=db_session,
        )
        db_session.commit()

        after = _count_orch_logs(db_session, org_a_id)
        assert after == before + 1
        assert outcome.log_id is not None


# ---------------------------------------------------------------------------
# Row field correctness
# ---------------------------------------------------------------------------


class TestRowFields:
    def test_deterministic_row_has_correct_fields(
        self, db_session: Session, org_a_id: uuid.UUID
    ) -> None:
        from database.models import AiDecisionLog  # noqa: PLC0415

        orch = _make_orch()
        # policy_qa routes to the LLM since P4-T8 (grounded Q&A needs synthesis);
        # the simple-expense path is the canonical deterministic route.
        outcome = orch.handle_task(
            _task("expense", signals={"expense_simple": True}),
            context=_ctx(org_a_id),
            session=db_session,
        )
        db_session.commit()

        row = db_session.get(AiDecisionLog, outcome.log_id)
        assert row is not None
        assert row.agent == AiAgent.orchestrator
        assert row.route == AiRoute.deterministic
        assert row.task_type == "expense_categorize_simple"
        assert row.model_provider is None  # orchestrator never calls LLM
        assert row.organization_id == org_a_id
        assert row.latency_ms is not None
        assert row.latency_ms >= 0

    def test_llm_row_has_correct_fields(self, db_session: Session, org_a_id: uuid.UUID) -> None:
        from database.models import AiDecisionLog  # noqa: PLC0415

        orch = _make_orch()
        outcome = orch.handle_task(
            _task("travel_planning"),
            context=_ctx(org_a_id),
            session=db_session,
        )
        db_session.commit()

        row = db_session.get(AiDecisionLog, outcome.log_id)
        assert row is not None
        assert row.agent == AiAgent.orchestrator
        assert row.route == AiRoute.llm
        assert row.model_provider is None
        assert float(row.confidence) > 0

    def test_user_id_recorded_when_provided(self, db_session: Session, org_a_id: uuid.UUID) -> None:
        from database.models import AiDecisionLog  # noqa: PLC0415

        # ai_decision_logs.user_id is an FK to users: record a real user.
        user_id = _make_user(db_session, org_a_id)
        orch = _make_orch()
        outcome = orch.handle_task(
            _task("general"),
            context=_ctx(org_a_id, user_id=user_id),
            session=db_session,
        )
        db_session.commit()

        row = db_session.get(AiDecisionLog, outcome.log_id)
        assert row is not None
        assert row.user_id == user_id


# ---------------------------------------------------------------------------
# M-R4: deterministic-vs-llm ratio
# ---------------------------------------------------------------------------


class TestM_R4Ratio:
    def test_m_r4_ratio_matches_routed_tasks(
        self, db_session: Session, org_a_id: uuid.UUID
    ) -> None:
        """Route a controlled mix; compute M-R4 from ai_decision_logs."""
        orch = _make_orch()
        ctx = _ctx(org_a_id)

        # 2 deterministic tasks
        orch.handle_task(
            _task("expense", signals={"expense_simple": True}), context=ctx, session=db_session
        )
        orch.handle_task(
            _task("policy_qa", signals={"policy_eval": True}), context=ctx, session=db_session
        )
        # 2 llm tasks
        orch.handle_task(_task("travel_planning"), context=ctx, session=db_session)
        orch.handle_task(_task("general"), context=ctx, session=db_session)
        db_session.commit()

        rows = _get_orch_logs(db_session, org_a_id)
        det_count = sum(1 for r in rows if r.route == AiRoute.deterministic)
        llm_count = sum(1 for r in rows if r.route == AiRoute.llm)
        total = det_count + llm_count

        # We routed 2 deterministic + 2 llm among recent rows
        assert total >= 4, f"Expected at least 4 orchestrator rows, got {total}"
        # Last 4 rows should match the mix we sent
        recent_rows = sorted(rows, key=lambda r: r.created_at)[-4:]
        det_recent = sum(1 for r in recent_rows if r.route == AiRoute.deterministic)
        llm_recent = sum(1 for r in recent_rows if r.route == AiRoute.llm)
        assert det_recent == 2
        assert llm_recent == 2


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


class TestCrossOrgIsolation:
    def test_org_a_logs_not_visible_from_org_b(
        self,
        db_session: Session,
        org_a_id: uuid.UUID,
        org_b_id: uuid.UUID,
    ) -> None:
        orch = _make_orch()
        before_b = _count_orch_logs(db_session, org_b_id)

        orch.handle_task(
            _task("policy_qa"),
            context=_ctx(org_a_id),
            session=db_session,
        )
        db_session.commit()

        after_b = _count_orch_logs(db_session, org_b_id)
        assert after_b == before_b  # org B count must not change
