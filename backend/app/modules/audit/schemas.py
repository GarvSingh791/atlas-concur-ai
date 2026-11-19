"""Audit module Pydantic schemas (#3b)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogItem(BaseModel):
    """One row of the append-only audit_log, shaped for GET /api/v1/audit.

    actor_name is resolved server-side (bulk users join); it is None for
    system-initiated events (actor_id is NULL). before_state/after_state are
    the raw JSONB blobs (null on create/delete respectively) — rendered
    structurally by the client, never re-derived.
    """

    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_name: str | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    created_at: datetime
