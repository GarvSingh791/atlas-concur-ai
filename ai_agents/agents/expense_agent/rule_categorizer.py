"""Rule-based expense categorizer for the simple categorize path (P8 C1, FR-X3).

Pure function, no I/O, no LLM.  Used by ExpenseCategorizeHandler to answer
``expense_categorize_simple`` tasks deterministically; the handler escalates to
the LLM only when the returned confidence is below its threshold.

Signal tiers (highest wins; structured signals beat free text):
  1. receipt ``expense_type``  (reuses categorize._EXPENSE_TYPE_TO_CATEGORY) → 0.95
  2. merchant category code (MCC, payload-only — no column exists)          → 0.95
  3. whole-word known merchant brand                                        → 0.90
  4. whole-word generic keyword                                             → 0.80
  Conflicting categories within a tier (or between brand and keyword)       → 0.50
  Nothing matched                                                           → other, 0.0

Design decisions:
- Whole-token matching (``re.findall(r"[a-z0-9]+")``), never substring containment.
  categorize._keyword_category uses containment ("dinner" ⊃ "inn" → lodging,
  "repair" ⊃ "air" → airfare); that function is intentionally left untouched so
  ExpenseAgent output is unchanged — this module only *reads* its keyword sets.
- Ambiguous tokens ("air", "american", "united", "delta", "ritz", "inn", "cab",
  "food", "rental", "enterprise") are excluded from single-token matching; some
  are recoverable via bigrams ("united airlines", "delta air", "ritz carlton",
  "car rental").  "train" matches only as "train ticket" / "train fare".
- ``matched`` is a rule key (brand/keyword constant, ``mcc:<code>``,
  ``expense_type:<key>``), never raw merchant text, so it is safe to log.
- Every category returned is an ExpenseCategory value.
- LAYERING LAW: never imports backend/ or any LLM provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai_agents.agents.expense_agent.categorize import (
    _AIRFARE_KEYWORDS,
    _EXPENSE_TYPE_TO_CATEGORY,
    _GROUND_KEYWORDS,
    _LODGING_KEYWORDS,
    _MEALS_KEYWORDS,
)
from ai_agents.tools.spend_policy import SPEND_CATEGORIES
from database.models.expense import ExpenseCategory

CATEGORIZE_CONFIDENCE_THRESHOLD: float = 0.80
"""Minimum rule confidence for a deterministic answer (handler default)."""

CONF_STRUCTURED: float = 0.95
CONF_BRAND: float = 0.90
CONF_KEYWORD: float = 0.80
CONF_CONFLICT: float = 0.50
CONF_NONE: float = 0.0

# Product gap 2: mileage / entertainment / gift are MANUAL-ONLY categories (a mileage
# amount is computed server-side) — receipt / card categorisation never yields them.
_VALID_CATEGORIES: frozenset[str] = frozenset(
    c.value for c in ExpenseCategory if c.value not in SPEND_CATEGORIES
)

# Category tie-break order (mirrors categorize._keyword_category priority).
_CATEGORY_PRIORITY: tuple[str, ...] = ("lodging", "airfare", "ground_transport", "meals")

# ── Merchant brands (tier 3) ────────────────────────────────────────────────
_MERCHANT_BRANDS: dict[str, str] = {
    "marriott": "lodging",
    "hilton": "lodging",
    "hyatt": "lodging",
    "westin": "lodging",
    "sheraton": "lodging",
    "southwest": "airfare",
    "jetblue": "airfare",
    "uber": "ground_transport",
    "lyft": "ground_transport",
    "hertz": "ground_transport",
    "avis": "ground_transport",
    "amtrak": "ground_transport",
    "starbucks": "meals",
    "mcdonald": "meals",
}
_BRAND_BIGRAMS: dict[str, str] = {
    "american airlines": "airfare",
    "united airlines": "airfare",
    "delta air": "airfare",
    "delta airlines": "airfare",
    "ritz carlton": "lodging",
    "enterprise rent": "ground_transport",
}

# Tokens too ambiguous for whole-word matching on their own ("United Way",
# "United States", "Delta Dental", "Ritz Crackers"); brand ones are recovered
# via _BRAND_BIGRAMS.
_AMBIGUOUS_TOKENS: frozenset[str] = frozenset(
    {"air", "american", "united", "delta", "ritz", "inn", "cab", "food", "rental", "enterprise"}
)

# ── Generic keywords (tier 4) — reused sets minus brands/ambiguous, plus extras ─
_EXTRA_KEYWORDS: dict[str, frozenset[str]] = {
    "lodging": frozenset({"motel", "hostel", "lodging"}),
    "airfare": frozenset({"airlines", "airfare"}),
    "ground_transport": frozenset({"taxicab", "shuttle"}),
    "meals": frozenset({"lunch", "dinner", "breakfast"}),
}
_REUSED_KEYWORDS: dict[str, frozenset[str]] = {
    "lodging": _LODGING_KEYWORDS,
    "airfare": _AIRFARE_KEYWORDS,
    "ground_transport": _GROUND_KEYWORDS,
    "meals": _MEALS_KEYWORDS,
}
_GENERIC_KEYWORDS: dict[str, str] = {
    kw: category
    for category, reused in _REUSED_KEYWORDS.items()
    for kw in (reused - _MERCHANT_BRANDS.keys() - _AMBIGUOUS_TOKENS) | _EXTRA_KEYWORDS[category]
}
# "train" alone is also a verb ("train the new hires"), so only noun phrases count.
_KEYWORD_BIGRAMS: dict[str, str] = {
    "car rental": "ground_transport",
    "rental car": "ground_transport",
    "train ticket": "ground_transport",
    "train fare": "ground_transport",
}

# ── MCC ranges (tier 2) — inclusive (low, high) → category ─────────────────
_MCC_RANGES: tuple[tuple[int, int, str], ...] = (
    (3000, 3299, "airfare"),
    (4511, 4511, "airfare"),
    (3501, 3999, "lodging"),
    (7011, 7011, "lodging"),
    (3351, 3441, "ground_transport"),
    (4111, 4111, "ground_transport"),
    (4121, 4121, "ground_transport"),
    (4131, 4131, "ground_transport"),
    (4789, 4789, "ground_transport"),
    (7512, 7512, "ground_transport"),
    (7523, 7523, "ground_transport"),
    (5811, 5814, "meals"),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RuleCategorization:
    """Result of rule-based categorization.

    Attributes:
        category: ExpenseCategory value (best guess, even on conflict).
        confidence: Rule confidence in [0, 1].
        signal: Which tier decided — "expense_type" | "mcc" | "merchant" |
            "keyword" | "conflict" | "none".
        matched: The rule key that fired (never raw merchant text); None when
            nothing matched.
    """

    category: str
    confidence: float
    signal: str
    matched: str | None


def _tokens(*texts: str | None) -> tuple[list[str], set[str]]:
    """Return (unigram list, bigram set) over the lower-cased texts."""
    unigrams: list[str] = []
    bigrams: set[str] = set()
    for text in texts:
        if not text:
            continue
        toks = _TOKEN_RE.findall(text.lower())
        unigrams.extend(toks)
        bigrams.update(f"{a} {b}" for a, b in zip(toks, toks[1:], strict=False))
    return unigrams, bigrams


def _lookup_keyword(token: str) -> str | None:
    """Whole-word keyword lookup with a naive plural fallback ("hotels")."""
    if token in _GENERIC_KEYWORDS:
        return _GENERIC_KEYWORDS[token]
    if len(token) > 3 and token.endswith("s") and token[:-1] in _GENERIC_KEYWORDS:
        return _GENERIC_KEYWORDS[token[:-1]]
    return None


def parse_mcc(mcc: str | int | None) -> int | None:
    """Return a 4-digit MCC as int, or None when absent/invalid."""
    if mcc is None or isinstance(mcc, bool):
        return None
    text = str(mcc).strip()
    if len(text) != 4 or not text.isdigit():
        return None
    return int(text)


def _mcc_category(mcc: int) -> str | None:
    for low, high, category in _MCC_RANGES:
        if low <= mcc <= high:
            return category
    return None


def _expense_type_category(expense_type: str | None) -> tuple[str, str] | None:
    """Return (category, key) for a known, non-'other' expense_type."""
    if not expense_type:
        return None
    key = expense_type.strip().lower()
    category = _EXPENSE_TYPE_TO_CATEGORY.get(key)
    if category is None or category == "other":
        return None
    return category, key


def _best_guess(categories: set[str]) -> str:
    for category in _CATEGORY_PRIORITY:
        if category in categories:
            return category
    return "other"


def _conflict(categories: set[str], best: str) -> RuleCategorization:
    return RuleCategorization(
        category=best,
        confidence=CONF_CONFLICT,
        signal="conflict",
        matched="|".join(sorted(categories)),
    )


def categorize_by_rules(
    *,
    merchant: str | None,
    description: str | None,
    expense_type: str | None = None,
    mcc: str | int | None = None,
) -> RuleCategorization:
    """Categorize an expense from rule signals only (no I/O, no LLM).

    Args:
        merchant: Merchant / vendor name (free text; may be None).
        description: Free-text description or the user's message (may be None).
        expense_type: Receipt OCR expense_type (e.g. "hotel"); highest-priority signal.
        mcc: Merchant category code (4 digits, str or int); invalid values are ignored.

    Returns:
        RuleCategorization with an ExpenseCategory value and a confidence in [0, 1].
    """
    # ── Tier 1 + 2: structured signals ──────────────────────────────────────
    et_hit = _expense_type_category(expense_type)
    mcc_code = parse_mcc(mcc)
    mcc_cat = _mcc_category(mcc_code) if mcc_code is not None else None

    if et_hit is not None and mcc_cat is not None and et_hit[0] != mcc_cat:
        return _conflict({et_hit[0], mcc_cat}, et_hit[0])
    if et_hit is not None:
        return RuleCategorization(
            category=et_hit[0],
            confidence=CONF_STRUCTURED,
            signal="expense_type",
            matched=f"expense_type:{et_hit[1]}",
        )
    if mcc_cat is not None:
        return RuleCategorization(
            category=mcc_cat,
            confidence=CONF_STRUCTURED,
            signal="mcc",
            matched=f"mcc:{mcc_code}",
        )

    # ── Tier 3 + 4: whole-word text signals ─────────────────────────────────
    unigrams, bigrams = _tokens(merchant, description)

    brand_hits: dict[str, str] = {}  # rule key → category
    keyword_hits: dict[str, str] = {}
    for bigram in sorted(bigrams):
        if bigram in _BRAND_BIGRAMS:
            brand_hits[bigram] = _BRAND_BIGRAMS[bigram]
        elif bigram in _KEYWORD_BIGRAMS:
            keyword_hits[bigram] = _KEYWORD_BIGRAMS[bigram]
    for token in unigrams:
        if token in _MERCHANT_BRANDS:
            brand_hits.setdefault(token, _MERCHANT_BRANDS[token])
            continue
        category = _lookup_keyword(token)
        if category is not None:
            keyword_hits.setdefault(token, category)

    categories = set(brand_hits.values()) | set(keyword_hits.values())
    if not categories:
        return RuleCategorization(
            category=ExpenseCategory.other.value,
            confidence=CONF_NONE,
            signal="none",
            matched=None,
        )
    if len(categories) > 1:
        best = _best_guess(set(brand_hits.values())) if brand_hits else _best_guess(categories)
        return _conflict(categories, best)

    (category,) = categories
    if brand_hits:
        return RuleCategorization(
            category=category,
            confidence=CONF_BRAND,
            signal="merchant",
            matched=next(iter(brand_hits)),
        )
    return RuleCategorization(
        category=category,
        confidence=CONF_KEYWORD,
        signal="keyword",
        matched=next(iter(keyword_hits)),
    )


def is_valid_category(value: object) -> bool:
    """True iff ``value`` is an ExpenseCategory value string."""
    return isinstance(value, str) and value in _VALID_CATEGORIES
