"""Spend caps, gift disclosure and mileage — pure, deterministic, Decimal (product gap 2).

Implements exactly what the TEP-FY26 clauses say (``database/seeds/corpus/policy.py``):

  MEAL-05  Team meals: the most senior employee present pays and expenses, listing
           all attendees; per-head dinner caps Tier 1 $90 / Tier 2 $75 / Tier 3 $60.
  ENT-02   Client meals up to $150/head (Tier 1) / $120 (Tier 2) / $100 (Tier 3);
           larger events require pre-approval.
  ENT-04   Entertainment of government officials requires Legal/Compliance
           pre-approval.  Gifts > $100 require disclosure.
  MIL-01   Reimbursed at the prevailing IRS standard mileage rate.
  MIL-02   Business miles only; normal commute miles are deducted from any claim.
  MIL-03   Trips > 300 miles each way: capped at the equivalent lowest logical
           airfare unless air travel was impractical.
  REF-08   No fuel receipt for personal-vehicle mileage (RECEIPT-REQ exemption).

LLM-free, I/O-free and independent of ``policy_eval`` (which wraps these outcomes
into ``RuleVerdict``s and resolves city tiers).  Money and distances are
``Decimal``; floats appear only in ``observed`` (JSON display values).

Outcome semantics consumed by ``presubmit.verdicts_to_flags``:
  * ``in_policy=False``                         -> policy_violation (blocking;
    a ``per_head_cap`` breach with a manager approver is justifiable);
  * ``in_policy=True`` + ``requires_approver``   -> approval_required (advisory
    at submit, never auto-approved: a human must confirm what Atlas could not
    verify);
  * ``None`` from an evaluator                   -> the rule is skipped
    (not_evaluated advisory).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

PER_HEAD_CAP = "per_head_cap"
GIFT_DISCLOSURE = "gift_disclosure"
MILEAGE = "mileage"
SPEND_RULE_TYPES: frozenset[str] = frozenset({PER_HEAD_CAP, GIFT_DISCLOSURE, MILEAGE})

# Categories added by product gap 2.  They are MANUAL-ONLY: receipt / card
# categorisation never produces them (OCR paths unchanged), and a mileage
# entry is only ever created / edited through the mileage computation.
MILEAGE_CATEGORY = "mileage"
SPEND_CATEGORIES: frozenset[str] = frozenset({"mileage", "entertainment", "gift"})
# Entry fields each category may carry (anything else is refused / cleared).
ATTENDEE_CATEGORIES: frozenset[str] = frozenset({"meals", "entertainment"})
DISCLOSURE_CATEGORIES: frozenset[str] = frozenset({"gift", "entertainment"})
# REF-08 / EXP-03: personal-vehicle mileage is documented by route and
# distance — there is no purchase to receipt.
RECEIPT_EXEMPT_CATEGORIES: frozenset[str] = frozenset({MILEAGE_CATEGORY})
RECEIPT_EXEMPT_CLAUSE = "REF-08"

# MEAL-05 applies to a TEAM meal: a meals line naming 2+ people (the claimant
# included).  A team meal is governed by its per-head cap, not by the
# claimant's own MEAL-01 per diem — only while an active per-head cap covers it
# (``team_meal_cap_minimum``); otherwise the per diem stays (fail closed).
TEAM_MEAL_MIN_ATTENDEES = 2
MAX_ATTENDEES = 500

CENT = Decimal("0.01")
TENTH = Decimal("0.1")
MILES_PER_KM_DIVISOR = Decimal("1.609344")  # international mile, exact
MAX_DISTANCE = Decimal("10000")  # per entry, in the entered unit (input sanity)
DISTANCE_UNITS: tuple[str, ...] = ("mi", "km")

MILEAGE_CHECKS: tuple[str, ...] = ("rate", "commute_offset", "air_cap")
DISCLOSURE_FIELDS: tuple[str, ...] = (
    "recipient",
    "recipient_organization",
    "business_purpose",
    "government_official",
)
REQUIRED_DISCLOSURE_FIELDS: tuple[str, ...] = (
    "recipient",
    "business_purpose",
    "government_official",
)


def to_decimal(value: Any) -> Decimal | None:
    """A finite Decimal from an int / float / numeric string, else None (bools refused)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def usd(value: Decimal) -> str:
    """'$60' for whole dollars, '$60.50' otherwise (the corpus' own style)."""
    q = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return f"${q:,.0f}" if q == q.to_integral_value() else f"${q:,.2f}"


def _money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Facts carried on the EvalItem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MileageFacts:
    """A mileage entry as stored (``expenses.mileage``), parsed.  Distances in ``unit``."""

    distance: Decimal | None  # one way, as entered
    unit: str
    round_trip: bool
    commute_offset: Decimal | None  # in ``unit``; None = not recorded
    air_impractical: bool
    origin: str | None = None
    destination: str | None = None
    rate_per_mile: Decimal | None = None  # the rate the server used at save time


@dataclass(frozen=True)
class SpendFacts:
    """Product-gap-2 entry facts (attendees, disclosure, mileage) + the exact amount."""

    category: str
    amount: Decimal | None
    attendee_count: int | None = None
    recipient: str | None = None
    recipient_organization: str | None = None
    business_purpose: str | None = None
    government_official: bool | None = None
    mileage: MileageFacts | None = None
    currency: str | None = None  # the line's own currency; None = not known


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def parse_mileage(raw: Any) -> MileageFacts | None:
    """``expenses.mileage`` JSON -> MileageFacts (None when absent / not an object)."""
    if not isinstance(raw, dict):
        return None
    unit = raw.get("unit") if raw.get("unit") in DISTANCE_UNITS else "mi"
    return MileageFacts(
        distance=to_decimal(raw.get("distance")),
        unit=str(unit),
        round_trip=raw.get("round_trip") is True,
        commute_offset=to_decimal(raw.get("commute_offset")),
        air_impractical=raw.get("air_impractical") is True,
        origin=_text(raw.get("origin")),
        destination=_text(raw.get("destination")),
        rate_per_mile=to_decimal(raw.get("rate_per_mile")),
    )


def spend_facts(
    *,
    category: str,
    amount: Any,
    attendee_count: Any = None,
    disclosure: Any = None,
    mileage: Any = None,
    currency: Any = None,
) -> SpendFacts:
    """Build SpendFacts from an entry's stored fields (junk -> None, never raises)."""
    count = attendee_count
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        count = None
    disc = disclosure if isinstance(disclosure, dict) else {}
    official = disc.get("government_official")
    return SpendFacts(
        category=category,
        amount=to_decimal(amount),
        attendee_count=count,
        recipient=_text(disc.get("recipient")),
        recipient_organization=_text(disc.get("recipient_organization")),
        business_purpose=_text(disc.get("business_purpose")),
        government_official=official if isinstance(official, bool) else None,
        mileage=parse_mileage(mileage),
        currency=_text(currency),
    )


def restore_spend(fields: dict[str, Any]) -> dict[str, Any]:
    """EvalItem kwargs whose ``spend`` went through ``dataclasses.asdict`` (a plain
    dict, e.g. a /policy/evaluate unit) -> the same kwargs with a SpendFacts again.
    Anything that is not a SpendFacts / dict becomes None (never raises)."""
    raw = fields.get("spend")
    if raw is None or isinstance(raw, SpendFacts):
        return fields
    out = dict(fields)
    if not isinstance(raw, dict):
        out["spend"] = None
        return out
    m = raw.get("mileage")
    mileage = None
    if isinstance(m, dict):
        mileage = {
            "distance": m.get("distance"),
            "unit": m.get("unit"),
            "round_trip": m.get("round_trip"),
            "commute_offset": m.get("commute_offset"),
            "air_impractical": m.get("air_impractical"),
            "origin": m.get("origin"),
            "destination": m.get("destination"),
            "rate_per_mile": m.get("rate_per_mile"),
        }
    out["spend"] = spend_facts(
        category=str(raw.get("category") or ""),
        amount=raw.get("amount"),
        attendee_count=raw.get("attendee_count"),
        disclosure={k: raw.get(k) for k in DISCLOSURE_FIELDS},
        mileage=mileage,
        currency=raw.get("currency"),
    )
    return out


def probe_spend_facts(category: str, amount: Any) -> SpendFacts:
    """Complete spend facts for a rule-authoring round-trip probe (never a real line):
    a team of two, a complete disclosure and a 10-mile trip, so every spend rule
    evaluates (non-skip) on its probe."""
    return spend_facts(
        category=category,
        amount=amount,
        attendee_count=TEAM_MEAL_MIN_ATTENDEES,
        disclosure={
            "recipient": "Probe Recipient",
            "business_purpose": "Probe purpose",
            "government_official": False,
        },
        mileage={"distance": "10", "unit": "mi", "commute_offset": "0"},
    )


# ---------------------------------------------------------------------------
# Mileage computation (MIL-01 / MIL-02) — shared by the server write path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MileageComputation:
    one_way_miles: Decimal
    claimed_miles: Decimal  # one way, doubled for a round trip
    commute_offset_miles: Decimal
    billable_miles: Decimal
    rate_per_mile: Decimal
    amount: Decimal


def to_miles(distance: Decimal, unit: str) -> Decimal:
    """Distance in miles, to 0.1 mile (ROUND_HALF_UP).  ``unit``: 'mi' or 'km'."""
    if unit == "km":
        return (distance / MILES_PER_KM_DIVISOR).quantize(TENTH, rounding=ROUND_HALF_UP)
    if unit != "mi":
        raise ValueError(f"unknown distance unit {unit!r}")
    return distance.quantize(TENTH, rounding=ROUND_HALF_UP)


def compute_mileage(
    *,
    distance: Decimal,
    unit: str,
    round_trip: bool,
    commute_offset: Decimal,
    rate_per_mile: Decimal,
) -> MileageComputation:
    """Billable miles and the reimbursable amount (Decimal, cents ROUND_HALF_UP).

    billable = one-way miles (x2 for a round trip) - commute offset miles (MIL-02);
    amount = billable x rate (MIL-01).  Raises ValueError on a non-positive
    distance / rate, a negative offset, or nothing left to reimburse.
    """
    if distance <= 0:
        raise ValueError("distance must be greater than 0")
    if commute_offset < 0:
        raise ValueError("commute offset cannot be negative")
    if rate_per_mile <= 0:
        raise ValueError("the mileage rate must be greater than 0")
    one_way = to_miles(distance, unit)
    if one_way <= 0:
        raise ValueError("distance must be at least 0.1 mile")
    claimed = one_way * 2 if round_trip else one_way
    offset = to_miles(commute_offset, unit)
    billable = claimed - offset
    if billable <= 0:
        raise ValueError(
            "the commute offset is at least the trip distance — no business miles remain"
        )
    amount = (billable * rate_per_mile).quantize(CENT, rounding=ROUND_HALF_UP)
    return MileageComputation(
        one_way_miles=one_way,
        claimed_miles=claimed,
        commute_offset_miles=offset,
        billable_miles=billable,
        rate_per_mile=rate_per_mile,
        amount=amount,
    )


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


def _categories(definition: Any) -> list[str] | None:
    cats = definition.get("expense_categories") if isinstance(definition, dict) else None
    if not isinstance(cats, list):
        return None
    return [c for c in cats if isinstance(c, str)]


def _team_meal_minimum(definition: Any) -> int | None:
    """The attendee count from which a team-meal per-head cap reaches a meals line."""
    if not isinstance(definition, dict):
        return None
    minimum = definition.get("min_attendees", TEAM_MEAL_MIN_ATTENDEES)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        minimum = TEAM_MEAL_MIN_ATTENDEES
    return int(minimum)


def team_meal_cap_minimum(rules: Iterable[tuple[Any, Any, Any]]) -> int | None:
    """The smallest attendee count any team-meal per-head cap in *rules* covers.

    *rules* are ``(rule_type, applies_to, definition)`` of the rule set being
    evaluated (enum members or their values).  A meals line naming at least this
    many people is governed by a per-head cap (MEAL-05), so — and ONLY then — the
    claimant's own per diem does not apply to it.  None when the rule set has no
    such cap (MEAL-05 archived / never seeded): every meals line keeps the per
    diem, whatever attendee count it claims (fail closed — an attendee count
    alone never switches a meal rule off).
    """
    found: list[int] = []
    for rule_type, applies_to, definition in rules:
        rt = getattr(rule_type, "value", rule_type)
        at = getattr(applies_to, "value", applies_to)
        if rt == PER_HEAD_CAP and at == "meals":
            minimum = _team_meal_minimum(definition)
            if minimum is not None:
                found.append(minimum)
    return min(found) if found else None


def team_meal_covered(
    item_type: str | None, facts: SpendFacts | None, cover_minimum: int | None
) -> bool:
    """A meals line a per-head cap of the evaluated rule set governs (see above)."""
    return (
        cover_minimum is not None
        and item_type == "meals"
        and facts is not None
        and facts.attendee_count is not None
        and facts.attendee_count >= cover_minimum
    )


def spend_rule_applies(
    rule_type: str,
    applies_to: str,
    definition: Any,
    *,
    item_type: str,
    expense_category: str | None,
    facts: SpendFacts | None,
) -> bool:
    """Whether a product-gap-2 rule is evaluated for an item at all.

    These rules reach ONLY the lines they govern, so they never add a verdict
    (or a not_evaluated note) to any other expense line or trip option:
      * per_head_cap on ``meals``: a TEAM meal (``min_attendees``+ people, default 2);
      * per_head_cap / gift_disclosure / mileage on ``all``: lines whose category
        is listed in ``expense_categories`` (a missing / malformed list reaches
        nothing — the activation validator refuses it).
    """
    if rule_type == PER_HEAD_CAP and applies_to == "meals":
        minimum = _team_meal_minimum(definition)
        if minimum is None:
            return False
        return (
            item_type == "meals"
            and facts is not None
            and facts.attendee_count is not None
            and facts.attendee_count >= minimum
        )
    if applies_to != "all":
        return False
    cats = _categories(definition)
    return bool(cats) and expense_category is not None and expense_category in (cats or [])


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendOutcome:
    in_policy: bool
    reason: str
    requires_approver: str | None
    observed: dict = field(default_factory=dict)
    clause_ref: str | None = None  # the clause the outcome cites


def _clause(definition: dict, fallback: str | None) -> str | None:
    ref = definition.get("corpus_clause_ref")
    return ref if isinstance(ref, str) and ref else fallback


def evaluate_per_head_cap(
    definition: dict,
    facts: SpendFacts | None,
    *,
    tier: dict,
    city: str | None,
    claimed_tier: tuple[dict, str] | None,
    clause_ref: str | None,
) -> SpendOutcome | None:
    """MEAL-05 / ENT-02: amount <= per-head cap (by city tier) x attendees.

    * ``tier`` is the tier of the VERIFIED city (none -> the lowest tier);
    * a missing attendee count is evaluated as ONE person (fail closed — the
      most restrictive limit) and said so;
    * ``claimed_tier`` (the tier of a city the claimant named but nothing
      verified) never clears the rule: a line within the cap only there is
      still a violation, marked ``within_cap_if_verified`` for the approver.
    Returns None (skip) when the amount is unknown.
    """
    ref = _clause(definition, clause_ref)
    if facts is None or facts.amount is None:
        return None
    cap = to_decimal(tier.get("cap"))
    if cap is None or cap < 0:
        return None
    amount = facts.amount
    heads = facts.attendee_count or 1
    limit = cap * heads
    in_policy = amount <= limit
    label = str(tier.get("label") or tier.get("tier") or "default")
    kind = "Team meal" if facts.category == "meals" else "Client entertainment"
    who = f"{heads} attendee" if heads == 1 else f"{heads} attendees"
    observed: dict[str, Any] = {
        "amount_usd": _money(amount),
        "attendee_count": facts.attendee_count,
        "per_head_cap": _money(cap),
        "limit_usd": _money(limit),
        "per_head_usd": _money(amount / heads),
        "tier": tier.get("tier"),
        "tier_label": label,
        "city": city,
        "currency": definition.get("currency", "USD"),
    }
    basis = f"{label} {usd(cap)}/head x {who} = {usd(limit)}"
    approver = definition.get("exception_approver")
    approver = approver if isinstance(approver, str) and approver else None
    if in_policy:
        reason = f"{kind} {usd(amount)} for {who} is within the {ref} cap ({basis})."
        # Review fix: the attendee count is the claimant's own word — it multiplies
        # the cap (and, for a team meal, replaces the per diem), and Atlas captures
        # no attendee list to verify it.  A claimed count therefore always reaches a
        # human (approval_required advisory, never auto-approved): a team meal from
        # 2 attendees, client entertainment whenever attendees are claimed.
        claimed = facts.attendee_count is not None and (
            facts.category != "meals" or facts.attendee_count >= 2
        )
        if not claimed:
            return SpendOutcome(True, reason, None, observed, ref)
        listing = "MEAL-05" if facts.category == "meals" else "ENT-01"
        observed["could_not_verify"] = "attendees"
        return SpendOutcome(
            True,
            f"{reason} Atlas cannot verify the attendee count — your "
            f"{approver or 'manager'} must confirm the list of attendees ({listing}).",
            approver or "manager",
            observed,
            ref,
        )
    parts = [f"{kind} {usd(amount)} for {who} is over the {ref} cap ({basis})."]
    if facts.attendee_count is None:
        observed["attendee_count_missing"] = True
        parts.append(
            "No attendee count was recorded, so it was checked as one person — "
            "enter the number of attendees (you included)."
        )
    if city is None:
        parts.append(f"No verified city, so the {label} cap applies.")
    if claimed_tier is not None:
        ctier, cname = claimed_tier
        ccap = to_decimal(ctier.get("cap"))
        if ccap is not None and ccap > cap:
            within = amount <= ccap * heads
            observed["claimed_city"] = cname
            observed["claimed_tier"] = ctier.get("tier")
            observed["claimed_cap"] = _money(ccap)
            observed["within_cap_if_verified"] = within
            if within:
                observed["unverified"] = ["city"]
                parts.append(
                    f"It would be within the cap for {cname} ({usd(ccap)}/head), "
                    "which nothing verifies — your approver must confirm the city."
                )
    if ref == "ENT-02":
        parts.append("Larger client events require pre-approval.")
    if approver:
        parts.append(f"Exceptions need {approver} approval: add a justification.")
    return SpendOutcome(False, " ".join(parts), approver, observed, ref)


def evaluate_gift_disclosure(
    definition: dict, facts: SpendFacts | None, *, clause_ref: str | None
) -> SpendOutcome | None:
    """ENT-04: a gift over the threshold needs a complete disclosure; a government
    official needs Legal/Compliance pre-approval (Atlas cannot verify it -> human).

    Strictly greater than the threshold ($100.00 needs none, $100.01 does).  An
    unknown amount on a disclosure category needs the disclosure (fail closed).
    """
    ref = _clause(definition, clause_ref)
    if facts is None:
        return None
    threshold = to_decimal(definition.get("threshold_usd"))
    if threshold is None:
        return None
    disclosure_cats = definition.get("disclosure_categories")
    if not isinstance(disclosure_cats, list):
        disclosure_cats = ["gift"]
    observed: dict[str, Any] = {
        "expense_category": facts.category,
        "threshold_usd": _money(threshold),
        "amount_usd": _money(facts.amount) if facts.amount is not None else None,
        "disclosure": {
            "recipient": facts.recipient,
            "recipient_organization": facts.recipient_organization,
            "business_purpose": facts.business_purpose,
            "government_official": facts.government_official,
        },
    }
    amount_text = usd(facts.amount) if facts.amount is not None else "an unknown amount"
    needs = facts.category in disclosure_cats and (facts.amount is None or facts.amount > threshold)
    observed["disclosure_required"] = needs
    if needs:
        missing = []
        if facts.recipient is None:
            missing.append("recipient")
        if facts.business_purpose is None:
            missing.append("business purpose")
        if facts.government_official is None:
            missing.append("whether the recipient is a government official")
        if missing:
            observed["missing_fields"] = missing
            return SpendOutcome(
                False,
                f"Gift of {amount_text} is over the {ref} disclosure threshold of "
                f"{usd(threshold)}: complete the gift disclosure — " + ", ".join(missing) + ".",
                None,
                observed,
                ref,
            )
    if facts.government_official is True:
        approver = definition.get("government_official_approver") or "Legal/Compliance"
        observed["could_not_verify"] = "legal_compliance_pre_approval"
        what = "Gift" if facts.category == "gift" else "Entertainment"
        return SpendOutcome(
            True,
            f"{what} involving a government official requires {approver} pre-approval "
            f"({ref}, FCPA / UK Bribery Act) — {_recipient_text(facts)}. Atlas cannot "
            "verify that pre-approval — your approver must confirm it before approving.",
            str(approver),
            observed,
            ref,
        )
    if needs:
        return SpendOutcome(
            True,
            f"Gift of {amount_text} is over {usd(threshold)}; the {ref} disclosure "
            f"is recorded (recipient: {facts.recipient}).",
            None,
            observed,
            ref,
        )
    return SpendOutcome(
        True,
        f"No {ref} disclosure needed ({amount_text} is not a gift over {usd(threshold)}).",
        None,
        observed,
        ref,
    )


def _route(m: MileageFacts) -> str:
    """'Boston → Providence' — the trip an approver is asked to confirm (review fix:
    the exception list alone must identify it).  Unknown ends are said out loud."""
    if m.origin is None and m.destination is None:
        return "route not recorded"
    return f"{m.origin or 'origin not recorded'} → {m.destination or 'destination not recorded'}"


def _route_observed(m: MileageFacts) -> dict[str, Any]:
    return {"origin": m.origin, "destination": m.destination}


def _recipient_text(facts: SpendFacts) -> str:
    """'Jane Roe (Port Authority), purpose: tour' for the ENT-04 human check."""
    who = facts.recipient or "recipient not recorded"
    if facts.recipient_organization:
        who = f"{who} ({facts.recipient_organization})"
    purpose = facts.business_purpose or "not recorded"
    return f"recipient: {who}; business purpose: {purpose}"


def _mileage_approver(definition: dict) -> str:
    approver = definition.get("exception_approver")
    return approver if isinstance(approver, str) and approver else "manager"


def evaluate_mileage(
    definition: dict, facts: SpendFacts | None, *, clause_ref: str | None
) -> SpendOutcome | None:
    """MIL-01 / MIL-02 / MIL-03 by ``definition["check"]``.

    * rate (MIL-01): the claimed amount must EQUAL billable miles x the rule's
      rate (to the cent) — missing / invalid trip details fail (blocking);
    * commute_offset (MIL-02): a commute offset must be recorded (0 allowed);
    * Atlas has no route / distance data and no home / office data, so an
      in-policy MIL-01 / MIL-02 outcome still names the approver (``could_not_verify``
      'distance' / 'commute_offset'): approval_required, never auto-approved;
    * air_cap (MIL-03): one-way miles above the limit cannot be verified against
      the lowest logical airfare (Atlas has no fare for the route) -> the approver
      must check it (approval_required; never auto-approved).
    Returns None (skip) for an unknown check or when MIL-02/03 lack trip details
    (MIL-01 already blocks those).
    """
    ref = _clause(definition, clause_ref)
    check = definition.get("check")
    if facts is None or check not in MILEAGE_CHECKS:
        return None
    m = facts.mileage
    if check == "rate":
        rate = to_decimal(definition.get("rate_per_mile"))
        if rate is None or rate <= 0:
            return None
        if facts.currency is not None and facts.currency != "USD":
            # Review fix (defence in depth): the MIL-01 amount is computed in USD;
            # the same figure labelled in another currency is never in policy.
            return SpendOutcome(
                False,
                f"A mileage line is always in USD ({ref}); this one is in "
                f"{facts.currency}. Save the mileage again so Atlas recomputes it in USD.",
                None,
                {"rate_per_mile": float(rate), "currency": facts.currency},
                ref,
            )
        if m is None or m.distance is None:
            return SpendOutcome(
                False,
                f"Mileage line has no trip details (distance, from / to) — {ref} cannot "
                "compute it. Delete it and use Add mileage.",
                None,
                {"rate_per_mile": float(rate), "mileage_details_missing": True},
                ref,
            )
        try:
            comp = compute_mileage(
                distance=m.distance,
                unit=m.unit,
                round_trip=m.round_trip,
                commute_offset=m.commute_offset or Decimal("0"),
                rate_per_mile=rate,
            )
        except ValueError as exc:
            return SpendOutcome(
                False,
                f"Mileage trip details are invalid ({exc}) — {ref}.",
                None,
                {"rate_per_mile": float(rate), **_route_observed(m)},
                ref,
            )
        route = _route(m)
        observed: dict[str, Any] = {
            **_route_observed(m),
            "one_way_miles": float(comp.one_way_miles),
            "claimed_miles": float(comp.claimed_miles),
            "commute_offset_miles": float(comp.commute_offset_miles),
            "billable_miles": float(comp.billable_miles),
            "rate_per_mile": float(rate),
            "computed_amount_usd": _money(comp.amount),
            "amount_usd": _money(facts.amount) if facts.amount is not None else None,
        }
        if facts.amount is not None and facts.amount == comp.amount:
            # The arithmetic is Atlas's, but the distance is the claimant's word:
            # Atlas has no route data, so a human confirms it (never auto-approved).
            approver = _mileage_approver(definition)
            observed["could_not_verify"] = "distance"
            return SpendOutcome(
                True,
                f"Mileage {route}: {comp.billable_miles} mi x ${rate}/mi = "
                f"{usd(comp.amount)} ({ref}). Atlas cannot verify the route distance — "
                f"your {approver} must confirm it.",
                approver,
                observed,
                ref,
            )
        stale = m.rate_per_mile is not None and m.rate_per_mile != rate
        hint = (
            f" The line was computed at ${m.rate_per_mile}/mi; the current rate is "
            f"${rate}/mi — save the mileage again to recompute it."
            if stale
            else " Save the mileage again so Atlas recomputes it."
        )
        claimed = usd(facts.amount) if facts.amount is not None else "an unknown amount"
        return SpendOutcome(
            False,
            f"Mileage {route}: amount {claimed} does not equal {comp.billable_miles} "
            "business mi x "
            f"${rate}/mi = {usd(comp.amount)} ({ref}).{hint}",
            None,
            observed,
            ref,
        )
    if m is None or m.distance is None:
        return None
    if check == "commute_offset":
        if m.commute_offset is None:
            return SpendOutcome(
                False,
                f"Normal commute miles must be deducted from a mileage claim ({ref}) — "
                f"record your commute offset for {_route(m)} (0 if the trip replaced no "
                "commute).",
                None,
                {"commute_offset_recorded": False, **_route_observed(m)},
                ref,
            )
        # Atlas has no home / office data, so the offset is the claimant's word too.
        approver = _mileage_approver(definition)
        return SpendOutcome(
            True,
            f"Mileage {_route(m)}: commute offset of {m.commute_offset} {m.unit} deducted "
            f"({ref}). Atlas cannot verify your normal commute — your {approver} must "
            "confirm the offset.",
            approver,
            {
                **_route_observed(m),
                "commute_offset_recorded": True,
                "commute_offset": float(m.commute_offset),
                "could_not_verify": "commute_offset",
            },
            ref,
        )
    # air_cap (MIL-03)
    limit = to_decimal(definition.get("max_one_way_miles"))
    if limit is None or limit <= 0:
        return None
    try:
        one_way = to_miles(m.distance, m.unit)
    except ValueError:
        return None
    route = _route(m)
    observed = {
        **_route_observed(m),
        "one_way_miles": float(one_way),
        "max_one_way_miles": float(limit),
        "air_impractical": m.air_impractical,
    }
    if one_way <= limit:
        return SpendOutcome(
            True,
            f"Mileage {route}: {one_way} mi each way is within the {limit}-mile limit " f"({ref}).",
            None,
            observed,
            ref,
        )
    approver = _mileage_approver(definition)
    observed["could_not_verify"] = "lowest_logical_airfare"
    if m.air_impractical:
        reason = (
            f"Mileage {route}: {one_way} mi each way is over {limit} miles ({ref}); you "
            "marked air travel "
            f"as impractical, which Atlas cannot verify — your {approver} must confirm it."
        )
    else:
        reason = (
            f"Mileage {route}: {one_way} mi each way is over {limit} miles: {ref} caps "
            "reimbursement at the "
            "equivalent lowest logical airfare unless air travel was impractical. Atlas "
            f"has no airfare for this route, so it could not verify the cap — your "
            f"{approver} must compare the claim with the airfare."
        )
    return SpendOutcome(True, reason, approver, observed, ref)
