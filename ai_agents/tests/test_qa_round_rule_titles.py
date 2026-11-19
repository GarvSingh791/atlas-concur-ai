"""QA round — readable, distinct titles for the product-gap policy rules (hermetic).

The browser QA run found the rule navigator listing the rules added by the product-gap
tracks under generic, duplicated titles ("Flight classes" for PTA-02 advance booking,
"Expense cap" for PTA-05, "Expense classes" for both EXP-01 and CARD-03, "Mileage
mileage" three times).  Titles are derived at read time from the stored rule
(``draft_rule_title``), so fixing the derivation retitles every existing row too —
there is no stored title column to migrate.
"""

from __future__ import annotations

import pytest

from ai_agents.agents.policy_authoring import draft_rule_title
from database.seeds.policy_content import ENTRY_RULES, PROCESS_RULES, RULES

ALL_RULES = [*RULES, *ENTRY_RULES, *PROCESS_RULES]


def _title(ref: str) -> str:
    rule = next(r for r in ALL_RULES if r["clause_ref"] == ref)
    return draft_rule_title(rule["rule_type"], rule["applies_to"], rule["definition"])


@pytest.mark.parametrize(
    ("ref", "title"),
    [
        ("EXP-01", "Report submission deadline — 30 days (EXP-01)"),
        ("PTA-02", "Advance booking — 14 days domestic, 21 days international (PTA-02)"),
        ("PTA-05", "Trip cost change re-approval — over 10% (PTA-05)"),
        ("AIR-06", "Lowest logical fare — within $150 domestic, $400 international (AIR-06)"),
        ("CARD-03", "Unreconciled card transactions — 30 days (CARD-03)"),
        ("MIL-01", "Mileage rate — $0.70/mile (MIL-01)"),
        ("MIL-02", "Mileage commute offset (MIL-02)"),
        ("MIL-03", "Mileage vs airfare — over 300 miles one way (MIL-03)"),
        ("MEAL-05", "Team meals per head (MEAL-05)"),
        ("ENT-02", "Client entertainment per head (ENT-02)"),
        ("ENT-04", "Gift disclosure — over $100 (ENT-04)"),
    ],
)
def test_new_rules_have_clear_titles(ref: str, title: str) -> None:
    assert _title(ref) == title


def test_every_seeded_rule_title_is_distinct() -> None:
    titles = [draft_rule_title(r["rule_type"], r["applies_to"], r["definition"]) for r in ALL_RULES]
    assert len(titles) == len(set(titles)), sorted(titles)


@pytest.mark.parametrize("bad", ["Mileage mileage", "Expense classes", "Flight classes"])
def test_no_generated_placeholder_titles_remain(bad: str) -> None:
    for rule in ALL_RULES:
        assert bad not in draft_rule_title(
            rule["rule_type"], rule["applies_to"], rule["definition"]
        )


def test_unsafe_clause_ref_is_never_echoed_into_a_title() -> None:
    """The clause suffix comes from the definition — only a clause-id shape is shown."""
    rule = next(r for r in PROCESS_RULES if r["clause_ref"] == "CARD-03")
    defn = {**rule["definition"], "corpus_clause_ref": "<script>alert(1)</script>"}
    title = draft_rule_title(rule["rule_type"], rule["applies_to"], defn)
    assert title == "Unreconciled card transactions — 30 days"


def test_edited_figures_follow_the_definition() -> None:
    """An admin-edited figure shows in the title (titles track the rule, not the seed)."""
    rule = next(r for r in PROCESS_RULES if r["clause_ref"] == "EXP-01")
    defn = {**rule["definition"], "deadline_days": 45}
    assert draft_rule_title(rule["rule_type"], rule["applies_to"], defn) == (
        "Report submission deadline — 45 days (EXP-01)"
    )


def test_malformed_definition_degrades_to_the_plain_name() -> None:
    rule = next(r for r in PROCESS_RULES if r["clause_ref"] == "PTA-02")
    defn = {"kind": "advance_booking", "domestic_days": "soon"}
    assert draft_rule_title(rule["rule_type"], rule["applies_to"], defn) == "Advance booking"
