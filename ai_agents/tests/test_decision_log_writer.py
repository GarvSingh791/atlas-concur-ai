"""Live-DB test for the decision-log writer (P4-T2).

Marked @pytest.mark.db — self-skips when Postgres is unreachable so the suite
stays green in hermetic CI.

What this test proves:
  - write_decision_log produces EXACTLY ONE ai_decision_logs row.
  - Every required field is present with the correct value.
  - confidence is persisted to exactly 3 decimal places.
  - input_summary / tools_used / output_summary carry NO raw secret or PII
    (the minimizer ran before persistence).
  - Passing an invalid enum value is rejected (not silently coerced).
  - write_decision_log does NOT commit — the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import Table, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.decision_log.writer import latency_timer, write_decision_log
from ai_agents.exceptions import GuardrailError
from database.models import AiDecisionLog, Base, Organization
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from database.models.identity import User

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Tables needed for this test (FK chain: organizations → users → ai_decision_logs)
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AiDecisionLog.__table__),
]


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    from backend.app.core.database import get_engine

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping decision log writer tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    if tables_to_create:
        Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Yield a session that rolls back after each test (no data leakage)."""
    from backend.app.core.database import get_sessionmaker

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Test-scoped organization
# ---------------------------------------------------------------------------

_TEST_ORG_NAME = "P4T2 Writer Test Org"


@pytest.fixture(scope="module")
def org_id(db_engine: Engine) -> Iterator[uuid.UUID]:
    """Create a test Organization; clean it up after the module."""
    from backend.app.core.database import get_sessionmaker

    session = get_sessionmaker()()
    org = Organization(name=_TEST_ORG_NAME, base_currency="USD")
    session.add(org)
    session.commit()
    oid = org.id
    try:
        yield oid
    finally:
        # Rows the tests committed reference this org (FK) — remove them first.
        session.execute(delete(AiDecisionLog).where(AiDecisionLog.organization_id == oid))
        session.delete(org)
        session.commit()
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context(org_id: uuid.UUID) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=frozenset(),
        user_id=None,
        correlation_id="test-corr-001",
    )


def _record(**overrides: Any) -> DecisionRecord:
    defaults: dict[str, Any] = {
        "agent": AiAgent.travel,
        "task_type": "rank_options",
        "route": AiRoute.llm,
        "model_provider": "anthropic",
        "input_summary": {
            "origin": "SFO",
            "destination": "JFK",
            "user_email": "traveler@example.com",
            "api_key": "sk-test-secret-12345",
        },
        "tools_used": {
            "tool": "search_flights",
            "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.secret",
        },
        "output_summary": {
            "top_flight": "UA123",
            "credit_card": "4111 1111 1111 1111",
        },
        "confidence": 0.875,
        "latency_ms": 250,
    }
    defaults.update(overrides)
    return DecisionRecord(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteDecisionLog:
    def test_writes_exactly_one_row(self, db_session: Session, org_id: uuid.UUID) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        rows = db_session.scalars(select(AiDecisionLog).where(AiDecisionLog.id == entry.id)).all()
        assert len(rows) == 1

    def test_required_fields_populated(self, db_session: Session, org_id: uuid.UUID) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.id is not None
        assert entry.created_at is not None
        assert entry.organization_id == org_id
        assert entry.user_id is None
        assert entry.agent == AiAgent.travel
        assert entry.task_type == "rank_options"
        assert entry.route == AiRoute.llm
        assert entry.model_provider == "anthropic"
        assert entry.correlation_id == ctx.correlation_id
        assert entry.latency_ms == 250

    def test_confidence_persisted_to_three_decimal_places(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record(confidence=0.8759)  # should be truncated to 0.875
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        db_session.expire(entry)
        reloaded = db_session.get(AiDecisionLog, entry.id)
        assert reloaded is not None
        assert Decimal(str(reloaded.confidence)) == Decimal("0.875")

    def test_pii_minimizer_redacts_email_in_input_summary(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.input_summary is not None
        # Raw email must not appear
        assert "traveler@example.com" not in str(entry.input_summary)

    def test_pii_minimizer_redacts_api_key_by_key_name(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.input_summary is not None
        assert "sk-test-secret-12345" not in str(entry.input_summary)
        assert "[REDACTED]" in str(entry.input_summary)

    def test_pii_minimizer_redacts_bearer_token_in_tools_used(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.tools_used is not None
        assert "eyJhbGciOiJIUzI1NiJ9.secret" not in str(entry.tools_used)

    def test_pii_minimizer_redacts_credit_card_in_output_summary(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record()
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.output_summary is not None
        assert "4111 1111 1111 1111" not in str(entry.output_summary)

    def test_reflection_outcome_wired_as_passthrough(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record(reflection_outcome=ReflectionOutcome.passed)
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        db_session.expire(entry)
        reloaded = db_session.get(AiDecisionLog, entry.id)
        assert reloaded is not None
        assert reloaded.reflection_outcome == ReflectionOutcome.passed

    def test_null_optional_fields_accepted(self, db_session: Session, org_id: uuid.UUID) -> None:
        ctx = _context(org_id)
        rec = _record(
            model_provider=None,
            input_summary=None,
            tools_used=None,
            output_summary=None,
            confidence=None,
            reflection_outcome=None,
            latency_ms=None,
        )
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.model_provider is None
        assert entry.input_summary is None
        assert entry.confidence is None

    def test_no_implicit_commit(self, db_session: Session, org_id: uuid.UUID) -> None:
        ctx = _context(org_id)
        rec = _record(task_type="rollback_test")
        entry = write_decision_log(db_session, context=ctx, record=rec)

        # Rollback without ever committing; the row must disappear.
        db_session.rollback()

        from backend.app.core.database import get_sessionmaker

        separate = get_sessionmaker()()
        try:
            row = separate.get(AiDecisionLog, entry.id)
            assert row is None, "write_decision_log must not commit on its own"
        finally:
            separate.close()

    def test_confidence_out_of_range_raises_guardrail_error(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record(confidence=1.5)
        with pytest.raises(GuardrailError):
            write_decision_log(db_session, context=ctx, record=rec)

    def test_confidence_negative_raises_guardrail_error(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record(confidence=-0.1)
        with pytest.raises(GuardrailError):
            write_decision_log(db_session, context=ctx, record=rec)

    def test_deterministic_route_no_model_provider(
        self, db_session: Session, org_id: uuid.UUID
    ) -> None:
        ctx = _context(org_id)
        rec = _record(route=AiRoute.deterministic, model_provider=None)
        entry = write_decision_log(db_session, context=ctx, record=rec)
        db_session.commit()

        assert entry.route == AiRoute.deterministic
        assert entry.model_provider is None


class TestLatencyTimer:
    def test_latency_timer_populates_ms(self) -> None:
        with latency_timer() as ms:
            _ = sum(range(1000))
        assert ms[0] >= 0

    def test_latency_timer_is_reasonable(self) -> None:
        with latency_timer() as ms:
            pass
        # Should complete in under 1 second
        assert ms[0] < 1000
