"""Approval Agent package — exports ApprovalAgent and registration helper (P4-T12)."""

from __future__ import annotations

from ai_agents.agents.approval_agent.agent import ApprovalAgent
from ai_agents.agents.approval_agent.persistence import AuditWriter
from ai_agents.orchestrator.registry import AgentRegistry

__all__ = ["ApprovalAgent", "register_approval"]


def register_approval(
    registry: AgentRegistry,
    *,
    audit_writer: AuditWriter | None = None,
) -> None:
    """Register ApprovalAgent with the given AgentRegistry.

    Args:
        registry: The AgentRegistry to register into.
        audit_writer: Optional injected AuditWriter port.  When None, audit
            rows are not written (tests that don't need full stack can omit it).
            Production callers inject backend.app.core.audit.record_audit here.
    """
    registry.register_agent(ApprovalAgent(audit_writer=audit_writer))
