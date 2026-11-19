"""AI module Pydantic schemas for conversation endpoints (P4-T13 Slice 1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from database.models.conversation import ConversationContextType


class ConversationCreate(BaseModel):
    context_type: ConversationContextType
    context_ref_id: uuid.UUID | None = None


class ConversationResponse(BaseModel):
    id: uuid.UUID
    context_type: ConversationContextType
    context_ref_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    payload: dict[str, Any] | None = None
    explain_with_llm: bool = False


class ConversationMessageResponse(BaseModel):
    id: uuid.UUID
    role: str  # MessageRole.value
    content: str
    tool_calls: dict[str, Any] | None
    citations: dict[str, Any] | None
    confidence_source: str | None = None
    near_topic_suggestion: dict[str, Any] | None = None
    sequence: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationDetailResponse(BaseModel):
    id: uuid.UUID
    context_type: ConversationContextType
    context_ref_id: uuid.UUID | None
    created_at: datetime
    messages: list[ConversationMessageResponse]

    model_config = ConfigDict(from_attributes=True)
