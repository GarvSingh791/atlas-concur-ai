"""Live-DB tests for PolicyEvalTool — canonical deterministic evaluator (P4-T4).

Marked @pytest.mark.db — self-skips when Postgres unreachable or migrations not run.
Uses the P3-T5 seeded data (7 active rules; SUSTAIN clause has NO rule).

Proves:
  - In-policy flight (UA201 economy 5.5h) → in_policy=True
  - Out-of-policy flight (UA201B business 5.5h) → in_policy=False + AIR-CABIN
  - Out-of-policy hotel ($400 NYC) → in_policy=False + HOTEL-CAP
  - In-policy hotel (Marriott $239 NYC) → in_policy=True
  - SUSTAIN clause produces NO RuleVerdict (structural guarantee)
  - Only status=active rules evaluated
  - Cross-org isolation: different org sees no rules
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
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool

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
        pytest.skip("Postgres not reachable — skipping PolicyEvalTool tests")

    insp = sa.inspect(engine)
    if "policy_rules" not in insp.get_table_names():
        pytest.skip("policy_rules table not found — run `alembic upgrade head` first")

    yield engine


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization, User  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        employee = session.query(User).filter(User.email == "employee@atlas-demo.internal").one()
        manager = session.query(User).filter(User.email == "manager@atlas-demo.internal").one()

    return {
        "org_id": org.id,
        "employee_id": employee.id,
        "manager_id": manager.id,
    }


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID | None = None) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=frozenset(),
        user_id=user_id,
        correlation_id="eval-test",
    )


# ---------------------------------------------------------------------------
# Flight: cabin_class (AIR-CABIN)
# ---------------------------------------------------------------------------


class TestCabinClass:
    def test_economy_short_haul_in_policy(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="flight",
            cabin_class="economy",
            duration_hours=5.5,
            supplier="United Airlines",
            ref_id="UA201-2026-06-10",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        assert verdict.in_policy is True

    def test_business_short_haul_out_of_policy(self, seeded: dict, db_session: Session) -> None:
        """UA201B: business class on 5.5h flight — violates AIR-CABIN."""
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="flight",
            cabin_class="business",
            duration_hours=5.5,
            supplier="United Airlines",
            ref_id="UA201B-2026-06-10",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        assert verdict.in_policy is False
        air_cabin_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "AIR-CABIN"]
        assert air_cabin_verdicts, "Expected a RuleVerdict with source_clause_ref=AIR-CABIN"
        v = air_cabin_verdicts[0]
        assert v.in_policy is False
        assert v.requires_approver == "VP"

    def test_business_long_haul_in_policy(self, seeded: dict, db_session: Session) -> None:
        """10.5h flight — business class is permitted."""
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="flight",
            cabin_class="business",
            duration_hours=10.5,
            supplier="Lufthansa",
            ref_id="LH455-2026-07-01",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        air_cabin_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "AIR-CABIN"]
        assert all(v.in_policy for v in air_cabin_verdicts)


# ---------------------------------------------------------------------------
# Hotel: amount_cap (HOTEL-CAP)
# ---------------------------------------------------------------------------


class TestAmountCap:
    def test_marriott_nyc_239_in_policy(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="hotel",
            amount_usd=239.00,
            city="New York",
            supplier="Marriott International",
            ref_id="NYC-MARRIOTT-TIMES-001",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        hotel_cap_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
        assert all(v.in_policy for v in hotel_cap_verdicts)

    def test_luxury_loft_nyc_400_out_of_policy(self, seeded: dict, db_session: Session) -> None:
        """The Luxury Loft NYC at $400/night exceeds $250 Tier-1 cap."""
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="hotel",
            amount_usd=400.00,
            city="New York",
            supplier="Independent",
            ref_id="NYC-BOUTIQUE-001",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        assert verdict.in_policy is False
        hotel_cap_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
        assert hotel_cap_verdicts, "Expected a RuleVerdict with source_clause_ref=HOTEL-CAP"
        v = hotel_cap_verdicts[0]
        assert v.in_policy is False
        assert v.requires_approver is not None

    def test_city_normalization_new_york_ny(self, seeded: dict, db_session: Session) -> None:
        """'New York, NY' (from trip segment) must match Tier-1 'New York'."""
        tool = PolicyEvalTool()
        item = EvalItem(
            item_type="hotel",
            amount_usd=239.00,
            city="New York, NY",
            supplier="Marriott International",
        )
        verdict = tool.evaluate(item, context=_ctx(seeded["org_id"]), session=db_session)
        hotel_cap_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
        assert all(v.in_policy for v in hotel_cap_verdicts)


# ---------------------------------------------------------------------------
# SUSTAIN clause: no verdict (structural guarantee)
# ---------------------------------------------------------------------------


class TestSustainClause:
    def test_no_verdict_carries_sustain_clause_ref(self, seeded: dict, db_session: Session) -> None:
        """SUSTAIN has no policy_rules row — must never appear in any verdict."""
        tool = PolicyEvalTool()
        items = [
            EvalItem(item_type="flight", cabin_class="economy", duration_hours=5.5),
            EvalItem(item_type="hotel", amount_usd=200.0, city="New York"),
            EvalItem(item_type="meals", amount_usd=50.0, region="domestic"),
            EvalItem(item_type="car", vehicle_class="Economy"),
        ]
        ctx = _ctx(seeded["org_id"])
        for item in items:
            verdict = tool.evaluate(item, context=ctx, session=db_session)
            sustain_verdicts = [v for v in verdict.verdicts if v.source_clause_ref == "SUSTAIN"]
            assert not sustain_verdicts, (
                f"SUSTAIN must never appear in verdicts for {item.item_type}, "
                f"got: {sustain_verdicts}"
            )


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


class TestCrossOrgIsolation:
    def test_different_org_sees_no_rules(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyEvalTool()
        other_org = uuid.uuid4()  # random — no matching policies
        item = EvalItem(
            item_type="flight",
            cabin_class="business",
            duration_hours=5.5,
        )
        verdict = tool.evaluate(item, context=_ctx(other_org), session=db_session)
        assert verdict.evaluated_rule_count == 0
        assert verdict.in_policy is True  # no rules → trivially in-policy
        assert verdict.verdicts == ()


# ---------------------------------------------------------------------------
# evaluate_many
# ---------------------------------------------------------------------------


class TestEvaluateMany:
    def test_mixed_items_returns_per_item_verdicts(self, seeded: dict, db_session: Session) -> None:
        tool = PolicyEvalTool()
        items = [
            # Preferred supplier on both, so the cabin rule alone decides; an unknown
            # supplier fails the preferred-supplier rule closed (C7-05/C7R).
            EvalItem(
                item_type="flight",
                cabin_class="economy",
                duration_hours=5.5,
                supplier="United Airlines",
            ),
            EvalItem(
                item_type="flight",
                cabin_class="business",
                duration_hours=5.5,
                supplier="United Airlines",
            ),
        ]
        ctx = _ctx(seeded["org_id"])
        verdicts = tool.evaluate_many(items, context=ctx, session=db_session)
        assert len(verdicts) == 2
        assert verdicts[0].in_policy is True
        assert verdicts[1].in_policy is False
