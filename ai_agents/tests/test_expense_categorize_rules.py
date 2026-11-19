"""Hermetic tests for the rule-based expense categorizer (P8 C1, FR-X3).

No database, no LLM, no network.  Covers every signal tier per category,
whole-word regressions for the substring bug in categorize._keyword_category,
conflict/empty/invalid handling, enum validity, and the no-LLM import fence.
"""

from __future__ import annotations

import pytest

import ai_agents.agents.expense_agent.rule_categorizer as rule_mod
from ai_agents.agents.expense_agent.rule_categorizer import (
    CATEGORIZE_CONFIDENCE_THRESHOLD,
    CONF_BRAND,
    CONF_KEYWORD,
    CONF_STRUCTURED,
    RuleCategorization,
    categorize_by_rules,
    is_valid_category,
)
from database.models.expense import ExpenseCategory

_VALID = {c.value for c in ExpenseCategory}


def _rules(
    merchant: str | None = None,
    description: str | None = None,
    expense_type: str | None = None,
    mcc: str | int | None = None,
) -> RuleCategorization:
    return categorize_by_rules(
        merchant=merchant, description=description, expense_type=expense_type, mcc=mcc
    )


def test_threshold_constant_is_080() -> None:
    assert CATEGORIZE_CONFIDENCE_THRESHOLD == 0.80


# ---------------------------------------------------------------------------
# Tier 1 — receipt expense_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expense_type", "expected"),
    [
        ("flight", "airfare"),
        ("Hotel", "lodging"),
        ("dining", "meals"),
        ("taxi", "ground_transport"),
    ],
)
def test_expense_type_tier(expense_type: str, expected: str) -> None:
    r = _rules(expense_type=expense_type)
    assert r.category == expected
    assert r.confidence == CONF_STRUCTURED
    assert r.signal == "expense_type"


def test_expense_type_other_is_not_a_signal() -> None:
    r = _rules(expense_type="other")
    assert r.signal == "none"
    assert r.confidence == 0.0


def test_expense_type_beats_text() -> None:
    r = _rules(merchant="Uber", expense_type="hotel")
    assert r.category == "lodging"
    assert r.signal == "expense_type"


# ---------------------------------------------------------------------------
# Tier 2 — MCC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mcc", "expected"),
    [
        (3000, "airfare"),
        ("4511", "airfare"),
        (7011, "lodging"),
        ("3600", "lodging"),
        (4121, "ground_transport"),
        ("7512", "ground_transport"),
        (5812, "meals"),
        ("5814", "meals"),
    ],
)
def test_mcc_tier(mcc: str | int, expected: str) -> None:
    r = _rules(mcc=mcc)
    assert r.category == expected
    assert r.confidence == CONF_STRUCTURED
    assert r.signal == "mcc"
    assert r.matched == f"mcc:{int(mcc)}"


@pytest.mark.parametrize("mcc", ["abcd", "58", "123456", "", " ", -5812, 9999, True])
def test_invalid_or_unknown_mcc_is_ignored(mcc: object) -> None:
    r = categorize_by_rules(merchant=None, description=None, mcc=mcc)  # type: ignore[arg-type]
    assert r.signal == "none"
    assert r.category == "other"


def test_expense_type_and_mcc_disagree_is_conflict() -> None:
    r = _rules(expense_type="hotel", mcc=5812)
    assert r.signal == "conflict"
    assert r.confidence < CATEGORIZE_CONFIDENCE_THRESHOLD
    assert r.category == "lodging"  # expense_type is the best guess


# ---------------------------------------------------------------------------
# Tier 3 — merchant brands (whole word)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("merchant", "expected"),
    [
        ("Marriott Downtown", "lodging"),
        ("DELTA AIR LINES", "airfare"),
        ("American Airlines", "airfare"),
        ("United Airlines", "airfare"),
        ("Delta Airlines", "airfare"),
        ("Ritz-Carlton Chicago", "lodging"),
        ("Amtrak", "ground_transport"),
        ("Uber *Trip", "ground_transport"),
        ("Enterprise Rent-A-Car", "ground_transport"),
        ("Starbucks #1234", "meals"),
        ("McDonald's", "meals"),
    ],
)
def test_brand_tier(merchant: str, expected: str) -> None:
    r = _rules(merchant=merchant)
    assert r.category == expected
    assert r.confidence == CONF_BRAND
    assert r.signal == "merchant"
    assert r.confidence >= CATEGORIZE_CONFIDENCE_THRESHOLD


def test_brand_matched_is_rule_key_not_raw_text() -> None:
    r = _rules(merchant="Uber *Trip HELP.UBER.COM")
    assert r.matched == "uber"


# ---------------------------------------------------------------------------
# Tier 4 — generic keywords (whole word)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("taxi", "ground_transport"),
        ("Airport taxi to client site", "ground_transport"),
        ("car rental", "ground_transport"),
        ("hotel", "lodging"),
        ("Two nights at a hotels.com booking", "lodging"),
        ("restaurant", "meals"),
        ("Team dinner", "meals"),
        ("flight change fee", "airfare"),
    ],
)
def test_keyword_tier(description: str, expected: str) -> None:
    r = _rules(description=description)
    assert r.category == expected
    assert r.confidence == CONF_KEYWORD
    assert r.signal == "keyword"
    assert r.confidence >= CATEGORIZE_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Word-boundary regressions (substring bug in categorize._keyword_category)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("merchant", "description", "not_category"),
    [
        (None, "Team dinner", "lodging"),  # "dinner" ⊃ "inn"
        ("Laptop repair shop", None, "airfare"),  # "repair" ⊃ "air"
        ("Cairo Bookstore", None, "airfare"),  # "cairo" ⊃ "air"
        ("Cabernet Wine Bar", None, "ground_transport"),  # "cabernet" ⊃ "cab"
        ("Seafood Market", None, "meals"),  # "seafood" ⊃ "food" (ambiguous token)
        ("United Way", "charity donation", "airfare"),  # "united" is not a brand alone
        ("Delta Dental", None, "airfare"),
        ("Ritz Crackers", None, "lodging"),
        (None, "Train the new hires", "ground_transport"),  # verb, not transport
    ],
)
def test_no_substring_matches(
    merchant: str | None, description: str | None, not_category: str
) -> None:
    r = _rules(merchant=merchant, description=description)
    assert r.category != not_category


def test_united_states_does_not_fire_airfare_brand() -> None:
    """The user's message lands in description; "United States" must not be a brand hit."""
    r = _rules(description="What category is my lunch in the United States?")
    assert r.category == "meals"
    assert r.signal == "keyword"
    assert r.matched == "lunch"


@pytest.mark.parametrize("description", ["Train ticket to Boston", "train fare"])
def test_train_noun_phrase_matches(description: str) -> None:
    r = _rules(description=description)
    assert r.category == "ground_transport"
    assert r.confidence == CONF_KEYWORD


def test_repair_is_uncategorized() -> None:
    r = _rules(merchant="Laptop repair shop")
    assert r.category == "other"
    assert r.confidence == 0.0


@pytest.mark.parametrize(
    "token",
    [
        "air",
        "american",
        "united",
        "delta",
        "ritz",
        "inn",
        "cab",
        "food",
        "rental",
        "enterprise",
        "train",
    ],
)
def test_ambiguous_tokens_alone_do_not_match(token: str) -> None:
    r = _rules(merchant=token)
    assert r.signal == "none"


# ---------------------------------------------------------------------------
# Conflict / empty / validity
# ---------------------------------------------------------------------------


def test_conflict_is_below_threshold() -> None:
    r = _rules(merchant="Hilton", description="dinner with client")
    assert r.signal == "conflict"
    assert r.confidence < CATEGORIZE_CONFIDENCE_THRESHOLD
    assert r.category == "lodging"  # brand is the best guess
    assert r.matched == "lodging|meals"


def test_keyword_conflict_is_below_threshold() -> None:
    r = _rules(description="hotel and taxi")
    assert r.signal == "conflict"
    assert r.confidence < CATEGORIZE_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize(
    ("merchant", "description"),
    [(None, None), ("", ""), ("   ", None), ("Acme Widgets", "misc purchase")],
)
def test_empty_or_unknown_returns_other_zero(merchant: str | None, description: str | None) -> None:
    r = _rules(merchant=merchant, description=description)
    assert (r.category, r.confidence) == ("other", 0.0)
    assert r.matched is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"merchant": "Marriott"},
        {"description": "taxi"},
        {"mcc": 5812},
        {"expense_type": "car"},
        {"merchant": "Hilton", "description": "dinner"},
        {"merchant": None, "description": None},
    ],
)
def test_every_output_is_a_valid_category(kwargs: dict) -> None:
    r = categorize_by_rules(
        merchant=kwargs.get("merchant"),
        description=kwargs.get("description"),
        expense_type=kwargs.get("expense_type"),
        mcc=kwargs.get("mcc"),
    )
    assert r.category in _VALID
    assert 0.0 <= r.confidence <= 1.0


def test_all_rule_tables_map_to_valid_categories() -> None:
    tables = [
        rule_mod._MERCHANT_BRANDS,
        rule_mod._BRAND_BIGRAMS,
        rule_mod._GENERIC_KEYWORDS,
        rule_mod._KEYWORD_BIGRAMS,
    ]
    for table in tables:
        assert set(table.values()) <= _VALID
    assert {c for _, _, c in rule_mod._MCC_RANGES} <= _VALID


def test_is_valid_category() -> None:
    assert is_valid_category("meals")
    assert not is_valid_category("Meals")
    assert not is_valid_category("food")
    assert not is_valid_category(None)


def test_rule_module_has_no_llm_import() -> None:
    """The rule module is pure: no LlmProvider symbol, no provider module import."""
    assert not hasattr(rule_mod, "LlmProvider")
    assert not hasattr(rule_mod, "LlmMessage")
    assert not hasattr(rule_mod, "get_llm_provider")
