"""C7-04 / C7-06 / C7-09 / C7-11 / C7-12 — policy + AI conversation endpoints (DB-backed).

Runs on the shared rollback ``db_session`` (backend/tests/conftest.py).  Orchestrators are
stubbed where the test is about the HTTP/DTO contract; the real orchestrator (local providers,
deterministic eval handler, scripted LLM for authoring) is used where the wiring itself is the
defect.

Regression proof against the pre-fix code (which assertion fails, and why):
  - C7-04 ``test_agent_without_draft_returns_422_and_no_audit``: old router returned the newest
    existing rule with 201 and wrote ``policy_rule.drafted`` -> status 201 != 422.
  - C7-04 ``test_archived_rule_reported_by_agent_is_never_offered``: old router returned the
    newest row (the archived one) with 201.
  - C7-04 ``test_real_agent_receives_intent_and_creates_matching_draft``: old payload had no
    ``intent`` -> the scripted LLM saw an empty intent and the title never reached the prompt.
  - C7-06 ``test_query_needs_review_contract``: old response had no ``needs_review`` /
    ``answer_structured`` and dropped the draft text.
  - C7-09 ``test_policy_qa_reply_is_persisted_and_done_carries_its_id``: old router persisted
    only the user message and the done frame carried the user message id.
  - C7-11 ``test_evaluate_own_report_sets_verdict_and_flags``: old endpoint returned
    verdict "unknown", flags [] and the raw TypeError text.
  - C7-11 ``test_evaluate_*_out_of_scope_404``: old endpoint never loaded the subject -> 200.
  - C7-12 ``test_evaluate_rejects_unknown_subject_type``: old schema accepted any string -> 200.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import (
    AgentResult,
    AgentResultStatus,
    OrchestratorOutcome,
    RoutingDecision,
)
from backend.app.modules.ai import runtime
from backend.app.modules.ai.runtime import get_orchestrator
from backend.tests.api._helpers import parse_sse, sse_done_message_id
from database.models.ai_decision_log import AiAgent, AiRoute
from database.models.audit_log import AuditLog
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.expense import ExpenseCategory
from database.models.identity import RoleKey, User
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import RULES
from services.llm_provider.interface import (
    LlmChunk,
    LlmMessage,
    LlmProvider,
    LlmResponse,
    LlmUsage,
)

pytestmark = pytest.mark.db

API = "/api/v1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decision(task_type: str = "policy_grounded_lookup") -> RoutingDecision:
    return RoutingDecision(
        target_agent=AiAgent.policy,
        route=AiRoute.llm,
        task_type=task_type,
        confidence=0.95,
        rationale="stub",
    )


class _StubOrchestrator:
    """Records the last task and returns a fixed AgentResult (optionally after a side effect)."""

    def __init__(self, result: AgentResult, effect: Any = None) -> None:
        self.result = result
        self.effect = effect
        self.tasks: list[Any] = []

    def handle_task(self, task: Any, *, context: Any, session: Session) -> OrchestratorOutcome:
        self.tasks.append(task)
        result = self.effect(task, context, session) if self.effect else self.result
        return OrchestratorOutcome(decision=_decision(), result=result, log_id=None)


@pytest.fixture()
def stub(app: Any) -> Iterator[Any]:
    """Install a stub orchestrator; the test sets ``holder['orch']``."""
    holder: dict[str, Any] = {}
    app.dependency_overrides[get_orchestrator] = lambda: holder["orch"]
    yield holder
    app.dependency_overrides.pop(get_orchestrator, None)


_HOTEL_CAP_CIT = Citation(
    kind=CitationKind.clause_ref,
    ref="HOTEL-CAP",
    detail="Tier-1 cities: USD 250 per night.",
    source="Atlas Global Travel & Expense Policy",
)
_DRAFT = "Tier-1 cities have a cap of USD 250 per night [Policy §HOTEL-CAP]."

_NEEDS_REVIEW_RESULT = AgentResult(
    status=AgentResultStatus.needs_review,
    output={
        "answer_type": "needs_review",
        "answer": _DRAFT,
        "needs_review": True,
        "answer_structured": {"full_text": _DRAFT, "needs_review": False, "sentences": []},
        "asserted": False,
        "confidence": 0.6,
        "confidence_source": "cosine",
        "threshold": None,
    },
    citations=(_HOTEL_CAP_CIT,),
)

_SILENT_RESULT = AgentResult(
    status=AgentResultStatus.ok,
    output={
        "answer_type": "silent",
        "answer": None,
        "rationale": "This query is not covered by the Atlas T&E documents.",
        "escalation": {"message": "For guidance, contact finance.", "secondary": None},
        "near_topic": None,
    },
    citations=(),
)


# Review fix: a needs_review result whose draft the agent WITHHELD (answer=None) — the agent
# result still carries the draft's citations and answer_structured (audit), but no user-facing
# surface may show them.
_WITHHELD_RESULT = AgentResult(
    status=AgentResultStatus.needs_review,
    output={
        "answer_type": "needs_review",
        "answer": None,
        "needs_review": True,
        "answer_structured": {"full_text": "WITHHELD DRAFT", "needs_review": True, "sentences": []},
        "asserted": False,
        "confidence": 0.6,
        "confidence_source": "cosine",
        "threshold": None,
    },
    citations=(_HOTEL_CAP_CIT,),
)


def _active_policy_with_rules(db: Session, org: Any) -> Policy:
    policy = f.make_policy(db, org=org, is_active=True)
    for r in RULES:
        f.make_policy_rule(
            db,
            policy=policy,
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
    return policy


# ---------------------------------------------------------------------------
# C7-06 — /policy/query contract
# ---------------------------------------------------------------------------


class TestPolicyQueryContract:
    def test_query_needs_review_contract(self, stub: dict, employee_client: Any) -> None:
        stub["orch"] = _StubOrchestrator(_NEEDS_REVIEW_RESULT)
        r = employee_client.post(f"{API}/policy/query", json={"question": "Tier-1 hotel cap?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer_type"] == "needs_review"
        assert body["needs_review"] is True
        assert body["answer"] == _DRAFT
        assert body["answer_structured"]["full_text"] == _DRAFT
        assert [c["ref"] for c in body["citations"]] == ["HOTEL-CAP"]

    def test_query_withheld_draft_leaks_no_text_or_citations(
        self, stub: dict, employee_client: Any
    ) -> None:
        # Review fix: previously the response carried citations=[HOTEL-CAP] and
        # answer_structured.full_text="WITHHELD DRAFT" next to answer=null.
        stub["orch"] = _StubOrchestrator(_WITHHELD_RESULT)
        r = employee_client.post(f"{API}/policy/query", json={"question": "Tier-1 hotel cap?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer_type"] == "needs_review"
        assert body["needs_review"] is True
        assert body["answer"] is None
        assert body["citations"] == []
        assert body["answer_structured"] is None
        assert "WITHHELD DRAFT" not in r.text

    def test_query_silent_shape_unchanged(self, stub: dict, employee_client: Any) -> None:
        stub["orch"] = _StubOrchestrator(_SILENT_RESULT)
        body = employee_client.post(f"{API}/policy/query", json={"question": "weather?"}).json()
        assert body["answer_type"] == "silent"
        assert body["answer"] is None
        assert body["needs_review"] is None


# ---------------------------------------------------------------------------
# C7-09 — assistant reply persisted for policy_qa / general conversations
# ---------------------------------------------------------------------------


def _conv(client: Any, context_type: str) -> str:
    r = client.post(f"{API}/conversations", json={"context_type": context_type})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


class TestConversationReplyPersistence:
    @pytest.mark.parametrize("context_type", ["policy_qa", "general"])
    def test_policy_qa_reply_is_persisted_and_done_carries_its_id(
        self, stub: dict, employee_client: Any, db_session: Session, context_type: str
    ) -> None:
        stub["orch"] = _StubOrchestrator(_NEEDS_REVIEW_RESULT)
        conv_id = _conv(employee_client, context_type)
        r = employee_client.post(
            f"{API}/conversations/{conv_id}/messages", json={"content": "Tier-1 hotel cap?"}
        )
        assert r.status_code == 200, r.text
        done_id = sse_done_message_id(r.text)
        result_frame = next(d for n, d in parse_sse(r.text) if n == "result")
        # SSE result frame carries the same contract shape as /policy/query.
        assert result_frame["output"]["answer"] == _DRAFT
        assert result_frame["output"]["needs_review"] is True

        detail = employee_client.get(f"{API}/conversations/{conv_id}").json()
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["user", "assistant"]
        assistant = detail["messages"][1]
        assert assistant["id"] == done_id  # done frame carries the ASSISTANT message id
        assert assistant["content"] == _DRAFT
        assert assistant["citations"]["answer_type"] == "needs_review"
        assert assistant["citations"]["needs_review"] is True
        assert [c["ref"] for c in assistant["citations"]["citations"]] == ["HOTEL-CAP"]

    def test_withheld_reply_persists_and_streams_no_citations(
        self, stub: dict, employee_client: Any
    ) -> None:
        # Review fix (C7-09 reload): a withheld needs_review turn used to persist the draft's
        # citations (chips under "No confident answer" after reload) and stream them / the
        # draft full_text on the SSE result frame.
        stub["orch"] = _StubOrchestrator(_WITHHELD_RESULT)
        conv_id = _conv(employee_client, "policy_qa")
        r = employee_client.post(
            f"{API}/conversations/{conv_id}/messages", json={"content": "Tier-1 hotel cap?"}
        )
        assert r.status_code == 200, r.text
        assert "WITHHELD DRAFT" not in r.text
        result_frame = next(d for n, d in parse_sse(r.text) if n == "result")
        assert result_frame["citations"] == []
        assert result_frame["output"]["answer_structured"] is None
        assert result_frame["output"]["needs_review"] is True
        msgs = employee_client.get(f"{API}/conversations/{conv_id}").json()["messages"]
        assistant = msgs[1]
        assert assistant["role"] == "assistant"
        assert assistant["content"].startswith("No confident answer")
        assert assistant["citations"]["citations"] == []
        assert assistant["citations"]["needs_review"] is True
        assert assistant["citations"]["answer_type"] == "needs_review"
        assert "answer_structured" not in assistant["citations"]

    def test_silent_reply_persists_the_abstention_text(
        self, stub: dict, employee_client: Any
    ) -> None:
        stub["orch"] = _StubOrchestrator(_SILENT_RESULT)
        conv_id = _conv(employee_client, "policy_qa")
        employee_client.post(f"{API}/conversations/{conv_id}/messages", json={"content": "x?"})
        msgs = employee_client.get(f"{API}/conversations/{conv_id}").json()["messages"]
        assert msgs[1]["role"] == "assistant"
        assert "not covered" in msgs[1]["content"]
        assert "contact finance" in msgs[1]["content"]
        assert msgs[1]["citations"]["answer_type"] == "silent"

    def test_agent_written_reply_is_not_double_written(
        self, stub: dict, employee_client: Any, db_session: Session
    ) -> None:
        # Travel/expense agents append their own assistant row; the router must not add one.
        def _agent_persists(task: Any, context: Any, session: Session) -> AgentResult:
            conv = uuid.UUID(task.payload["conversation_id"])
            seq = (
                session.scalar(
                    sa.select(sa.func.max(ConversationMessage.sequence)).where(
                        ConversationMessage.conversation_id == conv
                    )
                )
                or 0
            )
            session.add(
                ConversationMessage(
                    conversation_id=conv,
                    role=MessageRole.assistant,
                    content="agent reply",
                    sequence=seq + 1,
                )
            )
            session.flush()
            return _NEEDS_REVIEW_RESULT

        stub["orch"] = _StubOrchestrator(_NEEDS_REVIEW_RESULT, effect=_agent_persists)
        conv_id = _conv(employee_client, "general")
        r = employee_client.post(f"{API}/conversations/{conv_id}/messages", json={"content": "q"})
        msgs = employee_client.get(f"{API}/conversations/{conv_id}").json()["messages"]
        assert [m["content"] for m in msgs if m["role"] == "assistant"] == ["agent reply"]
        assert sse_done_message_id(r.text) == msgs[1]["id"]

    def test_expense_context_turn_without_agent_reply_writes_nothing(
        self, stub: dict, employee_client: Any, db_session: Session, users_a: dict[str, User]
    ) -> None:
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        stub["orch"] = _StubOrchestrator(
            AgentResult(status=AgentResultStatus.ok, output={"message": "nothing to assemble"})
        )
        r = employee_client.post(
            f"{API}/conversations",
            json={"context_type": "expense", "context_ref_id": str(report.id)},
        )
        conv_id = r.json()["id"]
        employee_client.post(f"{API}/conversations/{conv_id}/messages", json={"content": "q"})
        msgs = employee_client.get(f"{API}/conversations/{conv_id}").json()["messages"]
        assert [m["role"] for m in msgs] == ["user"]


# ---------------------------------------------------------------------------
# C7-11 / C7-12 — /policy/evaluate
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_orchestrator(app: Any, local_ai: None) -> Iterator[None]:
    app.dependency_overrides.pop(get_orchestrator, None)
    yield


class TestPolicyEvaluate:
    def test_evaluate_rejects_unknown_subject_type(self, employee_client: Any) -> None:
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={"subject_type": "spaceship", "subject_id": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text

    def test_evaluate_missing_subject_404(
        self, real_orchestrator: None, employee_client: Any
    ) -> None:
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={"subject_type": "expense_report", "subject_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404, r.text

    def test_evaluate_other_users_report_out_of_scope_404(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
    ) -> None:
        stranger = f.make_user(db_session, org=org_a, role=RoleKey.employee)
        report = f.make_expense_report(db_session, owner=stranger)
        entry = f.make_expense(db_session, report=report, category=ExpenseCategory.meals)
        for subject_type, subject_id in (
            ("expense_report", report.id),
            ("expense_entry", entry.id),
        ):
            r = employee_client.post(
                f"{API}/policy/evaluate",
                json={"subject_type": subject_type, "subject_id": str(subject_id)},
            )
            assert r.status_code == 404, (subject_type, r.text)

    def test_evaluate_cross_tenant_404(
        self, real_orchestrator: None, employee_client_b: Any, db_session: Session, users_a: Any
    ) -> None:
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        r = employee_client_b.post(
            f"{API}/policy/evaluate",
            json={"subject_type": "expense_report", "subject_id": str(report.id)},
        )
        assert r.status_code == 404, r.text

    def test_evaluate_own_report_sets_verdict_and_flags(
        self,
        real_orchestrator: None,
        employee_client: Any,
        manager_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
    ) -> None:
        _active_policy_with_rules(db_session, org_a)
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        hotel = f.make_expense(
            db_session,
            report=report,
            category=ExpenseCategory.lodging,
            amount=Decimal("389.00"),
            vendor="Marriott",
        )
        body = {"subject_type": "expense_report", "subject_id": str(report.id)}
        r = employee_client.post(f"{API}/policy/evaluate", json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["verdict"] == "violation"
        refs = {fl["source_clause_ref"] for fl in out["flags"]}
        assert "HOTEL-CAP" in refs
        # C7R: entry_ref is the approver-facing label (vendor), never the entry UUID.
        assert all(fl["entry_ref"] == "Marriott" for fl in out["flags"])
        assert hotel.vendor == "Marriott"
        assert out["output"]["items"][0]["item_type"] == "hotel"
        assert "error" not in out["output"]
        # The direct manager has READ scope on the report -> may evaluate it.
        assert manager_client.post(f"{API}/policy/evaluate", json=body).status_code == 200

    def test_evaluate_in_policy_entry(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
    ) -> None:
        _active_policy_with_rules(db_session, org_a)
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        entry = f.make_expense(
            db_session, report=report, category=ExpenseCategory.meals, amount=Decimal("20.00")
        )
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={"subject_type": "expense_entry", "subject_id": str(entry.id)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "in_policy"
        assert r.json()["flags"] == []

    def test_evaluate_invalid_context_is_422_without_exception_text(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        users_a: dict[str, User],
    ) -> None:
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={
                "subject_type": "expense_report",
                "subject_id": str(report.id),
                "context": {"amount_usd": 10},  # no item_type -> the C7-11 TypeError repro
            },
        )
        assert r.status_code == 422, r.text
        assert "__init__" not in r.text and "positional argument" not in r.text

    @pytest.mark.parametrize(
        "context",
        [
            {"item_type": "hotel", "amount_usd": "abc", "city": "New York"},
            {"item_type": "hotel", "amount_usd": 300, "city": 5},
            {"item_type": "meals", "amount_usd": 40, "region": "mars"},
            {"item_type": "hotel", "amount_usd": True, "city": "New York"},
            {"item_type": "hotel", "amount_usd": "inf", "city": "New York"},
            {"item_type": "hotel", "amount_usd": 250, "room_only_nightly_usd": True},
            {"item_type": "hotel", "amount_usd": 250, "room_only_nightly_usd": "nan"},
        ],
    )
    def test_evaluate_wrongly_typed_context_is_422_not_500(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
        context: dict[str, Any],
    ) -> None:
        # Review fix: EvalItem is an unchecked dataclass, so {"amount_usd": "abc"} was accepted
        # and PolicyEvalTool raised TypeError -> 500.  Needs active rules so the tool compares.
        _active_policy_with_rules(db_session, org_a)
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={
                "subject_type": "expense_report",
                "subject_id": str(report.id),
                "context": context,
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "not supported" not in r.text and "Traceback" not in r.text

    def test_evaluate_numeric_string_context_is_coerced(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
    ) -> None:
        _active_policy_with_rules(db_session, org_a)
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={
                "subject_type": "expense_report",
                "subject_id": str(report.id),
                "context": {"item_type": "hotel", "amount_usd": "389", "city": "New York"},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "violation"

    def test_evaluate_empty_report_is_not_vacuously_in_policy(
        self,
        real_orchestrator: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
    ) -> None:
        # Review fix: zero entries + no context used to return 200 {verdict: "in_policy"}.
        _active_policy_with_rules(db_session, org_a)
        report = f.make_expense_report(db_session, owner=users_a["employee"])
        r = employee_client.post(
            f"{API}/policy/evaluate",
            json={"subject_type": "expense_report", "subject_id": str(report.id)},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verdict"] == "needs_review"
        assert body["output"]["in_policy"] is False
        assert body["output"]["reason"] == "no_items_to_evaluate"


# ---------------------------------------------------------------------------
# C7-04 — AI rule authoring
# ---------------------------------------------------------------------------


def _drafted_audits(db: Session, org_id: uuid.UUID) -> list[AuditLog]:
    return list(
        db.scalars(
            sa.select(AuditLog).where(
                AuditLog.organization_id == org_id, AuditLog.action == "policy_rule.drafted"
            )
        ).all()
    )


class _ScriptedLlm(LlmProvider):
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> LlmResponse:
        self.prompts.append(messages[-1].content)
        text = self._responses[(len(self.prompts) - 1) % len(self._responses)]
        return LlmResponse(text=text, model="stub", usage=LlmUsage(input_tokens=1, output_tokens=1))

    def stream(
        self, messages: list[LlmMessage], *, tools: object = None, tool_choice: object = None
    ) -> Iterator[LlmChunk]:
        yield from []


# Issue 5: a car rule only covers booked car rentals — the old "taxi or rideshare" title
# drafted as a car rule encoded a CHANGED meaning and is now refused by the scope backstop
# (see test_issue5_rule_authoring_api_db.py), so this fixture names a car-rental intent.
_CAR_TITLE = "No single car rental expense over $150"
_CAR_RULE = {
    "rule_type": "amount_cap",
    "applies_to": "car",
    "definition": {"currency": "USD", "per": "day", "tiers": [{"tier": "standard", "cap": 150}]},
    "source_clause_ref": None,
}


class TestRuleAuthoring:
    def test_agent_without_draft_returns_422_and_no_audit(
        self, stub: dict, finance_client: Any, db_session: Session, org_a: Any
    ) -> None:
        policy = f.make_policy(db_session, org=org_a, is_active=True)
        f.make_policy_rule(db_session, policy=policy, status=PolicyRuleStatus.active)
        stub["orch"] = _StubOrchestrator(
            AgentResult(
                status=AgentResultStatus.needs_review,
                output={"draft_created": False, "reason": "no"},
            )
        )
        r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _CAR_TITLE})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert _drafted_audits(db_session, org_a.id) == []
        # Issue 5: the refused attempt is audited (no failure_kind -> legacy generic code).
        failed = list(
            db_session.scalars(
                sa.select(AuditLog).where(
                    AuditLog.organization_id == org_a.id,
                    AuditLog.action == "policy_rule.draft_failed",
                )
            ).all()
        )
        assert len(failed) == 1 and failed[0].entity_id == policy.id
        assert (failed[0].after_state or {})["error_code"] == "VALIDATION_ERROR"
        # The router passed the rule text under the key the agent reads.
        assert stub["orch"].tasks[0].payload["intent"].startswith(_CAR_TITLE)

    def test_archived_rule_reported_by_agent_is_never_offered(
        self, stub: dict, finance_client: Any, db_session: Session, org_a: Any
    ) -> None:
        policy = f.make_policy(db_session, org=org_a, is_active=True)
        archived = f.make_policy_rule(
            db_session,
            policy=policy,
            applies_to=PolicyAppliesTo.car,
            status=PolicyRuleStatus.archived,
        )
        stub["orch"] = _StubOrchestrator(
            AgentResult(
                status=AgentResultStatus.ok,
                output={"draft_created": True, "rule_id": str(archived.id), "status": "draft"},
            )
        )
        r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _CAR_TITLE})
        assert r.status_code == 422, r.text
        assert _drafted_audits(db_session, org_a.id) == []

    def test_created_draft_is_returned_with_provenance_audit(
        self,
        stub: dict,
        finance_client: Any,
        db_session: Session,
        org_a: Any,
        users_a: dict[str, User],
    ) -> None:
        policy = f.make_policy(db_session, org=org_a, is_active=True)

        # A NEWER unrelated rule exists — the old "newest row" lookup would return it.
        def _create(task: Any, context: Any, session: Session) -> AgentResult:
            rule = f.make_policy_rule(
                session,
                policy=policy,
                applies_to=PolicyAppliesTo.car,
                status=PolicyRuleStatus.draft,
                created_by=context.user_id,
            )
            f.make_policy_rule(session, policy=policy, status=PolicyRuleStatus.archived)
            return AgentResult(
                status=AgentResultStatus.ok,
                output={"draft_created": True, "rule_id": str(rule.id), "status": "draft"},
            )

        stub["orch"] = _StubOrchestrator(None, effect=_create)  # type: ignore[arg-type]
        r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _CAR_TITLE})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "draft"
        assert body["applies_to"] == "car"
        audits = _drafted_audits(db_session, org_a.id)
        assert len(audits) == 1
        assert str(audits[0].entity_id) == body["id"]
        assert audits[0].actor_id == users_a["finance"].id
        after_state = audits[0].after_state
        assert after_state is not None
        assert after_state["provenance"] == "ai_generated"
        assert after_state["status"] == "draft"

    def test_real_agent_receives_intent_and_creates_matching_draft(
        self,
        app: Any,
        local_ai: None,
        finance_client: Any,
        db_session: Session,
        org_a: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = f.make_policy(db_session, org=org_a, is_active=True)
        f.make_policy_rule(db_session, policy=policy, status=PolicyRuleStatus.archived)
        llm = _ScriptedLlm(json.dumps(_CAR_RULE), "FAITHFUL\nCaptures the cap.")
        monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
        runtime.build_orchestrator.cache_clear()
        app.dependency_overrides.pop(get_orchestrator, None)
        try:
            before = db_session.scalar(
                sa.select(sa.func.count()).where(PolicyRule.policy_id == policy.id)
            )
            r = finance_client.post(f"{API}/policies/{policy.id}/rules", json={"title": _CAR_TITLE})
        finally:
            runtime.build_orchestrator.cache_clear()

        assert r.status_code == 201, r.text
        assert _CAR_TITLE in llm.prompts[0]  # the intent reached the authoring prompt
        body = r.json()
        assert body["status"] == "draft"
        assert body["rule_type"] == "amount_cap"
        assert body["applies_to"] == "car"
        assert body["definition"]["tiers"][0]["cap"] == 150
        after = db_session.scalar(
            sa.select(sa.func.count()).where(PolicyRule.policy_id == policy.id)
        )
        assert before is not None and after == before + 1
        audits = _drafted_audits(db_session, org_a.id)
        assert [str(a.entity_id) for a in audits] == [body["id"]]

    def test_archived_rule_cannot_be_activated(
        self, finance_client: Any, db_session: Session, org_a: Any
    ) -> None:
        policy = f.make_policy(db_session, org=org_a, is_active=True)
        archived = f.make_policy_rule(db_session, policy=policy, status=PolicyRuleStatus.archived)
        r = finance_client.patch(f"{API}/policy-rules/{archived.id}", json={"status": "active"})
        assert r.status_code == 422, r.text
        db_session.refresh(archived)
        assert archived.status is PolicyRuleStatus.archived


# ---------------------------------------------------------------------------
# C7-06 / C7-10 end to end — staging reproduction (legacy stub index, local embedder, no
# reranker, chunk_tsv NULL), real orchestrator + PolicyAgent, scripted LLM.
# ---------------------------------------------------------------------------


def _seed_staging_stub_index(db: Session, org: Any) -> None:
    """The 8 stub clauses embedded with the CURRENT local embedder, as on the staging Neon DB.

    chunk_tsv is left NULL (legacy rows indexed before R1-T1), so the sparse path is empty —
    exactly the condition under which the C7-10 clause was culled.
    """
    from database.models.policy_embedding import PolicyEmbedding
    from database.seeds.policy_content import CLAUSES
    from services.embeddings_provider.local import embed

    policy = f.make_policy(db, org=org, is_active=True, name="Atlas Global Travel & Expense Policy")
    for i, c in enumerate(CLAUSES):
        db.add(
            PolicyEmbedding(
                policy_id=policy.id,
                chunk_text=c["chunk_text"],
                clause_ref=c["clause_ref"],
                ordinal=i,
                embedding=embed(c["chunk_text"]),
            )
        )
    db.flush()


@pytest.fixture()
def scripted_agent(app: Any, local_ai: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    llm = _ScriptedLlm(_DRAFT, "SUPPORTED\nThe clause states USD 250 per night for Tier-1.")
    monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
    runtime.build_orchestrator.cache_clear()
    app.dependency_overrides.pop(get_orchestrator, None)
    yield llm
    runtime.build_orchestrator.cache_clear()


def _latest_policy_decision(db: Session) -> Any:
    from database.models.ai_decision_log import AiDecisionLog

    return (
        db.execute(
            sa.select(AiDecisionLog)
            .where(AiDecisionLog.agent == AiAgent.policy)
            .order_by(AiDecisionLog.created_at.desc())
        )
        .scalars()
        .first()
    )


# A draft that answers what the Tier-1 question asks (hotel, nightly rate, cap, Tier-1 cities).
_RELEVANT_DRAFT = (
    "The nightly hotel rate cap for Tier-1 cities is USD 250 per night [Policy §HOTEL-CAP]."
)


@pytest.fixture()
def scripted_relevant_agent(
    app: Any, local_ai: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    llm = _ScriptedLlm(_RELEVANT_DRAFT, "SUPPORTED\nThe clause states USD 250 per night.")
    monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
    runtime.build_orchestrator.cache_clear()
    app.dependency_overrides.pop(get_orchestrator, None)
    yield llm
    runtime.build_orchestrator.cache_clear()


class TestStagingReproductionEndToEnd:
    """Staging = local embedder + LOCAL reranker: the cosine path, real confidence < 0.70."""

    def test_policy_query_returns_provisional_cited_answer(
        self, scripted_agent: Any, employee_client: Any, db_session: Session, org_a: Any
    ) -> None:
        _seed_staging_stub_index(db_session, org_a)
        r = employee_client.post(
            f"{API}/policy/query",
            json={"question": "What is the nightly hotel rate cap for Tier-1 cities?"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Cosine path, confidence in the local relevance band [0.55, 0.70): the terse draft
        # never says what the cap is FOR ("hotel", "rate"), so it fails the C7R answer-vs-
        # question relevance gate -> needs_review, and the critic-supported, verified draft is
        # shown provisionally instead of answer=null.  (Before C7R: the fixed 0.70 floor alone
        # made every answer here needs_review.)
        assert body["answer_type"] == "needs_review"
        assert body["needs_review"] is True
        assert body["answer"] == _DRAFT
        assert body["answer_structured"]["full_text"] == _DRAFT
        assert "HOTEL-CAP" in [c["ref"] for c in body["citations"]]
        log = _latest_policy_decision(db_session)
        assert 0.55 <= float(log.confidence) < 0.70
        assert log.output_summary["withheld_reason"] == "question_not_covered"

    def test_relevant_answer_below_070_is_grounded(
        self, scripted_relevant_agent: Any, employee_client: Any, db_session: Session, org_a: Any
    ) -> None:
        # C7R: the same real retrieval (local cosine confidence < 0.70, which the old fixed
        # floor always withheld) with a verified answer that covers the question is grounded.
        _seed_staging_stub_index(db_session, org_a)
        body = employee_client.post(
            f"{API}/policy/query",
            json={"question": "What is the nightly hotel rate cap for Tier-1 cities?"},
        ).json()
        assert body["answer_type"] == "grounded", body
        assert body["answer"] == _RELEVANT_DRAFT
        assert body.get("needs_review") is None
        assert [c["ref"] for c in body["citations"]] == ["HOTEL-CAP"]
        log = _latest_policy_decision(db_session)
        assert 0.55 <= float(log.confidence) < 0.70
        assert log.tools_used["GroundedRetriever"]["gate"]["source"] == "cosine"
        assert log.output_summary["question_relevance"]["relevant"] is True

    def test_conversation_sse_and_persisted_reply(
        self, scripted_agent: Any, employee_client: Any, db_session: Session, org_a: Any
    ) -> None:
        _seed_staging_stub_index(db_session, org_a)
        conv_id = _conv(employee_client, "policy_qa")
        r = employee_client.post(
            f"{API}/conversations/{conv_id}/messages",
            json={"content": "What is the nightly hotel rate cap for Tier-1 cities?"},
        )
        frames = dict(parse_sse(r.text))
        assert frames["result"]["output"]["answer_type"] == "needs_review"
        assert frames["result"]["output"]["answer"] == _DRAFT
        assert frames["done"]["needs_review"] is True
        msgs = employee_client.get(f"{API}/conversations/{conv_id}").json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[1]["id"] == frames["done"]["message_id"]
        assert msgs[1]["content"] == _DRAFT

    def test_in_corpus_clause_reaches_the_llm_context(
        self, scripted_agent: Any, employee_client: Any, db_session: Session, org_a: Any
    ) -> None:
        # C7-10: "nightly hotel cap" — HOTEL-CAP (cosine 0.142) used to be culled, leaving only
        # HOTEL-PREF in the prompt; the LLM then concluded there was no cap.
        _seed_staging_stub_index(db_session, org_a)
        r = employee_client.post(
            f"{API}/policy/query", json={"question": "What is the nightly hotel cap?"}
        )
        assert r.status_code == 200, r.text
        seed_prompt = scripted_agent.prompts[0]
        assert "USD 250 per night" in seed_prompt  # the HOTEL-CAP clause text was supplied
        assert "§HOTEL-CAP" in seed_prompt

    def test_out_of_corpus_question_is_not_answered(
        self,
        app: Any,
        local_ai: None,
        employee_client: Any,
        db_session: Session,
        org_a: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # "weather in Paris" slips past the weak 0.20 cosine gate on stop words (0.233); the
        # model cannot cite a clause, so there is no provisional answer.
        llm = _ScriptedLlm("The policy does not cover the weather.", "SUPPORTED\nok")
        monkeypatch.setattr(runtime, "get_llm_provider", lambda: llm)
        runtime.build_orchestrator.cache_clear()
        app.dependency_overrides.pop(get_orchestrator, None)
        try:
            _seed_staging_stub_index(db_session, org_a)
            body = employee_client.post(
                f"{API}/policy/query", json={"question": "What is the weather in Paris?"}
            ).json()
        finally:
            runtime.build_orchestrator.cache_clear()
        assert body["answer_type"] in ("silent", "needs_review")
        assert body["answer"] is None
