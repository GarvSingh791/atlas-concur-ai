"""Shared primitives for the P4-T4 grounding tool layer.

Convention (enforced by this module):
  - Tools raise ToolError (a GuardrailError subclass), never backend AppError.
  - Tools receive Session + AgentContext per-call; they never open a session.
  - Tools are READ-ONLY grounding fetchers — no INSERT/UPDATE/DELETE.
  - Tools return typed frozen dataclasses with citation-able ref fields.
    They do NOT construct Citation objects and do NOT call validate_grounding.
  - LAYERING LAW: ai_agents imports only config/, database.models, services/, stdlib.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.exceptions import GuardrailError


class ToolError(GuardrailError):
    """Raised by a grounding tool on invalid input or unresolvable state.

    Subclass of GuardrailError; never backend AppError.
    Messages must be safe for logging (no PII, no secrets).
    """

    code: str = "TOOL_ERROR"


@runtime_checkable
class GroundingTool(Protocol):
    """Structural type for grounding tools (type-checker contract only — not inherited).

    The tool registry (P4-T7) will use this protocol for routing.  Tools need not
    inherit from it; matching the call signature is sufficient.
    """

    def __call__(self, intent: Any, *, context: AgentContext, session: Session) -> Any: ...
