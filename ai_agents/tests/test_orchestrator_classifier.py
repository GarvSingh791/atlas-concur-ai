"""Hermetic tests for the deterministic IntentClassifier (P4-T7/T8).

No database, no LLM, no network.  All tests run in hermetic CI.

Key invariants verified:
  - travel_planning → AiAgent.travel, high confidence, requires_reasoning=True
  - expense (simple) → AiAgent.expense, deterministic path (requires_reasoning=False)
  - expense (reasoning) → AiAgent.expense, llm path (requires_reasoning=True)
  - policy_qa (lookup) → AiAgent.policy, requires_reasoning=True (LLM synthesis)
  - policy_qa (authoring) → AiAgent.policy, requires_reasoning=True
  - policy_qa (eval) → AiAgent.policy, policy_evaluate, requires_reasoning=False (deterministic)
  - general / None / unknown → AiAgent.policy, low confidence, requires_reasoning=False
  - approval_required signal overrides context_type → AiAgent.approval
  - Determinism: same Task → same Classification (no randomness)
  - Confidence values in [0, 1]
"""

from __future__ import annotations

import pytest

from ai_agents.orchestrator.classifier import (
    _CONF_HIGH,
    _CONF_LOW,
    IntentClassifier,
)
from ai_agents.orchestrator.envelopes import Task
from database.models.ai_decision_log import AiAgent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLASSIFIER = IntentClassifier()


def _task(
    context_type: str | None,
    signals: dict | None = None,
) -> Task:
    return Task(
        context_type=context_type,
        payload={"question": "test"},
        signals=signals or {},
    )


# ---------------------------------------------------------------------------
# Travel
# ---------------------------------------------------------------------------


class TestTravelClassification:
    def test_travel_planning_routes_to_travel_agent(self) -> None:
        cls = _CLASSIFIER.classify(_task("travel_planning"))
        assert cls.target_agent == AiAgent.travel

    def test_travel_planning_high_confidence(self) -> None:
        cls = _CLASSIFIER.classify(_task("travel_planning"))
        assert cls.confidence == _CONF_HIGH

    def test_travel_planning_requires_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("travel_planning"))
        assert cls.requires_reasoning is True

    def test_travel_task_type_canonical(self) -> None:
        cls = _CLASSIFIER.classify(_task("travel_planning"))
        assert cls.task_type == "trip_planning"


# ---------------------------------------------------------------------------
# Expense
# ---------------------------------------------------------------------------


class TestExpenseClassification:
    def test_expense_routes_to_expense_agent(self) -> None:
        cls = _CLASSIFIER.classify(_task("expense"))
        assert cls.target_agent == AiAgent.expense

    def test_expense_high_confidence(self) -> None:
        cls = _CLASSIFIER.classify(_task("expense"))
        assert cls.confidence == _CONF_HIGH

    def test_expense_simple_no_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("expense", signals={"expense_simple": True}))
        assert cls.requires_reasoning is False
        assert cls.task_type == "expense_categorize_simple"

    def test_expense_reasoning_path(self) -> None:
        cls = _CLASSIFIER.classify(_task("expense"))
        assert cls.requires_reasoning is True
        assert cls.task_type == "expense_reasoning"

    def test_expense_simple_false_flag_still_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("expense", signals={"expense_simple": False}))
        assert cls.requires_reasoning is True


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestPolicyClassification:
    def test_policy_qa_routes_to_policy_agent(self) -> None:
        cls = _CLASSIFIER.classify(_task("policy_qa"))
        assert cls.target_agent == AiAgent.policy

    def test_policy_qa_high_confidence(self) -> None:
        cls = _CLASSIFIER.classify(_task("policy_qa"))
        assert cls.confidence == _CONF_HIGH

    def test_policy_lookup_requires_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("policy_qa"))
        assert cls.requires_reasoning is True
        assert cls.task_type == "policy_grounded_lookup"

    def test_policy_authoring_requires_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("policy_qa", signals={"policy_authoring": True}))
        assert cls.requires_reasoning is True
        assert cls.task_type == "policy_authoring"

    def test_policy_eval_deterministic(self) -> None:
        cls = _CLASSIFIER.classify(_task("policy_qa", signals={"policy_eval": True}))
        assert cls.target_agent == AiAgent.policy
        assert cls.task_type == "policy_evaluate"
        assert cls.requires_reasoning is False
        assert cls.confidence == _CONF_HIGH


# ---------------------------------------------------------------------------
# Approval signal override
# ---------------------------------------------------------------------------


class TestApprovalSignalOverride:
    @pytest.mark.parametrize(
        "context_type",
        ["travel_planning", "expense", "policy_qa", "general", None],
    )
    def test_approval_signal_overrides_context_type(self, context_type: str | None) -> None:
        task = _task(context_type, signals={"approval_required": True})
        cls = _CLASSIFIER.classify(task)
        assert cls.target_agent == AiAgent.approval
        assert cls.task_type == "approval_decision"

    def test_approval_high_confidence(self) -> None:
        cls = _CLASSIFIER.classify(_task("general", signals={"approval_required": True}))
        assert cls.confidence == _CONF_HIGH

    def test_approval_requires_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("general", signals={"approval_required": True}))
        assert cls.requires_reasoning is True


# ---------------------------------------------------------------------------
# General / default / unknown
# ---------------------------------------------------------------------------


class TestGeneralAndDefaultClassification:
    @pytest.mark.parametrize("context_type", ["general", None, "unknown_ctx", ""])
    def test_general_context_routes_to_policy(self, context_type: str | None) -> None:
        cls = _CLASSIFIER.classify(_task(context_type))
        assert cls.target_agent == AiAgent.policy

    @pytest.mark.parametrize("context_type", ["general", None, "unknown_ctx"])
    def test_general_low_confidence(self, context_type: str | None) -> None:
        cls = _CLASSIFIER.classify(_task(context_type))
        assert cls.confidence == _CONF_LOW

    def test_general_no_reasoning(self) -> None:
        cls = _CLASSIFIER.classify(_task("general"))
        assert cls.requires_reasoning is False

    def test_general_task_type(self) -> None:
        cls = _CLASSIFIER.classify(_task("general"))
        assert cls.task_type == "general_query"

    def test_none_context_does_not_crash(self) -> None:
        cls = _CLASSIFIER.classify(_task(None))
        assert cls.target_agent is not None


# ---------------------------------------------------------------------------
# Determinism + confidence bounds
# ---------------------------------------------------------------------------


class TestDeterminismAndBounds:
    @pytest.mark.parametrize(
        "context_type,signals",
        [
            ("travel_planning", {}),
            ("expense", {"expense_simple": True}),
            ("policy_qa", {}),
            ("general", {}),
            (None, {"approval_required": True}),
        ],
    )
    def test_same_task_produces_same_classification(
        self, context_type: str | None, signals: dict
    ) -> None:
        task = _task(context_type, signals=signals)
        c1 = _CLASSIFIER.classify(task)
        c2 = _CLASSIFIER.classify(task)
        assert c1 == c2

    @pytest.mark.parametrize(
        "context_type",
        ["travel_planning", "expense", "policy_qa", "general", None],
    )
    def test_confidence_in_unit_range(self, context_type: str | None) -> None:
        cls = _CLASSIFIER.classify(_task(context_type))
        assert 0.0 <= cls.confidence <= 1.0
