"""P4-T13 Slice 1 — AI conversation endpoints validate gate.

Assertions:
  1. POST /conversations valid → 201 with id
  2. POST /conversations without token → 401
  3. POST /conversations/{id}/messages → SSE stream with event: status + event: done
  4. POST /conversations/{id}/messages from a different user → 404
  5. GET /conversations/{id} → detail with messages list
  6. GET /conversations/{id} from a different user → 404

Live-DB — self-skips when Postgres is unreachable.
The orchestrator dependency is overridden with a stub that returns a deterministic
AgentResult so the test does not require an LLM key.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Generator, Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import create_access_token, hash_password
from backend.app.main_entry.app import app
from backend.app.modules.ai.runtime import get_orchestrator
from database.models import Base
from database.models.ai_decision_log import AiDecisionLog
from database.models.audit_log import AuditLog
from database.models.conversation import Conversation
from database.models.conversation_message import ConversationMessage
from database.models.idempotency_key import IdempotencyKey
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.trip import Trip, TripStatus
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Tables scoped for create/drop in this test module
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, AiDecisionLog.__table__),
    cast(Table, Trip.__table__),
    cast(Table, Conversation.__table__),
    cast(Table, ConversationMessage.__table__),
    cast(Table, IdempotencyKey.__table__),
]

# ---------------------------------------------------------------------------
# Isolated email / name constants
# ---------------------------------------------------------------------------

_ORG_NAME = "Conversations API Test Org"
_ORG_B_NAME = "Conversations API Test Org B"
_USER_A_EMAIL = "conv-user-a@test.internal"
_USER_B_EMAIL = "conv-user-b@test.internal"
_TEST_PW = "test-password-conv-api"

# ---------------------------------------------------------------------------
# DB engine fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping conversations API tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed RBAC + org A (2 users) + org B (1 user)."""
    factory = get_sessionmaker()

    all_emails = [_USER_A_EMAIL, _USER_B_EMAIL]

    # Prune stale data from a previous incomplete run
    with factory() as session:
        stale = session.query(User).filter(User.email.in_(all_emails)).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(ConversationMessage).filter(
                ConversationMessage.conversation_id.in_(
                    session.query(Conversation.id).filter(
                        Conversation.organization_id.in_(stale_org_ids)
                    )
                )
            ).delete(synchronize_session=False)
            session.query(Conversation).filter(
                Conversation.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(AiDecisionLog).filter(
                AiDecisionLog.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(IdempotencyKey).filter(
                IdempotencyKey.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        org_a = Organization(name=_ORG_NAME, base_currency="USD")
        org_b = Organization(name=_ORG_B_NAME, base_currency="USD")
        session.add_all([org_a, org_b])
        session.flush()

        hpw = hash_password(_TEST_PW)

        user_a = User(
            organization_id=org_a.id,
            name="Conversation User A",
            email=_USER_A_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_a)
        session.flush()

        # user_b is in a different org so ownership checks should reject
        user_b = User(
            organization_id=org_b.id,
            name="Conversation User B",
            email=_USER_B_EMAIL,
            role=RoleKey.employee,
            department="Engineering",
            manager_id=None,
            status=UserStatus.active,
            password_hash=hpw,
        )
        session.add(user_b)
        session.flush()

        trip_a = Trip(
            organization_id=org_a.id,
            traveler_id=user_a.id,
            origin="SFO",
            primary_destination="NYC",
            start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2026, 8, 5),
            purpose="Team offsite",
            currency="USD",
            status=TripStatus.draft,
            created_by=user_a.id,
            updated_by=user_a.id,
        )
        session.add(trip_a)
        session.flush()

        session.commit()

    yield {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "user_a_id": user_a.id,
        "user_b_id": user_b.id,
        "trip_a_id": trip_a.id,
    }

    # Teardown in FK-safe order
    with factory() as session:
        org_ids = [org_a.id, org_b.id]
        session.query(ConversationMessage).filter(
            ConversationMessage.conversation_id.in_(
                session.query(Conversation.id).filter(Conversation.organization_id.in_(org_ids))
            )
        ).delete(synchronize_session=False)
        session.query(Conversation).filter(Conversation.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(AiDecisionLog).filter(AiDecisionLog.organization_id.in_(org_ids)).delete()
        session.query(IdempotencyKey).filter(IdempotencyKey.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# Stub orchestrator — deterministic, no LLM calls
# ---------------------------------------------------------------------------


def _stub_orchestrator() -> Any:
    """Build a deterministic mock Orchestrator for use in dependency_overrides."""
    from ai_agents.orchestrator.envelopes import (
        AgentResult,
        AgentResultStatus,
        OrchestratorOutcome,
        RoutingDecision,
    )
    from database.models.ai_decision_log import AiAgent, AiRoute

    mock_orch = MagicMock()

    def _handle_task(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
        decision = RoutingDecision(
            target_agent=AiAgent.policy,
            route=AiRoute.deterministic,
            task_type="policy_qa",
            confidence=0.99,
            rationale="test stub",
        )
        result = AgentResult(
            status=AgentResultStatus.ok,
            output={"answer": "Stub answer for testing."},
            citations=(),
        )
        return OrchestratorOutcome(decision=decision, result=result, log_id=None)

    mock_orch.handle_task = _handle_task
    return mock_orch


# ---------------------------------------------------------------------------
# Client fixture (with orchestrator override)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(seeded: dict[str, Any]) -> Generator[TestClient, None, None]:
    """TestClient with the orchestrator dependency stubbed out."""
    app.dependency_overrides[get_orchestrator] = _stub_orchestrator
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_orchestrator, None)


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


def _bearer(user_id: uuid.UUID, org_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(user_id, org_id)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. POST /conversations → 201
# ---------------------------------------------------------------------------


class TestCreateConversation:
    def test_create_conversation_success(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Authenticated user creates a conversation → 201 with id."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "general"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "id" in body
        assert body["context_type"] == "general"
        assert body["context_ref_id"] is None

    def test_create_conversation_unauthorized(self, client: TestClient) -> None:
        """No token → 401."""
        r = client.post("/api/v1/conversations", json={"context_type": "general"})
        assert r.status_code == 401

    def test_create_conversation_with_context_ref(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """context_ref_id pointing to a real Trip in the org → 201 with ref stored."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        ref_id = str(seeded["trip_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "travel_planning", "context_ref_id": ref_id},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["context_ref_id"] == ref_id

    def test_create_conversation_with_unknown_context_ref(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """context_ref_id with a random UUID (no matching Trip) → 404."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "travel_planning", "context_ref_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert r.status_code == 404, r.text

    def test_create_conversation_general_with_context_ref(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """context_type=general with a context_ref_id → 422 (ref not allowed)."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "general", "context_ref_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 2. POST /conversations/{id}/messages → SSE stream
# ---------------------------------------------------------------------------


class TestSendMessage:
    def _create_conversation(self, seeded: dict[str, Any], client: TestClient) -> str:
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "policy_qa"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def test_send_message_success(self, seeded: dict[str, Any], client: TestClient) -> None:
        """POST a message → SSE stream with event: status and event: done."""
        conv_id = self._create_conversation(seeded, client)
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "What is the meal reimbursement limit?"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.text
        # SSE body must contain status and done frames
        assert "event: status" in body, f"Missing event: status in SSE body:\n{body}"
        assert "event: done" in body, f"Missing event: done in SSE body:\n{body}"
        assert "event: result" in body, f"Missing event: result in SSE body:\n{body}"

    def test_send_message_not_owner(self, seeded: dict[str, Any], client: TestClient) -> None:
        """User B (different org) trying to message User A's conversation → 404."""
        conv_id = self._create_conversation(seeded, client)
        # user_b is in org_b — should NOT see org_a's conversation
        headers = _bearer(seeded["user_b_id"], seeded["org_b_id"])
        r = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Should fail"},
            headers=headers,
        )
        assert r.status_code == 404, r.text

    def test_send_message_nonexistent_conversation(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Random UUID conversation → 404."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            f"/api/v1/conversations/{uuid.uuid4()}/messages",
            json={"content": "Hello"},
            headers=headers,
        )
        assert r.status_code == 404

    def test_send_message_explain_with_llm(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """explain_with_llm=True with a narrative in output → narrative frames present."""
        # Override stub to include a narrative in the output
        from ai_agents.orchestrator.envelopes import (
            AgentResult,
            AgentResultStatus,
            OrchestratorOutcome,
            RoutingDecision,
        )
        from database.models.ai_decision_log import AiAgent, AiRoute

        def _narrative_stub() -> Any:
            mock_orch = MagicMock()

            def _handle(task: Any, *, context: Any, session: Any) -> OrchestratorOutcome:
                decision = RoutingDecision(
                    target_agent=AiAgent.policy,
                    route=AiRoute.llm,
                    task_type="policy_qa",
                    confidence=0.85,
                    rationale="narrative test",
                )
                result = AgentResult(
                    status=AgentResultStatus.ok,
                    output={
                        "answer": "The limit is $75.",
                        "narrative": (
                            "The meal reimbursement limit is set to seventy five dollars per day."
                        ),
                    },
                    citations=(),
                )
                return OrchestratorOutcome(decision=decision, result=result, log_id=None)

            mock_orch.handle_task = _handle
            return mock_orch

        app.dependency_overrides[get_orchestrator] = _narrative_stub
        try:
            conv_id = self._create_conversation(seeded, client)
            headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
            r = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"content": "Explain meal limits.", "explain_with_llm": True},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.text
            assert "event: narrative" in body
        finally:
            app.dependency_overrides[get_orchestrator] = _stub_orchestrator


# ---------------------------------------------------------------------------
# 2b. Explicit categorize intent → deterministic expense_categorize_simple (P8 C1)
# ---------------------------------------------------------------------------


def _sse_frames(body: str) -> dict[str, list[dict[str, Any]]]:
    """Parse an SSE body into {event_name: [data_dict, ...]}."""
    frames: dict[str, list[dict[str, Any]]] = {}
    for block in body.strip().split("\n\n"):
        event: str | None = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None and data is not None:
            frames.setdefault(event, []).append(data)
    return frames


def _no_llm_orchestrator() -> Any:
    """Real Orchestrator with the expense registrations and an LLM that must not be called."""
    from collections.abc import Iterator as _Iterator

    from ai_agents.agents.expense_agent import register_expense
    from ai_agents.orchestrator.orchestrator import Orchestrator
    from ai_agents.orchestrator.registry import AgentRegistry
    from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse

    class _NoLlm(LlmProvider):
        def complete(
            self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
        ) -> LlmResponse:
            raise AssertionError("LLM must NOT be called on the simple categorize path")

        def stream(
            self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
        ) -> _Iterator[LlmChunk]:
            raise AssertionError("LLM must NOT be called on the simple categorize path")

    registry = AgentRegistry()
    register_expense(registry, provider=_NoLlm())
    return Orchestrator(registry)


@pytest.mark.parametrize(
    ("context_type", "payload", "expected"),
    [
        ("expense", {"intent": "categorize"}, {"expense_simple": True}),
        ("expense", {}, {}),
        ("expense", {"intent": "summarize"}, {}),
        ("expense", {"question": "categorize this"}, {}),
        ("policy_qa", {"intent": "categorize"}, {}),
        ("general", {"intent": "categorize"}, {}),
    ],
)
def test_derive_task_signals(context_type: str, payload: dict[str, Any], expected: dict) -> None:
    """Only an explicit intent on an expense conversation sets expense_simple."""
    from backend.app.modules.ai.service import derive_task_signals

    assert derive_task_signals(context_type, payload) == expected


class TestExpenseCategorizeIntent:
    def _create_expense_conversation(self, seeded: dict[str, Any], client: TestClient) -> str:
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "expense"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def test_categorize_intent_routes_deterministic(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """intent=categorize on an expense conversation → simple handler, lodging, no review."""
        app.dependency_overrides[get_orchestrator] = _no_llm_orchestrator
        try:
            conv_id = self._create_expense_conversation(seeded, client)
            headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
            r = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={
                    "content": "Please categorize this charge.",
                    "payload": {"intent": "categorize", "merchant": "Marriott"},
                },
                headers=headers,
            )
        finally:
            app.dependency_overrides[get_orchestrator] = _stub_orchestrator
        assert r.status_code == 200, r.text
        frames = _sse_frames(r.text)
        status = frames["status"][0]
        assert status["task_type"] == "expense_categorize_simple"
        assert status["route"] == "deterministic"
        output = frames["result"][0]["output"]
        assert output["category"] == "lodging"
        assert output["needs_review"] is False
        assert output["route"] == "deterministic"

    def test_expense_without_intent_sends_no_simple_signal(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """Without intent the backend sends empty signals (expense_reasoning path unchanged)."""
        captured: list[Any] = []

        def _capturing_stub() -> Any:
            orch = _stub_orchestrator()
            inner = orch.handle_task

            def _handle(task: Any, *, context: Any, session: Any) -> Any:
                captured.append(task)
                return inner(task, context=context, session=session)

            orch.handle_task = _handle
            return orch

        app.dependency_overrides[get_orchestrator] = _capturing_stub
        try:
            conv_id = self._create_expense_conversation(seeded, client)
            headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
            r = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={
                    "content": "Categorize my Marriott stay",
                    "payload": {"merchant": "Marriott"},
                },
                headers=headers,
            )
        finally:
            app.dependency_overrides[get_orchestrator] = _stub_orchestrator
        assert r.status_code == 200, r.text
        assert len(captured) == 1
        assert dict(captured[0].signals) == {}


# ---------------------------------------------------------------------------
# 3. GET /conversations/{id}
# ---------------------------------------------------------------------------


class TestGetConversation:
    def _create_conversation(self, seeded: dict[str, Any], client: TestClient) -> str:
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.post(
            "/api/v1/conversations",
            json={"context_type": "expense"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def test_get_conversation_success(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Owner fetches own conversation → 200 with messages list."""
        conv_id = self._create_conversation(seeded, client)
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/conversations/{conv_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == conv_id
        assert "messages" in body
        assert isinstance(body["messages"], list)

    def test_get_conversation_with_messages(
        self, seeded: dict[str, Any], client: TestClient
    ) -> None:
        """After sending a message, GET returns message history."""
        conv_id = self._create_conversation(seeded, client)
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        # Send a message first
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "How do I submit an expense?"},
            headers=headers,
        )
        # Now GET the conversation
        r = client.get(f"/api/v1/conversations/{conv_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["messages"]) >= 1
        # First message must be the user turn
        user_msgs = [m for m in body["messages"] if m["role"] == "user"]
        assert len(user_msgs) >= 1
        assert user_msgs[0]["content"] == "How do I submit an expense?"

    def test_get_conversation_not_owner(self, seeded: dict[str, Any], client: TestClient) -> None:
        """User B tries to GET User A's conversation → 404."""
        conv_id = self._create_conversation(seeded, client)
        headers = _bearer(seeded["user_b_id"], seeded["org_b_id"])
        r = client.get(f"/api/v1/conversations/{conv_id}", headers=headers)
        assert r.status_code == 404, r.text

    def test_get_conversation_nonexistent(self, seeded: dict[str, Any], client: TestClient) -> None:
        """Random UUID → 404."""
        headers = _bearer(seeded["user_a_id"], seeded["org_a_id"])
        r = client.get(f"/api/v1/conversations/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404
