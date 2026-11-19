"""AI module service layer — conversation business logic (P4-T13 Slice 1).

All DB interactions for conversations live here. Endpoints delegate here;
no SQLAlchemy in router.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.policy_agent import presentable_policy_output
from backend.app.core.errors import NotFoundError, UnprocessableEntityError
from backend.app.core.rbac import scope_query
from database.models.conversation import Conversation, ConversationContextType
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.trip import Trip

if TYPE_CHECKING:
    from ai_agents.guardrails.citations import Citation

# C7-09: conversation context types whose agent (PolicyAgent) does NOT persist its own reply,
# so the router writes the assistant turn.  travel_planning / expense agents persist theirs.
ROUTER_PERSISTED_CONTEXTS: frozenset[str] = frozenset(
    {ConversationContextType.policy_qa.value, ConversationContextType.general.value}
)

# Shown (and stored) when a needs_review answer has no critic-supported cited draft.
NO_CONFIDENT_ANSWER_TEXT = "No confident answer — please check the policy or ask finance."


def create_conversation(
    session: Session,
    *,
    user: User,
    context_type: ConversationContextType,
    context_ref_id: uuid.UUID | None,
) -> Conversation:
    """Create and flush a new Conversation row.

    The caller owns the commit.
    """
    conv = Conversation(
        organization_id=user.organization_id,
        user_id=user.id,
        context_type=context_type,
        context_ref_id=context_ref_id,
    )
    session.add(conv)
    session.flush()
    return conv


def validate_context_ref_id(
    session: Session,
    *,
    context_type: ConversationContextType,
    context_ref_id: uuid.UUID,
    user: User,
) -> None:
    """Verify that context_ref_id is visible to the calling user.

    Raises UnprocessableEntityError if context_type is "general" (ref is meaningless).
    Raises UnprocessableEntityError if context_type is "policy_qa" (entity-scoping
    deferred; rejected by default).
    Raises NotFoundError if the referenced entity does not exist or is outside the
    caller's record-level scope (prevents cross-owner reference and existence leaking).
    """
    if context_type == ConversationContextType.general:
        raise UnprocessableEntityError(
            "context_ref_id is not allowed when context_type is 'general'."
        )

    if context_type == ConversationContextType.travel_planning:
        trip_stmt = sa.select(Trip).where(Trip.id == context_ref_id)
        trip_stmt = scope_query(
            trip_stmt,
            user=user,
            ownership_column=Trip.traveler_id,
            org_column=Trip.organization_id,
            session=session,
        )
        if session.scalars(trip_stmt).first() is None:
            raise NotFoundError("Trip not found.")
    elif context_type == ConversationContextType.expense:
        report_stmt = sa.select(ExpenseReport).where(ExpenseReport.id == context_ref_id)
        report_stmt = scope_query(
            report_stmt,
            user=user,
            ownership_column=ExpenseReport.owner_id,
            org_column=ExpenseReport.organization_id,
            session=session,
        )
        if session.scalars(report_stmt).first() is None:
            raise NotFoundError("Expense report not found.")
    else:
        # policy_qa entity-scoping deferred to T3; other context types rejected by default
        raise UnprocessableEntityError(
            "Entity-scoped conversations are not yet supported for this context type."
        )


def get_conversation_for_user(
    session: Session,
    *,
    conversation_id: uuid.UUID,
    user: User,
    for_update: bool = False,
) -> Conversation:
    """Load a Conversation and verify ownership.

    Raises NotFoundError if the conversation does not exist or does not belong
    to the requesting user (prevents existence leaking via 403 vs 404).

    ``for_update`` locks the row for the caller's transaction: send_message holds
    it so two concurrent messages in one conversation cannot take the same
    sequence number or race a planner selection (UAT-2 Issue 4).
    """
    stmt = sa.select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user.id,
        Conversation.organization_id == user.organization_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    conv = session.scalars(stmt).first()

    if conv is None:
        raise NotFoundError("Conversation not found.")

    if conv.user_id != user.id or conv.organization_id != user.organization_id:
        raise NotFoundError("Conversation not found.")

    return conv


def get_next_sequence(session: Session, *, conversation_id: uuid.UUID) -> int:
    """Return the next sequence number for a new message in this conversation.

    Returns 1 if no messages exist yet, or max(sequence) + 1 otherwise.
    """
    result = session.execute(
        sa.select(sa.func.max(ConversationMessage.sequence)).where(
            ConversationMessage.conversation_id == conversation_id
        )
    ).scalar()
    return 1 if result is None else result + 1


def derive_task_signals(context_type: str, payload: Mapping[str, Any]) -> dict[str, bool]:
    """Derive orchestrator classification signals from an explicit caller intent.

    Returns ``{"expense_simple": True}`` only when the conversation is an expense
    conversation AND the caller sent ``payload["intent"] == "categorize"`` — this
    routes the turn to the deterministic ``expense_categorize_simple`` handler (P8 C1).
    No heuristics over the message text: without the explicit intent the signals are
    empty and expense turns keep the ``expense_reasoning`` (ExpenseAgent) route.
    """
    if (
        context_type == ConversationContextType.expense.value
        and payload.get("intent") == "categorize"
    ):
        return {"expense_simple": True}
    return {}


def _policy_reply_text(output: Mapping[str, Any]) -> str:
    """The human-facing text of a PolicyAgent result (answer, or the abstention message)."""
    answer = output.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer
    if output.get("answer_type") == "silent":
        parts: list[str] = []
        for key in ("rationale", "near_topic"):
            val = output.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        escalation = output.get("escalation")
        if isinstance(escalation, Mapping):
            msg = escalation.get("message")
            if isinstance(msg, str) and msg.strip():
                parts.append(msg.strip())
        if parts:
            return " ".join(parts)
    return NO_CONFIDENT_ANSWER_TEXT


def append_policy_reply(
    session: Session,
    *,
    conversation_id: uuid.UUID,
    sequence: int,
    output: Mapping[str, Any],
    citations: tuple[Citation, ...] | None,
) -> ConversationMessage:
    """Persist the assistant turn for a policy_qa / general conversation (C7-09). Flush only.

    ``content`` is the answer text (grounded or provisional needs_review draft) or the
    abstention text; the ``citations`` JSONB carries the citation list plus the answer
    metadata (answer_type / needs_review / confidence / confidence_source) so a reloaded
    conversation can render the same trust surface without re-running the agent.
    """
    # A withheld needs_review draft keeps no citations / answer_structured on the persisted
    # turn, so a reload never shows sources under "No confident answer".
    output, citations = presentable_policy_output(output, citations)
    answer_type = output.get("answer_type")
    meta: dict[str, Any] = {
        "citations": [c.to_dict() for c in citations] if citations else [],
        "answer_type": answer_type,
        "needs_review": bool(output.get("needs_review")) or answer_type == "needs_review",
        "confidence": output.get("confidence"),
        "confidence_source": output.get("confidence_source"),
    }
    structured = output.get("answer_structured")
    if isinstance(structured, Mapping):
        meta["answer_structured"] = dict(structured)
    msg = ConversationMessage(
        conversation_id=conversation_id,
        role=MessageRole.assistant,
        content=_policy_reply_text(output),
        citations=meta,
        sequence=sequence,
    )
    session.add(msg)
    session.flush()
    return msg
