"""Server-side risk-level derivation for the approval read surfaces (P5-T8).

The Approval Agent (P4-T12) is a deterministic router — it does NOT emit a
risk *level*; it emits structured signals in ``risk_summary``
(``ai_agents/agents/approval_agent/evaluate.py``):
``route``, ``subject_in_policy``, ``blocking_flag_count``, ``requires_approver``,
``subject_total_usd``, ``flags``.

The decision dashboard needs a Low/Med/High badge. We derive that level HERE,
on the server, from those deterministic signals — so the client renders truth
and never scores risk itself (PRD "no client intelligence"). Pure function.
"""

from __future__ import annotations

from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]


def derive_risk_level(
    *,
    risk_summary: dict[str, Any] | None,
    is_auto_approved: bool,
) -> RiskLevel:
    """Map an approval's deterministic signals onto a Low/Med/High level.

    Rules (deterministic, from the agent's own signals):
      - auto-approved (flag or ``route == "auto_approve"``) → ``low``
      - any blocking flag (``blocking_flag_count >= 1``)      → ``high``
      - escalated without a blocking flag (soft / over-threshold / out-of-policy
        / requires-approver) → ``medium``
      - no risk_summary and not auto-approved (legacy/seed) → ``low`` (no signals)
    """
    if is_auto_approved:
        return "low"

    rs = risk_summary or {}
    if rs.get("route") == "auto_approve":
        return "low"

    blocking = rs.get("blocking_flag_count") or 0
    if isinstance(blocking, int) and blocking >= 1:
        return "high"

    if rs.get("route") == "escalate":
        return "medium"

    return "low"
