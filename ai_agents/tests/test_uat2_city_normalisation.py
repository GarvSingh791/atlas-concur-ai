"""UAT2 defect A — receipt / OCR location formats must resolve to their city tier.

Browser evidence: an OCR'd hotel receipt location 'New York NY' (no comma) resolved
to Tier 3 ($160 / $65) instead of Tier 1 ($300 / $85), so a $230/night NYC hotel
and $68.50/day NYC meals were flagged.  Every consumer of city matching derives
its keys from ``config.constants.city_names`` so the format never changes the tier:

  * HOTEL-CAP / MEALS-PD tier resolution (``PolicyEvalTool``), incl. claimed cities;
  * the receipt-vs-segment city comparison in pre-submit (``_same_city``);
  * the corpus ``figures.city_tier`` helper (planner mock-inventory parity);
  * the planner / approval location resolver (``resolve_airport``).

Hermetic: no DB, no LLM.  Rules are the REAL seeded definitions
(``database.seeds.policy_content.RULES``).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from ai_agents.agents.approval_agent import evaluate as ev
from ai_agents.agents.approval_agent.evaluate import _destination_key
from ai_agents.agents.expense_agent.presubmit import _same_city
from ai_agents.tests.test_c7_05_15_policy_gates import (  # noqa: F401  (autouse fixture)
    _assess,
    _entry,
    _receipt,
    _report,
    _seed_rules,
    _seeded_rules,
    _session_with_users,
    _violations,
)
from ai_agents.tests.test_issue6_canonical_caps import _hotel, _meals
from ai_agents.tools.policy_eval import PolicyEvalTool
from config.constants.city_names import (
    city_keys,
    primary_city_key,
    same_city,
    strip_us_region_suffix,
)
from database.models.approval import ApprovalSubjectType
from database.models.trip import Trip, TripStatus
from database.seeds.corpus import figures as F
from services.mock_inventory.locations import resolve_airport, resolve_city

DET = "deterministic"

# (location as written on a receipt / by OCR, expected DEF-05 tier)
FORMATS: list[tuple[str, str]] = [
    ("New York NY", "tier1"),
    ("New York, NY", "tier1"),
    ("New York, NY 10017", "tier1"),
    ("NEW YORK NY 10017", "tier1"),
    ("New York NY 10017-1234", "tier1"),
    ("New York, N.Y.", "tier1"),
    ("Chicago IL", "tier2"),
    ("Chicago, IL 60601", "tier2"),
    ("San Francisco, CA 94105", "tier1"),
    ("SAN FRANCISCO CA", "tier1"),
    ("Boston MA 02110", "tier1"),
    ("Seattle WA", "tier2"),
    ("London, UK", "tier1"),
    ("Paris, France", "tier1"),
    ("Washington DC", "tier2"),
    ("Washington, DC 20001", "tier2"),
    ("Austin TX", "tier3"),
    # Trailing country token after the state / ZIP with no comma (defect J).
    ("New York NY USA", "tier1"),
    ("NEW YORK NY 10017 US", "tier1"),
    ("New York, NY 10017, USA", "tier1"),
    ("New York NY U.S.", "tier1"),
    ("New York NY 10017 U.S.A.", "tier1"),
    ("New York NY United States", "tier1"),
    ("Chicago IL 60601 USA", "tier2"),
    ("Washington DC USA", "tier2"),
    ("Washington DC 20001 US", "tier2"),
]

# Locations that must NOT be pulled into a listed city.
NON_MATCHES: list[str] = [
    "Manhattan Beach CA",  # never the 'Manhattan' NYC alias
    "Manhattan Beach, CA 90266",
    "New York Mills MN",
    "York PA",
    "NY",  # a bare state code names no city
    "Paris FR",  # 'FR' is not a US state: kept, so no Paris match
    "London UK",  # 'UK' is not a US state: kept as written
    "Manhattan Beach CA USA",  # country stripped, still never 'Manhattan'
    "York PA USA",
    "NY USA",  # a bare state code + country names no city
    "Paris FR USA",  # 'FR' is not a US state, so 'USA' is not stripped either
]


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------


class TestHelper:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("New York NY", "New York"),
            ("NEW YORK NY 10017", "NEW YORK"),
            ("New York 10017", "New York"),
            ("new york ny 10017 1234", "new york"),
            ("New York NY 10017-1234", "New York"),
            ("chicago o'hare il", "chicago o'hare"),
            ("San Juan PR", "San Juan"),
            ("Paris FR", "Paris FR"),
            ("London UK", "London UK"),
            ("Washington DC", "Washington DC"),  # DC is part of the city's name
            ("NY", "NY"),  # nothing would remain
            # trailing country token, only after a US state / ZIP (defect J)
            ("New York NY USA", "New York"),
            ("NEW YORK NY 10017 US", "NEW YORK"),
            ("New York NY U.S.", "New York"),
            ("New York NY 10017 U.S.A.", "New York"),
            ("new york ny united states", "new york"),
            ("new york ny 10017 1234 usa", "new york"),
            ("New York NY 10017-1234 USA", "New York"),
            ("New York 10017 USA", "New York"),
            ("Washington DC USA", "Washington DC"),
            ("Washington DC 20001 US", "Washington DC"),
            ("New York USA", "New York USA"),  # no state / ZIP before it: kept
            ("New York US", "New York US"),
            ("Paris FR", "Paris FR"),
            ("Paris FR USA", "Paris FR USA"),
            ("London UK USA", "London UK USA"),
            ("NY USA", "NY USA"),  # nothing but a state would remain
            ("USA", "USA"),
            ("Columbus", "Columbus"),
            ("Kansas City KS", "Kansas City"),
            ("10017", "10017"),
            ("", ""),
        ],
    )
    def test_strip_us_region_suffix(self, raw: str, expected: str) -> None:
        assert strip_us_region_suffix(raw) == expected

    def test_city_keys_keep_comma_split_and_add_stripped(self) -> None:
        assert "new york" in city_keys("New York NY")
        assert "new york" in city_keys("NEW YORK NY 10017")
        assert "washington dc" in city_keys("Washington, DC, USA")
        assert "paris" in city_keys("Paris, France")
        assert "manhattan" not in city_keys("Manhattan Beach CA")
        assert "new york" in city_keys("New York NY USA")
        assert "new york" in city_keys("NEW YORK NY 10017 US")
        assert "manhattan" not in city_keys("Manhattan Beach CA USA")

    @pytest.mark.parametrize(
        "raw",
        [
            "New York NY",
            "New York, NY",
            "NEW YORK NY 10017",
            "new york",
            "New York, N.Y.",
            "New York NY USA",
            "NEW YORK NY 10017 US",
            "New York, NY 10017, USA",
            "New York NY 10017 U.S.A.",
            "New York NY United States",
        ],
    )
    def test_primary_key_names_the_city(self, raw: str) -> None:
        assert primary_city_key(raw) == "new york"

    def test_same_city(self) -> None:
        assert same_city("New York NY", "New York, NY")
        assert same_city("New York", "NEW YORK NY 10017")
        assert not same_city("New York City", "New York")  # aliases are a tier concern
        assert not same_city("Manhattan Beach CA", "Manhattan")
        assert not same_city("", "")


# ---------------------------------------------------------------------------
# Every consumer agrees
# ---------------------------------------------------------------------------


class TestPolicyEvalTiers:
    @pytest.mark.parametrize(("location", "tier"), FORMATS)
    def test_hotel_cap_tier(self, location: str, tier: str) -> None:
        v = _hotel(amount_usd=100.0, city=location, nights=1)
        assert v.observed["tier"] == tier, location

    @pytest.mark.parametrize(("location", "tier"), FORMATS)
    def test_meals_per_diem_tier(self, location: str, tier: str) -> None:
        v = _meals(amount_usd=10.0, city=location)
        assert v.observed["tier"] == tier, location

    @pytest.mark.parametrize("location", NON_MATCHES)
    def test_non_matches_fall_to_tier3(self, location: str) -> None:
        assert _hotel(amount_usd=100.0, city=location, nights=1).observed["tier"] == "tier3"
        assert _meals(amount_usd=10.0, city=location).observed["tier"] == "tier3"

    def test_browser_case_nyc_hotel_230_is_in_policy(self) -> None:
        v = _hotel(amount_usd=230.0, city="New York NY", nights=1)
        assert v.in_policy is True
        assert v.observed["cap"] == 300.0

    def test_browser_case_nyc_meals_68_50_is_in_policy(self) -> None:
        v = _meals(amount_usd=68.5, city="New York NY")
        assert v.in_policy is True
        assert v.observed["rate"] == 85.0

    def test_claimed_city_uses_the_same_keys(self) -> None:
        """An unverified 'New York NY' is still only a claim (Tier 3 applied), but the
        claim names Tier 1 — wording / escalation, never a cleared rule."""
        v = _hotel(amount_usd=230.0, claimed_city="New York NY", nights=1)
        assert v.in_policy is False
        assert v.observed["cap"] == 160.0
        assert v.observed["claimed_tier"] == "tier1"
        assert v.observed["claimed_cap"] == 300.0

    def test_public_resolve_tier(self) -> None:
        tiers = [
            {"tier": "tier3", "cap": 160, "default": True},
            {"tier": "tier1", "cap": 300, "cities": ["New York"]},
        ]
        assert PolicyEvalTool.resolve_tier(tiers, "NEW YORK NY 10017", "cap")["tier"] == "tier1"  # type: ignore[index]
        assert PolicyEvalTool.resolve_tier(tiers, "York PA", "cap")["tier"] == "tier3"  # type: ignore[index]


class TestCorpusCityTier:
    @pytest.mark.parametrize(("location", "tier"), FORMATS)
    def test_city_tier(self, location: str, tier: str) -> None:
        assert F.city_tier(location) == tier

    @pytest.mark.parametrize("location", NON_MATCHES)
    def test_non_matches(self, location: str) -> None:
        assert F.city_tier(location) == "tier3"


class TestPlannerAndApprovalResolver:
    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("New York NY", "JFK"),
            ("NEW YORK NY 10017", "JFK"),
            ("New York, NY 10017", "JFK"),
            ("Chicago IL", "ORD"),
            ("San Francisco CA 94105", "SFO"),
            ("New York NY USA", "JFK"),
            ("NEW YORK NY 10017 US", "JFK"),
            ("New York, NY 10017, USA", "JFK"),
            ("New York NY U.S.", "JFK"),
            ("New York NY 10017 U.S.A.", "JFK"),
            ("New York NY United States", "JFK"),
            ("Chicago IL 60601 USA", "ORD"),
            ("Manhattan Beach CA", None),
            ("Manhattan Beach CA USA", None),
            ("Paris FR", None),
            ("Paris FR USA", None),
            ("London UK", None),  # UK is not stripped (unchanged behaviour)
        ],
    )
    def test_resolve_airport(self, raw: str, code: str | None) -> None:
        assert resolve_airport(raw) == code

    def test_resolve_city(self) -> None:
        assert resolve_city("New York NY") == "New York"

    def test_split_trip_destination_key(self) -> None:
        assert _destination_key("New York NY") == _destination_key("JFK")
        assert _destination_key("Austin TX") == _destination_key("Austin, TX")
        assert _destination_key("Austin TX") == _destination_key("austin")
        assert _destination_key("Manhattan Beach CA") != _destination_key("Manhattan")


class TestPresubmitSameCity:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("New York", "New York NY"),
            ("New York, NY", "NEW YORK NY 10017"),
            ("Chicago", "Chicago IL"),
            ("New York", "New York NY USA"),
            ("New York, NY", "NEW YORK NY 10017 US"),
        ],
    )
    def test_same_city_formats(self, a: str, b: str) -> None:
        assert _same_city(a, b)

    def test_different_cities_still_conflict(self) -> None:
        assert not _same_city("New York", "Newark NJ")
        assert not _same_city("Manhattan", "Manhattan Beach CA")


# ---------------------------------------------------------------------------
# Pre-submit / approval-time: a deterministic receipt location 'New York NY'
# ---------------------------------------------------------------------------


def _receipt_report(
    category: str, amount: str, vendor: str, location: str, extra: dict[str, Any]
) -> Any:
    s = _session_with_users()
    r = _report(s, total=amount)
    e = _entry(s, r, category=category, amount=amount, vendor=vendor)
    _receipt(
        s,
        e,
        fields={"vendor": vendor, "amount": float(amount), "location": location, **extra},
        provenance={"vendor": DET, "amount": DET, "location": DET, "nights": DET},
    )
    return s, r


class TestDeterministicReceiptLocation:
    @pytest.mark.parametrize(
        "location", ["New York NY", "NEW YORK NY 10017", "New York NY USA", "NEW YORK NY 10017 US"]
    )
    def test_nyc_hotel_230_per_night_is_tier1_in_policy(self, location: str) -> None:
        s, r = _receipt_report("lodging", "230.00", "Marriott Marquis", location, {"nights": 1})
        a, _route = _assess(s, r)
        assert _violations(a, "HOTEL-CAP") == [], a.flags_snapshot

    def test_nyc_hotel_over_tier1_cap_says_tier1(self) -> None:
        s, r = _receipt_report(
            "lodging", "330.00", "Marriott Marquis", "New York NY", {"nights": 1}
        )
        a, _route = _assess(s, r)
        cap = _violations(a, "HOTEL-CAP")
        assert cap and cap[0]["observed"]["cap"] == 300.0
        assert cap[0]["observed"]["tier"] == "tier1"

    def test_nyc_meals_selects_tier1_rate(self) -> None:
        s, r = _receipt_report("meals", "90.00", "Joe's Pizza", "New York NY", {})
        a, _route = _assess(s, r)
        pd = _violations(a, "MEALS-PD")
        # 90 > 85: still a violation, but against the Tier 1 rate (not Tier 3's 65).
        assert pd and pd[0]["observed"]["tier"] == "tier1"
        assert pd[0]["observed"]["rate"] == 85.0

    def test_nyc_meals_under_tier1_rate_is_in_policy(self) -> None:
        s, r = _receipt_report("meals", "68.50", "Joe's Pizza", "New York NY", {})
        a, _route = _assess(s, r)
        assert _violations(a, "MEALS-PD") == [], a.flags_snapshot


class TestRefiledRejectedTripDestination:
    """The approval-time re-filed-trip backstop compares destinations with the same
    key: re-filing a rejected 'New York' trip as 'New York NY' still escalates."""

    class _Rows:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return list(self._rows)

    class _Session:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def execute(self, _stmt: Any) -> Any:
            return TestRefiledRejectedTripDestination._Rows(self._rows)

    def _flags(self, subject_dest: str, rejected_dest: str) -> list[dict]:
        trip = Trip(
            id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            traveler_id=uuid.uuid4(),
            origin="SFO",
            primary_destination=subject_dest,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 4),
            currency="USD",
            status=TripStatus.pending_approval,
        )
        session = self._Session([(rejected_dest, date(2026, 11, 1), date(2026, 11, 4))])
        with patch.object(ev, "_returned_snapshots", return_value=[]):
            return ev._refiled_rejected_flags(trip, ApprovalSubjectType.trip, session)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("subject_dest", "rejected_dest"),
        [("New York NY", "New York"), ("Austin TX", "Austin, TX"), ("new york", "New York")],
    )
    def test_same_destination_any_format_is_flagged(
        self, subject_dest: str, rejected_dest: str
    ) -> None:
        flags = self._flags(subject_dest, rejected_dest)
        assert [f["flag_type"] for f in flags] == ["refiled_rejected_claim"]
        assert flags[0]["severity"] == "blocking"

    def test_different_destination_is_not_flagged(self) -> None:
        assert self._flags("Manhattan Beach CA", "Manhattan") == []
