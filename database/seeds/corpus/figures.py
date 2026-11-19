"""Canonical numeric figures for the Helios Dynamics T&E corpus (R0-T2).

SINGLE SOURCE OF TRUTH.  Every dollar cap, day threshold, percentage, and city
tier used across the four corpus documents (policy / FAQ / how-to / reference)
is defined here ONCE and interpolated into the document prose.  This makes
cross-document numerical contradictions structurally impossible: if a figure
changes, it changes here and re-renders everywhere.

All values are ported from the authoritative policy (TEP-FY26, Part 4 of the
companion RAG research report) and are original synthetic content.

The corpus consistency test (test_corpus_consistency.py) re-derives the expected
tokens from these constants and greps the RENDERED document text of each doc to
prove agreement — it does not merely compare these constants to themselves.
"""

from __future__ import annotations

from config.constants.city_names import city_keys, norm_city

# ---------------------------------------------------------------------------
# Document identity
# ---------------------------------------------------------------------------
COMPANY: str = "Helios Dynamics, Inc."
POLICY_DOC_ID: str = "TEP-FY26"
POLICY_VERSION_LABEL: str = "4.0"  # human-facing label; policies.version (int) = 1
EFFECTIVE_FROM: str = "2026-01-01"  # ISO date → policies.effective_from

TRAVEL_DESK_EMAIL: str = "travel@heliosdynamics.com"
TRAVEL_DESK_PHONE: str = "+1-800-555-0123"
FINANCE_EMAIL: str = "expenses@heliosdynamics.com"
COMPLIANCE_EMAIL: str = "compliance@heliosdynamics.com"

# ---------------------------------------------------------------------------
# City tiers (policy DEF-05).  Tier 3 = all locations not listed below.
# ---------------------------------------------------------------------------
TIER1_CITIES: tuple[str, ...] = (
    "New York",
    "San Francisco",
    "London",
    "Tokyo",
    "Singapore",
    "Hong Kong",
    "Zurich",
    "Geneva",
    "Boston",
    "Los Angeles",
    "Paris",
)
TIER2_CITIES: tuple[str, ...] = (
    "Chicago",
    "Seattle",
    "Washington DC",
    "Toronto",
    "Sydney",
    "Dubai",
    "Frankfurt",
    "Amsterdam",
    "Dublin",
)

# Alternate spellings that name a listed city (matched like the city itself).
# NOT rendered into the corpus prose; consumed by the derived HOTEL-CAP /
# MEALS-PD rule definitions (database/seeds/policy_content.py) and city_tier().
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "New York": ("New York City", "NYC", "Manhattan"),
}


def city_tier(city: str | None) -> str:
    """The DEF-05 tier key ('tier1' / 'tier2' / 'tier3') of *city*.

    Matches the city part ('New York, NY' -> 'New York'), the whole name, or
    either without a trailing US ZIP / state code ('New York NY 10017'), ignoring
    case and punctuation, against TIER1/TIER2 cities and CITY_ALIASES — the same
    keys the policy evaluator uses (``config.constants.city_names``).
    Anything unlisted (or None) is Tier 3 ("all other locations").
    """
    if not city:
        return "tier3"
    keys = city_keys(city)
    for tier, cities in (("tier1", TIER1_CITIES), ("tier2", TIER2_CITIES)):
        for name in cities:
            names = (name, *CITY_ALIASES.get(name, ()))
            if any(norm_city(n) in keys for n in names):
                return tier
    return "tier3"


# ---------------------------------------------------------------------------
# Lodging nightly caps — room + tax (policy LOD-01), USD per night.
# ---------------------------------------------------------------------------
LODGING_CAP: dict[str, int] = {"tier1": 300, "tier2": 225, "tier3": 160}
LODGING_CAP_AUTO_EXCEPTION_PCT: int = 15  # LOD-02 ≤15% over cap auto-approved

# ---------------------------------------------------------------------------
# Meals & incidentals per diem (policy MEAL-01), USD per day.
# ---------------------------------------------------------------------------
PER_DIEM: dict[str, int] = {"tier1": 85, "tier2": 75, "tier3": 65}
PER_DIEM_PARTIAL_PCT: int = 75  # MEAL-02 departure/return days
TEAM_DINNER_CAP: dict[str, int] = {"tier1": 90, "tier2": 75, "tier3": 60}  # MEAL-05
CLIENT_MEAL_CAP: dict[str, int] = {"tier1": 150, "tier2": 120, "tier3": 100}  # ENT-02
GIFT_DISCLOSURE_THRESHOLD: int = 100  # ENT-04 gifts > $100 disclosed

# ---------------------------------------------------------------------------
# Receipts & documentation (policy Section 14).
# ---------------------------------------------------------------------------
RECEIPT_THRESHOLD: int = 25  # EXP-02 itemized receipt required for ≥ $25
AFFIDAVIT_MAX_AMOUNT: int = 75  # EXP-04 missing-receipt affidavit ≤ $75
AFFIDAVIT_MAX_PER_QUARTER: int = 3  # EXP-04 no more than 3× per quarter

# ---------------------------------------------------------------------------
# Deadlines & reimbursement timelines.
# ---------------------------------------------------------------------------
SUBMISSION_DEADLINE_DAYS: int = 30  # EXP-01 submit within 30 days of trip end
STALE_REPORT_DAYS: int = 60  # EXP-01 > 60 days → VP approval, may be taxable
REIMBURSEMENT_DAYS: str = "5-7"  # REIMB-01 business days
CARD_LOSS_REPORT_HOURS: int = 24  # CARD-04 report lost/stolen card within 24h
CARD_RECONCILE_DEADLINE_DAYS: int = 30  # CARD-03 reconcile within EXP-01 window
AUDIT_RETENTION_YEARS: int = 7  # POL-04 / DATA-01 reports retained 7 years

# ---------------------------------------------------------------------------
# Air travel.
# ---------------------------------------------------------------------------
ADVANCE_BOOKING_DOMESTIC_DAYS: int = 14  # PTA-02
ADVANCE_BOOKING_INTL_DAYS: int = 21  # PTA-02
TRIP_CHANGE_REAPPROVAL_PCT: int = 10  # PTA-05 re-approve if cost ↑ > 10%
PREFERRED_FARE_PCT: int = 10  # AIR-05 within 10% of lowest logical fare
LOWEST_FARE_TOLERANCE_DOMESTIC: int = 150  # AIR-06 USD
LOWEST_FARE_TOLERANCE_INTL: int = 400  # AIR-06 USD
CABIN_ECONOMY_MAX_HOURS: int = 6  # AIR-01 economy for flights < 6h
CABIN_PREMIUM_MIN_HOURS: int = 6  # AIR-02 premium economy 6–10h
CABIN_PREMIUM_MAX_HOURS: int = 10  # AIR-02
CABIN_BUSINESS_MIN_HOURS: int = 10  # AIR-03 business for flights > 10h
SVP_BUSINESS_MIN_HOURS: int = 6  # AIR-04 SVPs+ business on flights > 6h
LOUNGE_DELAY_EXCEPTION_HOURS: int = 4  # AIR-07 lounge / early boarding after delays > 4h

# ---------------------------------------------------------------------------
# Ground transport, rail, mileage.
# ---------------------------------------------------------------------------
RAIL_FIRST_MIN_HOURS: int = 3  # GRND-01 first/business rail for ≥ 3h trips
RAIL_PREFER_OVER_AIR_HOURS: int = 4  # GRND-01 / SUS-01 prefer rail where ≤ 4h
AIRPORT_PARKING_MAX_DAYS: int = 7  # GRND-03 economy lot up to 7 days
TIP_MAX_PCT: int = 20  # GRND-05 reasonable tips ≤ 20%
MILEAGE_AIR_CAP_MILES: int = 300  # MIL-03 trips > 300 miles capped to airfare
# MIL-01 says "the prevailing IRS standard mileage rate, updated annually" and
# names no figure, so this one is NOT rendered into the corpus prose.  It is
# the IRS standard business rate for 2025 (70 cents/mile), the most recent
# rate confirmed when this was built; finance updates it each year in the
# MIL-01 rule (rate_per_mile).  Kept as a string so it converts to an exact
# Decimal.  Consumed by policy_content.MIL-01 and the mileage amount computation.
MILEAGE_RATE_USD_PER_MILE: str = "0.70"


def usd(amount: int) -> str:
    """Render a whole-dollar USD figure as it appears in the corpus prose ('$300')."""
    return f"${amount}"
