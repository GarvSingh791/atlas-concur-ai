"""Product gap 2 / 8 — validation, summaries, titles and authoring for entry-level rules.

Hermetic: no DB, no LLM.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ai_agents.agents.entry_rule_semantics import narrow_ground_scope
from ai_agents.agents.expense_agent.entry_context import (
    ground_fields,
    receipt_line_items,
    receipt_parking_days,
    receipt_tip,
)
from ai_agents.agents.policy_authoring import draft_rule_title, probe_item
from ai_agents.agents.policy_rule_semantics import (
    describe_rule,
    intent_scope_conflict,
    validate_rule_definition,
)
from database.seeds.policy_content import (
    ENTRY_RULES,
    HOTEL_CAP_DEFINITION,
    MEALS_PD_DEFINITION,
    RULES,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

RIDESHARE_CAP: dict[str, Any] = {
    "rule_type": "amount_cap",
    "applies_to": "all",
    "definition": {
        "currency": "USD",
        "per": "item",
        "expense_categories": ["ground_transport"],
        "tiers": [{"tier": "standard", "cap": 50}],
        "exception_approver": "manager",
    },
    "source_clause_ref": None,
}


# ---------------------------------------------------------------------------
# Every seeded rule validates and is described precisely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", [*RULES, *ENTRY_RULES], ids=lambda r: r["clause_ref"])
def test_seeded_rules_are_valid(rule: dict) -> None:
    assert validate_rule_definition(rule["rule_type"], rule["applies_to"], rule["definition"]) == ()


@pytest.mark.parametrize(
    ("ref", "words", "title"),
    [
        ("GRND-01", ["rail", "3 hours", "First/Business"], "Rail class — Standard under 3h"),
        ("GRND-03", ["parking", "7 days", "economy lot"], "Airport parking — up to 7 days"),
        ("GRND-05", ["rideshare/taxi", "20%"], "Rideshare/taxi tips — up to 20% of the fare"),
    ],
)
def test_entry_rule_summary_and_title(ref: str, words: list[str], title: str) -> None:
    rule = next(r for r in ENTRY_RULES if r["clause_ref"] == ref)
    summary = " ".join(describe_rule(rule["rule_type"], rule["applies_to"], rule["definition"]))
    for word in words:
        assert word in summary
    assert "not covered" in summary and "could not verify" in summary
    assert draft_rule_title(rule["rule_type"], rule["applies_to"], rule["definition"]).startswith(
        title
    )


def test_hotel_and_meal_summaries_state_lod02_meal02_and_exclusions() -> None:
    hotel = " ".join(describe_rule("amount_cap", "hotel", HOTEL_CAP_DEFINITION))
    assert "LOD-02" in hotel and "15%" in hotel and "booked through the travel tool" in hotel
    meals = describe_rule("per_diem", "meals", MEALS_PD_DEFINITION)
    text = " ".join(meals)
    assert "MEAL-02" in text and "75%" in text and "same-day trip" in text
    assert "alcohol" in text and "MEAL-04" in text
    assert "Not checked automatically: the alcohol exclusion" not in text
    assert len(meals) <= 6


# ---------------------------------------------------------------------------
# Validation refuses shapes the engine would mis-enforce
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_type", "applies_to", "patch", "needle"),
    [
        ("amount_cap", "all", {"ground_subtypes": ["uber"]}, "ground_subtypes may only name"),
        ("amount_cap", "all", {"ground_subtypes": []}, "non-empty list"),
        (
            "amount_cap",
            "all",
            {"ground_subtypes": ["rail"], "expense_categories": ["other"]},
            "only used with applies_to 'all'",
        ),
        (
            "amount_cap",
            "all",
            {
                "ground_subtypes": [
                    "rideshare_taxi",
                    "car_rental",
                    "rail",
                    "transit",
                    "parking",
                ]
            },
            "drop it",
        ),
    ],
)
def test_ground_subtype_validation(
    rule_type: str, applies_to: str, patch: dict, needle: str
) -> None:
    defn = {**RIDESHARE_CAP["definition"], **patch}
    problems = " ".join(validate_rule_definition(rule_type, applies_to, defn))
    assert needle in problems


def test_tolerance_and_partial_day_validation() -> None:
    assert validate_rule_definition(
        "amount_cap", "hotel", {**HOTEL_CAP_DEFINITION, "auto_approve_over_cap_pct": 0}
    )
    assert validate_rule_definition(
        "amount_cap", "hotel", {**HOTEL_CAP_DEFINITION, "auto_approve_requires": "manager_ok"}
    )
    assert validate_rule_definition(
        "per_diem", "meals", {**MEALS_PD_DEFINITION, "partial_day_pct": 100}
    )


@pytest.mark.parametrize(
    ("index", "patch", "rule_type"),
    [
        (0, {"premium_min_hours": -1}, None),
        (1, {"max_days": 0}, None),
        (2, {"max_tip_pct": 120}, None),
        (2, {"ground_subtypes": ["rail"]}, None),
        (2, {"surprise": 1}, None),
        (1, {}, "per_diem"),
    ],
)
def test_kind_validation(index: int, patch: dict, rule_type: str | None) -> None:
    rule = ENTRY_RULES[index]
    defn = {**rule["definition"], **patch}
    assert validate_rule_definition(rule_type or rule["rule_type"], rule["applies_to"], defn)


# ---------------------------------------------------------------------------
# Authoring: rideshare intent narrows the broad ground cap (gap 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("Rideshare trips over $50 need a justification", ["rideshare_taxi"]),
        ("Taxi and Uber fares above $60 need manager approval", ["rideshare_taxi"]),
        ("Airport parking over $30 a day", ["parking"]),
        ("Train tickets over $200", ["rail"]),
        ("Taxi and parking over $40", ["rideshare_taxi", "parking"]),
    ],
)
def test_intent_naming_a_ground_kind_narrows_the_cap(intent: str, expected: list[str]) -> None:
    narrowed = narrow_ground_scope(intent, RIDESHARE_CAP)
    assert narrowed is not None
    assert narrowed["definition"]["ground_subtypes"] == expected
    assert "ground_subtypes" not in RIDESHARE_CAP["definition"]  # input untouched
    assert validate_rule_definition("amount_cap", "all", narrowed["definition"]) == ()
    assert intent_scope_conflict(intent, narrowed) is None


@pytest.mark.parametrize(
    "intent",
    [
        "Ground transportation over $100 needs a justification",
        "Any expense in other over $100",
        "Cap all local transport at $80",
    ],
)
def test_generic_ground_intent_keeps_the_broad_cap(intent: str) -> None:
    assert narrow_ground_scope(intent, RIDESHARE_CAP) is None


def test_narrowing_never_touches_other_shapes() -> None:
    hotel = {"rule_type": "amount_cap", "applies_to": "hotel", "definition": HOTEL_CAP_DEFINITION}
    assert narrow_ground_scope("Uber over $50", hotel) is None
    already = json.loads(json.dumps(RIDESHARE_CAP))
    already["definition"]["ground_subtypes"] = ["parking"]
    assert narrow_ground_scope("Uber over $50", already) is None


def test_narrowed_summary_says_precisely_what_it_covers() -> None:
    narrowed = narrow_ground_scope("Rideshare over $50", RIDESHARE_CAP)
    assert narrowed is not None
    summary = " ".join(describe_rule("amount_cap", "all", narrowed["definition"]))
    assert "Applies only to rideshare/taxi ground-transport expense lines" in summary
    # Review round 3: only lines whose kind the evidence confirms are excluded.
    assert (
        "car rental, rail, public transit, parking lines confirmed by that evidence are not "
        "covered" in summary
    )
    assert "still checked" in summary  # unclassified lines: fail safe, stated
    title = draft_rule_title("amount_cap", "all", narrowed["definition"])
    assert title == "Rideshare/taxi cap — Standard $50/item"
    probe = probe_item("all", narrowed["definition"])
    assert probe.ground_subtype == "rideshare_taxi" and probe.ground_verified


# ---------------------------------------------------------------------------
# Evidence extraction (trust boundary)
# ---------------------------------------------------------------------------


def test_typed_standard_class_never_clears_rail() -> None:
    assert ground_fields("Amtrak", "standard class")["travel_class"] is None
    assert ground_fields("Amtrak", "first class")["travel_class"] == "first"
    trusted = ground_fields("Amtrak", segment_type="rail", segment_class="Standard")
    assert trusted["travel_class"] == "standard"
    hours = ground_fields("Amtrak", "3h30m", segment_type="rail")
    assert hours["duration_hours"] == 3.5


def test_receipt_values_must_be_ocr_read() -> None:
    fields = {
        "line_items": [{"description": "Wine", "amount": 12}, {"amount": 3}, "junk"],
        "tip": 4.0,
        "parking_days": 5,
    }
    assert receipt_line_items(fields, {}) == (("Wine", 12.0),)
    assert receipt_line_items(fields, {"line_items": "user_edited"}) is None
    assert receipt_tip(fields, None) == 4.0
    assert receipt_tip(fields, {"tip": "ai_inferred"}) is None
    assert receipt_parking_days(fields, {}) == 5
    assert receipt_parking_days({"days": 2.5}, {}) is None


# ---------------------------------------------------------------------------
# Migration 0017 (static)
# ---------------------------------------------------------------------------


def _mig() -> Any:
    path = _REPO_ROOT / "database/migrations/versions/0017_entry_level_policy_rules.py"
    spec = importlib.util.spec_from_file_location("mig_0017", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mig_0015() -> Any:
    path = _REPO_ROOT / "database/migrations/versions/0015_canonical_policy_figures.py"
    spec = importlib.util.spec_from_file_location("mig_0015_static", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0017_chain_and_frozen_literals() -> None:
    mig, m15 = _mig(), _mig_0015()
    assert mig.revision == "0017_entry_level_policy_rules"
    # Integration: re-chained after 0016 (one linear history).
    assert mig.down_revision == "0016_report_trip_lifecycle"
    assert mig.OLD_HOTEL_CAP == m15.NEW_HOTEL_CAP
    assert mig.OLD_MEALS_PD == m15.NEW_MEALS_PD
    assert mig.NEW_HOTEL_CAP == HOTEL_CAP_DEFINITION
    assert mig.NEW_MEALS_PD == MEALS_PD_DEFINITION
    seeded = {
        (r["clause_ref"], r["rule_type"], r["applies_to"]): r["definition"] for r in ENTRY_RULES
    }
    frozen = {(ref, rt, at): d for ref, rt, at, d in mig.NEW_RULES}
    assert frozen == seeded
