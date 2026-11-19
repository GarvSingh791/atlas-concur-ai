"""Issue 6 — planner hotel nights / totals and the persisted hotel segment.

Hermetic: no DB, no LLM.  Seeded (canonical) rules via an in-memory rule list.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import build_eval_item
from ai_agents.agents.travel_agent.persistence import _estimated_cost, _option_to_segment
from ai_agents.agents.travel_agent.ranking import (
    RankedOption,
    _annotate,
    _option_to_eval_item,
    stay_nights,
)
from ai_agents.context import AgentContext
from ai_agents.tools.inventory import InventoryOption
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.policy_content import RULES

_CTX = AgentContext(
    organization_id=uuid.uuid4(), user_id=uuid.uuid4(), granted_permission_keys=frozenset()
)


@pytest.fixture(autouse=True)
def _rules():
    rules = [
        PolicyRule(
            id=uuid.uuid4(),
            policy_id=uuid.uuid4(),
            rule_type=PolicyRuleType(r["rule_type"]),
            applies_to=PolicyAppliesTo(r["applies_to"]),
            definition=r["definition"],
            source_clause_ref=r["clause_ref"],
            status=PolicyRuleStatus.active,
        )
        for r in RULES
    ]
    with patch.object(PolicyEvalTool, "_load_active_rules", return_value=rules):
        yield


def _hotel(
    rate: float, *, raw_in: str = "2026-06-10", raw_out: str = "2026-06-14"
) -> InventoryOption:
    """A fixture-style hotel whose OWN window (raw) differs from the requested stay."""
    return InventoryOption(
        option_id="NYC-BOUTIQUE-001",
        segment_type="hotel",
        supplier="Independent",
        amount_usd=rate,
        sustainability_score=4,
        cabin_class=None,
        duration_hours=None,
        is_direct=None,
        city="New York",
        nightly_rate_usd=rate,
        room_type="Deluxe",
        raw={"check_in_date": raw_in, "check_out_date": raw_out, "nightly_rate_usd": rate},
    )


def _evaluate(opt: InventoryOption, nights: int | None) -> RankedOption:
    item = _option_to_eval_item(opt, nights=nights)
    (verdict,) = PolicyEvalTool().evaluate_many([item], context=_CTX, session=None)  # type: ignore[arg-type]
    return _annotate(opt, verdict, [], nights=nights)


class TestStayNights:
    def test_from_requested_dates(self) -> None:
        assert stay_nights("2026-11-17", "2026-11-20") == 3

    @pytest.mark.parametrize(
        ("dep", "ret"),
        [("2026-11-17", "2026-11-17"), ("2026-11-17", None), (None, None), ("x", "y")],
    )
    def test_unknown_or_zero_is_none(self, dep: Any, ret: Any) -> None:
        assert stay_nights(dep, ret) is None


class TestHotelOptionEvaluation:
    def test_reason_uses_requested_nights_not_fixture_window(self) -> None:
        """The fixture window is 4 nights; the requested stay is 1 night."""
        ranked = _evaluate(_hotel(383.0), nights=1)
        assert ranked.in_policy is False
        assert "Nightly rate 383.00 USD (1 night) exceeds the Tier 1 cap 300.00" in (
            ranked.policy_reason
        )
        assert "could not be verified" not in ranked.policy_reason
        assert ranked.nights == 1 and ranked.stay_total_usd == 383.0

    def test_three_night_stay_total(self) -> None:
        ranked = _evaluate(_hotel(258.0), nights=3)
        assert ranked.in_policy is True
        assert ranked.stay_total_usd == 774.0
        assert "$774 total for 3 nights" in ranked.explanation

    def test_unknown_nights_never_says_full_amount(self) -> None:
        ranked = _evaluate(_hotel(383.0), nights=None)
        assert ranked.nights is None and ranked.stay_total_usd is None
        assert "quoted per-night rate" in ranked.policy_reason
        assert "full amount" not in ranked.policy_reason

    def test_flights_carry_no_stay_fields(self) -> None:
        flight = dataclasses.replace(
            _hotel(300.0),
            option_id="UA1",
            segment_type="flight",
            supplier="United Airlines",
            cabin_class="economy",
            duration_hours=5.5,
            is_direct=True,
            city=None,
            nightly_rate_usd=None,
            room_type=None,
        )
        ranked = _evaluate(flight, nights=None)
        assert ranked.nights is None and ranked.stay_total_usd is None

    def test_agent_serializes_stay_fields(self) -> None:
        from ai_agents.agents.travel_agent.agent import _option_to_dict

        d = _option_to_dict(_evaluate(_hotel(258.0), nights=3), 1)
        assert d["nights"] == 3 and d["stay_total_usd"] == 774.0
        assert d["nightly_rate_usd"] == 258.0


class TestPersistedHotelSegment:
    def test_total_nights_and_datetimes(self) -> None:
        ranked = _evaluate(_hotel(258.0), nights=3)
        seg = _option_to_segment(ranked, uuid.uuid4(), check_in=date(2026, 11, 17), nights=3)
        assert seg.amount == Decimal("774.00")
        assert seg.details["nights"] == 3
        assert seg.details["nightly_rate_usd"] == 258.0
        assert seg.details["check_in_date"] == "2026-11-17"
        assert seg.details["check_out_date"] == "2026-11-20"
        assert seg.start_datetime is not None and seg.end_datetime is not None
        assert seg.end_datetime >= seg.start_datetime
        assert seg.start_datetime.date() == date(2026, 11, 17)
        assert seg.end_datetime.date() == date(2026, 11, 20)

    def test_same_day_trip_is_one_night_and_satisfies_the_check(self) -> None:
        """Return chip cleared (start == end): one night, end strictly after start."""
        ranked = _evaluate(_hotel(258.0), nights=None)
        day = date(2026, 11, 17)
        seg = _option_to_segment(
            ranked, uuid.uuid4(), check_in=day, nights=max((day - day).days, 1)
        )
        assert seg.details["nights"] == 1
        assert seg.amount == Decimal("258.00")
        assert seg.end_datetime is not None and seg.start_datetime is not None
        assert seg.end_datetime > seg.start_datetime

    def test_segment_amount_matches_estimated_cost(self) -> None:
        ranked = _evaluate(_hotel(258.0), nights=3)
        start, end = date(2026, 11, 17), date(2026, 11, 20)
        seg = _option_to_segment(ranked, uuid.uuid4(), check_in=start, nights=3)
        assert _estimated_cost([ranked], start, end) == seg.amount

    def test_approval_time_reevaluation_compares_the_nightly_rate(self) -> None:
        """_fresh_trip_flags builds the item with build_eval_item(segment=...):
        the stay total is divided by details.nights -> 383 vs the 300 cap."""
        ranked = _evaluate(_hotel(383.0), nights=3)
        seg = _option_to_segment(ranked, uuid.uuid4(), check_in=date(2026, 11, 17), nights=3)
        entry = DraftEntry(
            category="lodging",
            amount=float(str(seg.amount)),
            currency="USD",
            expense_date="2026-11-17",
            vendor=seg.supplier,
            description=None,
            status="planned",
            match_source=None,
            trip_segment_id=None,
            is_itemized=False,
            primary_receipt_id=None,
            source_card_txn_id=None,
            sibling_group_id=None,
        )
        item = build_eval_item(entry, segment=seg)
        assert item.nights == 3 and item.amount_usd == pytest.approx(383.0)
        (verdict,) = PolicyEvalTool().evaluate_many([item], context=_CTX, session=None)  # type: ignore[arg-type]
        cap = next(v for v in verdict.verdicts if v.source_clause_ref == "HOTEL-CAP")
        assert cap.in_policy is False and cap.observed["cap"] == 300.0
        assert "(3 nights)" in cap.reason
