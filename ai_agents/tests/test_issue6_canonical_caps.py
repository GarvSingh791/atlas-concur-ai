"""Issue 6 — one canonical NYC hotel cap (and per diem) across every surface.

Hermetic: no DB, no LLM.  Rules are built in memory from the REAL seed
definitions (``database.seeds.policy_content.RULES``, derived from the published
TEP-FY26 figures in ``database.seeds.corpus.figures``) and from the FROZEN legacy
fixture (``evals/datasets/policy_rules.json``, eval-db) so both shapes are pinned.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)
from database.seeds.corpus import figures as F
from database.seeds.policy_content import DOCUMENT_TEXT, RULES

_REPO = Path(__file__).resolve().parents[2]
_LEGACY = json.loads((_REPO / "evals" / "datasets" / "policy_rules.json").read_text())


def _rule(
    ref: str, definition: dict[str, Any] | None = None, *, legacy: bool = False
) -> PolicyRule:
    if legacy:
        src = next(r for r in _LEGACY if r["source_clause_ref"] == ref)
        rule_type, applies_to, defn = src["rule_type"], src["applies_to"], src["definition"]
    else:
        src = next(r for r in RULES if r["clause_ref"] == ref)
        rule_type, applies_to, defn = src["rule_type"], src["applies_to"], src["definition"]
    return PolicyRule(
        id=uuid.uuid4(),
        policy_id=uuid.uuid4(),
        rule_type=PolicyRuleType(rule_type),
        applies_to=PolicyAppliesTo(applies_to),
        definition=definition if definition is not None else defn,
        source_clause_ref=ref,
        status=PolicyRuleStatus.active,
    )


def _eval(rule: PolicyRule, **item: Any) -> RuleVerdict:
    return PolicyEvalTool()._evaluate_rule(rule, EvalItem(**item))


def _hotel(**item: Any) -> RuleVerdict:
    return _eval(_rule("HOTEL-CAP"), item_type="hotel", **item)


def _meals(**item: Any) -> RuleVerdict:
    return _eval(_rule("MEALS-PD"), item_type="meals", **item)


# ---------------------------------------------------------------------------
# One figure set: the seeded rules ARE the corpus figures
# ---------------------------------------------------------------------------


class TestSeedRulesDeriveFromCorpusFigures:
    def test_hotel_cap_matches_lod01_figures(self) -> None:
        defn = next(r for r in RULES if r["clause_ref"] == "HOTEL-CAP")["definition"]
        by_tier = {t["tier"]: t for t in defn["tiers"]}
        assert {k: by_tier[k]["cap"] for k in by_tier} == F.LODGING_CAP
        assert by_tier["tier1"]["cities"] == list(F.TIER1_CITIES)
        assert by_tier["tier2"]["cities"] == list(F.TIER2_CITIES)
        assert by_tier["tier3"]["default"] is True and "cities" not in by_tier["tier3"]
        assert defn["exception_approver"] == "manager"

    def test_meals_pd_matches_meal01_figures(self) -> None:
        defn = next(r for r in RULES if r["clause_ref"] == "MEALS-PD")["definition"]
        by_tier = {t["tier"]: t for t in defn["rates"]}
        assert {k: by_tier[k]["amount"] for k in by_tier} == F.PER_DIEM
        assert by_tier["tier1"]["cities"] == list(F.TIER1_CITIES)
        assert by_tier["tier3"]["default"] is True

    def test_tiers_are_stored_lowest_first(self) -> None:
        """Rolling-deploy fail-safe: an evaluator that falls back to entry [0] gets Tier 3."""
        for ref, key, values in (
            ("HOTEL-CAP", "tiers", "cap"),
            ("MEALS-PD", "rates", "amount"),
        ):
            rows = next(r for r in RULES if r["clause_ref"] == ref)["definition"][key]
            assert rows[0]["tier"] == "tier3"
            assert [r[values] for r in rows] == sorted(r[values] for r in rows)

    def test_stub_document_sections_quote_the_canonical_figures(self) -> None:
        hotel = DOCUMENT_TEXT[
            DOCUMENT_TEXT.index("[HOTEL-CAP]") : DOCUMENT_TEXT.index("[HOTEL-PREF]")
        ]
        meals = DOCUMENT_TEXT[
            DOCUMENT_TEXT.index("[MEALS-PD]") : DOCUMENT_TEXT.index("[GROUND-CLASS]")
        ]
        for cap in F.LODGING_CAP.values():
            assert f"USD {cap}." in hotel
        for rate in F.PER_DIEM.values():
            assert f"USD {rate} per day" in meals
        assert "USD 250" not in hotel and "USD 180" not in hotel
        assert "USD 75 per day" in meals  # Tier 2 is 75 — but never "domestic"
        assert "Domestic" not in meals

    def test_city_tier_helper(self) -> None:
        assert F.city_tier("New York, NY") == "tier1"
        assert F.city_tier("NYC") == "tier1"
        assert F.city_tier("Chicago, IL") == "tier2"
        assert F.city_tier("Washington, D.C.") == "tier2"
        assert F.city_tier("Austin") == "tier3"
        assert F.city_tier(None) == "tier3"


# ---------------------------------------------------------------------------
# HOTEL-CAP with the canonical definition
# ---------------------------------------------------------------------------


class TestCanonicalHotelCap:
    def test_nyc_hotel_uses_tier1_300(self) -> None:
        over = _hotel(amount_usd=330.0, city="New York, NY", nights=3)
        assert over.in_policy is False
        assert over.observed["cap"] == 300.0 and over.observed["tier"] == "tier1"
        assert "Nightly rate 330.00 USD (3 nights) exceeds the Tier 1 cap 300.00 USD" in over.reason
        assert _hotel(amount_usd=299.0, city="New York, NY", nights=3).in_policy is True
        assert _hotel(amount_usd=300.0, city="New York", nights=1).in_policy is True

    @pytest.mark.parametrize("city", ["NYC", "New York City", "new york, ny", "Manhattan"])
    def test_nyc_aliases_resolve_to_tier1(self, city: str) -> None:
        assert _hotel(amount_usd=300.0, city=city, nights=1).observed["tier"] == "tier1"

    def test_chicago_is_tier2_225(self) -> None:
        v = _hotel(amount_usd=230.0, city="Chicago, IL", nights=2)
        assert v.in_policy is False and v.observed["cap"] == 225.0
        assert _hotel(amount_usd=225.0, city="Chicago", nights=2).in_policy is True

    @pytest.mark.parametrize("city", ["Washington DC", "Washington, DC", "Washington, D.C."])
    def test_washington_dc_variants_match_tier2(self, city: str) -> None:
        assert _hotel(amount_usd=200.0, city=city, nights=1).observed["tier"] == "tier2"

    def test_unlisted_city_is_tier3(self) -> None:
        v = _hotel(amount_usd=170.0, city="Austin, TX", nights=1)
        assert v.in_policy is False and v.observed["cap"] == 160.0
        assert v.observed["tier"] == "tier3"

    def test_no_city_uses_lowest_tier3_and_says_so(self) -> None:
        v = _hotel(amount_usd=170.0, nights=1)
        assert v.in_policy is False and v.observed["cap"] == 160.0
        assert "city could not be verified" in v.reason
        assert "lowest cap (Tier 3, 160.00 USD per night)" in v.reason
        assert "Attach the hotel folio" in v.reason

    def test_in_policy_reason_uses_tier_label(self) -> None:
        assert "(tier: Tier 1)" in _hotel(amount_usd=200.0, city="London", nights=1).reason

    def test_tier_order_does_not_matter(self) -> None:
        defn = next(r for r in RULES if r["clause_ref"] == "HOTEL-CAP")["definition"]
        reversed_defn = {**defn, "tiers": list(reversed(defn["tiers"]))}
        for city in (None, "New York", "Chicago", "Austin"):
            a = _eval(_rule("HOTEL-CAP"), item_type="hotel", amount_usd=250.0, city=city)
            b = _eval(
                _rule("HOTEL-CAP", reversed_defn), item_type="hotel", amount_usd=250.0, city=city
            )
            assert (a.in_policy, a.observed["cap"], a.observed["tier"]) == (
                b.in_policy,
                b.observed["cap"],
                b.observed["tier"],
            )


# ---------------------------------------------------------------------------
# Claimed nights / city (wording + pending verification, never clears a rule)
# ---------------------------------------------------------------------------


class TestClaimedHotelFacts:
    def test_claimed_nights_within_cap_is_still_a_violation_marked_pending(self) -> None:
        v = _hotel(amount_usd=750.0, city="New York, NY", claimed_nights=3)
        assert v.in_policy is False  # a claim never clears the rule
        assert v.observed["claimed_nightly_usd"] == 250.0
        assert v.observed["within_cap_if_verified"] is True
        assert v.observed["unverified"] == ["nights"]
        assert "250.00 USD per night" in v.reason and "300.00 USD per night" in v.reason
        assert "nights could not be verified" in v.reason
        assert "approver must confirm the stay length" in v.reason

    def test_claimed_nights_still_over_cap(self) -> None:
        v = _hotel(amount_usd=990.0, city="New York, NY", claimed_nights=3)
        assert v.in_policy is False
        assert v.observed["within_cap_if_verified"] is False
        assert "unverified" not in v.observed
        assert "330.00 USD per night, which exceeds the Tier 1 cap 300.00" in v.reason

    def test_verified_nights_ignore_claimed_nights(self) -> None:
        v = _hotel(amount_usd=450.0, city="New York", nights=1, claimed_nights=5)
        assert "claimed_nights" not in v.observed
        assert v.observed["amount_usd"] == 450.0

    def test_claimed_city_text_names_the_tier_cap(self) -> None:
        """The Q3 re-test: NYC lodging 3 nights 750 -> 250/night vs the NYC 300 cap."""
        v = _hotel(
            amount_usd=750.0,
            claimed_nights=3,
            claimed_city_text="Hotel - 3 nights @ 250.00/night NYC",
        )
        assert v.in_policy is False
        assert v.observed["cap"] == 160.0  # the applied cap is still the lowest
        assert v.observed["claimed_cap"] == 300.0
        assert v.observed["unverified"] == ["nights", "city"]
        assert "within the Tier 1 New York cap of $300/night if the city is confirmed" in v.reason
        assert "250.00 USD per night" in v.reason

    def test_claimed_city_over_its_cap_names_both(self) -> None:
        v = _hotel(amount_usd=400.0, nights=1, claimed_city="New York, NY")
        assert v.observed["within_cap_if_verified"] is False
        assert "(claimed: 'New York, NY')" in v.reason
        assert "would also exceed the Tier 1 New York cap of $300/night" in v.reason

    def test_unlisted_claimed_city_adds_nothing(self) -> None:
        v = _hotel(amount_usd=170.0, nights=1, claimed_city="Austin, TX")
        assert "claimed_cap" not in v.observed
        assert "within_cap_if_verified" not in v.observed

    def test_several_cities_in_text_pick_the_lowest(self) -> None:
        v = _hotel(amount_usd=220.0, nights=1, claimed_city_text="Flew NYC to Chicago")
        assert v.observed["claimed_tier"] == "tier2"
        assert v.observed["within_cap_if_verified"] is True

    def test_city_name_must_be_a_whole_word(self) -> None:
        v = _hotel(amount_usd=200.0, nights=1, claimed_city_text="Parish hotel Bostonian")
        assert "claimed_city" not in v.observed

    @pytest.mark.parametrize("bad", [0, -2, True])
    def test_invalid_claimed_counts_are_ignored(self, bad: Any) -> None:
        v = _hotel(amount_usd=750.0, city="New York", claimed_nights=bad)
        assert "claimed_nights" not in v.observed
        m = _meals(amount_usd=200.0, claimed_days=bad)
        assert "claimed_days" not in m.observed

    def test_planner_quoted_rate_with_requested_nights(self) -> None:
        v = _hotel(
            amount_usd=383.0,
            city="New York",
            nights=3,
            amount_is_nightly_rate=True,
            nights_source="requested",
            stay_amount_usd=1149.0,
        )
        assert "Nightly rate 383.00 USD (3 nights) exceeds the Tier 1 cap 300.00" in v.reason
        assert "could not be verified" not in v.reason


# ---------------------------------------------------------------------------
# MEALS-PD by tier
# ---------------------------------------------------------------------------


class TestCanonicalMealsPerDiem:
    def test_tier_rates(self) -> None:
        assert _meals(amount_usd=85.0, city="New York, NY").in_policy is True
        assert _meals(amount_usd=86.0, city="New York, NY").observed["rate"] == 85.0
        assert _meals(amount_usd=75.0, city="Chicago").in_policy is True
        assert _meals(amount_usd=66.0).observed["rate"] == 65.0

    def test_region_is_ignored_in_tier_mode(self) -> None:
        v = _meals(amount_usd=90.0, region="international")
        assert v.observed["rate"] == 65.0 and "region" not in v.observed

    def test_claimed_days_per_day_average(self) -> None:
        v = _meals(amount_usd=205.5, claimed_days=3)
        assert v.in_policy is False
        assert v.observed["claimed_daily_usd"] == 68.5
        assert v.observed["within_cap_if_verified"] is False
        assert "68.50 USD per day, which exceeds the Tier 3 per diem 65.00" in v.reason
        assert "Meal city could not be verified" in v.reason

    def test_claimed_days_within_verified_city_rate_is_pending(self) -> None:
        v = _meals(amount_usd=210.0, claimed_days=3, city="Chicago, IL")
        assert v.observed["unverified"] == ["days"]
        assert "70.00 USD per day — within the Tier 2 per diem 75.00" in v.reason

    def test_unknown_days_compares_daily_rate_and_asks_for_days(self) -> None:
        v = _meals(amount_usd=205.5)
        assert v.observed["amount_usd"] == 205.5 and v.observed["rate"] == 65.0
        assert 'state the number of days in the description (e.g. "3 days")' in v.reason

    def test_verified_days(self) -> None:
        v = _meals(amount_usd=68.5, days=3, stay_amount_usd=205.5, city="Austin")
        assert v.observed["days"] == 3 and v.observed["total_usd"] == 205.5
        assert "Daily meal average 68.50 USD (205.50 USD over 3 days)" in v.reason

    def test_group_meal_logic_unchanged(self) -> None:
        v = _meals(amount_usd=50.0, headcount=4)
        assert v.in_policy is True and v.requires_approver == "manager"
        assert "Group meal (4 attendees) requires manager approval." in v.reason


# ---------------------------------------------------------------------------
# Legacy shape (eval-db fixture) resolves exactly as before
# ---------------------------------------------------------------------------


class TestLegacyShapeUnchanged:
    @pytest.mark.parametrize(
        ("city", "tier", "cap"),
        [
            ("New York", "tier1", 250.0),
            ("Paris, France", "tier1", 250.0),
            ("New York City", "domestic", 180.0),  # look-alike trap: not listed
            ("Boston", "domestic", 180.0),
            ("Smalltown", "domestic", 180.0),
            (None, "domestic", 180.0),
        ],
    )
    def test_hotel_tier(self, city: str | None, tier: str, cap: float) -> None:
        v = _eval(_rule("HOTEL-CAP", legacy=True), item_type="hotel", amount_usd=200.0, city=city)
        assert (v.observed["tier"], v.observed["cap"]) == (tier, cap)
        assert set(v.observed) == {"amount_usd", "cap", "currency", "tier", "city"}

    @pytest.mark.parametrize(
        ("region", "rate"), [(None, 75.0), ("domestic", 75.0), ("international", 100.0)]
    )
    def test_meals_region(self, region: str | None, rate: float) -> None:
        v = _eval(_rule("MEALS-PD", legacy=True), item_type="meals", amount_usd=60.0, region=region)
        assert v.observed == {"amount_usd": 60.0, "rate": rate, "region": region or "domestic"}


# ---------------------------------------------------------------------------
# Rolling deploy: the PRE-0015 evaluator reading the NEW definitions fails closed
# ---------------------------------------------------------------------------


def _old_resolve_hotel_tier(defn: dict, city: str | None) -> dict | None:
    """Verbatim copy of the pre-Issue-6 PolicyEvalTool._resolve_hotel_tier."""
    tiers = defn.get("tiers", [])
    if city:
        city_normalized = city.split(",")[0].strip()
        for tier in tiers:
            if tier.get("tier") == "tier1":
                if any(city_normalized.lower() == c.lower() for c in tier.get("cities", [])):
                    return tier
    for tier in tiers:
        if tier.get("tier") == "domestic":
            return tier
    return tiers[0] if tiers else None


def _old_per_diem_rate(defn: dict, region: str | None) -> float:
    """Verbatim rate lookup of the pre-Issue-6 PolicyEvalTool._eval_per_diem."""
    region = region or "domestic"
    for r in defn.get("rates", []):
        if r.get("region") == region:
            return float(r["amount"])
    return float(defn["rates"][0]["amount"])


def test_old_resolver_on_new_definition_fails_closed() -> None:
    hotel = next(r for r in RULES if r["clause_ref"] == "HOTEL-CAP")["definition"]
    meals = next(r for r in RULES if r["clause_ref"] == "MEALS-PD")["definition"]
    # No city / unlisted / Tier 2 (the old code knows no Tier 2): the most restrictive.
    for city in (None, "Austin", "Chicago"):
        tier = _old_resolve_hotel_tier(hotel, city)
        assert tier is not None and tier["cap"] == F.LODGING_CAP["tier3"], city
    # A listed Tier 1 city still gets its own (correct) cap.
    tier = _old_resolve_hotel_tier(hotel, "New York, NY")
    assert tier is not None and tier["cap"] == F.LODGING_CAP["tier1"]
    for region in (None, "domestic", "international"):
        assert _old_per_diem_rate(meals, region) == F.PER_DIEM["tier3"]
