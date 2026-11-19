"""ConversationMessage domain model.

Per DATABASE_SCHEMA.md §conversation_messages.  Conventions applied:
  UUID PK, created_at ONLY (append-only message log).
  No tenant (scoped via conversation_id), no authorship, no version/soft-delete.

conversation_id FK: CASCADE — messages are deleted when the conversation is deleted.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class MessageRole(str, enum.Enum):
    """The actor who produced this message turn (PG type message_role)."""

    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class ConversationMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One turn in a conversation: prompt, response, or tool interaction.

    created_at only — messages are an append-only log of conversation turns.
    sequence orders messages within the conversation.
    """

    __tablename__ = "conversation_messages"

    # Parent conversation — CASCADE: messages deleted with the conversation.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[MessageRole] = mapped_column(
        SAEnum(MessageRole, name="message_role", create_type=True),
        nullable=False,
    )

    # Prompt or response text.
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Tools the agent invoked and their results (grounding trace).
    tool_calls: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Policy clause refs / source fields backing the answer.
    citations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_conversation_messages_conversation_id", "conversation_id"),)

    conversation: Mapped[object] = relationship("Conversation", back_populates="messages")
