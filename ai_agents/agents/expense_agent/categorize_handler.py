"""ExpenseCategorizeHandler — deterministic-first simple categorization (P8 C1, FR-X3).

DeterministicHandler registered under task_type='expense_categorize_simple'.

Flow:
  1. categorize_by_rules() over payload signals (expense_type, mcc, merchant/vendor,
     description/question).
  2. confidence ≥ threshold (default 0.80) → AgentResult ok, route=deterministic,
     zero provider calls.
  3. confidence < threshold → exactly one LLM call asking for a JSON category.
     Valid enum answer → ok, route=llm, model_provider set.
  4. ProviderError / no provider / unparseable or out-of-enum answer →
     needs_review with the rule's best-guess category, degraded=True (NFR-R1).

Writes exactly one ai_decision_logs row (agent=expense) carrying the route that
actually ran; the orchestrator writes its own row separately.  Never commits.

PII: input_summary carries booleans only — no merchant text, no description.
The LLM prompt carries merchant/description/amount only; never IDs or images
(NFR-SEC4).

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.persistence import append_assistant_message
from ai_agents.agents.expense_agent.rule_categorizer import (
    CATEGORIZE_CONFIDENCE_THRESHOLD,
    categorize_by_rules,
    is_valid_category,
    parse_mcc,
)
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.interface import LlmMessage, LlmProvider

logger = logging.getLogger(__name__)

EXPENSE_CATEGORIZE_TASK_TYPE = "expense_categorize_simple"

LLM_CATEGORY_CONFIDENCE: float = 0.60
"""Confidence assigned to a valid LLM category (gap-fill convention)."""

_MAX_PROMPT_FIELD_CHARS = 200

_CATEGORIZE_PROMPT = """\
You categorize a single corporate expense. Allowed categories: \
airfare, lodging, meals, ground_transport, other.

Merchant: {merchant}
Description: {description}
Amount: {amount}

Reply with only a JSON object of the form {{"category": "<one allowed category>"}}."""


def _opt_str(payload: Mapping[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string value among ``keys``."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _clip(text: str | None) -> str:
    if not text:
        return "(none)"
    return text.strip()[:_MAX_PROMPT_FIELD_CHARS]


def _parse_category(text: str) -> str | None:
    """Parse ``{"category": ...}`` from the LLM reply; None if invalid."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(ln for ln in lines if not ln.strip().startswith("```")).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    category = parsed.get("category")
    if isinstance(category, str):
        category = category.strip().lower()
    return category if is_valid_category(category) else None


def _llm_categorize(
    provider: LlmProvider, merchant: str | None, description: str | None, amount: object
) -> str | None:
    """One LLM call; returns a valid category or None (error / unparseable).

    This is the only LLM touchpoint of ExpenseCategorizeHandler, reached only when
    rule confidence is below the handler threshold.
    """
    amount_text = (
        f"{amount:.2f}"
        if isinstance(amount, int | float) and not isinstance(amount, bool)
        else "(none)"
    )
    prompt = _CATEGORIZE_PROMPT.format(
        merchant=_clip(merchant), description=_clip(description), amount=amount_text
    )
    try:
        response = provider.complete([LlmMessage(role="user", content=prompt)])
    except ProviderError as exc:
        logger.warning(
            "ExpenseCategorizeHandler: ProviderError(%s); rule fallback (NFR-R1).", exc.code
        )
        return None
    category = _parse_category(response.text or "")
    if category is None:
        logger.warning("ExpenseCategorizeHandler: unparseable LLM category; rule fallback.")
    return category


class ExpenseCategorizeHandler:
    """DeterministicHandler for simple expense categorization (deterministic-first).

    Answers from rules when confident; escalates internally to one LLM call
    otherwise, and logs its own route (see DeterministicHandler docstring).
    """

    def __init__(
        self,
        provider: LlmProvider | None = None,
        *,
        threshold: float = CATEGORIZE_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._provider = provider
        self._threshold = threshold

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        t0 = time.monotonic()
        payload: Mapping[str, Any] = task.payload or {}

        merchant = _opt_str(payload, "merchant", "vendor")
        description = _opt_str(payload, "description", "question")
        expense_type = _opt_str(payload, "expense_type")
        raw_mcc = payload.get("mcc")
        # Normalise up front so has_mcc reflects an MCC the rules actually used.
        mcc = parse_mcc(raw_mcc) if isinstance(raw_mcc, str | int) else None
        amount = payload.get("amount")

        rule = categorize_by_rules(
            merchant=merchant, description=description, expense_type=expense_type, mcc=mcc
        )

        route = AiRoute.deterministic
        model_provider: str | None = None
        llm_attempted = False
        degraded = False
        status = AgentResultStatus.ok
        category = rule.category
        confidence = rule.confidence

        if rule.confidence < self._threshold:
            route = AiRoute.llm
            llm_category: str | None = None
            if self._provider is None:
                logger.warning("ExpenseCategorizeHandler: no LLM provider; rule fallback (NFR-R1).")
            else:
                llm_attempted = True
                llm_category = _llm_categorize(self._provider, merchant, description, amount)
            if llm_category is not None:
                category = llm_category
                confidence = LLM_CATEGORY_CONFIDENCE
                model_provider = (
                    getattr(self._provider, "model", None) or type(self._provider).__name__
                )
            else:
                degraded = True
                status = AgentResultStatus.needs_review

        needs_review = status == AgentResultStatus.needs_review
        output: dict[str, Any] = {
            "category": category,
            "confidence": round(confidence, 3),
            "route": route.value,
            "signal": rule.signal,
            "needs_review": needs_review,
            "degraded": degraded,
        }

        conversation_id = payload.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            self._append_message(conversation_id, category, needs_review, session)

        latency_ms = int((time.monotonic() - t0) * 1000)
        record = DecisionRecord(
            agent=AiAgent.expense,
            task_type=decision.task_type,
            route=route,
            model_provider=model_provider,
            input_summary={
                "has_merchant": merchant is not None,
                "has_description": description is not None,
                "has_mcc": mcc is not None,
                "has_expense_type": expense_type is not None,
            },
            tools_used={"RuleCategorizer": {"signal": rule.signal, "matched": rule.matched}},
            output_summary={
                "category": category,
                "rule_confidence": round(rule.confidence, 3),
                "threshold": self._threshold,
                "degraded": degraded,
                "llm_attempted": llm_attempted,
            },
            confidence=confidence,
            reflection_outcome=None,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        return AgentResult(
            status=status,
            output=output,
            confidence=confidence,
            reflection_outcome=None,
        )

    @staticmethod
    def _append_message(
        conversation_id: str, category: str, needs_review: bool, session: Session
    ) -> None:
        content = f"Suggested category: {category}."
        if needs_review:
            content += " Low confidence — please review before submitting."
        try:
            # SAVEPOINT: a failed flush rolls back only this insert, so the
            # caller's session stays usable for write_decision_log.
            with session.begin_nested():
                append_assistant_message(
                    conversation_id=conversation_id,
                    content=content,
                    tool_calls_trace={"tools_used": {"RuleCategorizer": True}},
                    citations=(),
                    session=session,
                )
        except Exception as exc:
            logger.warning("ExpenseCategorizeHandler: append_assistant_message failed: %s", exc)


__all__ = [
    "EXPENSE_CATEGORIZE_TASK_TYPE",
    "LLM_CATEGORY_CONFIDENCE",
    "ExpenseCategorizeHandler",
]
