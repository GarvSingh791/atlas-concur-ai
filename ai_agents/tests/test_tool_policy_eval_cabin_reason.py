"""Hermetic: AIR-CABIN out-of-policy reasons are human-readable.

UAT showed planner cards reading "Allowed: ['economy']" — a Python list repr
leaking into the user-facing policy reason. No DB, no LLM.
"""

from __future__ import annotations

import uuid

import pytest

from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, format_cabin_classes
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)


def _air_cabin_rule() -> PolicyRule:
    """In-memory AIR-CABIN rule mirroring the seed definition."""
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType.cabin_class,
        applies_to=PolicyAppliesTo.flight,
        source_clause_ref="AIR-CABIN",
        status=PolicyRuleStatus.active,
        definition={
            "tiers": [
                {"max_hours": 6, "allowed_classes": ["economy"]},
                {
                    "min_hours": 6,
                    "max_hours": 10,
                    "allowed_classes": ["economy", "premium_economy"],
                },
                {"min_hours": 10, "allowed_classes": ["economy", "premium_economy", "business"]},
            ],
            "override_approver": "vp",
        },
    )


@pytest.mark.parametrize(
    ("classes", "text"),
    [
        (["economy"], "economy"),
        (["economy", "premium_economy"], "economy or premium economy"),
        (["economy", "premium_economy", "business"], "economy, premium economy or business"),
        ([], "none"),
    ],
)
def test_format_cabin_classes(classes: list[str], text: str) -> None:
    assert format_cabin_classes(classes) == text


@pytest.mark.parametrize(
    ("hours", "allowed"),
    [(5.5, "Allowed: economy."), (7.0, "Allowed: economy or premium economy.")],
)
def test_out_of_policy_reason_has_no_list_repr(hours: float, allowed: str) -> None:
    item = EvalItem(
        item_type="flight", amount_usd=1450.0, cabin_class="business", duration_hours=hours
    )
    verdict = PolicyEvalTool()._eval_cabin_class(_air_cabin_rule(), item)
    assert verdict.in_policy is False
    assert allowed in verdict.reason
    assert "[" not in verdict.reason and "]" not in verdict.reason
    # The structured observation keeps the raw list for machines.
    assert verdict.observed["allowed"] in (["economy"], ["economy", "premium_economy"])
