"""Entry-level policy rules — deterministic evaluators (product gap 2 + gap 8).

Pure functions, no DB, no LLM.  Money is ``Decimal`` (amounts arrive as floats on the
EvalItem and are converted through ``str`` and quantized to the cent before any
comparison).  ``PolicyEvalTool`` wraps every ``Outcome`` into a ``RuleVerdict``; this
module never imports it (no import cycle).

Implemented clauses (TEP-FY26 corpus, ``database/seeds/corpus/policy.py``):

* LOD-02 — a nightly rate <= 15% over the LOD-01 cap is AUTO-APPROVED when booked
  through the travel tool; > 15% requires manager approval with a justification.
  (``lodging_tolerance`` — a modifier of the HOTEL-CAP amount_cap rule.)
* MEAL-02 — 75% of the per diem on departure and return days
  (``per_diem_allowance`` — a modifier of the MEALS-PD per_diem rule).
* MEAL-04 / per-diem ``exclusions`` — alcohol is not reimbursable under per diem;
  excluded amounts are taken out of the reimbursable meal total
  (``exclusion_outcome`` / ``net_of_exclusions`` — generic: any rule's ``exclusions``).
* GRND-01 rail class, GRND-03 airport parking (7 days, economy lot), GRND-05 tips
  (<= 20%) — rules with a ``kind`` discriminator (``evaluate_kind``).

Fail-safe: when the evidence needed to decide is missing the outcome is an honest
"could not verify" (``observed["could_not_verify"]``), never a silent pass; a value a
claimant merely typed never CLEARS a rule.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ai_agents.tools.ground_transport import (
    PARKING,
    RAIL,
    RIDESHARE_TAXI,
    is_tip_line,
    mentions_tip,
    premium_parking,
    subtype_label,
    unconfirmed,
)

CENT = Decimal("0.01")
HUNDRED = Decimal(100)

# Rule kinds (``definition["kind"]``) and the rule_type each is stored under.  The kind
# decides the evaluator; the rule_type keeps the existing semantics of the row (caps
# are justifiable amount_caps; the rail rule is a class allow-list).
KIND_RAIL_CLASS = "rail_class"
KIND_PARKING_DAYS = "parking_days"
KIND_TIP_PCT = "tip_pct"
KIND_RULE_TYPES: dict[str, str] = {
    KIND_RAIL_CLASS: "category_allow",
    KIND_PARKING_DAYS: "amount_cap",
    KIND_TIP_PCT: "amount_cap",
}
KIND_SUBTYPE: dict[str, str] = {
    KIND_RAIL_CLASS: RAIL,
    KIND_PARKING_DAYS: PARKING,
    KIND_TIP_PCT: RIDESHARE_TAXI,
}

# Observed-dict markers read by presubmit.verdicts_to_flags.
COULD_NOT_VERIFY = "could_not_verify"  # in-policy verdict -> not_evaluated advisory flag
ADVISORY = "advisory"  # in-policy verdict -> policy_advisory flag (e.g. LOD-02 auto-approved)
JUSTIFIABLE = "justifiable_exception"  # a manager may approve it with a justification
# Review round 4: a could-not-verify outcome whose text promises an approver will check
# the evidence carries the approver here; verdicts_to_flags puts it on the flag's
# requires_approver, so the line is never auto-approved past that check.
APPROVER_CHECK = "approver_check"


@dataclass(frozen=True)
class Outcome:
    """What one entry-level rule decided for one item (wrapped into a RuleVerdict)."""

    in_policy: bool
    reason: str
    requires_approver: str | None = None
    observed: dict[str, Any] = field(default_factory=dict)
    clause_ref: str | None = None  # cite this clause instead of the rule's own ref


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------


def dec(value: Any) -> Decimal | None:
    """A finite Decimal from an int / float / numeric string, else None (bools refused)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def cents(value: Any) -> Decimal | None:
    d = dec(value)
    return d.quantize(CENT, rounding=ROUND_HALF_UP) if d is not None else None


def money(d: Decimal) -> str:
    """'$1,234.50' — always two decimals for computed amounts."""
    return f"${d.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"


def pct_text(d: Decimal, *, limit: Decimal | None = None) -> str:
    """'15.00%' (half-up to the hundredth).  A figure just past *limit* never displays as
    the limit itself (15.003% over a 15% limit -> '15.01%')."""
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if limit is not None and d > limit and q <= limit:
        q = (limit + CENT).quantize(CENT)
    return f"{q}%"


def setting(defn: Mapping[str, Any], key: str, default: Any) -> Decimal:
    """A numeric rule setting: *default* when absent; a present but unreadable value
    raises ValueError (the evaluator then returns an honest skip verdict, never a
    guess)."""
    if key not in defn:
        return Decimal(str(default))
    value = dec(defn.get(key))
    if value is None:
        raise ValueError(f"{key} is not a number")
    return value


def _clause(defn: Mapping[str, Any], key: str, default: str) -> str:
    value = defn.get(key)
    return value if isinstance(value, str) and value.strip() else default


def _approver(defn: Mapping[str, Any]) -> str:
    value = defn.get("exception_approver")
    return value if isinstance(value, str) and value.strip() else "manager"


# ---------------------------------------------------------------------------
# LOD-02 — nightly cap tolerance
# ---------------------------------------------------------------------------

TOLERANCE_KEYS = ("auto_approve_over_cap_pct", "auto_approve_clause_ref", "auto_approve_requires")


@dataclass(frozen=True)
class Tolerance:
    over_pct: Decimal  # exact percentage over the cap
    tolerance_pct: Decimal
    within: bool  # over_pct <= tolerance_pct
    booked_through_tool: bool
    clause_ref: str

    @property
    def auto_approved(self) -> bool:
        return self.within and self.booked_through_tool


def lodging_tolerance(
    defn: Mapping[str, Any], *, amount: float, cap: float, booked_through_tool: bool
) -> Tolerance | None:
    """LOD-02 assessment of an OVER-cap nightly figure, or None (no tolerance configured,
    or the figure is not over the cap).

    The nightly figure is taken to the cent; the comparison is exact
    (``(amount - cap) * 100 <= pct * cap``), so 15.00% over is within and 15.01% is not.
    Only a stay booked through the travel tool is auto-approved (the clause's condition);
    a rate within 15% booked elsewhere stays a manager exception.
    """
    pct = dec(defn.get("auto_approve_over_cap_pct"))
    a, c = cents(amount), dec(cap)
    if pct is None or pct <= 0 or a is None or c is None or c <= 0 or a <= c:
        return None
    over = (a - c) * HUNDRED / c
    return Tolerance(
        over_pct=over,
        tolerance_pct=pct,
        within=(a - c) * HUNDRED <= pct * c,
        booked_through_tool=bool(booked_through_tool),
        clause_ref=_clause(defn, "auto_approve_clause_ref", "LOD-02"),
    )


# ---------------------------------------------------------------------------
# MEAL-02 — partial (departure / return) days
# ---------------------------------------------------------------------------


def partial_day_pct(defn: Mapping[str, Any]) -> Decimal | None:
    pct = dec(defn.get("partial_day_pct"))
    return pct if pct is not None and 0 < pct < 100 else None


def per_diem_allowance(rate: Any, *, days: int, travel_days: int, pct: Any) -> Decimal:
    """Total per-diem allowance: full rate on full days, ``pct``% on travel days.

    ``travel_days`` is clamped to [0, days] (a one-day trip has a single travel day:
    departure and return are the same day, counted once).
    """
    r, p = dec(rate) or Decimal(0), dec(pct) or HUNDRED
    t = max(0, min(int(travel_days), int(days)))
    full = int(days) - t
    return (r * full + r * p / HUNDRED * t).quantize(CENT, rounding=ROUND_HALF_UP)


def travel_day_count(days: int) -> int:
    """Departure + return days within a trip of ``days`` covered calendar days.

    1 day (same-day trip) -> 1 (the day is both, counted once); >= 2 -> 2.
    """
    return 1 if days == 1 else 2 if days >= 2 else 0


# ---------------------------------------------------------------------------
# Exclusions (MEAL-04 alcohol and any rule's ``exclusions`` list)
# ---------------------------------------------------------------------------

# Words that identify an excluded item.  An exclusion not listed here is matched by its
# own words, so an admin-added exclusion ("minibar") still works.
EXCLUSION_TERMS: dict[str, tuple[str, ...]] = {
    "alcohol": (
        "alcohol",
        "alcoholic",
        "beer",
        "beers",
        "draft beer",
        "wine",
        "wines",
        "red wine",
        "white wine",
        "rose wine",
        "prosecco",
        "champagne",
        "sake",
        "cider",
        "lager",
        "ipa",
        "stout",
        "pilsner",
        "liquor",
        "spirits",
        "cocktail",
        "cocktails",
        "martini",
        "margarita",
        "mojito",
        "negroni",
        "whiskey",
        "whisky",
        "bourbon",
        "scotch",
        "vodka",
        "gin and tonic",
        "tequila",
        "brandy",
        "cognac",
        "mimosa",
        "sangria",
        "bar tab",
        # Review round 5: how a restaurant receipt usually itemizes alcohol — grape
        # varietals and wine styles, drink styles, spirits.
        "ale",
        "rum",
        "gin",
        "mezcal",
        "soju",
        "highball",
        "old fashioned",
        "spritz",
        "cabernet",
        "cab sauv",
        "merlot",
        "pinot noir",
        "pinot grigio",
        "pinot gris",
        "chardonnay",
        "sauvignon blanc",
        "malbec",
        "riesling",
        "rosé",
        "syrah",
        "shiraz",
        "zinfandel",
    ),
    "minibar": ("minibar", "mini bar", "mini-bar"),
}
# Words that name an exclusion only on a receipt LINE ITEM: on a vendor name they are
# too often something else ("Corona Cafe", "Port City Grill", "Stella's Diner").
LINE_ITEM_TERMS: dict[str, tuple[str, ...]] = {
    "alcohol": (
        "port",
        "sherry",
        "glass of",
        "pint",
        "pints",
        "heineken",
        "corona",
        "guinness",
        "stella",
        "stella artois",
        "bud light",
        "budweiser",
        "modelo",
        "peroni",
        "asahi",
        "sapporo",
    ),
}
# Phrases that contain an excluded word but are not excluded ("ginger ale", "root beer")
# and soft drinks by the glass / pint.  Food cooked with a drink ("beer-battered cod",
# "red wine braised short rib", "port reduction", "rum cake") is exempted separately:
# only a NAMED drink directly followed by the cooking word (``_cooked_pattern``).
_SOFT_DRINKS = (
    r"(?:sparkling\s+|still\s+|tap\s+)?water|milk|(?:\w+\s+)?juice|lemonade|soda|coke"
    r"|(?:iced\s+)?tea|coffee|kombucha"
)
_COOKED_WITH = (
    r"reduction|sauce|vinaigrette|glaze|glazed|jus|braised|battered|marinated|poached"
    r"|infused|gravy|cake|butter|demi[\s\-]*glace"
)
_NOT_EXCLUDED = re.compile(
    r"(?<![0-9a-z])(root beer|ginger beer|ginger ale|non[\s\-]*alcoholic(?:\s+\w+)?"
    r"|alcohol[\s\-]*free(?:\s+\w+)?"
    r"|zero[\s\-]*proof|mocktails?|na beer"
    rf"|(?:glass|pint)[\s\-]+of[\s\-]+(?:{_SOFT_DRINKS}))(?![0-9a-z])",
    re.IGNORECASE,
)
_CLAUSE_FOR_EXCLUSION = {"alcohol": "MEAL-04"}


# Between the words of a phrase: spaces / hyphens, or "and" / "&" where the phrase says
# "and" ("gin and tonic" matches "Gin & tonic").
_WORD_SEP = r"[\s\-]+"
_AND_SEP = r"(?:\s*&\s*|[\s\-]+and[\s\-]+)"


def _phrase_regex(phrase: str) -> str:
    out = ""
    words = phrase.split()
    for i, word in enumerate(words):
        if word == "and" and 0 < i < len(words) - 1:
            out += _AND_SEP
            continue
        if i and words[i - 1] != "and":
            out += _WORD_SEP
        out += re.escape(word)
    return out


def _term_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alts = "|".join(_phrase_regex(p) for p in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![0-9a-z])(?:{alts})(?![0-9a-z])", re.IGNORECASE)


def _cooked_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """A named drink DIRECTLY followed by cooking / food phrasing ("beer-battered",
    "port reduction", "rum cake") — integration security fix: the old exemption let any
    one or two words precede the food word, so "Cabernet Sauvignon cake" or "whiskey
    flight butter" hid a drink.  Only this span is stripped."""
    alts = "|".join(_phrase_regex(p) for p in sorted(words, key=len, reverse=True))
    return re.compile(
        rf"(?<![0-9a-z])(?:{alts})[\s\-]+(?:{_COOKED_WITH})(?![0-9a-z])", re.IGNORECASE
    )


def exclusion_matches(text: str | None, exclusion: str, *, line_item: bool = False) -> str | None:
    """The word in *text* that names *exclusion*, else None.

    ``line_item``: *text* is a receipt line-item description, where the brand / ambiguous
    words of ``LINE_ITEM_TERMS`` also count.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    key = exclusion.strip().lower()
    words = EXCLUSION_TERMS.get(key, (key,))
    if line_item:
        words = (*words, *LINE_ITEM_TERMS.get(key, ()))
    cleaned = _cooked_pattern(words).sub(" ", _NOT_EXCLUDED.sub(" ", text))
    m = _term_pattern(words).search(cleaned)
    return m.group(0) if m else None


def _exclusions(defn: Mapping[str, Any]) -> list[str]:
    raw = defn.get("exclusions")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, str) and e.strip()]


@dataclass(frozen=True)
class ExclusionSplit:
    excluded: Decimal  # quantified excluded amount ON THE RECEIPT (itemized); 0 when unknown
    terms: tuple[str, ...]  # exclusions found (e.g. ("alcohol",))
    words: tuple[str, ...]  # the words that matched
    itemized: bool  # True: amounts come from receipt line items; False: keyword only
    kept: Decimal = Decimal(0)  # sum of the receipt's NON-excluded line items


def exclusion_split(defn: Mapping[str, Any], item: Any) -> ExclusionSplit | None:
    """Which of the rule's exclusions the item contains, and how much (None: none).

    Receipt LINE ITEMS (read by OCR) are the quantified evidence: every line whose
    description names an exclusion is excluded at its amount.  Without itemized
    evidence a keyword in the entry's own words (vendor / description) only says an
    excluded item may be included — the amount is unknown (``itemized=False``).
    """
    exclusions = _exclusions(defn)
    if not exclusions:
        return None
    excluded = Decimal(0)
    kept = Decimal(0)
    terms: list[str] = []
    words: list[str] = []
    for desc, amount in item.line_items or ():
        amt = cents(amount)
        matched = False
        for term in exclusions:
            word = exclusion_matches(desc, term, line_item=True)
            if word and amt is not None and amt > 0:
                excluded += amt
                if term not in terms:
                    terms.append(term)
                words.append(word.lower())
                matched = True
                break
        if not matched and amt is not None and amt > 0:
            kept += amt
    if terms:
        return ExclusionSplit(excluded, tuple(terms), tuple(dict.fromkeys(words)), True, kept)
    for term in exclusions:
        word = exclusion_matches(item.item_text, term)
        if word:
            terms.append(term)
            words.append(word.lower())
    if terms:
        return ExclusionSplit(Decimal(0), tuple(terms), tuple(dict.fromkeys(words)), False)
    return None


def _verified_days(item: Any) -> int | None:
    days = getattr(item, "days", None)
    return days if isinstance(days, int) and not isinstance(days, bool) and days >= 1 else None


def line_claim(item: Any) -> Decimal | None:
    """What THIS line claims, to the cent (with verified ``days`` ``amount_usd`` is the
    trip's per-day average, so the line's own claim rides in ``line_claim_usd``)."""
    if _verified_days(item):
        return cents(getattr(item, "line_claim_usd", None))
    return cents(item.amount_usd)


@dataclass(frozen=True)
class ClaimShare:
    """How much excluded money (with its share of tax / tip) a line's claim holds."""

    in_claim: Decimal
    # True when the receipt's tax / tip / service cannot be attributed (the food is not
    # itemized, or the line items exceed the total) AND a strict reading would put more
    # excluded money in the claim than ``in_claim`` — the approver must check it.
    uncertain: bool = False
    excluded_with_extras: Decimal | None = None  # excluded items + their pro-rata extras


def excluded_share(split: ExclusionSplit, item: Any) -> ClaimShare:
    """How much of the receipt's excluded amount is actually INSIDE the claim.

    A claimant may claim only part of a receipt (the food without the wine); the receipt
    still lists the wine.  Integration (security review): the receipt's extras — its OCR
    total minus every itemized line (tax, tip, service) — are split PRO RATA over the
    excluded and the kept line items, so the tax and tip charged on the alcohol are
    excluded with it and cannot hide in the gap between the total and the items:

        excluded_full = excluded + extras x excluded / (excluded + kept)
        in_claim      = max(0, min(excluded_full, claim - (total - excluded_full)))

    An honest partial claim — the food alone, or the food with its own tax / tip — holds
    no excluded money and is neither flagged nor reduced (nothing is subtracted twice).
    Without an OCR total the line items are all there is (non-excluded = the kept items).
    When the extras cannot be attributed (no kept item itemized, or the items exceed the
    total) the old reading (non-excluded = total - excluded) decides a breach, and a
    claim that only the strict reading (non-excluded = the kept items) would breach is
    ``uncertain`` — a could-not-verify for the approver, never a silent pass.
    Without the line's claim the whole excluded amount is taken to be claimed (fail
    closed — flag it, never hide it).
    """
    if not split.itemized or split.excluded <= 0:
        return ClaimShare(Decimal(0))
    total = cents(getattr(item, "receipt_total_usd", None))
    items_total = split.excluded + split.kept
    known_total = total is not None and total > 0
    attributable = not known_total or (
        split.kept > 0 and total is not None and total >= items_total
    )
    excluded_full = split.excluded
    if known_total and attributable and total is not None:
        extras = total - items_total
        excluded_full = split.excluded + (extras * split.excluded / items_total).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
    claim = line_claim(item)
    if claim is None:
        return ClaimShare(excluded_full, excluded_with_extras=excluded_full)
    if getattr(item, "receipt_shared", False) is True:
        # The receipt also backs other lines: its food may be claimed by a sibling, so
        # this line's claim may be all wine — assume the worst (fail closed).
        return ClaimShare(
            max(Decimal(0), min(excluded_full, claim)), excluded_with_extras=excluded_full
        )
    if not known_total:
        non_excluded = split.kept
    elif attributable and total is not None:
        non_excluded = total - excluded_full
    else:
        assert total is not None
        lenient = max(
            Decimal(0), min(split.excluded, claim - max(total - split.excluded, Decimal(0)))
        )
        strict = max(Decimal(0), min(split.excluded, claim - split.kept))
        return ClaimShare(lenient, uncertain=strict > lenient, excluded_with_extras=None)
    return ClaimShare(
        max(Decimal(0), min(excluded_full, claim - non_excluded)),
        excluded_with_extras=excluded_full,
    )


def excluded_in_claim(split: ExclusionSplit, item: Any) -> Decimal:
    """The excluded money inside the claim (``excluded_share``)."""
    return excluded_share(split, item).in_claim


def net_of_exclusions(defn: Mapping[str, Any], item: Any, amount: float) -> float:
    """The compared per-diem figure with the excluded amount INSIDE the claim taken out.

    ``amount`` is the line amount, or — with verified ``days`` — the daily average of
    ``stay_amount_usd``; the excluded amount comes out of that total before averaging.
    An excluded item the claimant already left out of the claim is not subtracted again.
    """
    split = exclusion_split(defn, item)
    if split is None:
        return amount
    in_claim = excluded_in_claim(split, item)
    if in_claim <= 0:
        return amount
    days = _verified_days(item)
    total = dec(item.stay_amount_usd) if days else dec(amount)
    if total is None:
        return amount
    net = max(total - in_claim, Decimal(0))
    if days:
        net = net / days
    return float(net.quantize(CENT, rounding=ROUND_HALF_UP))


def pooled_total(defn: Mapping[str, Any], item: Any, amount: float, *, days: int) -> Decimal:
    """The pooled meal total compared with a multi-day allowance, to the cent.

    ``stay_amount_usd`` (the trip's meal total) when present, else ``amount`` x days;
    QUANTIFIED exclusions of this line are taken out (MEAL-04).
    """
    gross = cents(item.stay_amount_usd)
    if gross is None:
        gross = ((dec(amount) or Decimal(0)) * days).quantize(CENT, rounding=ROUND_HALF_UP)
        return gross  # ``amount`` is already net of exclusions (net_of_exclusions)
    split = exclusion_split(defn, item)
    if split is not None:
        gross = max(gross - excluded_in_claim(split, item), Decimal(0))
    return gross


def exclusion_outcome(defn: Mapping[str, Any], item: Any) -> Outcome | None:
    """The explicit exclusion flag for an item that contains an excluded item.

    * itemized (OCR line items): a VIOLATION — the excluded amount is not reimbursable;
      a manager may accept it only as approved client entertainment (MEAL-04), so it is
      justifiable;
    * keyword only: an honest "could not verify" advisory (the amount is unknown).
    """
    split = exclusion_split(defn, item)
    if split is None:
        return None
    term = split.terms[0]
    clause = _clause(defn, "exclusions_clause_ref", _CLAUSE_FOR_EXCLUSION.get(term, "MEALS-PD"))
    names = ", ".join(split.terms)
    words = ", ".join(repr(w) for w in split.words[:3])
    observed: dict[str, Any] = {
        "excluded_terms": list(split.terms),
        "excluded_words": list(split.words),
        "exclusion_itemized": split.itemized,
    }
    if split.itemized:
        share = excluded_share(split, item)
        in_claim = share.in_claim
        observed["receipt_excluded_usd"] = float(split.excluded)
        if share.excluded_with_extras is not None and share.excluded_with_extras != split.excluded:
            # Integration: the tax / tip charged on the excluded items, pro rata.
            observed["receipt_excluded_with_tax_tip_usd"] = float(share.excluded_with_extras)
        observed["excluded_usd"] = float(in_claim)
        claim = line_claim(item)
        if in_claim <= 0 and share.uncertain:
            return Outcome(
                in_policy=True,
                reason=(
                    f"Could not verify the {names} exclusion ({clause}): the receipt lists "
                    f"{money(split.excluded)} of {names} ({words}) but does not itemize the "
                    "rest of the bill, so Atlas cannot tell how much of the claimed amount"
                    f"{f' of {money(claim)}' if claim is not None else ''} is {names} or the "
                    f"tax / tip on it. {names.capitalize()} is not reimbursable under the per "
                    "diem — your approver should check the receipt."
                ),
                observed={
                    **observed,
                    COULD_NOT_VERIFY: "exclusion_share",
                    APPROVER_CHECK: _approver(defn),
                },
                clause_ref=clause,
            )
        if in_claim <= 0:
            # The claimant already left the excluded item out of the claim: nothing to
            # exclude, no flag (an informational in-policy verdict).
            return Outcome(
                in_policy=True,
                reason=(
                    f"The receipt lists {money(split.excluded)} of {names} ({words}), which "
                    f"is not reimbursable under the per diem ({clause}); the claimed amount"
                    f"{f' of {money(claim)}' if claim is not None else ''} does not include "
                    "it."
                ),
                observed=observed,
                clause_ref=clause,
            )
        reimbursable = None
        if claim is not None:
            reimbursable = max(claim - in_claim, Decimal(0))
            observed["reimbursable_usd"] = float(reimbursable)
        rest = (
            f" The reimbursable meal amount is {money(reimbursable)}."
            if reimbursable is not None
            else ""
        )
        return Outcome(
            in_policy=False,
            reason=(
                f"{money(in_claim)} of the claimed amount is {names} ({words}), which is not "
                f"reimbursable under the per diem ({clause}); it was excluded from the "
                f"reimbursable meal total.{rest} Remove it from the claim, or — for approved "
                "client entertainment — add a justification listing the attendees. "
                "Requires manager approval."
            ),
            requires_approver="manager",
            observed={**observed, JUSTIFIABLE: True},
            clause_ref=clause,
        )
    return Outcome(
        in_policy=True,
        reason=(
            f"Could not verify the {names} exclusion ({clause}): the entry mentions {words} "
            "but the receipt is not itemized, so the excluded amount could not be taken out "
            f"of the reimbursable meal total. {names.capitalize()} is not reimbursable under "
            "the per diem — your approver should check the receipt."
        ),
        observed={
            **observed,
            COULD_NOT_VERIFY: "exclusion_amount",
            APPROVER_CHECK: _approver(defn),
        },
        clause_ref=clause,
    )


# ---------------------------------------------------------------------------
# Ground transport rules with a ``kind`` (GRND-01 / GRND-03 / GRND-05)
# ---------------------------------------------------------------------------


def _kind_observed(item: Any) -> dict[str, Any]:
    return {
        "ground_subtype": item.ground_subtype,
        "amount_usd": item.amount_usd,
    }


def _unverified(
    defn: Mapping[str, Any], clause: str, what: str, reason: str, item: Any, **extra: Any
) -> Outcome:
    """In policy as far as Atlas can tell, but the evidence is missing: the reason tells
    the claimant an approver will check, so the flag escalates (APPROVER_CHECK)."""
    return Outcome(
        in_policy=True,
        reason=reason,
        observed={
            **_kind_observed(item),
            COULD_NOT_VERIFY: what,
            APPROVER_CHECK: _approver(defn),
            **extra,
        },
        clause_ref=clause,
    )


def _violation(defn: Mapping[str, Any], clause: str, reason: str, observed: dict) -> Outcome:
    approver = _approver(defn)
    return Outcome(
        in_policy=False,
        reason=f"{reason} Requires {approver} approval with a justification.",
        requires_approver=approver,
        observed={**observed, JUSTIFIABLE: True},
        clause_ref=clause,
    )


def _hours_text(hours: float) -> str:
    return f"{hours:g}-hour"


def _eval_rail_class(defn: Mapping[str, Any], item: Any) -> Outcome:
    clause = _clause(defn, "corpus_clause_ref", "GRND-01")
    min_h = float(setting(defn, "premium_min_hours", 3))
    standard = [str(c).lower() for c in defn.get("standard_classes") or ["standard", "economy"]]
    premium = [str(c).lower() for c in defn.get("premium_classes") or ["first", "business"]]
    condition = str(defn.get("premium_condition") or "cheaper than or comparable to flying")
    rule_text = (
        f"{clause}: Standard/Economy class for trips under {min_h:g} hours; First/Business "
        f"only for trips of {min_h:g} hours or more if {condition}"
    )
    cls = (item.travel_class or "").strip().lower() or None
    hours = item.duration_hours
    observed = {**_kind_observed(item), "travel_class": cls, "duration_hours": hours}
    if cls is None or (cls not in standard and cls not in premium):
        return _unverified(
            defn,
            clause,
            "travel_class",
            f"Could not verify the rail ticket class from the receipt or description "
            f"({rule_text}). Your approver should check the ticket.",
            item,
            travel_class=cls,
            duration_hours=hours,
        )
    if cls in standard:
        return Outcome(
            in_policy=True,
            reason=f"{cls.capitalize()}-class rail ticket — within {clause}.",
            observed=observed,
            clause_ref=clause,
        )
    label = cls.capitalize()
    if hours is None:
        return _violation(
            defn,
            clause,
            f"{label}-class rail ticket, but the journey duration could not be verified from "
            f"the receipt, description or an approved itinerary ({rule_text}).",
            observed,
        )
    if float(hours) < min_h:
        return _violation(
            defn,
            clause,
            f"{label}-class rail ticket for a {_hours_text(float(hours))} journey: {clause} "
            f"allows Standard/Economy class for trips under {min_h:g} hours.",
            observed,
        )
    return _violation(
        defn,
        clause,
        f"{label}-class rail ticket for a {_hours_text(float(hours))} journey: {clause} "
        f"permits First/Business for trips of {min_h:g} hours or more only if {condition}, "
        "which Atlas cannot verify — add the comparable airfare to the justification.",
        {**observed, "condition": condition},
    )


def _eval_parking_days(defn: Mapping[str, Any], item: Any) -> Outcome:
    clause = _clause(defn, "corpus_clause_ref", "GRND-03")
    max_days = int(setting(defn, "max_days", 7))
    lot = str(defn.get("lot") or "economy")
    rule_text = f"{clause} reimburses airport parking up to {max_days} days ({lot} lot)"
    observed: dict[str, Any] = {**_kind_observed(item), "max_days": max_days}
    premium = premium_parking(item.item_text)
    if premium:
        return _violation(
            defn,
            clause,
            f"The parking receipt names a {premium} lot: {rule_text}.",
            {**observed, "premium_lot": premium},
        )
    days = item.parking_days if isinstance(item.parking_days, int) else None
    claimed = item.claimed_days if isinstance(item.claimed_days, int) else None
    if days is None and claimed is not None and claimed > max_days:
        days = claimed  # a claim AGAINST the claimant's interest may be used
        observed["days_source"] = "stated on the entry"
    if days is None:
        stated = f" ({claimed} days stated on the entry, not verified)" if claimed else ""
        trip_days = getattr(item, "trip_days", None)
        trip_days = trip_days if isinstance(trip_days, int) and trip_days > 0 else None
        context = (
            f" The linked trip spans {trip_days} days; the receipt does not say how many "
            "of them were parked."
            if trip_days
            else ""
        )
        return _unverified(
            defn,
            clause,
            "parking_days",
            f"Could not verify how many days of parking this covers{stated}: {rule_text}."
            f"{context} Your approver should check the parking receipt.",
            item,
            max_days=max_days,
            claimed_days=claimed,
            trip_days=trip_days,
        )
    observed["parking_days"] = days
    if days <= max_days:
        return Outcome(
            in_policy=True,
            reason=f"Parking for {days} days — within {clause}'s {max_days}-day limit.",
            observed=observed,
            clause_ref=clause,
        )
    amount = cents(item.amount_usd)
    share = ""
    if amount is not None:
        reimbursable = (amount * max_days / days).quantize(CENT, rounding=ROUND_HALF_UP)
        observed["reimbursable_usd"] = float(reimbursable)
        share = f" {max_days} of {days} days ({money(reimbursable)} of {money(amount)}) are " + (
            "reimbursable."
        )
    return _violation(
        defn,
        clause,
        f"Parking for {days} days exceeds the {max_days}-day limit: {rule_text}.{share}",
        observed,
    )


def _tip_amount(item: Any) -> tuple[Decimal, bool] | None:
    """The tip READ from the receipt: an OCR tip field, else the tip line items.

    Returns (tip, from_line_items) or None when the receipt shows no tip.
    """
    tip = cents(item.tip_usd)
    if tip is not None:
        return tip, False
    total = Decimal(0)
    found = False
    for desc, amount in item.line_items or ():
        amt = cents(amount)
        if is_tip_line(desc) and amt is not None:
            total += amt
            found = True
    return (total, True) if found else None


def _tip_receipt_total(item: Any, *, tip_from_lines: bool) -> Decimal | None:
    """The receipt's total (fare + tip) READ from the receipt — never the claim.

    The OCR total; else, when the tip itself is a line item, the sum of the line items
    (fare lines + tip line).  An OCR tip FIELD with no OCR total cannot be placed (the
    line items may or may not include it) -> None (could not verify).
    """
    total = cents(getattr(item, "receipt_total_usd", None))
    if total is not None and total > 0:
        return total
    if tip_from_lines:
        amounts = [cents(a) for _, a in item.line_items or ()]
        if amounts and all(a is not None for a in amounts):
            return sum((a for a in amounts if a is not None), Decimal(0))
    return None


def _eval_tip_pct(defn: Mapping[str, Any], item: Any) -> Outcome:
    clause = _clause(defn, "corpus_clause_ref", "GRND-05")
    max_pct = setting(defn, "max_tip_pct", 20)
    observed: dict[str, Any] = {**_kind_observed(item), "max_tip_pct": float(max_pct)}
    found = _tip_amount(item)
    if found is None:
        if mentions_tip(item.item_text):
            return _unverified(
                defn,
                clause,
                "tip_amount",
                f"Could not verify the tip: the entry mentions a tip but the receipt does not "
                f"itemize it, so {clause}'s {max_pct:g}% limit could not be checked. Your "
                "approver should check the receipt.",
                item,
                max_tip_pct=float(max_pct),
            )
        return Outcome(
            in_policy=True,
            reason=f"No tip itemized on this ride ({clause} not applicable).",
            observed={**observed, "tip_identified": False},
            clause_ref=clause,
        )
    tip, from_lines = found
    # Review round 3: the fare is the RECEIPT's total minus the tip — never the claimed
    # amount minus the tip (a claimant may claim the fare only).
    receipt_total = _tip_receipt_total(item, tip_from_lines=from_lines)
    fare = (receipt_total - tip) if receipt_total is not None else None
    if fare is None or fare <= 0:
        return _unverified(
            defn,
            clause,
            "fare",
            f"Could not verify the fare behind a {money(tip)} tip ({clause}: tips up to "
            f"{max_pct:g}% of the fare) — the receipt's total was not read. Your approver "
            "should check the receipt.",
            item,
            tip_usd=float(tip),
        )
    pct = tip * HUNDRED / fare
    observed.update(
        {
            "tip_usd": float(tip),
            "fare_usd": float(fare),
            "tip_pct": float(pct),
            "receipt_total_usd": float(receipt_total) if receipt_total is not None else None,
        }
    )
    if tip * HUNDRED <= max_pct * fare:
        return Outcome(
            in_policy=True,
            reason=(
                f"Tip {money(tip)} is {pct_text(pct)} of the {money(fare)} fare — within "
                f"{clause}'s {max_pct:g}% limit."
            ),
            observed=observed,
            clause_ref=clause,
        )
    allowed = (fare * max_pct / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
    observed["reimbursable_tip_usd"] = float(allowed)
    claim = cents(item.amount_usd)
    # How much tip the CLAIM holds: whatever it claims beyond the fare.  Unknown claim ->
    # the whole tip (fail closed).
    if claim is None:
        tip_claimed = tip
    elif getattr(item, "receipt_shared", False) is True:
        # The fare may be claimed by a sibling line on the same receipt: this claim may
        # be all tip (fail closed).
        tip_claimed = min(tip, claim)
    else:
        tip_claimed = max(claim - fare, Decimal(0))
    observed["tip_claimed_usd"] = float(tip_claimed)
    if tip_claimed <= allowed:
        return Outcome(
            in_policy=True,
            reason=(
                f"The receipt's tip {money(tip)} is {pct_text(pct, limit=max_pct)} of the "
                f"{money(fare)} fare — over {clause}'s {max_pct:g}% limit — but the claim "
                f"includes only {money(tip_claimed)} of it, within the {money(allowed)} "
                "reimbursable."
            ),
            observed=observed,
            clause_ref=clause,
        )
    return _violation(
        defn,
        clause,
        f"Tip {money(tip)} is {pct_text(pct, limit=max_pct)} of the {money(fare)} fare — over "
        f"{clause}'s {max_pct:g}% limit for reasonable tips; up to {money(allowed)} is "
        f"reimbursable ({money(fare + allowed)} with the fare).",
        observed,
    )


_KIND_EVALUATORS = {
    KIND_RAIL_CLASS: _eval_rail_class,
    KIND_PARKING_DAYS: _eval_parking_days,
    KIND_TIP_PCT: _eval_tip_pct,
}


def rule_kind(definition: Any) -> str | None:
    kind = definition.get("kind") if isinstance(definition, Mapping) else None
    return kind if isinstance(kind, str) and kind in _KIND_EVALUATORS else None


def _subtype_unconfirmed(item: Any) -> bool:
    return unconfirmed(
        getattr(item, "ground_candidates", ()) or (),
        getattr(item, "ground_verified", False) is True,
    )


def evaluate_kind(definition: Mapping[str, Any], item: Any) -> Outcome:
    """Evaluate a ``kind`` rule (caller checked ``rule_kind``).

    On an UNCONFIRMED line (review round 5: no receipt / card / segment names its kind)
    a could-not-verify outcome says why the rule was applied at all.
    """
    kind = str(definition["kind"])
    if (
        kind == KIND_PARKING_DAYS
        and _subtype_unconfirmed(item)
        and getattr(item, "airport_parking", False) is not True
    ):
        # Not shown to be airport parking, but nothing trusted says what it is either:
        # GRND-03 can neither clear nor breach it (a hotel valet typed in is not an
        # airport-lot violation) -> the approver checks.
        clause = _clause(definition, "corpus_clause_ref", "GRND-03")
        max_days = int(setting(definition, "max_days", 7))
        return _unverified(
            definition,
            clause,
            "airport_parking",
            f"Atlas could not confirm what kind of ground transport this is (no receipt, "
            f"card or trip segment names it), so it could not tell whether {clause} "
            f"(airport parking up to {max_days} days) applies. Your approver should check "
            "the receipt.",
            item,
            max_days=max_days,
            ground_subtype_unconfirmed=True,
        )
    outcome = _KIND_EVALUATORS[kind](definition, item)
    if (
        COULD_NOT_VERIFY in outcome.observed
        and _subtype_unconfirmed(item)
        and item.ground_subtype != KIND_SUBTYPE.get(kind)
    ):
        names = subtype_label(KIND_SUBTYPE.get(kind))
        outcome = Outcome(
            in_policy=outcome.in_policy,
            reason=(
                f"Atlas could not confirm what kind of ground transport this is (no receipt, "
                f"card or trip segment names it), so the {names} rule was checked too. "
                + outcome.reason
            ),
            requires_approver=outcome.requires_approver,
            observed={**outcome.observed, "ground_subtype_unconfirmed": True},
            clause_ref=outcome.clause_ref,
        )
    return outcome


# ---------------------------------------------------------------------------
# Applicability of ground sub-type filters (PolicyEvalTool.rule_applies)
# ---------------------------------------------------------------------------


def _kind_evidence(kind: str, item: Any) -> bool:
    """The line carries RECEIPT evidence a kind rule decides on, whatever its words say:
    a tip read from the receipt (GRND-05)."""
    if kind == KIND_TIP_PCT:
        return _tip_amount(item) is not None
    return False


def ground_subtype_reach(definition: Mapping[str, Any], item: Any) -> bool | None:
    """Whether a ``ground_subtypes`` filter reaches *item* (None: the rule has no filter).

    * the item must be a ground-transport line (``expense_category``);
    * a VERIFIED line (trusted evidence names its one family, the claimant's words agree)
      is reached only by the rules of its sub-type;
    * an UNVERIFIED line — typed words only, typed words that disagree with the receipt /
      card / segment, or several families named — is never classified by the claimant's
      words alone (security review): a ``kind`` rule (GRND-01/03/05) reaches it when its
      sub-type is ANY candidate, or when the receipt carries that rule's evidence (a tip
      read from the receipt -> GRND-05); an authored cap reaches it whatever it names (it
      may be that kind; the reason says so);
    * the parking-days rule (GRND-03) only parking shown to be AIRPORT parking;
    * a line no word names at all is the broad category: authored caps reach it, kind
      rules only through their receipt evidence.
    A malformed filter reaches nothing (the activation validator refuses it).
    """
    if "ground_subtypes" not in definition:
        return None
    subtypes = definition.get("ground_subtypes")
    if not isinstance(subtypes, list) or not subtypes:
        return False
    wanted = {s for s in subtypes if isinstance(s, str)}
    if item.expense_category != "ground_transport":
        return False
    kind = rule_kind(definition)
    if kind is not None and _subtype_unconfirmed(item):
        # Review round 5: nothing trusted names the line's kind — it may be any of them
        # (airport parking included), so every kind rule reaches it and says what it
        # could not verify; none is skipped.
        return True
    if kind == KIND_PARKING_DAYS and getattr(item, "airport_parking", False) is not True:
        # GRND-03 is an AIRPORT-parking clause: hotel valet, a city garage, a meter are
        # parking but not airport parking — the clause does not reach them at all.
        return False
    verified = getattr(item, "ground_verified", False) is True
    candidates = set(getattr(item, "ground_candidates", ()) or ())
    if item.ground_subtype in wanted:
        return True
    if verified:
        return False
    if kind is not None:
        return bool(wanted & candidates) or _kind_evidence(kind, item)
    return True


def unclassified_note(definition: Mapping[str, Any], item: Any) -> str | None:
    """Reason suffix when a sub-type cap was applied to an UNCLASSIFIED line."""
    subtypes = definition.get("ground_subtypes")
    if not isinstance(subtypes, list) or item.ground_subtype in subtypes:
        return None
    names = " / ".join(subtype_label(s) for s in subtypes if isinstance(s, str))
    return (
        f"Atlas could not tell which kind of ground transport this line is (the receipt, "
        f"card or trip does not confirm it), so the {names} cap was applied; if it is "
        "another kind, say so in a justification."
    )
