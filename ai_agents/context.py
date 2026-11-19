"""AgentContext — the PII-minimal principal injected into every AI agent call.

Callers (eventually backend/modules/ai, P4-T13) resolve the user's permission
set via backend RBAC (get_user_permissions) and construct an AgentContext before
invoking any agent or guardrail function.  ai_agents never resolves users or
permissions from the database.

Design decisions:
- `granted_permission_keys` is the ONLY authorization signal.  `role` is purely
  advisory/observability — it is NEVER used to gate access (NFR-SEC6).  A forged
  role name in the principal cannot escalate privileges because the check is
  purely key-membership based.
- `correlation_id` arrives via injection; ai_agents never imports
  backend.app.core.correlation or its ContextVar.
- The dataclass is frozen so an AgentContext cannot be mutated after construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentContext:
    """Immutable principal passed into every agent invocation.

    Args:
        organization_id: Tenant this invocation belongs to (required; NOT NULL
            on every ai_decision_logs row).
        granted_permission_keys: The resolved set of permission keys for this
            principal, injected by the caller.  Drives all guardrail checks.
        user_id: Actor user, or None for system-initiated decisions.
        role: Advisory RoleKey value (employee / manager / finance_admin).
            Present for observability only; never used for authorization.
        correlation_id: Request-scoped correlation id, injected by the caller
            from the ASGI scope or a background-task header.
    """

    organization_id: uuid.UUID
    granted_permission_keys: frozenset[str]
    user_id: uuid.UUID | None = None
    role: str | None = None
    correlation_id: str | None = None
