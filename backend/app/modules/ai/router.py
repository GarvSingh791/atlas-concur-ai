"""AI module router — conversation endpoints (P4-T13 Slice 1).

Mounted at /api/v1/conversations (prefix set in backend/app/api/v1.py).

  POST   /api/v1/conversations
  POST   /api/v1/conversations/{conversation_id}/messages  (SSE streaming)
  GET    /api/v1/conversations/{conversation_id}

Thin router — business logic lives in service.py; SSE framing in streaming.py.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ai_agents.orchestrator.envelopes import Task
from ai_agents.orchestrator.orchestrator import Orchestrator
from backend.app.core.audit import record_audit
from backend.app.core.correlation import get_correlation_id
from backend.app.core.database import get_db
from backend.app.core.idempotency import (
    idempotency_key_header,
    idempotency_lookup,
    idempotency_record,
)
from backend.app.core.security import get_current_user
from backend.app.modules.ai import schemas, service
from backend.app.modules.ai.runtime import build_agent_context, get_orchestrator
from backend.app.modules.ai.streaming import build_sse_events
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.identity import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai"])


# ---------------------------------------------------------------------------
# POST /conversations
# ---------------------------------------------------------------------------


@router.post("", response_model=schemas.ConversationResponse, status_code=201)
def create_conversation(
    body: schemas.ConversationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.ConversationResponse:
    """Create a new AI conversation session.

    Requires a valid access token; no additional role/permission needed.
    """
    # validate context_ref_id is visible to the calling user (record-level scope)
    if body.context_ref_id is not None:
        service.validate_context_ref_id(
            db,
            context_type=body.context_type,
            context_ref_id=body.context_ref_id,
            user=current_user,
        )

    conv = service.create_conversation(
        db,
        user=current_user,
        context_type=body.context_type,
        context_ref_id=body.context_ref_id,
    )
    db.commit()
    return schemas.ConversationResponse.model_validate(conv)


# ---------------------------------------------------------------------------
# POST /conversations/{conversation_id}/messages  (SSE)
# ---------------------------------------------------------------------------


@router.post("/{conversation_id}/messages")
def send_message(
    conversation_id: uuid.UUID,
    body: schemas.MessageCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    idem_key: Annotated[str | None, Depends(idempotency_key_header)],
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
) -> StreamingResponse:
    """Send a message to the AI assistant and receive an SSE stream response.

    The orchestrator is run synchronously; results are committed once, then
    replayed as Server-Sent Events.
    """
    # 1. Load conversation + owner check; the row stays locked until the single commit
    #    below, so concurrent messages in ONE conversation are serialised.
    conv = service.get_conversation_for_user(
        db, conversation_id=conversation_id, user=current_user, for_update=True
    )

    # 2. Idempotency replay — if key already exists, stream the last assistant message
    if idem_key:
        prior_entity_id = idempotency_lookup(
            db,
            organization_id=current_user.organization_id,
            scope="conversation.message",
            key=idem_key,
        )
        if prior_entity_id is not None:
            # Replay: stream a minimal done frame with the prior message id
            prior_seq = service.get_next_sequence(db, conversation_id=conversation_id) - 1
            from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus

            replay_result = AgentResult(status=AgentResultStatus.ok, output={}, citations=())
            events = build_sse_events(
                replay_result,
                task_type="replay",
                route="deterministic",
                confidence=1.0,
                explain_with_llm=False,
                message_id=prior_entity_id,
                sequence=prior_seq,
            )
            return StreamingResponse(iter(events), media_type="text/event-stream")

    # 3. Get next sequence
    next_seq = service.get_next_sequence(db, conversation_id=conversation_id)

    # 4. Append user message (flush only — endpoint owns the single commit)
    user_msg = ConversationMessage(
        conversation_id=conv.id,
        role=MessageRole.user,
        content=body.content,
        sequence=next_seq,
    )
    db.add(user_msg)
    db.flush()

    # 5. Build task payload
    payload: dict = dict(body.payload or {})
    payload["conversation_id"] = str(conversation_id)
    payload.setdefault("question", body.content)
    payload["explain_with_llm"] = body.explain_with_llm
    # FIX-1: thread entity scope into the payload so agents can scope to the entity
    if conv.context_ref_id is not None:
        payload["context_ref_id"] = str(conv.context_ref_id)

    task = Task(
        context_type=conv.context_type.value,
        payload=payload,
        signals=service.derive_task_signals(conv.context_type.value, payload),
        task_type=None,
    )

    # 6. Build AgentContext
    agent_ctx = build_agent_context(current_user, db, correlation_id=get_correlation_id())

    # 7. Run orchestrator (does NOT commit — endpoint commits once below)
    outcome = orchestrator.handle_task(task, context=agent_ctx, session=db)

    # 8. Audit what the agent did to a trip (UAT-2 Issue 4: one planning conversation ->
    #    one draft trip).  created -> trip.created; a leg added / replaced on the
    #    conversation's draft -> trip.updated with before/after; unchanged -> no row.
    #    The internal before/after snapshots are stripped from the streamed frame.
    result_output = dict(outcome.result.output) if outcome.result.output else {}
    trip_change = result_output.get("trip_change")
    audit_snap: dict = {}
    if isinstance(trip_change, dict):
        trip_change = dict(trip_change)
        audit_snap = trip_change.pop("audit", None) or {}
        result_output["trip_change"] = trip_change
        outcome = dataclasses.replace(
            outcome, result=dataclasses.replace(outcome.result, output=result_output)
        )
    if result_output.get("trip_id"):
        _audit_trip_selection(
            db,
            current_user=current_user,
            conversation_id=conv.id,
            trip_id_raw=result_output["trip_id"],
            trip_change=trip_change if isinstance(trip_change, dict) else None,
            audit_snap=audit_snap,
        )

    # 9. Resolve the assistant message the agent appended (travel/expense agents persist their
    #    own reply).  Look for the highest-sequence assistant message after the user message.
    assistant_msg = db.scalars(
        sa.select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conv.id,
            ConversationMessage.role == MessageRole.assistant,
            ConversationMessage.sequence > next_seq,
        )
        .order_by(ConversationMessage.sequence.desc())
        .limit(1)
    ).first()

    # 10. C7-09: policy_qa / general turns are answered by the PolicyAgent, which does not
    #     persist a reply.  Persist the assistant turn here (answer or abstention text, with
    #     answer_type / citations in its metadata) so GET /conversations/{id} shows it and the
    #     done frame carries ITS id.  Only when no agent already wrote one (no double-write).
    if assistant_msg is None and conv.context_type.value in service.ROUTER_PERSISTED_CONTEXTS:
        assistant_msg = service.append_policy_reply(
            db,
            conversation_id=conv.id,
            sequence=service.get_next_sequence(db, conversation_id=conv.id),
            output=result_output,
            citations=outcome.result.citations,
        )

    msg_id = assistant_msg.id if assistant_msg is not None else user_msg.id
    msg_sequence = assistant_msg.sequence if assistant_msg is not None else next_seq

    # 11. Record idempotency key (before commit so it lands atomically).  It stores the id the
    #     done frame carries, so a replay streams the SAME message id as the original.
    if idem_key:
        idempotency_record(
            db,
            organization_id=current_user.organization_id,
            scope="conversation.message",
            key=idem_key,
            entity_id=msg_id,
        )

    # Single commit — user message + assistant reply + decision log + idempotency row land
    # atomically.
    db.commit()

    # 12. Build and stream SSE events
    events = build_sse_events(
        outcome.result,
        task_type=outcome.decision.task_type,
        route=outcome.decision.route.value,
        confidence=outcome.decision.confidence,
        explain_with_llm=body.explain_with_llm,
        message_id=msg_id,
        sequence=msg_sequence,
    )
    return StreamingResponse(iter(events), media_type="text/event-stream")


def _audit_trip_selection(
    db: Session,
    *,
    current_user: User,
    conversation_id: uuid.UUID,
    trip_id_raw: object,
    trip_change: dict | None,
    audit_snap: dict,
) -> None:
    """Write the trip.created / trip.updated audit row for a planner selection."""
    try:
        trip_id_val = uuid.UUID(str(trip_id_raw))
    except (ValueError, TypeError):
        logger.warning("malformed trip_id in output: %s", trip_id_raw)
        return
    action = trip_change.get("action") if trip_change else "created"
    if action == "unchanged":
        return
    after = dict(audit_snap.get("after") or {})
    after["source"] = "planner"
    after["conversation_id"] = str(conversation_id)
    if action == "updated":
        after["segments_added"] = list((trip_change or {}).get("segment_types_added") or [])
        after["segments_replaced"] = list((trip_change or {}).get("segment_types_replaced") or [])
        # UAT2 defect E: only the return date changed -> the same draft, in place.
        end_change = (trip_change or {}).get("end_date_change")
        if isinstance(end_change, dict):
            after["end_date_change"] = dict(end_change)
            after["stale_hotel"] = bool((trip_change or {}).get("stale_hotel"))
        record_audit(
            db,
            organization_id=current_user.organization_id,
            action="trip.updated",
            entity_type="trip",
            entity_id=trip_id_val,
            actor_id=current_user.id,
            before_state=audit_snap.get("before"),
            after_state=after,
        )
        return
    previous = (trip_change or {}).get("previous_trip")
    if isinstance(previous, dict):
        after["previous_trip_id"] = previous.get("id")
    # Product gap 2 stage 2 (PTA-05): the new draft changes an approved / pending trip.
    if (trip_change or {}).get("changes_trip_id"):
        after["changes_trip_id"] = (trip_change or {}).get("changes_trip_id")
        after["reapproval_required"] = bool((trip_change or {}).get("reapproval_required"))
    record_audit(
        db,
        organization_id=current_user.organization_id,
        action="trip.created",
        entity_type="trip",
        entity_id=trip_id_val,
        actor_id=current_user.id,
        after_state=after if trip_change else None,
    )


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}
# ---------------------------------------------------------------------------


@router.get("/{conversation_id}", response_model=schemas.ConversationDetailResponse)
def get_conversation(
    conversation_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.ConversationDetailResponse:
    """Retrieve a conversation with its full message history (owner only)."""
    conv = service.get_conversation_for_user(db, conversation_id=conversation_id, user=current_user)
    # Eagerly load messages (relationship is already defined on Conversation)
    db.refresh(conv, attribute_names=["messages"])
    return schemas.ConversationDetailResponse.model_validate(conv)
