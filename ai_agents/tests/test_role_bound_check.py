"""Hermetic tests for the role-bound action check (NFR-SEC6).

No database, no network.  All tests run in hermetic CI.

Key invariants verified:
  - Principal WITHOUT the required key → GuardrailBlocked.
  - Principal WITH the required key → allowed (no exception).
  - Swapping the role NAME while keeping the same key set → same outcome.
    (Proves key-based, not role-name-based, enforcement.)
  - All AgentActions have a corresponding entry in ACTION_PERMISSION_MAP.
"""

from __future__ import annotations

import uuid

import pytest

from ai_agents.context import AgentContext
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.guardrails.actions import (
    ACTION_PERMISSION_MAP,
    AgentAction,
    check_action,
)
from config.constants.permissions import (
    APPROVAL_DECIDE_REPORTS,
    EXPENSE_CREATE_OWN,
    EXPENSE_UPDATE_OWN,
    POLICY_MANAGE,
    TRIP_CREATE_OWN,
    TRIP_UPDATE_OWN,
)

_ORG_ID = uuid.uuid4()


def _ctx(*, keys: frozenset[str], role: str = "employee") -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        granted_permission_keys=keys,
        role=role,
    )


class TestCheckActionAllowed:
    @pytest.mark.parametrize(
        "action,key",
        [
            (AgentAction.TRIP_CREATE, TRIP_CREATE_OWN),
            (AgentAction.TRIP_UPDATE, TRIP_UPDATE_OWN),
            (AgentAction.TRIP_UPDATE_STATUS, TRIP_UPDATE_OWN),
            (AgentAction.EXPENSE_REPORT_CREATE, EXPENSE_CREATE_OWN),
            (AgentAction.EXPENSE_REPORT_SUBMIT, EXPENSE_UPDATE_OWN),
            (AgentAction.APPROVAL_DECIDE, APPROVAL_DECIDE_REPORTS),
            (AgentAction.POLICY_MANAGE, POLICY_MANAGE),
        ],
    )
    def test_action_allowed_when_key_present(self, action: AgentAction, key: str) -> None:
        ctx = _ctx(keys=frozenset({key}))
        check_action(ctx, action)  # must not raise

    @pytest.mark.parametrize("action", list(AgentAction))
    def test_action_allowed_when_all_keys_present(self, action: AgentAction) -> None:
        all_keys = frozenset(ACTION_PERMISSION_MAP.values())
        ctx = _ctx(keys=all_keys)
        check_action(ctx, action)  # must not raise


class TestCheckActionBlocked:
    @pytest.mark.parametrize(
        "action,key",
        [
            (AgentAction.TRIP_CREATE, TRIP_CREATE_OWN),
            (AgentAction.TRIP_UPDATE, TRIP_UPDATE_OWN),
            (AgentAction.TRIP_UPDATE_STATUS, TRIP_UPDATE_OWN),
            (AgentAction.EXPENSE_REPORT_CREATE, EXPENSE_CREATE_OWN),
            (AgentAction.EXPENSE_REPORT_SUBMIT, EXPENSE_UPDATE_OWN),
            (AgentAction.APPROVAL_DECIDE, APPROVAL_DECIDE_REPORTS),
            (AgentAction.POLICY_MANAGE, POLICY_MANAGE),
        ],
    )
    def test_action_blocked_when_key_absent(self, action: AgentAction, key: str) -> None:
        ctx = _ctx(keys=frozenset())
        with pytest.raises(GuardrailBlocked):
            check_action(ctx, action)

    def test_action_blocked_with_wrong_key_only(self) -> None:
        ctx = _ctx(keys=frozenset({"some.other.permission"}))
        with pytest.raises(GuardrailBlocked):
            check_action(ctx, AgentAction.POLICY_MANAGE)

    def test_blocked_exception_is_guardrail_blocked(self) -> None:
        ctx = _ctx(keys=frozenset())
        with pytest.raises(GuardrailBlocked) as exc_info:
            check_action(ctx, AgentAction.APPROVAL_DECIDE)
        assert exc_info.value.code == "GUARDRAIL_BLOCKED"

    def test_blocked_message_safe_for_logging(self) -> None:
        ctx = _ctx(keys=frozenset())
        with pytest.raises(GuardrailBlocked) as exc_info:
            check_action(ctx, AgentAction.TRIP_CREATE)
        msg = str(exc_info.value)
        assert "trip.create" in msg
        assert TRIP_CREATE_OWN in msg


class TestKeyBasedNotRoleNameBased:
    """Prove that authorization is key-based, not role-name-based (NFR-SEC6)."""

    def test_same_keys_different_role_name_same_outcome_allowed(self) -> None:
        key = TRIP_CREATE_OWN
        ctx_employee = _ctx(keys=frozenset({key}), role="employee")
        ctx_fake_admin = _ctx(keys=frozenset({key}), role="finance_admin")
        ctx_bogus = _ctx(keys=frozenset({key}), role="superuser")

        # All three have the key → all allowed
        check_action(ctx_employee, AgentAction.TRIP_CREATE)
        check_action(ctx_fake_admin, AgentAction.TRIP_CREATE)
        check_action(ctx_bogus, AgentAction.TRIP_CREATE)

    def test_same_keys_different_role_name_same_outcome_blocked(self) -> None:
        ctx_employee = _ctx(keys=frozenset(), role="employee")
        ctx_fake_admin = _ctx(keys=frozenset(), role="finance_admin")
        ctx_bogus = _ctx(keys=frozenset(), role="god_mode")

        # None have the key → all blocked regardless of role name
        with pytest.raises(GuardrailBlocked):
            check_action(ctx_employee, AgentAction.POLICY_MANAGE)
        with pytest.raises(GuardrailBlocked):
            check_action(ctx_fake_admin, AgentAction.POLICY_MANAGE)
        with pytest.raises(GuardrailBlocked):
            check_action(ctx_bogus, AgentAction.POLICY_MANAGE)

    def test_forged_admin_role_without_key_is_blocked(self) -> None:
        """A forged role='finance_admin' without the key is still blocked."""
        ctx = _ctx(keys=frozenset({"expense.read.own"}), role="finance_admin")
        with pytest.raises(GuardrailBlocked):
            check_action(ctx, AgentAction.POLICY_MANAGE)


class TestActionPermissionMapCompleteness:
    def test_all_actions_have_map_entry(self) -> None:
        for action in AgentAction:
            assert (
                action in ACTION_PERMISSION_MAP
            ), f"AgentAction.{action.name} is missing from ACTION_PERMISSION_MAP"

    def test_all_map_values_are_non_empty_strings(self) -> None:
        for action, key in ACTION_PERMISSION_MAP.items():
            assert (
                isinstance(key, str) and key
            ), f"ACTION_PERMISSION_MAP[{action}] must be a non-empty string"
