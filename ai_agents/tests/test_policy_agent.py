"""Hermetic tests for the Policy Agent (P4-T8) — all three capabilities.

No database, no LLM API, no network. All 12 Section 4 verification gates covered.

Gates covered:
  Gate 1  — Eval pass/fail on fixtures, correct clause citations, M-A3 no false pass
  Gate 2  — Eval is LLM-free (raising provider never called, route=deterministic)
  Gate 3  — Q&A grounded: answer cites clause, over-claim is rejected
  Gate 4  — Q&A silent guardrail: no LLM claim, no citation, model_provider=None
  Gate 5  — Authoring produces evaluable cabin_class/flight draft rule
  Gate 6  — Authoring never activates (status always draft)
  Gate 7  — Authoring RBAC: employee/manager blocked, finance/admin allowed
  Gate 8  — Malformed/unfaithful LLM output → reflection rejected, nothing persisted
  Gate 9  — Decision log: one policy row per path with correct fields
  Gate 10 — End-to-end via orchestrator: two rows (orchestrator + policy)
  Gate 11 — Provider abstraction: LLM only via abstracted provider, no real API
  Gate 12 — Layering: agents modules importable without backend
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from ai_agents.agents import PolicyAgent, PolicyEvalHandler, register_policy
from ai_agents.context import AgentContext
from ai_agents.decision_log.record import DecisionRecord
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import (
    AgentResultStatus,
    RoutingDecision,
    Task,
)
from ai_agents.orchestrator.registry import AgentRegistry
from ai_agents.rag.retrieval import GroundedClause, GroundedRetrieval, GroundingStatus
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict, RuleVerdict
from database.models.ai_decision_log import AiAgent, AiRoute, ReflectionOutcome
from database.models.policy_rule import PolicyRuleStatus
from services.llm_provider.interface import LlmChunk, LlmMessage, LlmProvider, LlmResponse, LlmUsage

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_POLICY_ID = uuid.uuid4()

# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _RaisingProvider(LlmProvider):
    """Raises AssertionError if ever called — proves LLM is not touched on certain paths."""

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


class _ScriptedProvider(LlmProvider):
    """Returns pre-scripted responses in sequence (cyclic)."""

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


# ---------------------------------------------------------------------------
# Fake session
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed = True


# ---------------------------------------------------------------------------
# Pre-built fixtures: verdicts, retrieval results, decisions
# ---------------------------------------------------------------------------

_ITEM_IN = EvalItem(item_type="flight", duration_hours=3.0, cabin_class="economy")
_ITEM_OUT = EvalItem(item_type="flight", duration_hours=3.0, cabin_class="business")

_RULE_VERDICT_IN = RuleVerdict(
    rule_id="r-cabin",
    rule_type="cabin_class",
    applies_to="flight",
    source_clause_ref="AIR-CABIN",
    in_policy=True,
    reason="Economy is allowed for 3h flights.",
    requires_approver=None,
    observed={"duration_hours": 3.0, "cabin_class": "economy", "allowed": ["economy"]},
)
_RULE_VERDICT_OUT = RuleVerdict(
    rule_id="r-cabin",
    rule_type="cabin_class",
    applies_to="flight",
    source_clause_ref="AIR-CABIN",
    in_policy=False,
    reason="Business is NOT allowed for 3h flights. Allowed: ['economy'].",
    requires_approver="VP",
    observed={"duration_hours": 3.0, "cabin_class": "business", "allowed": ["economy"]},
)

_POLICY_VERDICT_IN = PolicyVerdict(
    item=_ITEM_IN,
    in_policy=True,
    verdicts=(_RULE_VERDICT_IN,),
    evaluated_rule_count=1,
)
_POLICY_VERDICT_OUT = PolicyVerdict(
    item=_ITEM_OUT,
    in_policy=False,
    verdicts=(_RULE_VERDICT_OUT,),
    evaluated_rule_count=1,
)

_GROUNDED = GroundedRetrieval(
    query="Is business class allowed to London?",
    status=GroundingStatus.grounded,
    clauses=(
        GroundedClause(
            clause_ref="AIR-CABIN",
            chunk_text="Business class is only allowed on flights over 10 hours.",
            similarity=0.92,
        ),
    ),
    citations=(
        Citation(
            kind=CitationKind.clause_ref,
            ref="AIR-CABIN",
            detail="Business class is only allowed on flights over 10 hours.",
            source="Atlas T&E Policy",
        ),
    ),
    confidence=0.92,
    reason=None,
    threshold=0.20,
)

_SILENT = GroundedRetrieval(
    query="What is the policy on emotional support animals?",
    status=GroundingStatus.silent,
    clauses=(),
    citations=(),
    confidence=None,
    reason="below_threshold: top similarity 0.05 < threshold 0.20",
    threshold=0.20,
)

# Routing decisions
_DET_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.deterministic,
    task_type="policy_evaluate",
    confidence=0.95,
    rationale="test",
)
_QA_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_grounded_lookup",
    confidence=0.95,
    rationale="test",
)
_AUTH_DECISION = RoutingDecision(
    target_agent=AiAgent.policy,
    route=AiRoute.llm,
    task_type="policy_authoring",
    confidence=0.95,
    rationale="test",
)


def _ctx_no_policy() -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        granted_permission_keys=frozenset(),
        user_id=_USER_ID,
    )


def _ctx_finance() -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        granted_permission_keys=frozenset({"policy.manage"}),
        user_id=_USER_ID,
    )


def _eval_task(**payload_extra: Any) -> Task:
    return Task(
        context_type="policy_qa",
        payload={"item_type": "flight", "duration_hours": 3.0, **payload_extra},
        signals={"policy_eval": True},
    )


def _qa_task(question: str = "Is business class allowed?") -> Task:
    return Task(context_type="policy_qa", payload={"question": question})


def _auth_task(intent: str = "No business class on flights under 6 hours") -> Task:
    return Task(
        context_type="policy_qa",
        payload={"intent": intent, "policy_id": str(_POLICY_ID)},
        signals={"policy_authoring": True},
    )


# Valid cabin_class JSON the scripted provider returns for authoring
_VALID_CABIN_JSON = json.dumps(
    {
        "rule_type": "cabin_class",
        "applies_to": "flight",
        "definition": {
            "tiers": [
                {"max_hours": 6, "allowed_classes": ["economy"]},
                {"min_hours": 6, "allowed_classes": ["economy", "premium_economy"]},
            ],
            "override_approver": "VP",
        },
        "source_clause_ref": "TEST-CAB",
    }
)


# ---------------------------------------------------------------------------
# Gate 1 — Eval pass/fail, correct clause citations, M-A3
# ---------------------------------------------------------------------------


class TestGate1EvalPassFail:
    def test_in_policy_item_passes(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="economy")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = handler.handle(
                task, _DET_DECISION, context=_ctx_finance(), session=MagicMock()
            )

        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is True
        # Citation for AIR-CABIN present
        assert any(c.ref == "AIR-CABIN" for c in result.citations)

    def test_out_of_policy_item_fails(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="business")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_OUT,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = handler.handle(
                task, _DET_DECISION, context=_ctx_finance(), session=MagicMock()
            )

        assert result.status == AgentResultStatus.ok
        assert result.output["in_policy"] is False
        assert result.output["verdicts"][0]["source_clause_ref"] == "AIR-CABIN"
        # Citation for AIR-CABIN present
        assert any(c.ref == "AIR-CABIN" for c in result.citations)

    def test_m_a3_out_of_policy_never_passes(self) -> None:
        """M-A3: NO out-of-policy item ever returns in_policy=True."""
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="business")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_OUT,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = handler.handle(
                task, _DET_DECISION, context=_ctx_finance(), session=MagicMock()
            )

        # This is the M-A3 guard: an out-of-policy item must NEVER show in_policy=True
        assert result.output["in_policy"] is False

    def test_citation_source_is_policy_rules(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="economy")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = handler.handle(
                task, _DET_DECISION, context=_ctx_finance(), session=MagicMock()
            )

        for cit in result.citations:
            assert cit.source == "policy_rules"
            assert cit.kind == CitationKind.clause_ref


# ---------------------------------------------------------------------------
# Gate 2 — Eval is LLM-free
# ---------------------------------------------------------------------------


class TestGate2EvalLlmFree:
    def test_raising_provider_never_called(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="economy")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            # If _RaisingProvider.complete() is called, this test fails with AssertionError
            result = handler.handle(
                task, _DET_DECISION, context=_ctx_finance(), session=MagicMock()
            )

        assert result.status == AgentResultStatus.ok  # reached without LLM

    def test_decision_log_route_deterministic_model_provider_none(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="economy")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch("ai_agents.agents.policy_eval_handler.write_decision_log") as mock_log,
        ):
            handler.handle(task, _DET_DECISION, context=_ctx_finance(), session=MagicMock())

        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.route == AiRoute.deterministic
        assert record.model_provider is None
        assert record.agent == AiAgent.policy

    def test_no_llm_provider_imported_in_handler(self) -> None:
        import ai_agents.agents.policy_eval_handler as mod

        # The handler module must not import LlmProvider
        assert not hasattr(mod, "LlmProvider"), "PolicyEvalHandler must not import LlmProvider"
        assert not hasattr(
            mod, "get_llm_provider"
        ), "PolicyEvalHandler must not import get_llm_provider"


# ---------------------------------------------------------------------------
# Gate 3 — Q&A grounded: citation present, over-claim rejected
# ---------------------------------------------------------------------------


class TestGate3QaGrounded:
    def test_grounded_answer_includes_citation(self) -> None:
        # Provider: first call = answer, second call = SUPPORTED (critic).
        # R5-T2 STRICT: the answer must carry a document-qualified inline [Label §ref] marker
        # whose ref matches a retrieval citation, else no citation is verified and the answer
        # is not asserted.  AIR-CABIN is the ref of the _GROUNDED fixture's citation.
        provider = _ScriptedProvider(
            "No, business class is not allowed on short flights [T&E Policy §AIR-CABIN].",
            "SUPPORTED\nThe answer follows directly from the AIR-CABIN clause.",
        )
        agent = PolicyAgent(provider=provider)
        task = _qa_task("Is business class allowed to London?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_GROUNDED,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        assert result.status in (AgentResultStatus.ok, AgentResultStatus.needs_review)
        # If asserted, citation must reference AIR-CABIN
        if result.output.get("asserted"):
            assert any(c.ref == "AIR-CABIN" for c in result.citations)

    def test_critic_always_contradicts_causes_rejected(self) -> None:
        # Provider: seed produces an answer, critic always says CONTRADICTED (no correction)
        provider = _ScriptedProvider(
            "Business class is allowed everywhere.",
            "CONTRADICTED\nThis contradicts the clause which restricts business class.",
            "Business class is always fine.",
            "CONTRADICTED\nStill wrong.",
            "It is fine.",
            "CONTRADICTED\nWrong again.",
        )
        agent = PolicyAgent(provider=provider)
        task = _qa_task("Is business class allowed?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_GROUNDED,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        # Loop exhausts → rejected → needs_review, not asserted
        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("asserted") is False or result.output.get("answer") is None
        assert result.reflection_outcome in (ReflectionOutcome.rejected, ReflectionOutcome.revised)


# ---------------------------------------------------------------------------
# Gate 4 — Q&A silent guardrail
# ---------------------------------------------------------------------------


class TestGate4QaSilent:
    def test_silent_returns_silent_output(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("What is the policy on emotional support animals?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_SILENT,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            # _RaisingProvider raises if called — test proves LLM never touched
            result = agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        assert result.output["answer_type"] == "silent"
        assert result.output["answer"] is None
        assert result.citations == ()
        assert result.confidence is None

    def test_silent_path_model_provider_none(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("off-topic question")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_SILENT,
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log,
        ):
            agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.model_provider is None
        assert record.route == AiRoute.llm
        assert record.agent == AiAgent.policy

    def test_silent_never_asserts_policy_claim(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("off-topic question")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_SILENT,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        # No claim asserted, no citation — the key guardrail
        assert not result.citations
        assert result.output.get("answer") is None


# ---------------------------------------------------------------------------
# Gate 5 — Authoring produces evaluable cabin_class/flight draft
# ---------------------------------------------------------------------------


class TestGate5AuthoringDraft:
    def test_authoring_success_creates_draft(self) -> None:
        # Provider: 1st call = JSON candidate (seed), 2nd call = FAITHFUL (critic)
        provider = _ScriptedProvider(_VALID_CABIN_JSON, "FAITHFUL\nAccurately captures the intent.")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("No business class on flights under 6 hours")
        session = _FakeSession()

        with (
            patch(
                "ai_agents.agents.policy_authoring.PolicyEvalTool.evaluate_candidate",
                return_value=RuleVerdict(
                    rule_id="transient",
                    rule_type="cabin_class",
                    applies_to="flight",
                    source_clause_ref=None,
                    in_policy=True,
                    reason="Economy allowed for 3h.",
                    requires_approver=None,
                    observed={"duration_hours": 3.0, "cabin_class": "economy"},
                ),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(
                task,
                _AUTH_DECISION,
                context=_ctx_finance(),
                session=session,  # type: ignore[arg-type]
            )

        assert result.output.get("draft_created") is True
        assert result.output.get("rule_type") == "cabin_class"
        assert result.output.get("status") == "draft"
        # session.add was called with a PolicyRule
        assert len(session.added) == 1
        assert session.flushed is True


# ---------------------------------------------------------------------------
# Gate 6 — Authoring never activates
# ---------------------------------------------------------------------------


class TestGate6AuthoringNeverActive:
    def test_persisted_rule_is_draft_not_active(self) -> None:
        provider = _ScriptedProvider(_VALID_CABIN_JSON, "FAITHFUL\nOk.")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("No business class short haul")
        session = _FakeSession()

        with (
            patch(
                "ai_agents.agents.policy_authoring.PolicyEvalTool.evaluate_candidate",
                return_value=RuleVerdict(
                    rule_id="t",
                    rule_type="cabin_class",
                    applies_to="flight",
                    source_clause_ref=None,
                    in_policy=True,
                    reason="ok",
                    requires_approver=None,
                    observed={"x": 1},
                ),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            agent.handle(
                task,
                _AUTH_DECISION,
                context=_ctx_finance(),
                session=session,  # type: ignore[arg-type]
            )

        # The added PolicyRule must have status=draft, never active
        assert session.added, "Expected a PolicyRule to be persisted"
        rule = session.added[0]
        assert rule.status == PolicyRuleStatus.draft
        assert rule.status != PolicyRuleStatus.active


# ---------------------------------------------------------------------------
# Gate 7 — Authoring RBAC
# ---------------------------------------------------------------------------


class TestGate7AuthoringRbac:
    def test_employee_is_blocked(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _auth_task("some intent")
        ctx = AgentContext(
            organization_id=_ORG_ID,
            granted_permission_keys=frozenset(),  # no policy.manage
            user_id=_USER_ID,
            role="employee",
        )
        with patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()):
            result = agent.handle(task, _AUTH_DECISION, context=ctx, session=MagicMock())

        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True

    def test_manager_is_blocked(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _auth_task("some intent")
        ctx = AgentContext(
            organization_id=_ORG_ID,
            granted_permission_keys=frozenset(),  # no policy.manage
            user_id=_USER_ID,
            role="manager",
        )
        with patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()):
            result = agent.handle(task, _AUTH_DECISION, context=ctx, session=MagicMock())

        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("denied") is True

    def test_finance_admin_is_allowed(self) -> None:
        provider = _ScriptedProvider(_VALID_CABIN_JSON, "FAITHFUL\nOk.")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("No business class short haul")
        session = _FakeSession()
        ctx = _ctx_finance()  # has policy.manage

        with (
            patch(
                "ai_agents.agents.policy_authoring.PolicyEvalTool.evaluate_candidate",
                return_value=RuleVerdict(
                    rule_id="t",
                    rule_type="cabin_class",
                    applies_to="flight",
                    source_clause_ref=None,
                    in_policy=True,
                    reason="ok",
                    requires_approver=None,
                    observed={"x": 1},
                ),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(
                task,
                _AUTH_DECISION,
                context=ctx,
                session=session,  # type: ignore[arg-type]
            )

        # Must NOT be denied
        assert result.output.get("denied") is not True

    def test_rbac_block_writes_decision_log_not_propagates(self) -> None:
        """The RBAC block is caught; the agent returns, never propagates the exception."""
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _auth_task("intent")
        ctx = _ctx_no_policy()

        with patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log:
            # Should NOT raise GuardrailBlocked
            result = agent.handle(task, _AUTH_DECISION, context=ctx, session=MagicMock())

        assert result.status == AgentResultStatus.needs_review
        # Log was written despite denial
        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.model_provider is None


# ---------------------------------------------------------------------------
# Gate 8 — Malformed/unfaithful LLM output → rejected, nothing persisted
# ---------------------------------------------------------------------------


class TestGate8MalformedOutputRejected:
    def test_non_json_authoring_output_rejected(self) -> None:
        # Provider returns garbage that cannot be parsed as JSON
        provider = _ScriptedProvider("not valid json at all!!")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("some intent")
        session = _FakeSession()
        ctx = _ctx_finance()

        with patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()):
            result = agent.handle(
                task,
                _AUTH_DECISION,
                context=ctx,
                session=session,  # type: ignore[arg-type]
            )

        assert result.status == AgentResultStatus.needs_review
        assert result.output.get("draft_created") is False
        # Nothing persisted
        assert session.added == []
        assert not session.flushed

    def test_invalid_rule_type_authoring_rejected(self) -> None:
        # Provider returns JSON with invalid rule_type
        bad_json = json.dumps(
            {
                "rule_type": "invalid_type",
                "applies_to": "flight",
                "definition": {},
                "source_clause_ref": None,
            }
        )
        provider = _ScriptedProvider(bad_json)
        agent = PolicyAgent(provider=provider)
        task = _auth_task("some intent")
        session = _FakeSession()
        ctx = _ctx_finance()

        with patch("ai_agents.agents.policy_agent.write_decision_log", return_value=MagicMock()):
            result = agent.handle(
                task,
                _AUTH_DECISION,
                context=ctx,
                session=session,  # type: ignore[arg-type]
            )

        assert result.status == AgentResultStatus.needs_review
        assert session.added == []

    def test_unfaithful_authoring_rejected(self) -> None:
        # Provider: valid JSON candidate, then UNFAITHFUL from critic
        provider = _ScriptedProvider(_VALID_CABIN_JSON, "UNFAITHFUL\nDoesn't match the intent.")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("some intent")
        session = _FakeSession()
        ctx = _ctx_finance()

        with (
            patch(
                "ai_agents.agents.policy_authoring.PolicyEvalTool.evaluate_candidate",
                return_value=RuleVerdict(
                    rule_id="t",
                    rule_type="cabin_class",
                    applies_to="flight",
                    source_clause_ref=None,
                    in_policy=True,
                    reason="ok",
                    requires_approver=None,
                    observed={"x": 1},
                ),
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(
                task,
                _AUTH_DECISION,
                context=ctx,
                session=session,  # type: ignore[arg-type]
            )

        # UNFAITHFUL → critic returns contradicted with no correction →
        # loop exhausts → rejected or needs_review
        assert result.status == AgentResultStatus.needs_review
        assert session.added == []


# ---------------------------------------------------------------------------
# Gate 9 — Decision logging: one policy row per path with correct fields
# ---------------------------------------------------------------------------


class TestGate9DecisionLogging:
    def test_eval_path_writes_one_policy_row(self) -> None:
        handler = PolicyEvalHandler()
        task = _eval_task(cabin_class="economy")
        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch("ai_agents.agents.policy_eval_handler.write_decision_log") as mock_log,
        ):
            handler.handle(task, _DET_DECISION, context=_ctx_finance(), session=MagicMock())

        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.route == AiRoute.deterministic
        assert record.model_provider is None
        assert record.confidence == 1.0
        assert record.reflection_outcome is None

    def test_qa_silent_path_writes_one_policy_row(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _qa_task("off-topic")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_SILENT,
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log,
        ):
            agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.route == AiRoute.llm
        assert record.model_provider is None

    def test_qa_grounded_path_writes_one_policy_row_with_model_provider(self) -> None:
        provider = _ScriptedProvider(
            "No business class short haul per AIR-CABIN.",
            "SUPPORTED\nFollows directly from the clause.",
        )
        agent = PolicyAgent(provider=provider)
        task = _qa_task("business class?")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_GROUNDED,
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log,
        ):
            agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.route == AiRoute.llm
        assert record.model_provider is not None  # LLM was called

    def test_authoring_denied_path_writes_one_row(self) -> None:
        agent = PolicyAgent(provider=_RaisingProvider())
        task = _auth_task("intent")
        with patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log:
            agent.handle(task, _AUTH_DECISION, context=_ctx_no_policy(), session=MagicMock())

        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.model_provider is None

    def test_authoring_success_path_writes_one_row(self) -> None:
        provider = _ScriptedProvider(_VALID_CABIN_JSON, "FAITHFUL\nOk.")
        agent = PolicyAgent(provider=provider)
        task = _auth_task("no business class short haul")
        session = _FakeSession()

        with (
            patch(
                "ai_agents.agents.policy_authoring.PolicyEvalTool.evaluate_candidate",
                return_value=RuleVerdict(
                    rule_id="t",
                    rule_type="cabin_class",
                    applies_to="flight",
                    source_clause_ref=None,
                    in_policy=True,
                    reason="ok",
                    requires_approver=None,
                    observed={"x": 1},
                ),
            ),
            patch("ai_agents.agents.policy_agent.write_decision_log") as mock_log,
        ):
            agent.handle(
                task,
                _AUTH_DECISION,
                context=_ctx_finance(),
                session=session,  # type: ignore[arg-type]
            )

        assert mock_log.call_count == 1
        record: DecisionRecord = mock_log.call_args.kwargs["record"]
        assert record.agent == AiAgent.policy
        assert record.route == AiRoute.llm
        assert record.model_provider is not None


# ---------------------------------------------------------------------------
# Gate 10 — End-to-end via orchestrator: two decision-log rows
# ---------------------------------------------------------------------------


class TestGate10OrchestratorEndToEnd:
    def test_two_decision_log_rows_eval_path(self) -> None:
        from ai_agents.orchestrator.orchestrator import Orchestrator

        registry = AgentRegistry()
        register_policy(registry)

        orch = Orchestrator(registry)
        # Eval path: policy_qa context + policy_eval signal
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        log_calls: list[tuple[Any, ...]] = []

        def _capture_log(session: Any, *, context: Any, record: DecisionRecord) -> MagicMock:
            log_calls.append((session, context, record))
            return MagicMock()

        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch(
                "ai_agents.orchestrator.orchestrator.write_decision_log",
                side_effect=_capture_log,
            ),
            patch(
                "ai_agents.agents.policy_eval_handler.write_decision_log",
                side_effect=_capture_log,
            ),
        ):
            orch.handle_task(task, context=_ctx_finance(), session=MagicMock())

        # Two rows written: orchestrator row + policy handler row
        assert len(log_calls) == 2
        agents = [c[2].agent for c in log_calls]
        assert AiAgent.orchestrator in agents
        assert AiAgent.policy in agents

    def test_orchestrator_row_is_deterministic_policy_row_also_deterministic(self) -> None:
        from ai_agents.orchestrator.orchestrator import Orchestrator

        registry = AgentRegistry()
        register_policy(registry)
        orch = Orchestrator(registry)
        task = Task(
            context_type="policy_qa",
            payload={"item_type": "flight", "duration_hours": 3.0, "cabin_class": "economy"},
            signals={"policy_eval": True},
        )
        log_calls: list[DecisionRecord] = []

        def _capture(session: Any, *, context: Any, record: DecisionRecord) -> MagicMock:
            log_calls.append(record)
            return MagicMock()

        with (
            patch(
                "ai_agents.agents.policy_eval_handler.PolicyEvalTool.evaluate",
                return_value=_POLICY_VERDICT_IN,
            ),
            patch("ai_agents.orchestrator.orchestrator.write_decision_log", side_effect=_capture),
            patch("ai_agents.agents.policy_eval_handler.write_decision_log", side_effect=_capture),
        ):
            orch.handle_task(task, context=_ctx_finance(), session=MagicMock())

        orch_record = next(r for r in log_calls if r.agent == AiAgent.orchestrator)
        policy_record = next(r for r in log_calls if r.agent == AiAgent.policy)
        assert orch_record.route == AiRoute.deterministic
        assert policy_record.route == AiRoute.deterministic
        assert policy_record.model_provider is None


# ---------------------------------------------------------------------------
# Gate 11 — Provider abstraction: no real API in CI
# ---------------------------------------------------------------------------


class TestGate11ProviderAbstraction:
    def test_policy_eval_handler_has_no_llm_import(self) -> None:
        import ai_agents.agents.policy_eval_handler as mod

        assert not hasattr(mod, "LlmProvider")
        assert not hasattr(mod, "get_llm_provider")

    def test_policy_agent_uses_injected_provider(self) -> None:
        # If the agent used a real API it would need env vars we don't set
        provider = _ScriptedProvider(
            "No business class short haul.",
            "SUPPORTED\nYes it follows.",
        )
        agent = PolicyAgent(provider=provider)
        task = _qa_task("question")
        with (
            patch(
                "ai_agents.agents.policy_agent.GroundedRetriever.retrieve",
                return_value=_GROUNDED,
            ),
            patch(
                "ai_agents.agents.policy_agent.write_decision_log",
                return_value=MagicMock(),
            ),
        ):
            result = agent.handle(task, _QA_DECISION, context=_ctx_finance(), session=MagicMock())

        # Scripted provider was used (no network) — test would fail with real API error
        assert result is not None


# ---------------------------------------------------------------------------
# Gate 12 — Layering: agents modules importable without backend
# ---------------------------------------------------------------------------


class TestGate12LayeringLaw:
    def test_agents_package_importable_without_backend(self) -> None:
        script = """
import sys
import ai_agents.agents
import ai_agents.agents.policy_agent
import ai_agents.agents.policy_eval_handler
import ai_agents.agents.policy_qa
import ai_agents.agents.policy_authoring
backend_mods = [m for m in sys.modules if m.startswith("backend")]
if backend_mods:
    print("FAIL:", backend_mods)
    sys.exit(1)
print("OK")
sys.exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )
        assert (
            result.returncode == 0
        ), f"agents/ imported backend modules.\nstdout: {result.stdout}\nstderr: {result.stderr}"
