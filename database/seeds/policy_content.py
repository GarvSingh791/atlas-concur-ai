"""Policy document content, clause chunks, and derived rule definitions for P3-T5 seed.

All numbers in the rule definitions exactly match the prose in DOCUMENT_TEXT.
CLAUSES has exactly 8 entries (each becomes one policy_embeddings row).
RULES has exactly 7 entries (each becomes one policy_rules row); the SUSTAIN clause
intentionally has no structured rule — it demonstrates clause-without-rule.

Issue 6 (one canonical figure set): the HOTEL-CAP and MEALS-PD rule definitions
and their DOCUMENT_TEXT sections are DERIVED from the published Helios TEP-FY26
figures (``database/seeds/corpus/figures.py``: LOD-01 lodging caps, MEAL-01 per
diem, DEF-05 city tiers), so the rule engine, the planner, approvals and policy
Q&A all quote the same numbers.  Tiers / rates are ordered LOWEST FIRST (Tier 3
default first) so an older evaluator that falls back to the first entry picks the
most restrictive value (fail closed during a rolling deploy).  Existing databases
are reconciled by migration 0015_canonical_policy_figures.

CLAUSES stays the FROZEN legacy RAG fixture (USD 250 / 180 / 75 / 100 wording)
that the eval-db retrieval baseline embeds; it is never served in production
(R0-T4 retired the stub embeddings) and must not be edited.

Each rule entry carries:
  clause_ref  : str  — matches a clause in CLAUSES; satisfies source_clause_ref FK
  rule_type   : str  — must be a valid PolicyRuleType value
  applies_to  : str  — must be a valid PolicyAppliesTo value
  definition  : dict — machine-evaluable JSONB body
"""

from __future__ import annotations

from database.seeds.corpus import figures as F
from database.seeds.grade_rules_content import GRADE_RULES
from database.seeds.spend_rules_content import SPEND_RULES

POLICY_NAME: str = "Atlas Global Travel & Expense Policy"
POLICY_VERSION: int = 1
POLICY_EFFECTIVE_FROM: str = "2026-01-01"  # ISO date string

_DOCUMENT_TEMPLATE: str = """ATLAS GLOBAL TRAVEL & EXPENSE POLICY
Version 1  |  Effective 2026-01-01

This policy governs all business travel and expense reimbursements for Atlas Demo Corp
employees.  All bookings must be made through the approved Atlas travel portal.

[AIR-CABIN] SECTION 1 — AIR TRAVEL CABIN CLASS
Cabin class is determined by scheduled flight duration (gate-to-gate):
  • Flights under 6 hours: Economy class required.
  • Flights 6–10 hours: Premium Economy permitted; Economy required unless booked
    within 21 days of departure.
  • Flights over 10 hours: Business class permitted.
Upgrades beyond the permitted tier require VP-level pre-approval.

[AIR-PREF] SECTION 2 — PREFERRED AIR CARRIERS
Travelers must book with preferred carriers (United Airlines, Delta Air Lines,
Lufthansa) when a fare on a preferred carrier is within 10% of the lowest
available fare.  Booking a non-preferred carrier when a preferred-carrier fare
is within the 10% threshold requires manager pre-approval and written justification.

[HOTEL-CAP] SECTION 3 — HOTEL NIGHTLY RATE CAPS
Reimbursable nightly hotel rates (room + tax) are capped by city tier:
  • Tier 1 ({tier1_cities}): USD {lod1}.
  • Tier 2 ({tier2_cities}): USD {lod2}.
  • Tier 3 (all other locations): USD {lod3}.
Rates exceeding the cap require manager pre-approval prior to booking.

[HOTEL-PREF] SECTION 4 — PREFERRED HOTEL PARTNERS
Travelers should book within the Marriott International or Hilton portfolio when
properties are available at the applicable capped rate in the destination city.
Preferred-hotel bookings accrue corporate loyalty points that offset future travel costs.

[MEALS-PD] SECTION 5 — MEAL AND INCIDENTALS PER DIEM
Daily meal and incidental allowances by city tier:
  • Tier 1 cities: USD {pd1} per day.
  • Tier 2 cities: USD {pd2} per day.
  • Tier 3 (all other locations): USD {pd3} per day.
Alcohol is never reimbursable regardless of context.  Group meals (3+ Atlas employees)
require manager approval and must be submitted with an attendee list.

[GROUND-CLASS] SECTION 6 — GROUND TRANSPORTATION
Rental cars must be booked in the Economy or Standard vehicle class unless 3 or more
colleagues share the vehicle (Intermediate permitted) or cargo requirements justify an
upgrade (pre-approval required).  Luxury, SUV, and specialty classes are not reimbursable
without VP pre-approval.

[RECEIPT-REQ] SECTION 7 — RECEIPT REQUIREMENTS
Itemized receipts are required for all expenses of USD 25 or more.  Regardless of amount,
receipts are always required for airfare and lodging.  Expenses without required receipts
will be flagged for review and may be denied.

[SUSTAIN] SECTION 8 — SUSTAINABILITY GUIDANCE
Atlas encourages lower-emission travel choices.  When scheduling permits, travelers
should consider direct flights over connecting itineraries and choose a lower-emission
flight option when it is within 10% of the cost of the preferred option and adds no
more than 2 hours to total travel time.  Sustainability scores are displayed in the
booking tool and are tracked at an organizational level.
"""

# Section labels / ordering are unchanged so the chunker still yields 8 chunks.
DOCUMENT_TEXT: str = _DOCUMENT_TEMPLATE.format(
    tier1_cities=", ".join(F.TIER1_CITIES),
    tier2_cities=", ".join(F.TIER2_CITIES),
    lod1=F.LODGING_CAP["tier1"],
    lod2=F.LODGING_CAP["tier2"],
    lod3=F.LODGING_CAP["tier3"],
    pd1=F.PER_DIEM["tier1"],
    pd2=F.PER_DIEM["tier2"],
    pd3=F.PER_DIEM["tier3"],
)

# ---------------------------------------------------------------------------
# Clause chunks (8 total) — each becomes one policy_embeddings row.
# clause_ref is the stable citation label used in source_clause_ref on rules.
# FROZEN legacy RAG fixture (eval-db retrieval baseline) — do NOT edit, even
# though the HOTEL-CAP / MEALS-PD wording predates the canonical figures.
# ---------------------------------------------------------------------------

CLAUSES: list[dict] = [
    {
        "clause_ref": "AIR-CABIN",
        "chunk_text": (
            "Air travel cabin class is determined by scheduled flight duration. "
            "Flights under 6 hours require Economy class. "
            "Flights 6 to 10 hours permit Premium Economy. "
            "Flights over 10 hours permit Business class. "
            "Upgrades beyond permitted tier require VP-level pre-approval."
        ),
    },
    {
        "clause_ref": "AIR-PREF",
        "chunk_text": (
            "Preferred air carriers are United Airlines, Delta Air Lines, and Lufthansa. "
            "Travelers must book with a preferred carrier when its fare is within 10 percent "
            "of the lowest available fare. "
            "Booking a non-preferred carrier when a preferred-carrier fare is within the "
            "10 percent threshold requires manager pre-approval and written justification."
        ),
    },
    {
        "clause_ref": "HOTEL-CAP",
        "chunk_text": (
            "Hotel nightly rate caps exclude taxes. "
            "Tier-1 cities (New York, San Francisco, London, Tokyo, Paris, Zurich): "
            "USD 250 per night. "
            "Domestic US cities (non-Tier-1): USD 180 per night. "
            "International high-cost cities (non-Tier-1): USD 300 per night. "
            "All other international cities: USD 200 per night. "
            "Rates exceeding the cap require manager pre-approval prior to booking."
        ),
    },
    {
        "clause_ref": "HOTEL-PREF",
        "chunk_text": (
            "Preferred hotel partners are Marriott International and Hilton. "
            "Travelers should book within the Marriott or Hilton portfolio when properties "
            "are available at the applicable capped rate in the destination city. "
            "Preferred-hotel bookings accrue corporate loyalty points."
        ),
    },
    {
        "clause_ref": "MEALS-PD",
        "chunk_text": (
            "Daily meal and incidentals allowance: USD 75 per day for domestic US travel; "
            "USD 100 per day for international travel. "
            "Alcohol is never reimbursable. "
            "Group meals with three or more Atlas employees require manager approval "
            "and an attendee list."
        ),
    },
    {
        "clause_ref": "GROUND-CLASS",
        "chunk_text": (
            "Rental cars must be booked in Economy or Standard class. "
            "Intermediate class is permitted when three or more colleagues share the vehicle. "
            "Luxury, SUV, and specialty classes are not reimbursable without VP pre-approval. "
            "Cargo-justified upgrades require pre-approval."
        ),
    },
    {
        "clause_ref": "RECEIPT-REQ",
        "chunk_text": (
            "Itemized receipts are required for all expenses of USD 25 or more. "
            "Receipts are always required for airfare and lodging regardless of amount. "
            "Expenses without required receipts will be flagged for review and may be denied."
        ),
    },
    {
        "clause_ref": "SUSTAIN",
        "chunk_text": (
            "Atlas encourages lower-emission travel choices. "
            "Travelers should prefer direct flights over connecting itineraries "
            "when scheduling permits. "
            "A lower-emission flight option should be chosen when it is within 10 percent of the "
            "cost of the preferred option and adds no more than 2 hours to total travel time. "
            "Sustainability scores are displayed in the booking tool."
        ),
    },
]

# ---------------------------------------------------------------------------
# Canonical HOTEL-CAP / MEALS-PD definitions — derived from corpus/figures.py.
# LOWEST tier first: an evaluator that falls back to the first entry (the
# pre-0015 code: no 'domestic' tier / no matching 'region' rate) then applies
# the most restrictive figure.  Frozen copies live in migration 0015.
# ---------------------------------------------------------------------------


def _tier_cities(tier: str) -> dict:
    cities = F.TIER1_CITIES if tier == "tier1" else F.TIER2_CITIES
    out: dict = {"cities": list(cities)}
    aliases = {c: list(F.CITY_ALIASES[c]) for c in cities if c in F.CITY_ALIASES}
    if aliases:
        out["aliases"] = aliases
    return out


HOTEL_CAP_DEFINITION: dict = {
    "currency": "USD",
    "per": "night",
    "basis": "room_plus_tax",
    "corpus_clause_ref": "LOD-01",
    "tiers": [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "cap": F.LODGING_CAP["tier3"],
        },
        {
            "tier": "tier2",
            "label": "Tier 2",
            **_tier_cities("tier2"),
            "cap": F.LODGING_CAP["tier2"],
        },
        {
            "tier": "tier1",
            "label": "Tier 1",
            **_tier_cities("tier1"),
            "cap": F.LODGING_CAP["tier1"],
        },
    ],
    "exception_approver": "manager",
    # Product gap 2 — LOD-02: <= 15% over the cap is auto-approved when booked through
    # the travel tool; > 15% requires manager approval (migration 0017 for live DBs).
    "auto_approve_over_cap_pct": F.LODGING_CAP_AUTO_EXCEPTION_PCT,
    "auto_approve_clause_ref": "LOD-02",
    "auto_approve_requires": "booked_through_tool",
}

MEALS_PD_DEFINITION: dict = {
    "currency": "USD",
    "per": "day",
    "corpus_clause_ref": "MEAL-01",
    "rates": [
        {
            "tier": "tier3",
            "label": "Tier 3",
            "description": "All other locations",
            "default": True,
            "amount": F.PER_DIEM["tier3"],
        },
        {
            "tier": "tier2",
            "label": "Tier 2",
            **_tier_cities("tier2"),
            "amount": F.PER_DIEM["tier2"],
        },
        {
            "tier": "tier1",
            "label": "Tier 1",
            **_tier_cities("tier1"),
            "amount": F.PER_DIEM["tier1"],
        },
    ],
    "exclusions": ["alcohol"],
    "group_meal_threshold": 3,
    "group_meal_approver": "manager",
    # Product gap 2 — MEAL-02 partial (departure / return) days and the MEAL-04 alcohol
    # exclusion clause (migration 0017 for live DBs).
    "partial_day_pct": F.PER_DIEM_PARTIAL_PCT,
    "partial_day_clause_ref": "MEAL-02",
    "exclusions_clause_ref": "MEAL-04",
}

# ---------------------------------------------------------------------------
# Derived policy rules (7 total — SUSTAIN has no structured rule).
# Each entry maps to one policy_rules row.
# ---------------------------------------------------------------------------

RULES: list[dict] = [
    {
        "clause_ref": "AIR-CABIN",
        "rule_type": "cabin_class",
        "applies_to": "flight",
        "definition": {
            "tiers": [
                {"max_hours": 6, "allowed_classes": ["economy"]},
                {
                    "min_hours": 6,
                    "max_hours": 10,
                    "allowed_classes": ["economy", "premium_economy"],
                },
                {"min_hours": 10, "allowed_classes": ["economy", "premium_economy", "business"]},
            ],
            "override_approver": "VP",
            # AIR-04 (product gap 2 stage 2): SVPs+ may book business on flights > 6 h.
            # Atlas records no grade -> an approver at that level confirms it.
            "role_exception_class": "business",
            "role_exception_min_hours": F.SVP_BUSINESS_MIN_HOURS,
            "role_exception_role": "SVP",
            "role_exception_clause_ref": "AIR-04",
        },
    },
    {
        "clause_ref": "AIR-PREF",
        "rule_type": "preferred_supplier",
        "applies_to": "flight",
        "definition": {
            "preferred_suppliers": ["United Airlines", "Delta Air Lines", "Lufthansa"],
            "within_pct": 10,
            "exception_approver": "manager",
        },
    },
    {
        "clause_ref": "HOTEL-CAP",
        "rule_type": "amount_cap",
        "applies_to": "hotel",
        "definition": HOTEL_CAP_DEFINITION,
    },
    {
        "clause_ref": "HOTEL-PREF",
        "rule_type": "preferred_supplier",
        "applies_to": "hotel",
        "definition": {
            "preferred_suppliers": ["Marriott International", "Hilton"],
            "condition": "when available at capped rate",
        },
    },
    {
        "clause_ref": "MEALS-PD",
        "rule_type": "per_diem",
        "applies_to": "meals",
        "definition": MEALS_PD_DEFINITION,
    },
    {
        "clause_ref": "GROUND-CLASS",
        "rule_type": "category_allow",
        "applies_to": "car",
        "definition": {
            "allowed_classes": ["Economy", "Standard"],
            "conditional_classes": [
                {
                    "class": "Intermediate",
                    "condition": "3+ colleagues sharing vehicle",
                }
            ],
            "excluded_classes": ["Luxury", "SUV", "Specialty"],
            "exception_approver": "VP",
        },
    },
    {
        "clause_ref": "RECEIPT-REQ",
        "rule_type": "receipt_required",
        "applies_to": "all",
        "definition": {
            "threshold_usd": 25,
            "always_required_categories": ["airfare", "lodging"],
            "consequence": "flagged_for_review",
            # EXP-04 (product gap 2 stage 2): a signed missing-receipt affidavit is
            # allowed up to $75 and no more than 3 times per quarter.
            "affidavit_max_usd": F.AFFIDAVIT_MAX_AMOUNT,
            "affidavit_max_per_quarter": F.AFFIDAVIT_MAX_PER_QUARTER,
            "affidavit_clause_ref": "EXP-04",
        },
    },
]

# Product gap 2 (policy2): MEAL-05 / ENT-02 / ENT-04 / MIL-01..03 structured rules
# (database/seeds/spend_rules_content.py; existing databases: migration 0019).
# They cite TEP-FY26 clause ids, not stub CLAUSES.
RULES.extend(SPEND_RULES)

# ---------------------------------------------------------------------------
# Entry-level rules (product gap 2) — they cite the canonical TEP-FY26 corpus clause
# (database/seeds/corpus/policy.py) directly; the frozen 8-section stub document has no
# section for them.  Evaluated by ai_agents/tools/entry_rules.py (``kind``).  Seeded for
# fresh databases; migration 0017_entry_level_policy_rules adds them to live ones.
# ---------------------------------------------------------------------------

RAIL_CLASS_DEFINITION: dict = {
    "kind": "rail_class",
    "corpus_clause_ref": "GRND-01",
    "expense_categories": ["ground_transport"],
    "ground_subtypes": ["rail"],
    "standard_classes": ["standard", "economy"],
    "premium_classes": ["first", "business"],
    "premium_min_hours": F.RAIL_FIRST_MIN_HOURS,
    "premium_condition": "cheaper than or comparable to flying",
    "exception_approver": "manager",
}

PARKING_DAYS_DEFINITION: dict = {
    "kind": "parking_days",
    "corpus_clause_ref": "GRND-03",
    "expense_categories": ["ground_transport"],
    "ground_subtypes": ["parking"],
    "max_days": F.AIRPORT_PARKING_MAX_DAYS,
    "lot": "economy",
    "exception_approver": "manager",
}

TIP_PCT_DEFINITION: dict = {
    "kind": "tip_pct",
    "corpus_clause_ref": "GRND-05",
    "expense_categories": ["ground_transport"],
    "ground_subtypes": ["rideshare_taxi"],
    "max_tip_pct": F.TIP_MAX_PCT,
    "exception_approver": "manager",
}

ENTRY_RULES: list[dict] = [
    {
        "clause_ref": "GRND-01",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": RAIL_CLASS_DEFINITION,
    },
    {
        "clause_ref": "GRND-03",
        "rule_type": "amount_cap",
        "applies_to": "all",
        "definition": PARKING_DAYS_DEFINITION,
    },
    {
        "clause_ref": "GRND-05",
        "rule_type": "amount_cap",
        "applies_to": "all",
        "definition": TIP_PCT_DEFINITION,
    },
]


# ---------------------------------------------------------------------------
# Process & timing rules (product gap 2, stage 2) — cite the canonical corpus clause.
# Never evaluated per expense line / trip option (``process_rules.is_process_rule``):
# the report, trip-approval and planner wiring (ai_agents/agents/process_checks.py)
# runs them.  Seeded for fresh databases; migration 0018_process_timing_rules adds
# them to live ones.
# ---------------------------------------------------------------------------

SUBMISSION_DEADLINE_DEFINITION: dict = {
    "kind": "submission_deadline",
    "corpus_clause_ref": "EXP-01",
    "deadline_days": F.SUBMISSION_DEADLINE_DAYS,
    "stale_days": F.STALE_REPORT_DAYS,
    "stale_approver": "VP",
}

ADVANCE_BOOKING_DEFINITION: dict = {
    "kind": "advance_booking",
    "corpus_clause_ref": "PTA-02",
    "domestic_days": F.ADVANCE_BOOKING_DOMESTIC_DAYS,
    "international_days": F.ADVANCE_BOOKING_INTL_DAYS,
    "exception_approver": "manager",
}

TRIP_CHANGE_DEFINITION: dict = {
    "kind": "trip_change_reapproval",
    "corpus_clause_ref": "PTA-05",
    "max_increase_pct": F.TRIP_CHANGE_REAPPROVAL_PCT,
    "exception_approver": "manager",
}

LOWEST_FARE_DEFINITION: dict = {
    "kind": "lowest_fare",
    "corpus_clause_ref": "AIR-06",
    "domestic_tolerance_usd": F.LOWEST_FARE_TOLERANCE_DOMESTIC,
    "international_tolerance_usd": F.LOWEST_FARE_TOLERANCE_INTL,
    "exception_approver": "manager",
}

CARD_RECONCILE_DEFINITION: dict = {
    "kind": "card_reconciliation",
    "corpus_clause_ref": "CARD-03",
    "deadline_days": F.CARD_RECONCILE_DEADLINE_DAYS,
}

PROCESS_RULES: list[dict] = [
    {
        "clause_ref": "EXP-01",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": SUBMISSION_DEADLINE_DEFINITION,
    },
    {
        "clause_ref": "PTA-02",
        "rule_type": "category_allow",
        "applies_to": "flight",
        "definition": ADVANCE_BOOKING_DEFINITION,
    },
    {
        "clause_ref": "PTA-05",
        "rule_type": "amount_cap",
        "applies_to": "all",
        "definition": TRIP_CHANGE_DEFINITION,
    },
    {
        "clause_ref": "AIR-06",
        "rule_type": "amount_cap",
        "applies_to": "flight",
        "definition": LOWEST_FARE_DEFINITION,
    },
    {
        "clause_ref": "CARD-03",
        "rule_type": "category_allow",
        "applies_to": "all",
        "definition": CARD_RECONCILE_DEFINITION,
    },
]

# Follow-up (grades): AIR-07 / LOD-05 / NR-01 are process ``kind`` rules too
# (``grade_entitlement``) — seeded with the stub; migration 0025 adds them to live data.
PROCESS_RULES.extend(GRADE_RULES)
