"""Deterministic intent classifier (P4-T7).

Classifies a Task into a (target_agent, task_type, confidence, requires_reasoning)
tuple using rule-based heuristics on context_type and signal keys.  No LLM call.

Classification rules (evaluated top-to-bottom; first match wins):
  1. signals['approval_required'] is truthy → AiAgent.approval
  2. context_type == 'travel_planning'      → AiAgent.travel
  3. context_type == 'expense'              → AiAgent.expense
  4. context_type == 'policy_qa'            → AiAgent.policy
  5. context_type == 'general' / None / any → default (AiAgent.policy, low confidence)

Confidence values are deterministic per rule tier:
  HIGH  = 0.95  — unambiguous context_type or explicit approval signal
  MED   = 0.80  — clear context but reasoning path may vary
  LOW   = 0.40  — 'general' / unknown; human routing preferred

requires_reasoning:
  Reflects whether the task semantically requires agent LLM reasoning.
  - approval: True (approval decisions require judgment)
  - travel planning: True (trip planning requires reasoning over options)
  - expense categorize_simple: False; other expense tasks: True
  - policy grounded_lookup: True (Q&A requires LLM synthesis); policy_evaluate: False
    (deterministic rule probe); policy authoring: True
  - general / default: False

Seam for future LLM-assisted classifier:
  IntentClassifier.classify() is the only public entry point.  A future
  LlmClassifier can be registered as a strategy via the constructor without
  changing the orchestrator's call site.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

from ai_agents.orchestrator.envelopes import Classification, Task
from database.models.ai_decision_log import AiAgent

# ---- Confidence tiers (deterministic, not tunable at runtime) ---------------
_CONF_HIGH: float = 0.95  # unambiguous match
_CONF_MED: float = 0.80  # clear but multi-path
_CONF_LOW: float = 0.40  # general / unknown; low certainty

# ---- Canonical task types (strings stored in ai_decision_logs.task_type) ----
_TT_APPROVAL = "approval_decision"
_TT_TRIP_PLAN = "trip_planning"
_TT_EXPENSE_SIMPLE = "expense_categorize_simple"
_TT_EXPENSE_REASONING = "expense_reasoning"
_TT_POLICY_LOOKUP = "policy_grounded_lookup"
_TT_POLICY_AUTHORING = "policy_authoring"
_TT_POLICY_EVALUATE = "policy_evaluate"
_TT_GENERAL = "general_query"

# ---- Context type string constants (mirrors ConversationContextType values) --
_CTX_TRAVEL = "travel_planning"
_CTX_EXPENSE = "expense"
_CTX_POLICY = "policy_qa"
_CTX_GENERAL = "general"


def _classify_approval(task: Task) -> Classification:
    return Classification(
        target_agent=AiAgent.approval,
        task_type=_TT_APPROVAL,
        confidence=_CONF_HIGH,
        requires_reasoning=True,
    )


def _classify_travel(task: Task) -> Classification:
    return Classification(
        target_agent=AiAgent.travel,
        task_type=_TT_TRIP_PLAN,
        confidence=_CONF_HIGH,
        requires_reasoning=True,
    )


def _classify_expense(task: Task) -> Classification:
    # 'simple' expense tasks (categorize, lookup) → deterministic path;
    # everything else → llm.
    is_simple = bool(task.signals.get("expense_simple"))
    return Classification(
        target_agent=AiAgent.expense,
        task_type=_TT_EXPENSE_SIMPLE if is_simple else _TT_EXPENSE_REASONING,
        confidence=_CONF_HIGH,
        requires_reasoning=not is_simple,
    )


def _classify_policy(task: Task) -> Classification:
    is_authoring = bool(task.signals.get("policy_authoring"))
    is_eval = bool(task.signals.get("policy_eval"))
    if is_authoring:
        return Classification(
            target_agent=AiAgent.policy,
            task_type=_TT_POLICY_AUTHORING,
            confidence=_CONF_HIGH,
            requires_reasoning=True,
        )
    if is_eval:
        return Classification(
            target_agent=AiAgent.policy,
            task_type=_TT_POLICY_EVALUATE,
            confidence=_CONF_HIGH,
            requires_reasoning=False,  # deterministic
        )
    # Default policy_qa: full Q&A with LLM synthesis (requires reasoning)
    return Classification(
        target_agent=AiAgent.policy,
        task_type=_TT_POLICY_LOOKUP,
        confidence=_CONF_HIGH,
        requires_reasoning=True,  # FLIPPED from False — Q&A needs LLM synthesis
    )


def _classify_general(task: Task) -> Classification:
    # Fall-through: unknown or 'general' context.  Route to policy as the
    # most likely Q&A target at low confidence; orchestrator will pick
    # llm path (low confidence falls below the threshold).
    return Classification(
        target_agent=AiAgent.policy,
        task_type=_TT_GENERAL,
        confidence=_CONF_LOW,
        requires_reasoning=False,
    )


class IntentClassifier:
    """Deterministic rule-based intent classifier.

    classify() is the sole public method.  No state is mutated; instances are
    safe to reuse across calls.

    A future LLM-assisted classifier strategy can be injected via __init__
    without changing the Orchestrator call site.
    """

    def classify(self, task: Task) -> Classification:
        """Classify task into (target_agent, task_type, confidence, requires_reasoning).

        Rules are evaluated top-to-bottom; first match wins.
        Never raises; falls back to _classify_general for unknown context types.
        """
        # Rule 1: explicit approval signal overrides context_type
        if task.signals.get("approval_required"):
            return _classify_approval(task)

        ctx = (task.context_type or "").lower()

        if ctx == _CTX_TRAVEL:
            return _classify_travel(task)
        if ctx == _CTX_EXPENSE:
            return _classify_expense(task)
        if ctx == _CTX_POLICY:
            return _classify_policy(task)

        # Rule 5: general / None / unknown
        return _classify_general(task)
