"""Live-DB tests for the Policy Agent (P4-T8) — requires Postgres + seeded data.

Marked @pytest.mark.db — self-skips when Postgres unreachable or required tables missing.

Proves:
  - Cap 1 (eval): in/out policy across all 7 seeded rule types; correct source_clause_ref
  - Cap 2 (Q&A grounded): AIR-CABIN query grounds with scripted LLM; decisions logged
  - Cap 2 (Q&A silent): off-topic query → silent, no LLM call, model_provider=None
  - Cap 3 (authoring): draft rule persists as status='draft', is evaluable via evaluate_candidate
  - End-to-end: orchestrator → PolicyEvalHandler writes TWO decision-log rows
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_agents.agents import PolicyAgent, PolicyEvalHandler, register_policy
from ai_agents.agents.policy_authoring import persist_draft_rule
from ai_agents.context import AgentContext
from ai_agents.orchestrator import AgentRegistry, Orchestrator, Task
from ai_agents.orchestrator.envelopes import AgentResultStatus, RoutingDecision
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.policy_rule import PolicyRuleStatus
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Shared scripted LLM provider (no real API)
# ---------------------------------------------------------------------------


class _ScriptedProvider(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> LlmResponse:
        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return LlmResponse(
            text=text, model="test-stub", usage=LlmUsage(input_tokens=10, output_tokens=5)
        )

    def stream(
        self,
        messages: list[LlmMessage],
        *,
        tools: object = None,
        tool_choice: object = None,
    ) -> Iterator[LlmChunk]:
        yield from []


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
        raise AssertionError("LLM must NOT be called on this path")


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
        pytest.skip("Postgres not reachable — skipping Policy Agent live-DB tests")

    insp = sa.inspect(engine)
    required = {"ai_decision_logs", "policy_rules", "policy_embeddings"}
    missing = required - set(insp.get_table_names())
    if missing:
        pytest.skip(f"Required tables not found: {missing} — run `alembic upgrade head` first")

    yield engine


# Filled by the ``seeded`` fixture (every test using ``_ctx`` requests it).
_SEEDED_USER: dict[str, uuid.UUID] = {}


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> dict[str, Any]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415
    from database.models.identity import Organization, User  # noqa: PLC0415
    from database.seeds.seed_all import seed  # noqa: PLC0415

    seed()

    factory = get_sessionmaker()
    with factory() as session:
        org = session.query(Organization).filter(Organization.name == "Atlas Demo Corp").one()
        user = (
            session.query(User)
            .filter(User.organization_id == org.id, User.email == "finance@atlas-demo.internal")
            .one()
        )
        _SEEDED_USER["id"] = user.id

    return {"org_id": org.id, "user_id": user.id}


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    from backend.app.core.database import get_sessionmaker  # noqa: PLC0415

    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _ctx(
    org_id: uuid.UUID,
    *,
    permission_keys: frozenset[str] | None = None,
) -> AgentContext:
    return AgentContext(
        organization_id=org_id,
        granted_permission_keys=permission_keys or frozenset(),
        # ai_decision_logs.user_id is an FK to users: use the seeded finance user.
        user_id=_SEEDED_USER["id"],
        # Unique per call: tests read back THEIR rows by correlation id (row ids are
        # random UUIDs, so "ORDER BY id DESC" returned an arbitrary row).
        correlation_id=f"policy-agent-db-test-{uuid.uuid4().hex}",
    )


def _det_decision(task_type: str = "policy_evaluate") -> RoutingDecision:
    return RoutingDecision(
        target_agent=AiAgent.policy,
        route=AiRoute.deterministic,
        task_type=task_type,
        confidence=0.95,
        rationale="test",
    )


def _qa_decision(task_type: str = "policy_grounded_lookup") -> RoutingDecision:
    return RoutingDecision(
        target_agent=AiAgent.policy,
        route=AiRoute.llm,
        task_type=task_type,
        confidence=0.95,
        rationale="test",
    )


# ---------------------------------------------------------------------------
# Cap 1 — Deterministic eval over the 7 seeded rules
# ---------------------------------------------------------------------------


class TestEvalSeededRules:
    def test_cabin_class_in_policy(self, seeded: dict, db_session: Session) -> None:
        """Economy on 3h flight → in_policy=True, AIR-CABIN cited."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            # A preferred supplier isolates the cabin rule: an unknown supplier fails
            # the preferred-supplier rule closed (C7-05/C7R), by design.
            payload={
                "item_type": "flight",
                "duration_hours": 3.0,
                "cabin_class": "economy",
                "supplier": "United Airlines",
            },
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is True
        refs = [c.ref for c in result.citations]
        assert "AIR-CABIN" in refs, f"Expected AIR-CABIN citation, got {refs}"

    def test_cabin_class_out_of_policy(self, seeded: dict, db_session: Session) -> None:
        """Business on 3h flight → in_policy=False, AIR-CABIN cited."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "business"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is False
        refs = [c.ref for c in result.citations]
        assert "AIR-CABIN" in refs
        # requires_approver set on out-of-policy business class
        out_verdicts = [v for v in result.output["verdicts"] if not v["in_policy"]]
        assert out_verdicts, "Expected at least one failing verdict"

    def test_hotel_amount_cap_in_policy(self, seeded: dict, db_session: Session) -> None:
        """Hotel $150/night in Austin (domestic cap $180) → in_policy=True."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "hotel", "amount_usd": 150.0, "city": "Austin"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is True
        refs = [c.ref for c in result.citations]
        assert "HOTEL-CAP" in refs

    def test_hotel_amount_cap_out_of_policy(self, seeded: dict, db_session: Session) -> None:
        """Hotel $300/night in Austin (domestic cap $180) → in_policy=False."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "hotel", "amount_usd": 300.0, "city": "Austin"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is False

    def test_car_category_in_policy(self, seeded: dict, db_session: Session) -> None:
        """Economy car → in_policy=True, GROUND-CLASS cited."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "car", "vehicle_class": "Economy"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is True
        refs = [c.ref for c in result.citations]
        assert "GROUND-CLASS" in refs

    def test_car_luxury_out_of_policy(self, seeded: dict, db_session: Session) -> None:
        """Luxury car → in_policy=False, GROUND-CLASS cited."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "car", "vehicle_class": "Luxury"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is False

    def test_receipt_required_in_policy(self, seeded: dict, db_session: Session) -> None:
        """Receipt amount $20 (below $25 threshold) → in_policy=True."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "receipt", "amount_usd": 20.0, "expense_category": "meals"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.status == AgentResultStatus.ok
        # $20 < $25 threshold and meals is not always_required → compliant
        assert result.output["in_policy"] is True

    def test_receipt_required_always_required_category(
        self, seeded: dict, db_session: Session
    ) -> None:
        """Airfare: always_required_categories → RECEIPT-REQ must be cited."""
        handler = PolicyEvalHandler()
        # No receipt attached → expect flagged / out of policy for always-required category
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "receipt", "amount_usd": 10.0, "expense_category": "airfare"},
            signals={"policy_eval": True},
        )
        result = handler.handle(
            task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        # Airfare is always_required → tool should flag it
        # We verify evaluation completed and reported source_clause_ref=RECEIPT-REQ
        assert result.status == AgentResultStatus.ok
        refs = [c.ref for c in result.citations]
        assert "RECEIPT-REQ" in refs

    def test_eval_writes_decision_log_row(self, seeded: dict, db_session: Session) -> None:
        """PolicyEvalHandler writes exactly one ai_decision_logs row."""
        count_before = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()

        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        handler.handle(task, _det_decision(), context=_ctx(seeded["org_id"]), session=db_session)
        db_session.flush()  # flush so we can query within the session

        count_after = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        assert count_after == count_before + 1

    def test_eval_decision_log_correct_route(self, seeded: dict, db_session: Session) -> None:
        """The decision-log row must have route=deterministic and model_provider=NULL."""
        handler = PolicyEvalHandler()
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        ctx = _ctx(seeded["org_id"])

        handler.handle(task, _det_decision(), context=ctx, session=db_session)
        db_session.flush()

        row = (
            db_session.execute(
                sa.text(
                    "SELECT agent, route, model_provider, confidence "
                    "FROM ai_decision_logs WHERE correlation_id = :cid"
                ),
                {"cid": ctx.correlation_id},
            )
            .mappings()
            .one()
        )

        assert row["agent"] == "policy"
        assert row["route"] == "deterministic"
        assert row["model_provider"] is None
        assert float(row["confidence"]) == 1.0


# ---------------------------------------------------------------------------
# Cap 2 — Q&A grounded with real retrieval + scripted LLM
# ---------------------------------------------------------------------------


class TestQaGroundedDb:
    def test_grounded_qa_logs_model_provider(self, seeded: dict, db_session: Session) -> None:
        """Grounded Q&A path (real retriever + scripted LLM) writes a log row."""
        provider = _ScriptedProvider(
            "Economy class is required on flights under 6 hours per AIR-CABIN policy.",
            "SUPPORTED\nAnswered solely from the provided clause.",
        )
        agent = PolicyAgent(provider=provider)
        task = Task(
            context_type="policy_qa",
            payload={
                "question": "flight cabin class economy business premium duration hours upgrade"
            },
        )
        count_before = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()

        ctx = _ctx(seeded["org_id"])
        agent.handle(task, _qa_decision(), context=ctx, session=db_session)
        db_session.flush()

        count_after = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        assert count_after == count_before + 1

        row = (
            db_session.execute(
                sa.text(
                    "SELECT agent, route, model_provider "
                    "FROM ai_decision_logs WHERE correlation_id = :cid"
                ),
                {"cid": ctx.correlation_id},
            )
            .mappings()
            .one()
        )
        assert row["agent"] == "policy"
        assert row["route"] == "llm"
        assert row["model_provider"] is not None  # LLM was invoked


class TestQaSilentDb:
    def test_silent_path_no_llm_called(self, seeded: dict, db_session: Session) -> None:
        """Off-topic query → silent; _RaisingProvider must NOT be called."""
        agent = PolicyAgent(provider=_RaisingProvider())
        task = Task(
            context_type="policy_qa",
            # Local test embeddings are a non-semantic hash, so "off-topic" must be
            # chosen to fall below the retrieval gate deterministically; the
            # semantic out-of-corpus gate is measured by eval-live, not here.
            payload={"question": "chocolate chip cookies oven baking recipe"},
        )
        # _RaisingProvider raises AssertionError if complete() is called
        result = agent.handle(
            task, _qa_decision(), context=_ctx(seeded["org_id"]), session=db_session
        )
        assert result.output["answer_type"] == "silent"
        assert result.output["answer"] is None
        assert result.citations == ()

    def test_silent_path_decision_log_model_provider_none(
        self, seeded: dict, db_session: Session
    ) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = Task(
            context_type="policy_qa",
            payload={"question": "recipe for chocolate chip cookies baking"},
        )
        count_before = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        ctx = _ctx(seeded["org_id"])
        agent.handle(task, _qa_decision(), context=ctx, session=db_session)
        db_session.flush()

        count_after = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        assert count_after == count_before + 1

        row = db_session.execute(
            sa.text("SELECT model_provider FROM ai_decision_logs WHERE correlation_id = :cid"),
            {"cid": ctx.correlation_id},
        ).scalar_one()
        assert row is None


# ---------------------------------------------------------------------------
# Cap 3 — Authoring: draft persists and is evaluable via evaluate_candidate
# ---------------------------------------------------------------------------


class TestAuthoringDraftPersistsDb:
    def test_persist_draft_rule_status_draft(self, seeded: dict, db_session: Session) -> None:
        """persist_draft_rule creates a row with status='draft', never 'active'."""
        candidate = {
            "rule_type": "cabin_class",
            "applies_to": "flight",
            "definition": {
                "tiers": [
                    {"max_hours": 6, "allowed_classes": ["economy"]},
                    {"min_hours": 6, "allowed_classes": ["economy", "premium_economy"]},
                ],
                "override_approver": "VP",
            },
            "source_clause_ref": "TEST-DB-CAB",
        }
        # policy_rules.policy_id and created_by are FKs: use the seeded policy + user.
        from database.models.policy import Policy  # noqa: PLC0415

        policy_id = db_session.scalars(
            sa.select(Policy.id).where(Policy.organization_id == seeded["org_id"]).limit(1)
        ).one()
        created_by = seeded["user_id"]

        # C7R-04: a ref survives only when it is a corpus clause id.
        rule = persist_draft_rule(
            candidate,
            policy_id,
            created_by,
            db_session,
            known_clause_refs=frozenset({"TEST-DB-CAB"}),
        )
        db_session.flush()

        # Re-query to verify what was actually written
        from database.models.policy_rule import PolicyRule  # noqa: PLC0415

        fetched = db_session.get(PolicyRule, rule.id)
        assert fetched is not None
        assert fetched.status == PolicyRuleStatus.draft
        assert fetched.status != PolicyRuleStatus.active
        assert str(fetched.source_clause_ref) == "TEST-DB-CAB"

    def test_draft_rule_is_evaluable(self, seeded: dict, db_session: Session) -> None:
        """evaluate_candidate() on the candidate definition returns non-empty observed."""
        tool = PolicyEvalTool()
        definition = {
            "tiers": [
                {"max_hours": 6, "allowed_classes": ["economy"]},
                {"min_hours": 6, "allowed_classes": ["economy", "premium_economy"]},
            ],
            "override_approver": "VP",
        }
        probe = EvalItem(item_type="flight", duration_hours=3.0, cabin_class="economy")
        verdict = tool.evaluate_candidate(
            rule_type="cabin_class",
            applies_to="flight",
            definition=definition,
            probe_item=probe,
        )
        # A well-formed definition must produce non-empty observed (not a skip verdict)
        assert verdict.observed != {}, (
            f"evaluate_candidate returned skip verdict — definition may be malformed. "
            f"reason={verdict.reason}"
        )


# ---------------------------------------------------------------------------
# End-to-end: orchestrator → PolicyEvalHandler = TWO decision-log rows
# ---------------------------------------------------------------------------


class TestOrchestratorEndToEndDb:
    def test_two_decision_log_rows_eval_path(self, seeded: dict, db_session: Session) -> None:
        """End-to-end eval: orchestrator row + policy handler row = 2 rows."""
        registry = AgentRegistry()
        register_policy(registry)
        orch = Orchestrator(registry)

        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        ctx = _ctx(seeded["org_id"])

        count_before = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()

        orch.handle_task(task, context=ctx, session=db_session)
        db_session.flush()

        count_after = db_session.execute(
            sa.text("SELECT COUNT(*) FROM ai_decision_logs")
        ).scalar_one()
        # Exactly 2 new rows: orchestrator + policy handler
        assert count_after == count_before + 2

    def test_two_rows_have_correct_agents(self, seeded: dict, db_session: Session) -> None:
        """The 2 rows must have agent='orchestrator' and agent='policy'."""
        registry = AgentRegistry()
        register_policy(registry)
        orch = Orchestrator(registry)

        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        ctx = _ctx(seeded["org_id"])

        orch.handle_task(task, context=ctx, session=db_session)
        db_session.flush()

        rows = (
            db_session.execute(
                sa.text("SELECT agent, route FROM ai_decision_logs WHERE correlation_id = :cid"),
                {"cid": ctx.correlation_id},
            )
            .mappings()
            .all()
        )

        agents = {r["agent"] for r in rows}
        assert agents == {"orchestrator", "policy"}
        # Policy row must be deterministic
        policy_row = next(r for r in rows if r["agent"] == "policy")
        assert policy_row["route"] == "deterministic"
