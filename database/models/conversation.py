"""Conversation domain model.

Per DATABASE_SCHEMA.md §conversations.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id).
  NO authorship — user_id is the session owner, not an edit-provenance pair.

context_ref_id is a polymorphic reference (no FK) — it may point to a trip or
expense_report, resolved at the application layer.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationContextType(str, enum.Enum):
    """Routes and scopes the AI assistant session (PG type conversation_context_type)."""

    travel_planning = "travel_planning"
    expense = "expense"
    policy_qa = "policy_qa"
    general = "general"


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """An AI assistant session scoped to a user and optionally to a context object.

    context_ref_id has NO FK — polymorphic context reference; app-layer integrity.
    """

    __tablename__ = "conversations"

    # The user holding this conversation session.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    context_type: Mapped[ConversationContextType] = mapped_column(
        SAEnum(ConversationContextType, name="conversation_context_type", create_type=True),
        nullable=False,
    )

    # Polymorphic reference to a trip or expense_report; NULL for general sessions.
    context_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    # Structured trip intent extracted by the Travel Agent; editable by the user.
    parsed_intent: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_conversations_organization_id", "organization_id"),
        Index("ix_conversations_user_id", "user_id"),
    )

    messages: Mapped[list[object]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ConversationMessage.sequence",
    )
