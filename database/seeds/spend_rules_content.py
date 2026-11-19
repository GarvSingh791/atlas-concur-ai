"""Product gap 2 (policy2) — structured rules for MEAL-05, ENT-02, ENT-04, MIL-01..03.

Every figure is DERIVED from the canonical TEP-FY26 figures
(``database/seeds/corpus/figures.py``) so the rule engine, policy Q&A and the
corpus quote the same numbers.  ``policy_content.RULES`` appends these rows for
fresh databases; migration ``0019_spend_caps_mileage`` adds FROZEN copies to
existing databases (idempotently, never overwriting an admin-edited rule).

Clause → rule (``source_clause_ref`` is the TEP-FY26 clause id itself, so every
flag cites it):

  MEAL-05  per_head_cap     meals  team meals (>= 2 attendees): per-head dinner cap
                                   by city tier x attendees.
  ENT-02   per_head_cap     all    client entertainment lines: per-head cap by city
                                   tier x attendees; larger events need pre-approval.
  ENT-04   gift_disclosure  all    gifts over $100 need a disclosure (recipient,
                                   business purpose, government-official answer);
                                   government officials need Legal/Compliance
                                   pre-approval.
  MIL-01   mileage (rate)   all    amount = billable miles x the IRS rate.
  MIL-02   mileage (commute) all   normal commute miles deducted from the claim.
  MIL-03   mileage (air_cap) all   > 300 miles each way: capped at the lowest
                                   logical airfare unless air was impractical.

Tiers are stored LOWEST FIRST (Tier 3 default first) like HOTEL-CAP / MEALS-PD,
so a resolver that falls back to the first entry applies the most restrictive cap.
"""

from __future__ import annotations

from database.seeds.corpus import figures as F


def _tiers(caps: dict[str, int]) -> list[dict]:
    def cities(tier: str) -> dict:
        names = F.TIER1_CITIES if tier == "tier1" else F.TIER2_CITIES
        out: dict = {"cities": list(names)}
        aliases = {c: list(F.CITY_ALIASES[c]) for c in names if c in F.CITY_ALIASES}
        if aliases:
            out["aliases"] = aliases
        return out

    return [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "cap": caps["tier3"],
        },
        {"tier": "tier2", "label": "Tier 2", **cities("tier2"), "cap": caps["tier2"]},
        {"tier": "tier1", "label": "Tier 1", **cities("tier1"), "cap": caps["tier1"]},
    ]


TEAM_MEAL_CAP_DEFINITION: dict = {
    "currency": "USD",
    "per": "head",
    "corpus_clause_ref": "MEAL-05",
    "min_attendees": 2,
    "tiers": _tiers(F.TEAM_DINNER_CAP),
    "exception_approver": "manager",
}

CLIENT_MEAL_CAP_DEFINITION: dict = {
    "currency": "USD",
    "per": "head",
    "corpus_clause_ref": "ENT-02",
    "expense_categories": ["entertainment"],
    "tiers": _tiers(F.CLIENT_MEAL_CAP),
    "exception_approver": "manager",
}

GIFT_DISCLOSURE_DEFINITION: dict = {
    "currency": "USD",
    "corpus_clause_ref": "ENT-04",
    "threshold_usd": F.GIFT_DISCLOSURE_THRESHOLD,
    "expense_categories": ["gift", "entertainment"],
    "disclosure_categories": ["gift"],
    "government_official_approver": "Legal/Compliance",
}

MILEAGE_RATE_DEFINITION: dict = {
    "check": "rate",
    "currency": "USD",
    "rate_per_mile": F.MILEAGE_RATE_USD_PER_MILE,
    "rate_basis": "IRS standard business mileage rate",
    "expense_categories": ["mileage"],
    "corpus_clause_ref": "MIL-01",
}

MILEAGE_COMMUTE_DEFINITION: dict = {
    "check": "commute_offset",
    "expense_categories": ["mileage"],
    "corpus_clause_ref": "MIL-02",
}

MILEAGE_AIR_CAP_DEFINITION: dict = {
    "check": "air_cap",
    "max_one_way_miles": F.MILEAGE_AIR_CAP_MILES,
    "exception_approver": "manager",
    "expense_categories": ["mileage"],
    "corpus_clause_ref": "MIL-03",
}

SPEND_RULES: list[dict] = [
    {
        "clause_ref": "MEAL-05",
        "rule_type": "per_head_cap",
        "applies_to": "meals",
        "definition": TEAM_MEAL_CAP_DEFINITION,
    },
    {
        "clause_ref": "ENT-02",
        "rule_type": "per_head_cap",
        "applies_to": "all",
        "definition": CLIENT_MEAL_CAP_DEFINITION,
    },
    {
        "clause_ref": "ENT-04",
        "rule_type": "gift_disclosure",
        "applies_to": "all",
        "definition": GIFT_DISCLOSURE_DEFINITION,
    },
    {
        "clause_ref": "MIL-01",
        "rule_type": "mileage",
        "applies_to": "all",
        "definition": MILEAGE_RATE_DEFINITION,
    },
    {
        "clause_ref": "MIL-02",
        "rule_type": "mileage",
        "applies_to": "all",
        "definition": MILEAGE_COMMUTE_DEFINITION,
    },
    {
        "clause_ref": "MIL-03",
        "rule_type": "mileage",
        "applies_to": "all",
        "definition": MILEAGE_AIR_CAP_DEFINITION,
    },
]
