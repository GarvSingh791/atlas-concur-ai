"""Live-DB tests for the Travel Agent (P4-T9) — Gates 6/7/8 with real persistence.

Requires Postgres + seeded data; auto-skips when unreachable.
Run with: pytest -m db ai_agents/tests/test_travel_agent_db.py

Gates covered:
  6  — Persist: Trip + TripSegments via persist_trip_selection(); recommendation_context
       populated; status=draft; no direct backend call.
  7b — Role-bound + GuardrailBlocked on TRIP_CREATE with missing permission.
  8b — Decision log: real AiDecisionLog row flushed (not committed so visible in test txn).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.travel_agent import TravelAgent
from ai_agents.agents.travel_agent.persistence import persist_trip_selection
from ai_agents.agents.travel_agent.ranking import RankedOption
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from config.constants.permissions import TRIP_CREATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment
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
    if "trips" not in insp.get_table_names():
        pytest.skip("trips table not found — run `alembic upgrade head` first")
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


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID, *, with_trip_create: bool = True) -> AgentContext:
    keys: frozenset[str] = frozenset({TRIP_CREATE_OWN}) if with_trip_create else frozenset()
    return AgentContext(
        organization_id=org_id,
        user_id=user_id,
        granted_permission_keys=keys,
        correlation_id="db-test",
    )


_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.llm,
    task_type="trip_planning",
    confidence=0.95,
    rationale="db-test",
)


def _task(payload: dict) -> Task:
    return Task(context_type="travel_planning", payload=payload)


def _make_ranked_option(
    option_id: str, amount: float = 320.0, in_policy: bool = True
) -> RankedOption:
    return RankedOption(
        option_id=option_id,
        segment_type="flight",
        supplier="Test Airline",
        amount_usd=amount,
        sustainability_score=3,
        cabin_class="economy",
        duration_hours=5.5,
        is_direct=True,
        city=None,
        nightly_rate_usd=None,
        room_type=None,
        in_policy=in_policy,
        policy_reason="" if in_policy else "Test out-of-policy reason",
        clause_refs=("AIR-CABIN",),
        requires_approver=None if in_policy else "VP",
        rule_in_policy=in_policy,
        rank_score=100.0 if in_policy else 0.0,
        explanation="Test explanation.",
    )


def _in_policy_verdict(ref_id: str, amount: float = 320.0) -> PolicyVerdict:
    return PolicyVerdict(
        item=EvalItem(
            item_type="flight",
            amount_usd=amount,
            cabin_class="economy",
            duration_hours=5.5,
            ref_id=ref_id,
        ),
        in_policy=True,
        verdicts=(
            RuleVerdict(
                rule_id=str(uuid.uuid4()),
                rule_type="cabin_class",
                applies_to="flight",
                source_clause_ref="AIR-CABIN",
                in_policy=True,
                reason="Economy permitted.",
                requires_approver=None,
                observed={},
            ),
        ),
        evaluated_rule_count=1,
    )


# ---------------------------------------------------------------------------
# Gate 6 — Persist
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestPersist:
    def test_persist_creates_trip_and_segments(self, seeded: dict, db_session: Session) -> None:
        """persist_trip_selection creates Trip + TripSegment with recommendation_context."""
        from ai_agents.agents.travel_agent.intent import TripIntent

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        option_id = "UA201-2026-06-10"
        opt = _make_ranked_option(option_id, amount=320.0, in_policy=True)
        shortlist = [opt]
        selected_ids = [option_id]

        intent = TripIntent(
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
            return_date="2026-06-14",
            travelers=1,
            currency="USD",
        )

        trip = persist_trip_selection(
            intent=intent,
            shortlist=shortlist,
            selected_ids=selected_ids,
            reflection_outcome_value="passed",
            context=ctx,
            session=db_session,
        )

        assert trip.id is not None
        assert trip.status == TripStatus.draft
        assert trip.origin == "SFO"
        assert trip.primary_destination == "JFK"
        assert trip.traveler_id == user_id
        assert trip.organization_id == org_id

        # recommendation_context populated
        rec_ctx = trip.recommendation_context
        assert rec_ctx is not None
        assert rec_ctx["reflection_outcome"] == "passed"
        assert rec_ctx["agent_task"] == "trip_planning"
        assert option_id in rec_ctx["selected_option_ids"]
        selected_opt = rec_ctx["selected_options"][0]
        assert selected_opt["option_id"] == option_id
        assert selected_opt["in_policy"] is True

        # Segments created
        segments = db_session.scalars(
            sa.select(TripSegment).where(TripSegment.trip_id == trip.id)
        ).all()
        assert len(segments) == 1
        seg = segments[0]
        assert seg.in_policy is True
        assert (seg.details or {})["option_id"] == option_id

    def test_persist_sets_estimated_cost_from_selected_options(
        self, seeded: dict, db_session: Session
    ) -> None:
        """C7R: the saved trip carries the selection's cost (fare + nightly x nights).

        Without it the trip's estimated_cost was null, so the approval risk
        summary had no amount and an in-policy $298 trip could never auto-approve.
        """
        from dataclasses import replace

        from ai_agents.agents.travel_agent.intent import TripIntent

        ctx = _ctx(seeded["org_id"], seeded["employee_id"])
        flight = _make_ranked_option("UA1-2026-06-10", amount=298.0)
        hotel = replace(
            _make_ranked_option("H1", amount=150.0),
            segment_type="hotel",
            nightly_rate_usd=150.0,
            city="New York",
        )

        def _save(selected: list, currency: str = "USD") -> Trip:
            return persist_trip_selection(
                intent=TripIntent(
                    origin="SFO",
                    destination="JFK",
                    departure_date="2026-06-10",
                    return_date="2026-06-14",
                    travelers=1,
                    currency=currency,
                ),
                shortlist=[flight, hotel],
                selected_ids=[o.option_id for o in selected],
                reflection_outcome_value="passed",
                context=ctx,
                session=db_session,
            )

        assert _save([flight]).estimated_cost == Decimal("298.00")
        # 4 nights x $150 + $298 fare
        assert _save([flight, hotel]).estimated_cost == Decimal("898.00")
        # USD option prices are never stored as a cost in another currency.
        assert _save([flight], currency="EUR").estimated_cost is None

    def test_persist_writes_via_flush_not_commit(self, seeded: dict, db_session: Session) -> None:
        """Trip is visible within the session after flush but not committed."""
        from ai_agents.agents.travel_agent.intent import TripIntent

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        opt = _make_ranked_option("UA201-2026-06-10")
        intent = TripIntent(
            origin="SFO",
            destination="JFK",
            departure_date="2026-06-10",
            return_date="2026-06-14",
            travelers=1,
            currency="USD",
        )

        trip = persist_trip_selection(
            intent=intent,
            shortlist=[opt],
            selected_ids=[opt.option_id],
            reflection_outcome_value="passed",
            context=ctx,
            session=db_session,
        )

        # Visible in the current session (flush worked)
        fetched = db_session.get(Trip, trip.id)
        assert fetched is not None
        assert fetched.status == TripStatus.draft
        # db_session fixture rolls back so the row is NOT permanently committed

    def test_no_direct_backend_import(self) -> None:
        """persistence.py must not import from backend."""
        import sys

        before = set(sys.modules)
        import ai_agents.agents.travel_agent.persistence  # noqa: F401

        after = set(sys.modules)
        new_mods = {m for m in (after - before) if m.startswith("backend")}
        assert not new_mods, f"persistence.py imported backend modules: {new_mods}"


# ---------------------------------------------------------------------------
# Gate 7b — Role-bound (missing TRIP_CREATE permission)
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestRoleBoundDB:
    def test_missing_permission_blocks_trip_create(self, seeded: dict, db_session: Session) -> None:
        """check_action raises GuardrailBlocked when TRIP_CREATE_OWN is absent."""
        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id, with_trip_create=False)

        verdict = _in_policy_verdict("UA201-2026-06-10")
        with (
            patch(
                "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
                return_value=(verdict,),
            ),
            patch(
                "ai_agents.agents.travel_agent.agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            result = agent.handle(
                _task(
                    {
                        "origin": "SFO",
                        "destination": "JFK",
                        "departure_date": "2026-06-10",
                        "travelers": 1,
                        "currency": "USD",
                        "include_hotels": False,
                        "selected_option_ids": ["UA201-2026-06-10"],
                    }
                ),
                _DECISION,
                context=ctx,
                session=db_session,
            )

        assert result.status == AgentResultStatus.needs_review
        assert "trip_persist_error" in result.output
        # Trip was NOT created
        assert result.output.get("trip_id") is None


# ---------------------------------------------------------------------------
# Gate 8b — Decision log (real flush)
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestDecisionLogDB:
    def test_decision_log_row_flushed_on_happy_path(
        self, seeded: dict, db_session: Session
    ) -> None:
        """A real AiDecisionLog row is flushed to the DB on the happy path."""
        from database.models.ai_decision_log import AiDecisionLog

        org_id = seeded["org_id"]
        user_id = seeded["employee_id"]
        ctx = _ctx(org_id, user_id)

        verdict = _in_policy_verdict("UA201-2026-06-10")
        with patch(
            "ai_agents.agents.travel_agent.ranking.PolicyEvalTool.evaluate_many",
            return_value=(verdict,),
        ):
            agent = TravelAgent(provider=_RaisingProvider())
            agent.handle(
                _task(
                    {
                        "origin": "SFO",
                        "destination": "JFK",
                        "departure_date": "2026-06-10",
                        "travelers": 1,
                        "currency": "USD",
                        "include_hotels": False,
                    }
                ),
                _DECISION,
                context=ctx,
                session=db_session,
            )

        # Read the flushed row (visible in the same session before rollback)
        rows = db_session.scalars(
            sa.select(AiDecisionLog)
            .where(
                AiDecisionLog.agent == AiAgent.travel,
                AiDecisionLog.organization_id == org_id,
                AiDecisionLog.user_id == user_id,
            )
            .order_by(AiDecisionLog.created_at.desc())
            .limit(1)
        ).all()

        assert len(rows) == 1, "Expected exactly one AiDecisionLog row"
        row = rows[0]
        assert row.task_type == "trip_planning"
        assert row.route == AiRoute.deterministic
        assert row.output_summary is not None
        assert row.confidence is not None
        assert row.reflection_outcome is not None
