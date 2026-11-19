"""Product gap 2, stage 2 — review fixes (hermetic).

* AIR-06 in the planner: a flight more than the tolerance above the cheapest fare of the
  same cabin is OUT of policy (clause cited), ranked below the compliant options, and
  the policy-honesty critic keeps it that way;
* PTA-05: the effective estimate of a change = the new selection + the approved trip's
  legs of the types NOT re-selected (unpriced / non-USD kept leg -> could not verify);
* EXP-04: the affidavit ceiling used by the quarterly count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ai_agents.agents.process_checks import (
    ProcessRules,
    RuleRef,
    effective_change_estimate,
    option_notices,
)
from ai_agents.agents.travel_agent.ranking import _annotate
from ai_agents.agents.travel_agent.reflection import (
    TravelPolicyHonestyCritic,
    TravelShortlistHypothesizer,
)
from ai_agents.guardrails.reflection import ReflectionOutcome, reflect
from ai_agents.tools import process_rules as pr
from ai_agents.tools.inventory import InventoryOption
from ai_agents.tools.policy_eval import EvalItem, PolicyVerdict

FARE = {
    "kind": "lowest_fare",
    "corpus_clause_ref": "AIR-06",
    "domestic_tolerance_usd": 150,
    "international_tolerance_usd": 400,
    "exception_approver": "manager",
}
TODAY = date(2026, 11, 14)


def _flight(oid: str, fare: float, cabin: str = "economy") -> InventoryOption:
    return InventoryOption(
        option_id=oid,
        segment_type="flight",
        supplier="United Airlines",
        amount_usd=fare,
        sustainability_score=3,
        cabin_class=cabin,
        duration_hours=3.0,
        is_direct=True,
        city=None,
        nightly_rate_usd=None,
        room_type=None,
        raw={},
    )


def _clean_verdict(opt: InventoryOption) -> PolicyVerdict:
    item = EvalItem(item_type="flight", amount_usd=opt.amount_usd, ref_id=opt.option_id)
    return PolicyVerdict(item=item, in_policy=True, verdicts=(), evaluated_rule_count=0)


class TestAir06InThePlanner:
    def test_over_tolerance_flight_is_out_of_policy_and_ranked_below(self) -> None:
        rules = ProcessRules(by_kind={pr.KIND_LOWEST_FARE: (RuleRef("r1", "x", FARE),)})
        cheap, near, far = _flight("cheap", 300.0), _flight("near", 450.0), _flight("far", 450.01)
        findings, lowest = option_notices(
            rules,
            flights=[(o.option_id, o.cabin_class, o.amount_usd, 3.0) for o in (cheap, near, far)],
            departure=(TODAY + timedelta(days=30)).isoformat(),
            origin="ORD",
            destination="JFK",
            today=TODAY,
        )
        ranked = sorted(
            (
                _annotate(
                    o,
                    _clean_verdict(o),
                    [],
                    findings=tuple(findings.get(o.option_id, ())),
                    lowest_fare=lowest.get(o.option_id),
                )
                for o in (far, near, cheap)
            ),
            key=lambda o: o.rank_score,
            reverse=True,
        )
        by_id = {o.option_id: o for o in ranked}
        assert by_id["near"].in_policy is True and "AIR-06" not in by_id["near"].clause_refs
        breach = by_id["far"]
        assert breach.in_policy is False and breach.rule_in_policy is False
        assert "AIR-06" in breach.clause_refs
        assert "$150.01 above" in breach.policy_reason
        assert breach.requires_approver == "manager"
        assert [o.option_id for o in ranked][-1] == "far"
        assert all(breach.rank_score < o.rank_score for o in ranked if o.in_policy)

        # The policy-honesty critic agrees with the label: it stays out of policy.
        result = reflect(
            hypothesizer=TravelShortlistHypothesizer(ranked),
            critic=TravelPolicyHonestyCritic(),
        )
        assert result.outcome == ReflectionOutcome.passed
        assert isinstance(result.value, list)
        final = {o.option_id: o for o in result.value}
        assert final["far"].in_policy is False and "AIR-06" in final["far"].clause_refs


@dataclass
class _Seg:
    segment_type: str
    amount: Any
    currency: str = "USD"


class TestEffectiveChangeEstimate:
    PRIOR = (_Seg("flight", Decimal("152.00")), _Seg("hotel", Decimal("783.00")))

    def test_a_flight_only_change_keeps_the_approved_hotel(self) -> None:
        assert effective_change_estimate("759.00", {"flight"}, self.PRIOR) == Decimal("1542.00")

    def test_replacing_every_leg_counts_only_the_new_ones(self) -> None:
        got = effective_change_estimate("900.00", {"flight", "hotel"}, self.PRIOR)
        assert got == Decimal("900.00")

    def test_every_prior_leg_of_a_replaced_type_is_dropped(self) -> None:
        prior = (*self.PRIOR, _Seg("flight", Decimal("140.00")))
        assert effective_change_estimate("759", {"flight"}, prior) == Decimal("1542.00")

    def test_unpriced_or_foreign_kept_leg_is_could_not_verify(self) -> None:
        assert effective_change_estimate("759", {"flight"}, (_Seg("hotel", None),)) is None
        foreign = (_Seg("hotel", Decimal("700"), "EUR"),)
        assert effective_change_estimate("759", {"flight"}, foreign) is None
        assert effective_change_estimate(None, {"flight"}, self.PRIOR) is None
        finding = pr.trip_change(
            {"kind": "trip_change_reapproval", "max_increase_pct": 10},
            approved_estimate="935",
            new_estimate=None,
            approved_label="the trip",
        )
        assert finding is not None and finding.flag_type == pr.NOT_EVALUATED


class TestAffidavitCeiling:
    def test_configured_default_and_unreadable(self) -> None:
        assert pr.affidavit_ceiling({"affidavit_max_usd": 75}) == Decimal("75.00")
        assert pr.affidavit_ceiling({}) == Decimal("75")
        assert pr.affidavit_ceiling({"affidavit_max_usd": "lots"}) is None
