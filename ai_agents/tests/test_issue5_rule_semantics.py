"""Issue 5 — rule semantics: validation, engine-derived descriptions, scope backstop.

* ``validate_rule_definition`` accepts every seeded rule (policy_content.RULES incl.
  the Issue-6 canonical HOTEL-CAP / MEALS-PD tier shape, and seed_all's CAR-CAP-DRAFT)
  and rejects shapes the engine would silently mis-enforce;
* ``describe_rule`` is pinned against REAL evaluator outcomes: for every seeded rule a
  hand-picked failing line is run through PolicyEvalTool + presubmit.verdicts_to_flags
  (independently of the describer's own probes) and the consequence sentence must
  match what actually happens;
* both are total on malformed legacy JSON;
* the category-scoped amount_cap (Option B) reaches only the listed categories.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from ai_agents.agents.expense_agent.presubmit import build_eval_item, verdicts_to_flags
from ai_agents.agents.policy_authoring import draft_rule_title
from ai_agents.agents.policy_rule_semantics import (
    GENERIC_DESCRIPTION,
    describe_rule,
    expense_reach,
    intent_scope_conflict,
    offers_justification,
    sanitize_reason,
    validate_rule_definition,
)
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool
from database.seeds.policy_content import RULES
from database.seeds.seed_all import CAR_CAP_DRAFT_RULE

RIDESHARE: dict[str, Any] = {
    "clause_ref": None,
    "rule_type": "amount_cap",
    "applies_to": "all",
    "definition": {
        "currency": "USD",
        "per": "item",
        "expense_categories": ["ground_transport"],
        "tiers": [{"tier": "standard", "cap": 75}],
        "exception_approver": "manager",
    },
}
SEEDED = [*RULES, CAR_CAP_DRAFT_RULE]
_BY_REF = {r["clause_ref"]: r for r in SEEDED}


def _ids(rules: list[dict[str, Any]]) -> list[str]:
    return [str(r["clause_ref"]) for r in rules]


def _entry(category: str, amount: float, **kw: Any) -> Any:
    from ai_agents.agents.expense_agent.categorize import DraftEntry

    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-03-02",
        vendor=kw.get("vendor"),
        description=None,
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


# ---------------------------------------------------------------------------
# validate_rule_definition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", [*SEEDED, RIDESHARE], ids=_ids([*SEEDED, RIDESHARE]))
def test_validate_accepts_every_seeded_rule(rule: dict[str, Any]) -> None:
    assert validate_rule_definition(rule["rule_type"], rule["applies_to"], rule["definition"]) == ()


def test_seeded_canonical_tiers_carry_issue6_keys() -> None:
    """The regression covers the canonical tier shape (tier / label / cities / default)."""
    hotel = _BY_REF["HOTEL-CAP"]["definition"]["tiers"]
    meals = _BY_REF["MEALS-PD"]["definition"]["rates"]
    for rows in (hotel, meals):
        assert any(r.get("default") is True for r in rows)
        assert any(r.get("cities") for r in rows)
        assert all("label" in r and "tier" in r for r in rows)


def test_validate_rejects_unknown_definition_key() -> None:
    defn = {**RIDESHARE["definition"], "requires_justification": True}
    errors = validate_rule_definition("amount_cap", "all", defn)
    assert any("requires_justification" in e and "not used" in e for e in errors)


_CAP = {"currency": "USD", "per": "item", "tiers": [{"tier": "standard", "cap": 75}]}
_CABIN_OK = [
    {"max_hours": 6, "allowed_classes": ["economy"]},
    {"min_hours": 6, "allowed_classes": ["economy", "business"]},
]


@pytest.mark.parametrize(
    ("rule_type", "applies_to", "definition", "needle"),
    [
        ("amount_cap", "flight", {**_CAP, "tiers": []}, "non-empty"),
        ("amount_cap", "flight", {**_CAP, "tiers": [{"tier": "s", "cap": 0}]}, "greater than 0"),
        ("amount_cap", "flight", {**_CAP, "tiers": [{"tier": "s", "cap": True}]}, "greater than"),
        (
            "amount_cap",
            "flight",
            {**_CAP, "tiers": [{"tier": "economy", "cap": 1500}, {"tier": "biz", "cap": 4000}]},
            "exactly one tier",
        ),
        ("amount_cap", "flight", {**_CAP, "currency": "EUR"}, "USD"),
        ("amount_cap", "flight", {**_CAP, "per": "trip"}, "not supported"),
        (
            "amount_cap",
            "hotel",
            {"per": "night", "tiers": [{"tier": "nyc", "cap": 300, "cities": ["New York"]}]},
            "default",
        ),
        (
            "amount_cap",
            "hotel",
            {
                "per": "night",
                "tiers": [
                    {"tier": "nyc", "cap": 300, "cities": ["New York"]},
                    {"tier": "other", "cap": 400},
                ],
            },
            "default: true",
        ),
        (
            "amount_cap",
            "hotel",
            {"per": "night", "tiers": [{"tier": "a", "cap": 300}, {"tier": "b", "cap": 200}]},
            "omit cities",
        ),
        ("amount_cap", "all", _CAP, "expense_categories"),
        ("amount_cap", "all", {**_CAP, "expense_categories": ["meals"]}, "may only name"),
        ("amount_cap", "car", {**_CAP, "expense_categories": ["ground_transport"]}, "only used"),
        ("amount_cap", "meals", _CAP, "per_diem"),
        ("amount_cap", "rail", _CAP, "rail"),
        ("amount_cap", "flight", {**_CAP, "exception_approver": "CFO"}, "exception_approver"),
        ("per_diem", "hotel", {"rates": [{"region": "domestic", "amount": 75}]}, "meals"),
        (
            "per_diem",
            "meals",
            {
                "rates": [
                    {"region": "domestic", "amount": 75},
                    {"region": "international", "amount": 100},
                ]
            },
            "international",
        ),
        (
            "cabin_class",
            "flight",
            {"tiers": [{"max_hours": 6, "allowed_classes": ["economy"]}]},
            "open-ended",
        ),
        (
            "cabin_class",
            "flight",
            {"tiers": [{"min_hours": 10, "allowed_classes": ["business"]}]},
            "without gaps",
        ),
        (
            "cabin_class",
            "flight",
            {
                "tiers": [
                    {"max_hours": 6, "allowed_classes": ["economy"]},
                    {"min_hours": 8, "allowed_classes": ["business"]},
                ]
            },
            "without gaps",
        ),
        ("cabin_class", "flight", {"tiers": [{"allowed_classes": ["lie-flat"]}]}, "cabins"),
        (
            "receipt_required",
            "all",
            {"threshold_usd": 25, "always_required_categories": ["yachts"]},
            "categories",
        ),
        ("receipt_required", "car", {"threshold_usd": 75}, "all expense lines"),
        ("preferred_supplier", "car", {"preferred_suppliers": ["Hertz"]}, "supplier"),
        (
            "preferred_supplier",
            "flight",
            {"preferred_suppliers": ["United"], "condition": "x"},
            "hotel",
        ),
        (
            "category_allow",
            "car",
            {"allowed_classes": ["SUV"], "excluded_classes": ["SUV"]},
            "both",
        ),
    ],
)
def test_validate_rejects_shapes_the_engine_would_mis_enforce(
    rule_type: str, applies_to: str, definition: dict[str, Any], needle: str
) -> None:
    errors = validate_rule_definition(rule_type, applies_to, definition)
    assert errors, (rule_type, applies_to, definition)
    assert any(needle.lower() in e.lower() for e in errors), errors


def test_valid_cabin_bands_pass() -> None:
    assert validate_rule_definition("cabin_class", "flight", {"tiers": _CABIN_OK}) == ()


_GARBAGE: list[Any] = [
    None,
    [],
    "x",
    {},
    {"cap": 50},
    {"max_amount": 500},
    {"tiers": "x"},
    {"tiers": [None, 3, "a"]},
    {"tiers": [{"tier": 1, "cap": "x", "cities": "Paris", "default": "yes"}]},
    {"rates": [{"tier": None, "amount": float("nan")}]},
    {"rates": "x", "exclusions": 3},
    {"preferred_suppliers": [None, 1], "within_pct": "ten"},
    {"allowed_classes": "SUV", "conditional_classes": [1]},
    {"threshold_usd": "25", "always_required_categories": None},
    {"tiers": [{"max_hours": "x", "allowed_classes": None}]},
    {"expense_categories": "ground_transport", "tiers": [{"tier": "s", "cap": 1}]},
]


@pytest.mark.parametrize("definition", _GARBAGE)
@pytest.mark.parametrize(
    ("rule_type", "applies_to"),
    [
        ("amount_cap", "all"),
        ("amount_cap", "hotel"),
        ("amount_cap", "meals"),
        ("per_diem", "meals"),
        ("cabin_class", "flight"),
        ("preferred_supplier", "hotel"),
        ("category_allow", "car"),
        ("receipt_required", "all"),
        ("bogus", "all"),
        (None, None),
    ],
)
def test_validate_describe_and_title_are_total(
    rule_type: Any, applies_to: Any, definition: Any
) -> None:
    errors = validate_rule_definition(rule_type, applies_to, definition)
    summary = describe_rule(rule_type, applies_to, definition)
    title = draft_rule_title(rule_type, applies_to, definition)
    assert isinstance(errors, tuple) and isinstance(summary, tuple) and isinstance(title, str)
    assert summary and all(isinstance(s, str) and s for s in summary)
    assert len(summary) <= 6
    if definition in (None, [], "x") or rule_type in ("bogus", None):
        assert errors
    if errors and summary != (GENERIC_DESCRIPTION,):
        assert summary[0].startswith("Warning:")


def test_legacy_malformed_rule_describes_with_warning_not_crash() -> None:
    summary = describe_rule("amount_cap", "meals", {"cap": 50})
    assert summary[0].startswith("Warning:")


# ---------------------------------------------------------------------------
# describe_rule pinned against real evaluator outcomes (independent probes)
# ---------------------------------------------------------------------------

# For each seeded rule: (failing EvalItem built by the engine's own builder, category,
# has_receipt).  Hand-picked here — NOT taken from the describer — so the test checks
# the description against what the engine really does.
_FAILING: dict[str | None, tuple[EvalItem, str, bool]] = {
    "AIR-CABIN": (
        dataclasses.replace(
            build_eval_item(_entry("airfare", 900.0), segment=None),
            cabin_class="business",
            duration_hours=3.0,
        ),
        "airfare",
        True,
    ),
    "AIR-PREF": (
        dataclasses.replace(
            build_eval_item(_entry("airfare", 400.0), segment=None), supplier="Ryanair"
        ),
        "airfare",
        True,
    ),
    "HOTEL-CAP": (build_eval_item(_entry("lodging", 161.0), segment=None), "lodging", True),
    "HOTEL-PREF": (
        dataclasses.replace(
            build_eval_item(_entry("lodging", 100.0), segment=None), supplier="Motel Nine"
        ),
        "lodging",
        True,
    ),
    "MEALS-PD": (build_eval_item(_entry("meals", 66.0), segment=None), "meals", True),
    "GROUND-CLASS": (
        EvalItem(item_type="car", vehicle_class="SUV", amount_usd=90.0),
        "ground_transport",
        True,
    ),
    "RECEIPT-REQ": (build_eval_item(_entry("other", 30.0), segment=None), "other", False),
    "CAR-CAP-DRAFT": (
        EvalItem(item_type="car", vehicle_class="Economy", amount_usd=76.0),
        "ground_transport",
        True,
    ),
    None: (
        build_eval_item(_entry("ground_transport", 76.0), segment=None),
        "ground_transport",
        True,
    ),
    # Product gap 2: a line that breaks each spend-cap / gift / mileage rule.
    "MEAL-05": (
        build_eval_item(
            dataclasses.replace(_entry("meals", 120.01), attendee_count=2), segment=None
        ),
        "meals",
        True,
    ),
    "ENT-02": (
        build_eval_item(
            dataclasses.replace(_entry("entertainment", 100.01), attendee_count=1), segment=None
        ),
        "entertainment",
        True,
    ),
    "ENT-04": (build_eval_item(_entry("gift", 100.01), segment=None), "gift", True),
    "MIL-01": (
        build_eval_item(
            dataclasses.replace(
                _entry("mileage", 7.01),
                mileage={"distance": "10", "unit": "mi", "commute_offset": "0"},
            ),
            segment=None,
        ),
        "mileage",
        False,
    ),
    "MIL-02": (
        build_eval_item(
            dataclasses.replace(_entry("mileage", 7.0), mileage={"distance": "10", "unit": "mi"}),
            segment=None,
        ),
        "mileage",
        False,
    ),
    "MIL-03": (
        build_eval_item(
            dataclasses.replace(
                _entry("mileage", 210.07),
                mileage={"distance": "300.1", "unit": "mi", "commute_offset": "0"},
            ),
            segment=None,
        ),
        "mileage",
        False,
    ),
}


def _engine_outcome(rule: dict[str, Any]) -> str:
    item, category, has_receipt = _FAILING[rule["clause_ref"]]
    assert PolicyEvalTool.rule_applies(
        rule["rule_type"], rule["applies_to"], rule["definition"], item
    )
    verdict = PolicyEvalTool().evaluate_candidate(
        rule["rule_type"], rule["applies_to"], rule["definition"], item
    )
    flags, _ = verdicts_to_flags(
        [verdict], entry_ref="x", has_receipt=has_receipt, category=category
    )
    if not flags:
        return "noted"
    if flags[0].severity == "advisory":
        return "advisory"
    justified, _ = verdicts_to_flags(
        [verdict],
        entry_ref="x",
        has_receipt=has_receipt,
        category=category,
        justification="Client dinner overran.",
    )
    return "justifiable" if justified[0].severity == "advisory" else "blocking"


_SENTENCE = {
    "justifiable": "blocks submission until the employee adds a written justification",
    "blocking": "blocks submission; a written justification does not unblock it",
    "advisory": "advisory only and never blocks submission",
    "noted": "noted but never flagged or blocked",
}


@pytest.mark.parametrize("rule", [*SEEDED, RIDESHARE], ids=_ids([*SEEDED, RIDESHARE]))
def test_describe_rule_consequence_matches_the_engine(rule: dict[str, Any]) -> None:
    outcome = _engine_outcome(rule)
    summary = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
    assert _SENTENCE[outcome] in summary, (outcome, summary)
    for other, text in _SENTENCE.items():
        if other != outcome and other != "blocking":
            assert text not in summary, (other, summary)
    assert offers_justification(rule["rule_type"], rule["applies_to"], rule["definition"]) is (
        outcome == "justifiable"
    )


def test_hotel_cap_summary_states_engine_tier_resolution() -> None:
    rule = _BY_REF["HOTEL-CAP"]
    tiers = rule["definition"]["tiers"]
    summary = " ".join(describe_rule("amount_cap", "hotel", rule["definition"]))
    ny = PolicyEvalTool.resolve_tier(tiers, "New York", "cap")
    other = PolicyEvalTool.resolve_tier(tiers, "Boise", "cap")
    unknown = PolicyEvalTool.resolve_tier(tiers, None, "cap")
    assert ny is not None and other is not None and unknown is not None
    assert "Tier 1 (New York" in summary and f"${ny['cap']:.0f}" in summary
    assert f"any other city (Tier 3): ${other['cap']:.0f}" in summary
    assert f"the lowest cap (${unknown['cap']:.0f}) applies" in summary


def test_meals_summary_states_rates_and_pooling() -> None:
    rule = _BY_REF["MEALS-PD"]
    summary = " ".join(describe_rule("per_diem", "meals", rule["definition"]))
    assert "$85/day" in summary and "$75/day" in summary and "$65/day" in summary
    assert "pooled over the trip's days" in summary
    assert "alcohol" in summary and "not checked automatically" in summary.lower()


@dataclasses.dataclass(frozen=True)
class _ProductCarSegment:
    """A car segment as the product stores it: POST /trips details, no vehicle_class."""

    details: dict[str, Any] = dataclasses.field(
        default_factory=lambda: {"pickup_location": "SFO", "confirmation": "HZ123"}
    )
    location: str | None = "San Francisco"
    supplier: str | None = "Hertz"
    start_datetime: str | None = None
    end_datetime: str | None = None


def _rideshare_flags(item: EvalItem, category: str, justification: str | None = None) -> Any:
    assert item.expense_category == category  # the engine sees the line's category
    verdict = PolicyEvalTool().evaluate_candidate(
        "amount_cap", "all", RIDESHARE["definition"], item
    )
    flags, _ = verdicts_to_flags(
        [verdict], entry_ref="x", has_receipt=True, category=category, justification=justification
    )
    return flags


def _product_shaped_ground_lines() -> list[tuple[str, EvalItem, str]]:
    from ai_agents.agents.expense_agent.categorize import category_for_expense_type

    car_cat = category_for_expense_type("car")
    rail_cat = category_for_expense_type("train")
    assert car_cat and rail_cat
    return [
        (
            "uber",
            build_eval_item(_entry("ground_transport", 92.0), segment=None),
            "ground_transport",
        ),
        (
            "hertz linked to a car segment",
            build_eval_item(_entry(car_cat, 240.0), segment=_ProductCarSegment()),
            car_cat,
        ),
        ("hertz receipt", build_eval_item(_entry(car_cat, 240.0), segment=None), car_cat),
        ("amtrak receipt", build_eval_item(_entry(rail_cat, 150.0), segment=None), rail_cat),
    ]


def test_rideshare_summary_is_honest_about_ground_transport() -> None:
    """Reviewer blocker: the summary must not claim car rentals / trains are excluded.

    Product-shaped lines (a car segment as POST /trips stores it — no vehicle_class — and
    receipts categorized by categorize.py) are run through the real engine; every one the
    engine blocks must be named in the summary finance approves.
    """
    summary = " ".join(
        describe_rule(RIDESHARE["rule_type"], RIDESHARE["applies_to"], RIDESHARE["definition"])
    )
    for name, item, category in _product_shaped_ground_lines():
        flags = _rideshare_flags(item, category)
        assert [(f.flag_type, f.severity) for f in flags] == [
            ("policy_violation", "blocking")
        ], name
        justified = _rideshare_flags(item, category, justification="Client site has no transit.")
        assert [f.severity for f in justified] == ["advisory"], name
    for word in ("taxi", "rideshare", "car rentals", "train tickets", "transit", "parking"):
        assert word in summary
    assert "not covered" not in summary
    assert "$75" in summary
    assert "car-rental segments (each at its full rental amount) are checked too" in summary


def test_no_product_path_records_a_vehicle_class() -> None:
    """describe_rule says Atlas never records a car segment's vehicle class; keep it true.

    If a planner / form / seed starts writing ``vehicle_class``, car-rental lines stop
    being ground-transport lines and describe_rule's ground-transport and car-rule
    sentences must be revisited.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    allowed = {
        "ai_agents/agents/expense_agent/presubmit.py",  # reads it (C2)
        "ai_agents/tools/policy_eval.py",  # reads it
        "ai_agents/agents/policy_rule_semantics.py",  # probes
        "ai_agents/agents/policy_authoring.py",  # round-trip probe item
    }
    offenders = []
    for base in (
        "backend/app",
        "ai_agents/agents",
        "ai_agents/tools",
        "database",
        "frontend/app",
        "frontend/components",
        "frontend/lib",
    ):
        for path in (root / base).rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "node_modules" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if rel in allowed:
                continue
            if "vehicle_class" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(rel)
    assert offenders == []


@pytest.mark.parametrize("ref", ["CAR-CAP-DRAFT", "GROUND-CLASS"])
def test_car_rule_summary_says_rentals_only_and_advisory(ref: str) -> None:
    rule = _BY_REF[ref]
    summary = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
    assert "taxi, rideshare, transit and parking expenses are not covered" in summary
    assert "never record a vehicle class" in summary
    assert "advisory only" in summary
    # Engine fact behind "in practice no expense line is checked": a product-shaped
    # car-rental line never reaches a car rule.
    for _name, item, _cat in _product_shaped_ground_lines():
        assert not PolicyEvalTool.rule_applies(
            rule["rule_type"], rule["applies_to"], rule["definition"], item
        )


def test_car_cap_per_day_is_disclosed_as_not_prorated() -> None:
    rule = _BY_REF["CAR-CAP-DRAFT"]
    summary = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
    assert "not divided by the number of days" in summary


def test_receipt_summary_discloses_unapplied_always_required_categories() -> None:
    rule = _BY_REF["RECEIPT-REQ"]
    summary = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
    assert "$25 or more" in summary
    # airfare/lodging lines are evaluated without their category (engine fact).
    item = build_eval_item(_entry("airfare", 10.0), segment=None)
    verdict = PolicyEvalTool().evaluate_candidate(
        "receipt_required", "all", rule["definition"], item
    )
    assert verdict.observed["receipt_required"] is False
    assert "airfare and lodging, but those lines under $25 are not checked" in summary


def test_hotel_pref_advisory_never_flags() -> None:
    rule = _BY_REF["HOTEL-PREF"]
    assert _engine_outcome(rule) == "noted"


# ---------------------------------------------------------------------------
# Option B — category-scoped amount_cap reach
# ---------------------------------------------------------------------------


def test_category_cap_reaches_only_ground_transport_lines() -> None:
    reach = expense_reach("amount_cap", "all", RIDESHARE["definition"])
    assert reach == ("ground_transport", "car_rental", "rail")
    for item in (
        EvalItem(item_type="flight", amount_usd=900.0),
        EvalItem(item_type="hotel", amount_usd=900.0, city="Austin"),
        EvalItem(item_type="car", amount_usd=900.0, vehicle_class="Economy"),
        EvalItem(item_type="receipt", amount_usd=900.0, expense_category="other"),
        EvalItem(item_type="receipt", amount_usd=900.0),
    ):
        assert not PolicyEvalTool.rule_applies("amount_cap", "all", RIDESHARE["definition"], item)


def test_category_filter_never_changes_an_unfiltered_all_rule() -> None:
    rule = _BY_REF["RECEIPT-REQ"]
    for item in (EvalItem(item_type="flight"), EvalItem(item_type="receipt")):
        assert PolicyEvalTool.rule_applies(
            rule["rule_type"], rule["applies_to"], rule["definition"], item
        )


def test_malformed_category_filter_reaches_nothing() -> None:
    defn = {**RIDESHARE["definition"], "expense_categories": "ground_transport"}
    item = EvalItem(item_type="receipt", amount_usd=90.0, expense_category="ground_transport")
    assert not PolicyEvalTool.rule_applies("amount_cap", "all", defn, item)


# ---------------------------------------------------------------------------
# intent_scope_conflict
# ---------------------------------------------------------------------------

_CAR = {"rule_type": "amount_cap", "applies_to": "car", "definition": _CAP}
_RECEIPT = {
    "rule_type": "receipt_required",
    "applies_to": "all",
    "definition": {"threshold_usd": 75, "consequence": "flagged_for_review"},
}


@pytest.mark.parametrize(
    "intent",
    [
        "Rideshare trips over $75 need a written business justification",
        "Uber and Lyft rides above $75",
        "Car rentals and taxis over $75",
        "Ground transportation over $75 needs approval",
    ],
)
def test_ground_transport_intent_on_a_car_rule_conflicts(intent: str) -> None:
    reason = intent_scope_conflict(intent, _CAR)
    assert reason and "ground-transport" in reason


def test_ground_transport_intent_on_the_category_cap_is_fine() -> None:
    assert (
        intent_scope_conflict(
            "Rideshare trips over $75 need a written business justification", RIDESHARE
        )
        is None
    )


@pytest.mark.parametrize(
    "candidate",
    [
        _RECEIPT,
        {**RIDESHARE, "definition": {**RIDESHARE["definition"], "exception_approver": "VP"}},
        _CAR,
    ],
)
def test_justification_intent_needs_a_justifiable_rule(candidate: dict[str, Any]) -> None:
    reason = intent_scope_conflict("Hotel stays above $75 need a written justification", candidate)
    assert reason and "justification" in reason


_CABIN = {
    "rule_type": "cabin_class",
    "applies_to": "flight",
    "definition": {
        "tiers": [
            {"max_hours": 6, "allowed_classes": ["economy"]},
            {"min_hours": 6, "allowed_classes": ["economy", "premium_economy"]},
        ],
        "override_approver": "VP",
    },
}


@pytest.mark.parametrize(
    ("intent", "candidate"),
    [
        ("Flights with a transit over 8 hours may fly premium economy", _CABIN),
        ("Hotel stays including parking are capped at $160 a night", None),
    ],
)
def test_transit_or_parking_in_another_targets_intent_is_not_a_conflict(
    intent: str, candidate: dict[str, Any] | None
) -> None:
    hotel = _BY_REF["HOTEL-CAP"]
    cand = candidate or {
        "rule_type": hotel["rule_type"],
        "applies_to": hotel["applies_to"],
        "definition": hotel["definition"],
    }
    assert intent_scope_conflict(intent, cand) is None


def test_bare_parking_intent_still_needs_ground_transport() -> None:
    reason = intent_scope_conflict("Parking over $40 needs manager sign-off", _CAR)
    assert reason and "ground-transport" in reason


def test_negated_justification_is_not_a_justification_request() -> None:
    allow = {
        "rule_type": "category_allow",
        "applies_to": "car",
        "definition": {"allowed_classes": ["Economy"], "excluded_classes": ["Luxury"]},
    }
    intent = "Luxury car rentals are never allowed; no justification accepted"
    assert intent_scope_conflict(intent, allow) is None
    assert intent_scope_conflict("Luxury car rentals need a justification", allow)


def test_no_conflict_for_plain_car_rental_intent() -> None:
    assert intent_scope_conflict("Car rentals over $150 a day", _CAR) is None


# ---------------------------------------------------------------------------
# sanitize_reason + titles
# ---------------------------------------------------------------------------


def test_sanitize_reason_strips_verdict_controls_and_bounds_length() -> None:
    assert sanitize_reason("UNFAITHFUL. The rule\x00 only\n\tflags **review**.") == (
        "The rule only flags review."
    )
    assert sanitize_reason("**FAITHFUL** — ok") == "ok"
    long = sanitize_reason("x" * 1000)
    assert len(long) == 300 and long.endswith("…")
    assert sanitize_reason(None) == ""


def test_canonical_tier_titles() -> None:
    hotel = _BY_REF["HOTEL-CAP"]
    meals = _BY_REF["MEALS-PD"]
    assert draft_rule_title("amount_cap", "hotel", hotel["definition"]) == (
        "Hotel cap — Tier 1 $300, Tier 2 $225, Tier 3 $160/night"
    )
    assert draft_rule_title("per_diem", "meals", meals["definition"]) == (
        "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day"
    )
    assert draft_rule_title("amount_cap", "all", RIDESHARE["definition"]) == (
        "Ground transport cap — Standard $75/item"
    )


def _migration_0015() -> Any:
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "database"
        / "migrations"
        / "versions"
        / "0015_canonical_policy_figures.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0015_issue5", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0015_frozen_definitions_validate() -> None:
    """The definitions migration 0015 writes into live databases are activatable."""
    mig = _migration_0015()
    assert validate_rule_definition("amount_cap", "hotel", mig.NEW_HOTEL_CAP) == ()
    assert validate_rule_definition("per_diem", "meals", mig.NEW_MEALS_PD) == ()
    assert "Tier 1 $300" in draft_rule_title("amount_cap", "hotel", mig.NEW_HOTEL_CAP)
    assert draft_rule_title("per_diem", "meals", mig.NEW_MEALS_PD) == (
        "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day"
    )
