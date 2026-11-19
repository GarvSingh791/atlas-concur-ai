"""Role-bound action check (NFR-SEC6).

AgentAction enumerates every state-changing operation an agent may attempt.
ACTION_PERMISSION_MAP maps each action to the exact permission key constant from
config/constants/permissions.py — no hardcoded string literals, no role names.

check_action() enforces the check: allow only if the required key is present in
AgentContext.granted_permission_keys (injected by the caller).

Security properties:
  - Permission-KEY based: a principal with a forged role name but the same key set
    is treated identically.  A different role name with a different key set is
    correctly blocked.
  - Enumerated action set: an LLM-generated or prompt-injected instruction cannot
    request an action outside this enum — there is no "execute arbitrary action"
    surface.
  - No DB lookup: the check is a pure set-membership test on injected data;
    ai_agents never queries the database for permissions.

Future consumer: agents (P4-T8+) import check_action and call it before any
state-changing tool invocation, passing their AgentContext.
"""

from __future__ import annotations

import enum

from ai_agents.context import AgentContext
from ai_agents.exceptions import GuardrailBlocked
from config.constants.permissions import (
    APPROVAL_DECIDE_REPORTS,
    EXPENSE_CREATE_OWN,
    EXPENSE_UPDATE_OWN,
    POLICY_MANAGE,
    TRIP_CREATE_OWN,
    TRIP_UPDATE_OWN,
)


class AgentAction(str, enum.Enum):
    """Enumerated state-changing agent actions.

    Read-only / retrieval operations (search, query, summarize) are NOT listed
    here — they bypass the action check.  Only operations that mutate business
    state require a permission gate.
    """

    TRIP_CREATE = "trip.create"
    TRIP_UPDATE = "trip.update"
    """Changing an existing draft trip's legs (planner selection) — same key as PATCH /trips."""
    TRIP_UPDATE_STATUS = "trip.update_status"
    EXPENSE_REPORT_CREATE = "expense_report.create"
    EXPENSE_REPORT_SUBMIT = "expense_report.submit"
    """Submit is gated by EXPENSE_UPDATE_OWN per P2-T7 (submit is an update transition)."""
    APPROVAL_DECIDE = "approval.decide"
    POLICY_MANAGE = "policy.manage"


ACTION_PERMISSION_MAP: dict[AgentAction, str] = {
    AgentAction.TRIP_CREATE: TRIP_CREATE_OWN,
    AgentAction.TRIP_UPDATE: TRIP_UPDATE_OWN,
    AgentAction.TRIP_UPDATE_STATUS: TRIP_UPDATE_OWN,
    AgentAction.EXPENSE_REPORT_CREATE: EXPENSE_CREATE_OWN,
    AgentAction.EXPENSE_REPORT_SUBMIT: EXPENSE_UPDATE_OWN,
    AgentAction.APPROVAL_DECIDE: APPROVAL_DECIDE_REPORTS,
    AgentAction.POLICY_MANAGE: POLICY_MANAGE,
}
"""Maps each AgentAction to the config/constants/permissions key that gates it.

No string literals — always references the constants module so key renames are
caught at import time, not at runtime.
"""


def check_action(context: AgentContext, action: AgentAction) -> None:
    """Allow *action* if the required permission key is in context.granted_permission_keys.

    Args:
        context: Injected AgentContext for the current invocation.
        action: The state-changing action the agent wants to perform.

    Raises:
        GuardrailBlocked: The principal does not have the required permission key.
            Message is safe to log; it does not expose the full key set.
    """
    required_key = ACTION_PERMISSION_MAP[action]
    if required_key not in context.granted_permission_keys:
        raise GuardrailBlocked(
            f"Action {action.value!r} requires permission {required_key!r}; "
            "principal does not have it."
        )
