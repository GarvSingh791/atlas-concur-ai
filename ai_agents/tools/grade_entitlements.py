"""Grade entitlements AIR-07 / LOD-05 / NR-01 — deterministic evaluators (follow-up, grades).

TEP-FY26 grants three more things only to "SVPs and above" (``database/seeds/corpus/
policy.py``; the ladder is ``config/constants/grades.py``):

* AIR-07 — "Early-boarding and lounge fees are not reimbursable (except for SVPs+ or
  delays > 4 hours)."                                        -> ``airport_extras``
* LOD-05 — "standard room only; suites/upgrades not reimbursable except SVPs+."
                                                             -> ``room_upgrade``
* NR-01  — "lounge memberships (except SVPs+)" are not reimbursable.
                                                             -> ``lounge_membership``

Pure functions, no DB, no LLM.  Money is ``Decimal`` and converted to USD with the
reference table every other rule uses (``process_rules.amount_in_usd``).

Detection (which line IS such a fee) reads two kinds of words:

* TRUSTED words — the receipt's OCR vendor / line items and the card merchant (the
  claimant cannot type into them);
* the claimant's own words — vendor / description.

Words may only WIDEN scrutiny: a match in either kind detects the line (a claimant
who writes "lounge pass" has said so against their own interest), and nothing the
claimant writes can remove a match the receipt or card made.  A typed-only match is
labelled as such in the reason ("named on the entry, not on a receipt or card").

Whether a line is AT A HOTEL (a "Suite" / "Upgrade" line is a room) or AT AN AIRPORT (a
bare "Lounge" line is an airport lounge) is the context of the WHOLE entry: any trusted
receipt word / line item or card merchant with hotel (airline) words, or a linked hotel
(flight) trip segment.  The claimant's category may only ADD that context ("lodging" /
"airfare"); filing a folio under "other" never hides its suite line (review fix).

The decision is made from the OWNER's grade of record (``users.grade``, read by the
caller from the database — never from a request):

* at or above the rule's ``min_grade`` (SVP)  -> in policy, an advisory note citing
  the clause (``policy_advisory``, never escalating);
* a known junior grade -> ``policy_violation`` (blocking; a written justification
  moves it to advisory) that must be granted by an approver of ``min_grade`` or above;
* no grade on record / not on the ladder -> ``approval_required`` by an approver of
  ``min_grade`` or above, ``could_not_verify: traveller_grade`` (never auto-approved).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ai_agents.tools import process_rules as pr
from config.constants import grades as grade_ladder

KIND = pr.KIND_GRADE_ENTITLEMENT

AIRPORT_EXTRAS = "airport_extras"  # AIR-07 early-boarding / lounge fees
ROOM_UPGRADE = "room_upgrade"  # LOD-05 suites / room upgrades
LOUNGE_MEMBERSHIP = "lounge_membership"  # NR-01 lounge memberships
ENTITLEMENTS: tuple[str, ...] = (AIRPORT_EXTRAS, ROOM_UPGRADE, LOUNGE_MEMBERSHIP)

DEFAULT_CLAUSE: dict[str, str] = {
    AIRPORT_EXTRAS: "AIR-07",
    ROOM_UPGRADE: "LOD-05",
    LOUNGE_MEMBERSHIP: "NR-01",
}

# What the line is, and what the clause allows (wording of reasons and summaries).
LINE_LABEL: dict[str, str] = {
    AIRPORT_EXTRAS: "Early-boarding / airport lounge fee",
    ROOM_UPGRADE: "Hotel suite / room upgrade",
    LOUNGE_MEMBERSHIP: "Airport lounge membership",
}
ALLOWANCE: dict[str, str] = {
    AIRPORT_EXTRAS: "early-boarding and lounge fees",
    ROOM_UPGRADE: "suites and room upgrades",
    LOUNGE_MEMBERSHIP: "lounge memberships",
}

# Flag types (all already in the finance exception catalog).
POLICY_ADVISORY = "policy_advisory"
POLICY_VIOLATION = pr.POLICY_VIOLATION
APPROVAL_REQUIRED = pr.APPROVAL_REQUIRED

# AIR-07's other exception: "delays > 4 hours".  Atlas holds no flight-delay data, so
# it can never clear a fee on its own — the reason asks for the delay in a justification.
DEFAULT_DELAY_HOURS = 4

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_LOUNGE_BRANDS = (
    r"admirals?\s*club|united\s*club|(?:delta\s*)?sky\s*club|alaska\s*lounge|"
    r"centurion\s*lounge|polaris\s*lounge|plaza\s*premium|priority\s*pass|lounge\s*key|"
    r"airport\s*lounge|flagship\s*lounge|maple\s*leaf\s*lounge"
)
_BRAND_RE = re.compile(rf"(?<![a-z])(?:{_LOUNGE_BRANDS})(?![a-z])", re.IGNORECASE)
_LOUNGE_FEE_RE = re.compile(
    r"(?<![a-z])lounge\s*(?:access|pass|day\s*pass|entry|visit|fee|admission|guest)(?![a-z])",
    re.IGNORECASE,
)
_BARE_LOUNGE_RE = re.compile(r"(?<![a-z])lounge(?![a-z])", re.IGNORECASE)
_MEMBERSHIP_RE = re.compile(
    r"(?<![a-z])(?:membership|member\s*fee|annual\s*(?:fee|dues)|dues|renewal|subscription)"
    r"(?![a-z])",
    re.IGNORECASE,
)
_EARLY_BOARDING_RE = re.compile(
    r"(?<![a-z])(?:early[\s-]*bird(?:\s*check[\s-]*in)?|early[\s-]*boarding|"
    r"priority\s*boarding|boarding\s*priority|upgraded\s*boarding)(?![a-z])",
    re.IGNORECASE,
)
# A hotel room type — the SINGULAR "suite", so brand names ("Embassy Suites",
# "Homewood Suites") never match — unless it is ADDRESS-shaped: "Suite #12",
# "Suite No. 4", "Suite number 9" or "Suite 400" where the number is NOT followed by a
# stay length or a date ("Suite 1 night", "Executive Suite 2 nights", "Suite 3x",
# "Junior Suite 11/12-11/14" are room lines).  ``\d+(?!\d)`` takes the WHOLE number
# (no backtracking into "1" of "11/12").
_SUITE_RE = re.compile(
    r"(?<![a-z])suite(?![a-z])"
    r"(?!\s*(?:#|no\.?\s*\d|number|"
    r"\d+(?!\d)(?!\s*(?:nights?(?![a-z])|nts?(?![a-z])|x(?![a-z])|[/.-]\d))))",
    re.IGNORECASE,
)
# Phrases that name a hotel room ON THEIR OWN — matched without any hotel context.
_ROOM_UPGRADE_RE = re.compile(
    r"(?<![a-z])(?:room\s*upgrade|hotel\s*upgrade|upgraded\s*room)(?![a-z])",
    re.IGNORECASE,
)
# Upgrade phrases that are a room ONLY at a hotel (review fix): "Adobe Creative Suite
# upgrade" or "Upgrade to Executive plan" on a software receipt is not a room.
_CONTEXT_UPGRADE_RE = re.compile(
    r"(?<![a-z])(?:suite\s*upgrade|"
    r"upgrade\s*to\s*(?:a\s*)?(?:suite|club|executive|deluxe))(?![a-z])",
    re.IGNORECASE,
)
_UPGRADE_RE = re.compile(r"(?<![a-z])upgrade[sd]?(?![a-z])", re.IGNORECASE)
# Words that place a line at a hotel ("suite" on a software receipt is not a room).
# Read across the WHOLE trusted evidence of the entry (every receipt word / line item and
# the card merchant), never only the line itself and never narrowed by the claimant's
# category (review fix).  Plural "Suites" is a hotel brand word (Embassy Suites).  A bare
# "room" / "rooms" is NOT one ("Conference room", "Room divider", "meeting rooms"); only
# hotel-shaped room phrases are (review fix).
_HOTEL_WORDS_RE = re.compile(
    r"(?<![a-z])(?:hotels?|resorts?|inns?|motel|lodging|nights|nts|"
    r"room\s*(?:nights?|rate|charge|tax|and\s*tax)|guest\s*room|per\s*night|folio|"
    r"suites|marriott|hilton|hyatt|sheraton|westin|radisson|wyndham|intercontinental|"
    r"doubletree|hampton|courtyard|fairmont|novotel|ibis|four\s*seasons|ritz[\s-]*carlton)"
    r"(?![a-z])",
    re.IGNORECASE,
)
# Words that place a line at an airport / with an airline (bare "lounge" is then an
# airport lounge).  "airport" itself is NOT one: an "Airport Marriott" lobby lounge is a
# hotel bar.
_AIRLINE_WORDS_RE = re.compile(
    r"(?<![a-z])(?:airlines?|air\s*lines|airways|boarding\s*pass|jetblue|lufthansa|"
    r"ryanair|easyjet)(?![a-z])",
    re.IGNORECASE,
)


def _match_one(entitlement: str, text: str, *, at_hotel: bool, at_airport: bool) -> str | None:
    """The phrase in *text* that makes the line this entitlement's kind, or None.

    *at_hotel* / *at_airport* are the context of the WHOLE entry (``_context``); the
    line's own words can only add to it.
    """
    if entitlement == LOUNGE_MEMBERSHIP:
        brand = _BRAND_RE.search(text) or _BARE_LOUNGE_RE.search(text)
        member = _MEMBERSHIP_RE.search(text)
        if brand and member:
            return f"{brand.group(0)} {member.group(0)}"
        return None
    if entitlement == AIRPORT_EXTRAS:
        early = _EARLY_BOARDING_RE.search(text)
        if early:
            return early.group(0)
        if _MEMBERSHIP_RE.search(text) and (_BRAND_RE.search(text) or _BARE_LOUNGE_RE.search(text)):
            return None  # a membership is NR-01, not a per-visit fee
        lounge = _BRAND_RE.search(text) or _LOUNGE_FEE_RE.search(text)
        if lounge is None and (at_airport or _AIRLINE_WORDS_RE.search(text)):
            lounge = _BARE_LOUNGE_RE.search(text)
        return lounge.group(0) if lounge else None
    if entitlement == ROOM_UPGRADE:
        upgrade = _ROOM_UPGRADE_RE.search(text)
        if upgrade:
            return upgrade.group(0)
        if not (at_hotel or _HOTEL_WORDS_RE.search(text)):
            return None
        hit = _CONTEXT_UPGRADE_RE.search(text) or _SUITE_RE.search(text) or _UPGRADE_RE.search(text)
        return hit.group(0) if hit else None
    return None


def _context(
    *, category: str | None, segment_type: str | None, evidence: Iterable[str]
) -> tuple[bool, bool]:
    """(at a hotel, at an airport) for the whole entry.

    From TRUSTED evidence only — any receipt word / line item or the card merchant with
    hotel (airline) words, or a linked hotel (flight) trip segment.  The claimant's
    category may only ADD context ("lodging" / "airfare"), never remove it.
    """
    texts = list(evidence)
    at_hotel = (
        category == "lodging"
        or segment_type == "hotel"
        or any(_HOTEL_WORDS_RE.search(t) for t in texts)
    )
    at_airport = (
        category == "airfare"
        or segment_type == "flight"
        or any(_AIRLINE_WORDS_RE.search(t) for t in texts)
    )
    return at_hotel, at_airport


@dataclass(frozen=True)
class Detection:
    """Why a line is an entitlement's kind.

    matched : the phrase that matched (for the reason; never decides on its own).
    trusted : the phrase came from the receipt's OCR words or the card merchant.
    amount  : the sum of the TRUSTED receipt line items that match (the part of a
              folio that is the upgrade / lounge fee), or None -> the whole claim.
    """

    entitlement: str
    matched: str
    trusted: bool
    amount: Decimal | None = None


def _texts(values: Iterable[str | None]) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def detect(
    entitlement: str,
    *,
    category: str | None,
    typed: Iterable[str | None],
    trusted: Iterable[str | None],
    line_items: Iterable[tuple[str, Any]] | None = None,
    segment_type: str | None = None,
) -> Detection | None:
    """Detect whether a line is *entitlement*'s kind (typed OR trusted words; see module).

    *segment_type* is the type of the entry's linked trip segment ("hotel", "flight", ...)
    or None.  Mileage lines (computed, no receipt) are never such a fee.
    """
    if entitlement not in ENTITLEMENTS or category == "mileage":
        return None
    items = [(d, a) for d, a in (line_items or ()) if isinstance(d, str) and d.strip()]
    trusted_texts = _texts(trusted)
    at_hotel, at_airport = _context(
        category=category,
        segment_type=segment_type,
        evidence=[*trusted_texts, *(d for d, _ in items)],
    )

    def match(text: str) -> str | None:
        return _match_one(entitlement, text, at_hotel=at_hotel, at_airport=at_airport)

    item_hits: list[tuple[str, Decimal | None]] = []
    for desc, amount in items:
        hit = match(desc)
        if hit is not None:
            item_hits.append((hit, pr.cents(amount)))
    trusted_hit = next((h for t in trusted_texts if (h := match(t))), None)
    if item_hits:
        amounts = [a for _, a in item_hits]
        total = sum((a for a in amounts if a is not None), Decimal(0))
        return Detection(
            entitlement,
            item_hits[0][0],
            trusted=True,
            amount=total if all(a is not None for a in amounts) and total > 0 else None,
        )
    if trusted_hit is not None:
        return Detection(entitlement, trusted_hit, trusted=True)
    typed_hit = next((h for t in _texts(typed) if (h := match(t))), None)
    if typed_hit is not None:
        return Detection(entitlement, typed_hit, trusted=False)
    return None


# ---------------------------------------------------------------------------
# Rule settings
# ---------------------------------------------------------------------------


def rule_entitlement(defn: Mapping[str, Any]) -> str:
    value = defn.get("entitlement")
    if not isinstance(value, str) or value not in ENTITLEMENTS:
        raise ValueError("entitlement is not a known grade entitlement")
    return value


def rule_min_grade(defn: Mapping[str, Any]) -> str:
    """The grade threshold ("SVP"); a value that is not a ladder code raises ValueError
    (the caller reports an honest not-evaluated flag; never treated as entitled)."""
    value = defn.get("min_grade", grade_ladder.SENIOR_EXCEPTION_MIN_GRADE)
    if grade_ladder.grade(value) is None:
        raise ValueError("min_grade is not on the grade ladder")
    return str(value)


def rule_clause(defn: Mapping[str, Any]) -> str:
    value = defn.get("corpus_clause_ref")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_CLAUSE.get(str(defn.get("entitlement")), "DEF-10")


def grade_label(code: str) -> str:
    """ "SVPs" — the plural short form the clauses use."""
    g = grade_ladder.grade(code)
    return f"{g.short}s" if g is not None else f"{code}s"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _amount_text(amount: Decimal | None, currency: str | None) -> tuple[str, dict[str, Any]]:
    usd, convertible = pr.amount_in_usd(amount, currency)
    observed: dict[str, Any] = {
        "amount": str(amount) if amount is not None else None,
        "currency": currency,
        "amount_usd": str(usd) if usd is not None else None,
    }
    if amount is None:
        return "", observed
    cur = (currency or "USD").strip().upper() or "USD"
    if not convertible:
        observed["could_not_verify"] = "currency"
        return f" ({amount:,.2f} {cur}; no USD rate for {cur})", observed
    if cur == "USD" or usd is None:
        return f" ({pr.money(amount)})", observed
    return f" ({amount:,.2f} {cur} ≈ {pr.money(usd)})", observed


def evaluate(
    defn: Mapping[str, Any],
    *,
    detection: Detection,
    traveller_grade: str | None,
    claim_amount: Any,
    currency: str | None,
) -> pr.Finding:
    """The finding for one detected line (see module docstring for the three outcomes).

    *traveller_grade* must be the report OWNER's grade of record from the database.
    A malformed rule (unknown entitlement / grade threshold) raises ValueError.
    """
    entitlement = rule_entitlement(defn)
    min_grade = rule_min_grade(defn)
    clause = rule_clause(defn)
    who = grade_label(min_grade)
    allowance = ALLOWANCE[entitlement]
    amount = detection.amount if detection.amount is not None else pr.cents(claim_amount)
    amount_text, money_obs = _amount_text(amount, currency)
    source = (
        "named on the receipt or card"
        if detection.trusted
        else "named on the entry, not on a receipt or card"
    )
    head = f"{LINE_LABEL[entitlement]} ('{detection.matched}', {source}){amount_text}"
    known = grade_ladder.grade(traveller_grade) is not None
    observed: dict[str, Any] = {
        **money_obs,
        "grade_entitlement": entitlement,
        "required_grade": min_grade,
        "traveller_grade": traveller_grade if known else None,
        "matched": detection.matched,
        "evidence": "receipt_or_card" if detection.trusted else "entry_text",
        "line_item_amount": detection.amount is not None,
    }
    entitled = grade_ladder.at_or_above(traveller_grade, min_grade)
    if entitled is True:
        title = grade_ladder.title_of(traveller_grade)
        return pr.Finding(
            clause_ref=clause,
            flag_type=POLICY_ADVISORY,
            severity="advisory",
            reason=(
                f"{head}: {clause} allows {allowance} for {who} and above; the traveler's "
                f"grade of record is {title}, so it is in policy."
            ),
            observed=observed,
        )
    if entitled is False:
        title = grade_ladder.title_of(traveller_grade)
        extra = ""
        if entitlement == AIRPORT_EXTRAS:
            hours = defn.get("delay_exception_hours", DEFAULT_DELAY_HOURS)
            extra = (
                f" {clause} also allows them after a flight delay of more than {hours} hours; "
                "Atlas holds no delay data, so state the delay in a justification."
            )
        return pr.Finding(
            clause_ref=clause,
            flag_type=POLICY_VIOLATION,
            severity="blocking",
            reason=(
                f"{head}: {clause} reimburses {allowance} only for {who} and above, and the "
                f"traveler's grade of record is {title}.{extra} Only an approver at "
                f"{min_grade} level or above may grant the exception (never auto-approved)."
            ),
            requires_approver=min_grade,
            observed={**observed, "justifiable_exception": True},
        )
    return pr.Finding(
        clause_ref=clause,
        flag_type=APPROVAL_REQUIRED,
        severity="advisory",
        reason=(
            f"{head}: {clause} reimburses {allowance} only for {who} and above, and no grade "
            "is recorded for the traveler, so it cannot be applied automatically; an approver "
            f"at {min_grade} level or above must confirm it (never auto-approved)."
        ),
        requires_approver=min_grade,
        observed={**observed, "could_not_verify": "traveller_grade"},
    )
