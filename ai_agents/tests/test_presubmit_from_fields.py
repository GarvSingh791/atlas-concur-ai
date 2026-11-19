"""Hermetic tests for the receipt-upload policy plumbing (FEATURE B).

No database, no LLM, no network. PolicyEvalTool._load_active_rules is patched to
return an in-memory HOTEL-CAP rule so the full chain
    build_eval_item_from_fields → EvalItem → PolicyEvalTool.evaluate → verdict
is exercised deterministically.

Proves:
  - A NYC hotel at per_night 495 (over the $250 Tier-1 cap) → in_policy=False with
    a HOTEL-CAP RuleVerdict requiring the 'manager' approver.
  - build_eval_item_from_fields uses the PER-NIGHT rate (not the folio total).
  - verdicts_to_flags surfaces that verdict as a blocking policy_violation flag.
  - A compliant NYC hotel ($210) → in_policy=True, no blocking flag.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from ai_agents.agents.expense_agent.presubmit import (
    build_eval_item_from_fields,
    build_flags_snapshot,
    verdicts_to_flags,
)
from ai_agents.context import AgentContext
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)

_ORG_ID = uuid.uuid4()


def _ctx() -> AgentContext:
    return AgentContext(
        organization_id=_ORG_ID,
        granted_permission_keys=frozenset(),
        user_id=uuid.uuid4(),
        correlation_id="presubmit-from-fields-test",
    )


def _hotel_cap_rule() -> PolicyRule:
    """In-memory HOTEL-CAP rule mirroring the P3 seed definition."""
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.amount_cap,
        applies_to=PolicyAppliesTo.hotel,
        source_clause_ref="HOTEL-CAP",
        status=PolicyRuleStatus.active,
        definition={
            "currency": "USD",
            "per": "night",
            "tiers": [
                {
                    "tier": "tier1",
                    "cities": ["New York", "San Francisco", "London", "Tokyo", "Paris", "Zurich"],
                    "cap": 250,
                },
                {"tier": "domestic", "description": "All other US cities", "cap": 180},
            ],
            "exception_approver": "manager",
        },
    )


def _evaluate(fields: dict, *, category: str = "lodging", ref_id: str = "Marriott"):
    """Run build_eval_item_from_fields → PolicyEvalTool.evaluate with a stubbed rule set."""
    item = build_eval_item_from_fields(fields, category=category, ref_id=ref_id)
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=[_hotel_cap_rule()]):
        verdict = PolicyEvalTool().evaluate(item, context=_ctx(), session=object())  # type: ignore[arg-type]
    return item, verdict


class TestNycHotelOverCap:
    def test_per_night_495_violates_hotel_cap(self) -> None:
        """NYC hotel, 3 nights, per_night 495 → over the $250 Tier-1 cap."""
        fields = {
            "amount": 1485.0,  # folio total (3 * 495)
            "currency": "USD",
            "location": "New York, NY",
            "nights": 3,
            "per_night_rate": 495.0,
            "expense_type": "hotel",
        }
        item, verdict = _evaluate(fields)

        # build_eval_item_from_fields must use the PER-NIGHT rate, not the folio total.
        assert item.item_type == "hotel"
        assert item.amount_usd == 495.0

        assert verdict.in_policy is False
        hotel_cap = [v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP"]
        assert hotel_cap, "Expected a HOTEL-CAP RuleVerdict"
        v = hotel_cap[0]
        assert v.in_policy is False
        assert v.requires_approver == "manager"
        assert v.observed["cap"] == 250.0
        assert v.observed["amount_usd"] == 495.0

    def test_over_cap_surfaces_blocking_policy_violation_flag(self) -> None:
        """The over-cap verdict → a blocking policy_violation flag via verdicts_to_flags."""
        fields = {
            "amount": 1485.0,
            "currency": "USD",
            "location": "New York, NY",
            "nights": 3,
            "per_night_rate": 495.0,
            "expense_type": "hotel",
        }
        _, verdict = _evaluate(fields)

        flags, failing = verdicts_to_flags(
            list(verdict.verdicts),
            entry_ref="Marriott",
            has_receipt=True,
            category="lodging",
        )
        assert failing == 1
        blocking = [f for f in flags if f.severity == "blocking"]
        assert any(f.flag_type == "policy_violation" for f in blocking)

        snapshot = build_flags_snapshot(flags, entry_count=1)
        assert snapshot["blocking_count"] >= 1
        assert snapshot["evaluated_entry_count"] == 1

    def test_per_night_derived_from_nights_when_rate_absent(self) -> None:
        """No per_night_rate → derive amount / nights (1485 / 3 = 495) → still over cap."""
        fields = {
            "amount": 1485.0,
            "currency": "USD",
            "location": "New York, NY",
            "nights": 3,
            "expense_type": "hotel",
        }
        item, verdict = _evaluate(fields)
        assert item.amount_usd == 495.0
        assert verdict.in_policy is False


class TestCompliantNycHotel:
    def test_in_policy_hotel_no_blocking_flag(self) -> None:
        """NYC hotel at $210/night → within the $250 cap → in_policy, no blocking flag."""
        fields = {
            "amount": 210.0,
            "currency": "USD",
            "location": "New York, NY",
            "nights": 1,
            "per_night_rate": 210.0,
            "expense_type": "hotel",
        }
        item, verdict = _evaluate(fields)
        assert item.amount_usd == 210.0
        assert verdict.in_policy is True

        flags, failing = verdicts_to_flags(
            list(verdict.verdicts),
            entry_ref="Marriott",
            has_receipt=True,
            category="lodging",
        )
        assert failing == 0
        assert [f for f in flags if f.severity == "blocking"] == []
        snapshot = build_flags_snapshot(flags, entry_count=1)
        assert snapshot["blocking_count"] == 0
