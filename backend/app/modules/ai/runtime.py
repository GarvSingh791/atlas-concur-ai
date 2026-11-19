"""AI composition root — where backend builds the orchestrator (P4-T13).

Wires all four agents into the AgentRegistry, wraps them in an Orchestrator,
and exposes a FastAPI-compatible dependency for injection.

LAYERING LAW: ai_agents never imports backend; imports run one way only,
backend → ai_agents.  This module is the single place the orchestrator and its
agents are constructed.  Other backend modules (ai, approval, expense, policy,
receipt, search) import ai_agents directly for envelopes, schemas and the
deterministic rule helpers they share with the agents.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from ai_agents.agents import register_policy
from ai_agents.agents.approval_agent import register_approval
from ai_agents.agents.expense_agent import register_expense
from ai_agents.agents.travel_agent import register_travel
from ai_agents.context import AgentContext
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.orchestrator.registry import AgentRegistry
from backend.app.core.audit import record_audit
from backend.app.core.correlation import get_correlation_id
from backend.app.core.rbac import get_user_permissions
from database.models.identity import User
from services.llm_provider.factory import get_llm_provider

# Upper bound on the persisted/logged correlation id. The inbound
# X-Correlation-ID header is caller-controlled and unbounded; it lands in a TEXT
# column (ai_decision_logs.correlation_id) and in log lines, so cap it here.
# The middleware still echoes the header verbatim (error-contract tests).
MAX_CORRELATION_ID_LENGTH = 128


@functools.lru_cache(maxsize=1)
def build_orchestrator() -> Orchestrator:
    """Construct and cache the single production Orchestrator.

    Builds one AgentRegistry, registers all four agents/handlers, and returns
    an Orchestrator wrapping that registry.  The lru_cache ensures a single
    instance is reused across the process lifetime.

    Agents are registered in this order:
        1. policy  — Cap 1 deterministic handler + Cap 2/3 LLM agent
        2. travel  — LLM agent
        3. expense — LLM agent
        4. approval — deterministic agent (no LLM) with audit-write side-effect
    """
    registry = AgentRegistry()
    provider = get_llm_provider()
    register_policy(registry, provider=provider)
    register_travel(registry, provider=provider)
    register_expense(registry, provider=provider)
    register_approval(registry, audit_writer=record_audit)
    return Orchestrator(registry)


def get_orchestrator() -> Orchestrator:
    """FastAPI dependency — returns the cached Orchestrator.

    Tests may override via ``app.dependency_overrides[get_orchestrator] = ...``.
    """
    return build_orchestrator()


def build_agent_context(
    user: User,
    session: Session,
    correlation_id: str | None = None,
) -> AgentContext:
    """Resolve the user's permissions and construct an AgentContext.

    Args:
        user: The authenticated, DB-loaded user.
        session: Active SQLAlchemy session (for the RBAC query).
        correlation_id: Optional request-scoped correlation identifier. When
            omitted (or empty), defaults to the request ContextVar populated by
            CorrelationIDMiddleware, so every AI call site persists the
            request's id on ai_decision_logs without threading it explicitly.
            Outside a request (no ContextVar value) it resolves to None, never
            "". Values longer than MAX_CORRELATION_ID_LENGTH are truncated.

    Returns:
        An immutable AgentContext ready to be injected into any agent call.
    """
    resolved_cid: str | None = (correlation_id or get_correlation_id()) or None
    if resolved_cid is not None:
        resolved_cid = resolved_cid[:MAX_CORRELATION_ID_LENGTH]
    permission_keys: frozenset[str] = frozenset(get_user_permissions(user, session))
    return AgentContext(
        organization_id=user.organization_id,
        granted_permission_keys=permission_keys,
        user_id=user.id,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        correlation_id=resolved_cid,
    )


# Advisory role label on a system principal (observability only — never used
# for authorization; NFR-SEC6 gates on granted_permission_keys alone).
SYSTEM_ROLE = "system"


def build_system_agent_context(
    organization_id: uuid.UUID,
    *,
    permission_keys: Iterable[str],
    correlation_id: str | None = None,
) -> AgentContext:
    """AgentContext for a SYSTEM-initiated agent run (no human actor).

    Used when the platform itself runs an agent as a side-effect of a user action
    the user is not authorised to perform directly — e.g. opening the approval on
    an owner's submission (the submitter has no approval.decide.reports and must
    never decide their own subject).  ``user_id`` is None (decision logs and the
    agent's audit rows record no human actor); ``permission_keys`` is the
    explicit, minimal grant for this one run.  Correlation id resolution matches
    build_agent_context.
    """
    resolved_cid: str | None = (correlation_id or get_correlation_id()) or None
    if resolved_cid is not None:
        resolved_cid = resolved_cid[:MAX_CORRELATION_ID_LENGTH]
    return AgentContext(
        organization_id=organization_id,
        granted_permission_keys=frozenset(permission_keys),
        user_id=None,
        role=SYSTEM_ROLE,
        correlation_id=resolved_cid,
    )
