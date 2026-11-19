"""Live-DB: planner multi-turn intent merge (C7R planner defect).

Real Postgres + the seeded demo org/policy; the TravelAgent runs un-patched
(real InventoryTool, real PolicyEvalTool, real decision log rows) inside a
rolled-back session. Pins the UAT flow end to end:

  turn 1  "Fly to New York next Tuesday for 3 days for a client meeting"
          -> ONE clarify ("departing from?"), stored as ONE assistant message
             whose tool_calls carry the partial intent
  turn 2  "Chicago (ORD)" with NO chips -> merges onto turn 1 from the DB,
          plans ORD->JFK for 2026-11-17 with a 2-night hotel stay, and the
          out-of-policy business fare reads "Allowed: economy." (no list repr)
  turn 3  selection -> Trip persisted with origin/return/purpose from the text
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.travel_agent import TravelAgent
from ai_agents.context import AgentContext
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision, Task
from config.constants.permissions import TRIP_CREATE_OWN, TRIP_UPDATE_OWN
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.conversation import Conversation, ConversationContextType
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.trip import Trip
from services.llm_provider.local import LocalLlmProvider

TODAY = date(2026, 11, 11)  # a Wednesday

_DECISION = RoutingDecision(
    target_agent=AiAgent.travel,
    route=AiRoute.llm,
    task_type="trip_planning",
    confidence=0.95,
    rationale="context_type=travel_planning",
)


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
    if "conversation_messages" not in insp.get_table_names():
        pytest.skip("conversation_messages table not found — run `alembic upgrade head` first")
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


def _assistant_messages(session: Session, conv_id: uuid.UUID) -> list[ConversationMessage]:
    return list(
        session.scalars(
            sa.select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conv_id,
                ConversationMessage.role == MessageRole.assistant,
            )
            .order_by(ConversationMessage.sequence)
        ).all()
    )


@pytest.mark.db
def test_clarify_answer_merges_across_turns_and_persists_trip(
    seeded: dict, db_session: Session
) -> None:
    ctx = AgentContext(
        organization_id=seeded["org_id"],
        user_id=seeded["employee_id"],
        granted_permission_keys=frozenset({TRIP_CREATE_OWN, TRIP_UPDATE_OWN}),
        correlation_id="planner-multiturn",
    )
    conv = Conversation(
        organization_id=seeded["org_id"],
        user_id=seeded["employee_id"],
        context_type=ConversationContextType.travel_planning,
    )
    db_session.add(conv)
    db_session.flush()
    conv_id = str(conv.id)
    agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)

    def turn(payload: dict) -> dict:
        result = agent.handle(
            Task(context_type="travel_planning", payload={"conversation_id": conv_id, **payload}),
            _DECISION,
            context=ctx,
            session=db_session,
        )
        assert result.status in (AgentResultStatus.ok, AgentResultStatus.needs_review)
        return dict(result.output)

    # ── Turn 1: partial request -> exactly one clarify, persisted once ───────
    out1 = turn(
        {
            "question": "Fly to New York next Tuesday for 3 days for a client meeting",
            "currency": "USD",
        }
    )
    assert out1["clarify"] is True
    assert "departing from" in out1["question"]
    msgs = _assistant_messages(db_session, conv.id)
    assert [m.content for m in msgs] == [out1["question"]]
    trace = msgs[0].tool_calls
    assert trace is not None
    assert trace["intent"]["destination"] == "JFK"
    assert trace["intent"]["return_date"] == "2026-11-19"

    # ── Turn 2: bare answer, no chips -> merged from the stored intent ───────
    out2 = turn({"question": "Chicago (ORD)"})
    assert out2["clarify"] is False
    assert out2["intent"] == {
        "origin": "ORD",
        "destination": "JFK",
        "departure_date": "2026-11-17",
        "return_date": "2026-11-19",
        "travelers": 1,
        "currency": "USD",
        "purpose": "Client meeting",
    }
    shortlist = out2["shortlist"]
    flights = [o for o in shortlist if o["segment_type"] == "flight"]
    hotels = [o for o in shortlist if o["segment_type"] == "hotel"]
    assert len(flights) == 3 and len(hotels) == 3
    assert all(o["city"] == "New York" for o in hotels)
    business = next(o for o in flights if o["cabin_class"] == "business")
    assert business["in_policy"] is False
    assert "Allowed: economy." in business["policy_reason"]
    assert "[" not in business["policy_reason"]
    # In-policy options rank above out-of-policy ones (deterministic ranking).
    ranks_in = [o["rank"] for o in shortlist if o["in_policy"]]
    ranks_out = [o["rank"] for o in shortlist if not o["in_policy"]]
    assert ranks_in and max(ranks_in) < min(ranks_out)
    assert len(_assistant_messages(db_session, conv.id)) == 2

    # ── Turn 3: select the top in-policy flight -> Trip carries the text slots ─
    pick = next(o["option_id"] for o in flights if o["in_policy"])
    out3 = turn({"question": "Plan my trip", "selected_option_ids": [pick]})
    assert out3["trip_id"], out3.get("trip_persist_error")
    trip = db_session.get(Trip, uuid.UUID(out3["trip_id"]))
    assert trip is not None
    assert trip.origin == "ORD"
    assert trip.primary_destination == "JFK"
    assert trip.start_date == date(2026, 11, 17)
    assert trip.end_date == date(2026, 11, 19)
    assert trip.purpose == "Client meeting"

    # ── Turn 4 (UAT-2 Issue 4): the hotel joins the SAME draft trip ──────────
    hotel = next(o for o in hotels if o["in_policy"])
    out4 = turn({"question": "Plan my trip", "selected_option_ids": [hotel["option_id"]]})
    assert out4["trip_id"] == out3["trip_id"], out4.get("trip_persist_error")
    assert out4["trip_change"]["action"] == "updated"
    db_session.refresh(trip)
    fare = Decimal(str(next(o for o in flights if o["option_id"] == pick)["amount_usd"]))
    assert trip.estimated_cost == fare + Decimal(str(hotel["nightly_rate_usd"])) * 2
    assert trip.purpose == "Client meeting"
    assert len(trip.segments) == 2
    db_session.refresh(conv)
    assert conv.context_ref_id == trip.id


@pytest.mark.db
def test_unknown_route_returns_explained_no_options(seeded: dict, db_session: Session) -> None:
    ctx = AgentContext(
        organization_id=seeded["org_id"],
        user_id=seeded["employee_id"],
        granted_permission_keys=frozenset({TRIP_CREATE_OWN}),
        correlation_id="planner-no-options",
    )
    agent = TravelAgent(provider=LocalLlmProvider(), today=lambda: TODAY)
    result = agent.handle(
        Task(
            context_type="travel_planning",
            payload={"question": "from Boston to Austin on Dec 3 for 2 nights"},
        ),
        _DECISION,
        context=ctx,
        session=db_session,
    )
    assert result.output["shortlist"] == []
    assert "Boston → Austin, 2026-12-03 to 2026-12-05" in result.output["message"]
    assert result.output["intent"]["return_date"] == "2026-12-05"
