"""Process & timing policy rules — deterministic evaluators (product gap 2, stage 2).

Pure functions, no DB, no LLM, no clock: every date-dependent evaluator takes the
reference day (``today`` / ``booked_on``) as an argument so callers and tests inject
it.  Money is ``Decimal`` (quantized to the cent before any comparison); percentages
are compared exactly by cross-multiplication, never as rounded floats.

Implemented clauses (TEP-FY26 corpus, ``database/seeds/corpus/policy.py``; figures from
``database/seeds/corpus/figures.py``):

* EXP-01 — "submit expense reports within 30 days of trip completion; reports > 60
  days old require VP approval and may be treated as taxable."
  (``submission_deadline``: day 30 on time, day 31 late (advisory), day 61 stale
  (VP approver); kind ``submission_deadline``.)
* EXP-04 — "a signed missing-receipt affidavit is allowed up to $75 and no more than 3
  times per quarter."  (``affidavit``: a modifier of the RECEIPT-REQ rule.)
* PTA-02 — "domestic flights >= 14 days ahead; international >= 21 days ahead …
  Shorter lead time requires a documented business reason."  (``advance_booking``.)
* PTA-05 — "Trip changes must be re-approved if total cost increases > 10%."
  (``trip_change``.)
* AIR-04 — "SVPs and above may book business class on flights > 6 hours."
  (``role_cabin_exception``: a modifier of the AIR-CABIN rule, decided from the
  traveller's grade of record — ``users.grade`` of the trip / expense OWNER, read
  from the database, ladder ``config/constants/grades.py``.  An unknown grade is
  never entitled: an approver confirms (known limits, AIR-04 grade).)
* AIR-06 — "travelers must accept fares within $150 (domestic) / $400
  (international) of the cheapest option meeting routing/timing needs."
  (``lowest_fare``.)
* CARD-03 — "Reconcile all card transactions in an expense report within the EXP-01
  deadline."  (``card_reconciliation``: 30 days from the transaction date.)

CARD-04 (lost / stolen card reported within 24 hours) is NOT implemented: Atlas holds
no card-loss events to evaluate.

Each evaluator returns a ``Finding`` (the clause decided something worth a flag) or
None (in policy, nothing to say).  Fail-safe: missing evidence yields an honest
"could not verify" finding (``flag_type="not_evaluated"``), never a silent pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ai_agents.tools.spend_policy import RECEIPT_EXEMPT_CATEGORIES
from config.constants import grades as grade_ladder
from config.constants.fx_rates import UnknownCurrency, convert, normalize_currency
from services.mock_inventory.locations import resolve_airport

CENT = Decimal("0.01")
HUNDRED = Decimal(100)

# Rule kinds (``definition["kind"]``) of the PROCESS rules.  They are not evaluated
# per expense line / trip option by PolicyEvalTool (``is_process_rule``) — the report,
# trip and planner wiring (``ai_agents.agents.process_checks``) runs them.
KIND_SUBMISSION_DEADLINE = "submission_deadline"
KIND_ADVANCE_BOOKING = "advance_booking"
KIND_TRIP_CHANGE = "trip_change_reapproval"
KIND_LOWEST_FARE = "lowest_fare"
KIND_CARD_RECONCILIATION = "card_reconciliation"
# Follow-up (grades): AIR-07 / LOD-05 / NR-01 — SVP-and-above entitlements, evaluated per
# expense line by ``ai_agents/tools/grade_entitlements.py`` (wired from presubmit).
KIND_GRADE_ENTITLEMENT = "grade_entitlement"

# kind -> (rule_type, applies_to) the row is stored under (no enum change).
PROCESS_KIND_SHAPES: dict[str, tuple[str, str]] = {
    KIND_SUBMISSION_DEADLINE: ("category_allow", "all"),
    KIND_ADVANCE_BOOKING: ("category_allow", "flight"),
    KIND_TRIP_CHANGE: ("amount_cap", "all"),
    KIND_LOWEST_FARE: ("amount_cap", "flight"),
    KIND_CARD_RECONCILIATION: ("category_allow", "all"),
    KIND_GRADE_ENTITLEMENT: ("category_allow", "all"),
}
PROCESS_KINDS: frozenset[str] = frozenset(PROCESS_KIND_SHAPES)

DEFAULT_CLAUSE: dict[str, str] = {
    KIND_SUBMISSION_DEADLINE: "EXP-01",
    KIND_ADVANCE_BOOKING: "PTA-02",
    KIND_TRIP_CHANGE: "PTA-05",
    KIND_LOWEST_FARE: "AIR-06",
    KIND_CARD_RECONCILIATION: "CARD-03",
    KIND_GRADE_ENTITLEMENT: "DEF-10",
}

# Modifier keys on existing rules.
AFFIDAVIT_KEYS = ("affidavit_max_usd", "affidavit_max_per_quarter", "affidavit_clause_ref")
ROLE_EXCEPTION_KEYS = (
    "role_exception_class",
    "role_exception_min_hours",
    "role_exception_role",
    "role_exception_clause_ref",
)

# Flag types this module emits (the UI renders every flag type; these two are new).
LATE_SUBMISSION = "late_submission"
UNRECONCILED_CARD = "unreconciled_card"
APPROVAL_REQUIRED = "approval_required"
POLICY_VIOLATION = "policy_violation"
NOT_EVALUATED = "not_evaluated"
MISSING_RECEIPT = "missing_receipt"

# Countries of the airports the (mock) inventory serves.  Anything else is unknown —
# the evaluators then fail safe (stricter tolerance / could-not-verify).
AIRPORT_COUNTRY: dict[str, str] = {"SFO": "US", "JFK": "US", "ORD": "US", "LHR": "GB"}


@dataclass(frozen=True)
class Finding:
    """What one process rule decided (wrapped into a flag by the caller)."""

    clause_ref: str
    flag_type: str
    severity: str  # "blocking" | "advisory"
    reason: str
    requires_approver: str | None = None
    observed: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def process_kind(definition: Any) -> str | None:
    """The process kind of a rule definition, or None."""
    kind = definition.get("kind") if isinstance(definition, Mapping) else None
    return kind if isinstance(kind, str) and kind in PROCESS_KINDS else None


def is_process_rule(definition: Any) -> bool:
    """True for a process rule: never evaluated per line / option by PolicyEvalTool."""
    return process_kind(definition) is not None


def dec(value: Any) -> Decimal | None:
    """A finite Decimal from an int / float / numeric string / Decimal (bools refused)."""
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
    return f"${d.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"


def _int_setting(defn: Mapping[str, Any], key: str, default: int) -> int:
    """A whole-number setting: *default* when absent; present but unreadable raises."""
    if key not in defn:
        return default
    value = defn.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} is not a whole number")
    return value


def _money_setting(defn: Mapping[str, Any], key: str, default: int) -> Decimal:
    if key not in defn:
        return Decimal(default)
    value = cents(defn.get(key))
    if value is None or value < 0:
        raise ValueError(f"{key} is not an amount")
    return value


def _clause(defn: Mapping[str, Any], key: str, default: str) -> str:
    value = defn.get(key)
    return value if isinstance(value, str) and value.strip() else default


def _approver(defn: Mapping[str, Any], key: str = "exception_approver") -> str:
    value = defn.get(key)
    return value if isinstance(value, str) and value.strip() else "manager"


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def as_date(value: Any) -> date | None:
    """A date from a date / datetime / ISO string, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def quarter_bounds(day: date) -> tuple[date, date]:
    """[first day of *day*'s calendar quarter, first day of the next quarter)."""
    first_month = 3 * ((day.month - 1) // 3) + 1
    start = date(day.year, first_month, 1)
    end = date(day.year + 1, 1, 1) if first_month == 10 else date(day.year, first_month + 3, 1)
    return start, end


def quarter_label(day: date) -> str:
    return f"Q{(day.month - 1) // 3 + 1} {day.year}"


def is_international(origin: Any, destination: Any) -> bool | None:
    """True / False when both ends resolve to an airport of known country, else None."""
    countries = []
    for place in (origin, destination):
        code = resolve_airport(str(place)) if place else None
        country = AIRPORT_COUNTRY.get(code or "")
        if country is None:
            return None
        countries.append(country)
    return countries[0] != countries[1]


def pct_increase(old: Decimal, new: Decimal) -> Decimal:
    """(new - old) / old x 100, for display only (comparisons cross-multiply)."""
    return ((new - old) * HUNDRED / old).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# EXP-01 — submission deadline / stale report
# ---------------------------------------------------------------------------


def submission_deadline(
    defn: Mapping[str, Any], *, completed_on: date | None, today: date, basis: str
) -> Finding | None:
    """EXP-01 for one report.

    *completed_on* is the trip's completion (end) date, or — for a report not linked to
    a trip — its earliest expense date (each line's own "completion"; a recent line
    cannot reset the clock for old ones).  *basis* names it in the reason.

    * age <= deadline_days (30): on time — no finding.
    * deadline_days < age <= stale_days (31 … 60): late — advisory ``late_submission``
      (the clause names no approver for it).
    * age > stale_days (61+): stale — ``approval_required`` by the clause's VP approver
      (never auto-approved); "may be treated as taxable" is stated.
    """
    if completed_on is None:
        return None
    clause = _clause(defn, "corpus_clause_ref", "EXP-01")
    deadline = _int_setting(defn, "deadline_days", 30)
    stale = _int_setting(defn, "stale_days", 60)
    age = (today - completed_on).days
    observed = {
        "completed_on": completed_on.isoformat(),
        "basis": basis,
        "age_days": age,
        "deadline_days": deadline,
        "stale_days": stale,
    }
    if age <= deadline:
        return None
    since = f"{_days(age)} after {basis} ({completed_on.isoformat()})"
    if age > stale:
        approver = _approver(defn, "stale_approver")
        return Finding(
            clause_ref=clause,
            flag_type=APPROVAL_REQUIRED,
            severity="advisory",
            reason=(
                f"This report is {since}. {clause}: reports more than {_days(stale)} old "
                f"require {approver} approval and may be treated as taxable — it goes to an "
                "approver and is never auto-approved."
            ),
            requires_approver=approver,
            observed={**observed, "stale": True},
        )
    return Finding(
        clause_ref=clause,
        flag_type=LATE_SUBMISSION,
        severity="advisory",
        reason=(
            f"This report is {since}. {clause} asks for expense reports within "
            f"{_days(deadline)} of trip completion; after {_days(stale)} it needs "
            "VP approval and may be treated as taxable."
        ),
        observed={**observed, "late": True},
    )


# ---------------------------------------------------------------------------
# EXP-04 — missing-receipt affidavit
# ---------------------------------------------------------------------------


def affidavit_configured(receipt_defn: Mapping[str, Any] | None) -> bool:
    return isinstance(receipt_defn, Mapping) and "affidavit_max_usd" in receipt_defn


def affidavit_ceiling(receipt_defn: Mapping[str, Any]) -> Decimal | None:
    """The EXP-04 amount ceiling, or None when the setting is unreadable (then the
    quarterly count keeps every signed affidavit — never fewer than are in use)."""
    try:
        return _money_setting(receipt_defn, "affidavit_max_usd", 75)
    except ValueError:
        return None


def amount_in_usd(amount: Any, currency: Any) -> tuple[Decimal | None, bool]:
    """(*amount* in USD to the cent, whether the currency could be converted).

    Integration (currency): the EXP-04 ceiling and the RECEIPT-REQ threshold are USD
    figures, so a line in another currency is converted with the same reference table
    as the policy engine (``config.constants.fx_rates``).  No currency / USD: the
    amount unchanged.  An unsupported code: (None, False) — never treated as USD.
    """
    claim = cents(amount)
    if claim is None:
        return None, True
    if currency is None or (isinstance(currency, str) and not currency.strip()):
        return claim, True
    try:
        source = normalize_currency(currency)
    except UnknownCurrency:
        return None, False
    if source == "USD":
        return claim, True
    return cents(convert(claim, source, "USD")), True


def receipt_needed(
    receipt_defn: Mapping[str, Any], *, amount_usd: Decimal | None, category: Any
) -> bool:
    """RECEIPT-REQ for one line (the engine's rule): an always-required category, or an
    amount at / above ``threshold_usd``; REF-08 exempt categories (mileage) never.
    An unknown USD amount needs the receipt (fail closed)."""
    cat = str(getattr(category, "value", category) or "")
    if cat in RECEIPT_EXEMPT_CATEGORIES:
        return False
    always = receipt_defn.get("always_required_categories") or []
    if isinstance(always, list) and cat in always:
        return True
    if amount_usd is None:
        return True
    try:
        threshold = Decimal(str(receipt_defn.get("threshold_usd", 25)))
    except (ArithmeticError, ValueError):
        return True
    return amount_usd >= threshold


def affidavit(
    receipt_defn: Mapping[str, Any],
    *,
    amount: Any,
    signed: bool,
    justification: str | None,
    prior_in_quarter: int,
    quarter: str,
    currency: Any = None,
) -> Finding:
    """EXP-04 for one line that NEEDS a receipt and has none attached.

    * amount > affidavit_max_usd ($75.01 and up): an affidavit is not allowed — the
      receipt is required (blocking), signed or not.
    * not signed: blocking, and the reason says an affidavit is possible.
    * signed but no written explanation (the justification): blocking.
    * signed, but ``prior_in_quarter`` >= affidavit_max_per_quarter (the 4th in the
      quarter or later): blocking.
    * signed, explained, <= $75.00, within the quarter count: ACCEPTED — an advisory
      ``missing_receipt`` flag (still an escalating type: a human always sees a line
      without a receipt).
    """
    clause = _clause(receipt_defn, "affidavit_clause_ref", "EXP-04")
    ceiling = _money_setting(receipt_defn, "affidavit_max_usd", 75)
    per_quarter = _int_setting(receipt_defn, "affidavit_max_per_quarter", 3)
    claim, convertible = amount_in_usd(amount, currency)
    observed: dict[str, Any] = {
        "affidavit_clause_ref": clause,
        "affidavit_max_usd": float(ceiling),
        "affidavit_max_per_quarter": per_quarter,
        "affidavit_signed": bool(signed),
        "affidavits_before_in_quarter": prior_in_quarter,
        "quarter": quarter,
    }
    if claim is not None and isinstance(currency, str) and currency.strip().upper() != "USD":
        observed["amount_usd"] = float(claim)
        observed["currency"] = currency.strip().upper()
    if not convertible:
        # Integration: the $ ceiling cannot be checked in an unsupported currency —
        # the affidavit cannot be accepted (fail closed), and the reason says why.
        return Finding(
            clause_ref=clause,
            flag_type=MISSING_RECEIPT,
            severity="blocking",
            reason=(
                f"{clause}: a missing-receipt affidavit is allowed only up to "
                f"{money(ceiling)}; this line's currency ({currency}) is not supported, so "
                "the amount could not be checked against it and the receipt is required."
            ),
            observed={
                **observed,
                "affidavit_allowed": False,
                "affidavit_issue": "currency_not_supported",
            },
        )
    if claim is None or claim > ceiling:
        what = money(claim) if claim is not None else "an unknown amount"
        return Finding(
            clause_ref=clause,
            flag_type=MISSING_RECEIPT,
            severity="blocking",
            reason=(
                f"{clause}: a missing-receipt affidavit is allowed only up to "
                f"{money(ceiling)}; this line is {what}, so the receipt is required."
            ),
            observed={**observed, "affidavit_allowed": False, "affidavit_issue": "over_amount"},
        )
    if not signed:
        return Finding(
            clause_ref=clause,
            flag_type=MISSING_RECEIPT,
            severity="blocking",
            reason=(
                f"If the receipt is lost, explain why in the justification and sign the "
                f"missing-receipt affidavit ({clause}: allowed up to {money(ceiling)}, at most "
                f"{per_quarter} per quarter)."
            ),
            observed={**observed, "affidavit_allowed": True, "affidavit_issue": "not_signed"},
        )
    if not (justification or "").strip():
        return Finding(
            clause_ref=clause,
            flag_type=MISSING_RECEIPT,
            severity="blocking",
            reason=(
                f"The missing-receipt affidavit is signed but the justification does not "
                f"explain the lost receipt — add the explanation ({clause})."
            ),
            observed={**observed, "affidavit_allowed": True, "affidavit_issue": "no_explanation"},
        )
    if prior_in_quarter >= per_quarter:
        return Finding(
            clause_ref=clause,
            flag_type=MISSING_RECEIPT,
            severity="blocking",
            reason=(
                f"{clause} allows no more than {per_quarter} missing-receipt affidavits per "
                f"quarter; {prior_in_quarter} were already signed for {quarter}, so this line "
                "needs its receipt."
            ),
            observed={**observed, "affidavit_allowed": False, "affidavit_issue": "over_count"},
        )
    return Finding(
        clause_ref=clause,
        flag_type=MISSING_RECEIPT,
        severity="advisory",
        reason=(
            f"Missing-receipt affidavit signed ({clause}: {money(claim)} is within "
            f"{money(ceiling)}; affidavit {prior_in_quarter + 1} of {per_quarter} for "
            f"{quarter}). Your approver reviews the explanation."
        ),
        observed={**observed, "affidavit_allowed": True, "affidavit_accepted": True},
    )


# ---------------------------------------------------------------------------
# PTA-02 — advance booking
# ---------------------------------------------------------------------------


def advance_booking(
    defn: Mapping[str, Any],
    *,
    departure: date | None,
    booked_on: date,
    international: bool | None,
) -> Finding | None:
    """PTA-02 for one flight: lead time = departure - booking day (whole days).

    * lead >= required (14 domestic / 21 international): no finding;
    * shorter: ``approval_required`` by the exception approver (a documented business
      reason is needed — the approver reviews it);
    * route of unknown country and 14 <= lead < 21: could not verify (it is enough for a
      domestic flight, not for an international one) — the approver checks;
    * no departure date: could not verify.
    """
    clause = _clause(defn, "corpus_clause_ref", "PTA-02")
    domestic = _int_setting(defn, "domestic_days", 14)
    intl = _int_setting(defn, "international_days", 21)
    approver = _approver(defn)
    observed: dict[str, Any] = {
        "booked_on": booked_on.isoformat(),
        "domestic_days": domestic,
        "international_days": intl,
        "international": international,
    }
    if departure is None:
        return Finding(
            clause_ref=clause,
            flag_type=NOT_EVALUATED,
            severity="advisory",
            reason=(
                f"Atlas could not tell when this flight departs, so it could not check "
                f"{clause}'s advance-booking lead time ({_days(domestic)} domestic / "
                f"{_days(intl)} international). Your approver should check it."
            ),
            requires_approver=approver,
            observed={**observed, "could_not_verify": "departure"},
        )
    lead = (departure - booked_on).days
    observed.update({"departure": departure.isoformat(), "lead_days": lead})
    if international is None:
        if lead >= max(domestic, intl):
            return None
        if lead >= domestic:
            return Finding(
                clause_ref=clause,
                flag_type=NOT_EVALUATED,
                severity="advisory",
                reason=(
                    f"Booked {_days(lead)} ahead: enough for a domestic flight "
                    f"({clause}: {_days(domestic)}) but not for an international one "
                    f"({_days(intl)}), and Atlas could not tell which this route is. Your "
                    "approver should check it."
                ),
                requires_approver=approver,
                observed={**observed, "could_not_verify": "route", "required_days": intl},
            )
        required, scope = domestic, "domestic"
    else:
        required, scope = (intl, "international") if international else (domestic, "domestic")
    observed["required_days"] = required
    if lead >= required:
        return None
    return Finding(
        clause_ref=clause,
        flag_type=APPROVAL_REQUIRED,
        severity="advisory",
        reason=(
            f"Booked {_days(lead)} ahead of departure ({departure.isoformat()}); {clause} "
            f"requires {scope} flights to be booked at least {_days(required)} ahead. A "
            f"shorter lead time needs a documented business reason — the {approver} "
            "reviews it (never auto-approved)."
        ),
        requires_approver=approver,
        observed={**observed, "short_notice": True},
    )


# ---------------------------------------------------------------------------
# PTA-05 — re-approval when the estimated cost rises > 10%
# ---------------------------------------------------------------------------


def trip_change(
    defn: Mapping[str, Any],
    *,
    approved_estimate: Any,
    new_estimate: Any,
    approved_label: str,
) -> Finding | None:
    """PTA-05: the new estimate vs the APPROVED (or under-review) trip's estimate.

    Increase compared exactly: (new - old) x 100 > pct x old.  Exactly 10.00% is
    within; 10.01% is not.  Unknown / zero baseline or estimate: could not verify (the
    approver checks — never a silent pass).
    """
    clause = _clause(defn, "corpus_clause_ref", "PTA-05")
    pct = dec(defn.get("max_increase_pct", 10))
    if pct is None or pct < 0:
        raise ValueError("max_increase_pct is not a percentage")
    approver = _approver(defn)
    old, new = cents(approved_estimate), cents(new_estimate)
    observed: dict[str, Any] = {
        "changes_trip": approved_label,
        "max_increase_pct": float(pct),
        "approved_estimate": str(old) if old is not None else None,
        "new_estimate": str(new) if new is not None else None,
    }
    if old is None or new is None or old <= 0:
        return Finding(
            clause_ref=clause,
            flag_type=NOT_EVALUATED,
            severity="advisory",
            reason=(
                f"This trip changes {approved_label}, but Atlas could not compare its "
                f"estimated cost with the approved estimate ({clause}: re-approval when the "
                f"total rises more than {pct:g}%). Your approver should check it."
            ),
            requires_approver=approver,
            observed={**observed, "could_not_verify": "estimate"},
        )
    if (new - old) * HUNDRED <= pct * old:
        return None
    rise = pct_increase(old, new)
    return Finding(
        clause_ref=clause,
        flag_type=APPROVAL_REQUIRED,
        severity="advisory",
        reason=(
            f"This trip changes {approved_label}: the estimated cost rose from {money(old)} "
            f"to {money(new)} ({rise}% more). {clause}: trip changes that raise the total "
            f"cost by more than {pct:g}% must be re-approved — it goes to the {approver} "
            "(never auto-approved)."
        ),
        requires_approver=approver,
        observed={**observed, "increase_pct": float(rise), "reapproval_required": True},
    )


# ---------------------------------------------------------------------------
# AIR-06 — lowest logical fare
# ---------------------------------------------------------------------------


def lowest_fare(
    defn: Mapping[str, Any],
    *,
    fare: Any,
    lowest: Any,
    international: bool | None,
) -> Finding | None:
    """AIR-06: the chosen fare vs the cheapest comparable option of the same search.

    Over = fare - lowest, compared to the cent with the tolerance ($150 domestic /
    $400 international; an unknown route gets the stricter domestic tolerance).
    Exactly the tolerance is within.  No search baseline: could not verify (advisory,
    the approver sees it; a hand-typed flight was never priced against a search).
    """
    clause = _clause(defn, "corpus_clause_ref", "AIR-06")
    dom = _money_setting(defn, "domestic_tolerance_usd", 150)
    intl = _money_setting(defn, "international_tolerance_usd", 400)
    tolerance = intl if international is True else dom
    scope = "international" if international is True else "domestic"
    paid, base = cents(fare), cents(lowest)
    observed: dict[str, Any] = {
        "fare_usd": str(paid) if paid is not None else None,
        "lowest_fare_usd": str(base) if base is not None else None,
        "tolerance_usd": str(tolerance),
        "international": international,
    }
    if paid is None or base is None:
        return Finding(
            clause_ref=clause,
            flag_type=NOT_EVALUATED,
            severity="advisory",
            reason=(
                f"This flight was not chosen from a trip-planner search, so Atlas could not "
                f"compare its fare with the cheapest comparable option ({clause}: within "
                f"{money(dom)} domestic / {money(intl)} international)."
            ),
            observed={**observed, "could_not_verify": "lowest_fare"},
        )
    over = paid - base
    observed["over_lowest_usd"] = str(over)
    if over <= tolerance:
        return None
    approver = _approver(defn)
    return Finding(
        clause_ref=clause,
        flag_type=POLICY_VIOLATION,
        severity="blocking",
        reason=(
            f"Fare {money(paid)} is {money(over)} above the cheapest comparable option in "
            f"the same search ({money(base)}); {clause} requires accepting fares within "
            f"{money(tolerance)} ({scope}) of the cheapest option meeting routing/timing "
            f"needs. Requires {approver} approval."
        ),
        requires_approver=approver,
        observed={**observed, "over_tolerance": True},
    )


def lowest_comparable_fares(
    flights: Iterable[tuple[str, str | None, Any]],
) -> dict[str, Decimal]:
    """option_id -> lowest fare among the flights of the SAME cabin class in one search.

    *flights*: (option_id, cabin_class, fare).  "Comparable" = same route and date (one
    search) and the same cabin — the cheapest economy fare is not the baseline of a
    business seat (cabin entitlement is AIR-01..04's job).
    """
    rows = [(oid, (cabin or "").lower(), cents(fare)) for oid, cabin, fare in flights]
    by_cabin: dict[str, Decimal] = {}
    for _oid, cabin, fare in rows:
        if fare is not None and (cabin not in by_cabin or fare < by_cabin[cabin]):
            by_cabin[cabin] = fare
    return {oid: by_cabin[cabin] for oid, cabin, fare in rows if cabin in by_cabin}


# ---------------------------------------------------------------------------
# AIR-04 — SVP business-class exception
# ---------------------------------------------------------------------------


def _allowed_cabins(cabin_defn: Mapping[str, Any], duration: float) -> list[str]:
    for tier in cabin_defn.get("tiers") or []:
        if not isinstance(tier, Mapping):
            continue
        min_h = float(tier.get("min_hours", 0.0))
        max_h = tier.get("max_hours")
        if max_h is None:
            if duration >= min_h:
                return list(tier.get("allowed_classes") or [])
        elif min_h <= duration < float(max_h):
            return list(tier.get("allowed_classes") or [])
    return []


@dataclass(frozen=True)
class RoleException:
    """Whether AIR-04 reaches one flight and, if so, what the traveller's grade says.

    applies   : the seat is the exception class (business), the flight is longer than
                ``role_exception_min_hours`` and the AIR-CABIN tiers do not already
                permit it — otherwise AIR-04 has nothing to decide.
    entitled  : True (grade at or above the rule's role), False (a junior grade), None
                (no grade on record / not on the ladder — could not verify).
    """

    applies: bool
    entitled: bool | None = None
    clause: str = "AIR-04"
    role: str = grade_ladder.SENIOR_EXCEPTION_MIN_GRADE
    min_hours: Decimal | None = None
    hours: Decimal | None = None
    seat: str = ""


def role_exception_status(
    cabin_defn: Mapping[str, Any],
    *,
    cabin: Any,
    duration_hours: Any,
    traveller_grade: str | None,
) -> RoleException:
    """AIR-04 for one flight, as data (shared by the AIR-CABIN verdict and the flags).

    *traveller_grade* must be the OWNER's grade of record from the database (never a
    request field).  A malformed ``role_exception_min_hours`` raises ValueError.
    """
    if "role_exception_class" not in cabin_defn:
        return RoleException(applies=False)
    wanted = str(cabin_defn.get("role_exception_class") or "").lower()
    seat = str(cabin or "").lower()
    hours = dec(duration_hours)
    if not wanted or seat != wanted or hours is None:
        return RoleException(applies=False)
    min_hours = dec(cabin_defn.get("role_exception_min_hours", 6))
    if min_hours is None:
        raise ValueError("role_exception_min_hours is not a number")
    if hours <= min_hours or seat in [
        str(c).lower() for c in _allowed_cabins(cabin_defn, float(hours))
    ]:
        return RoleException(applies=False)
    role = _clause(cabin_defn, "role_exception_role", grade_ladder.SENIOR_EXCEPTION_MIN_GRADE)
    return RoleException(
        applies=True,
        entitled=grade_ladder.at_or_above(traveller_grade, role),
        clause=_clause(cabin_defn, "role_exception_clause_ref", "AIR-04"),
        role=role,
        min_hours=min_hours,
        hours=hours,
        seat=seat,
    )


def _role_label(role: str) -> str:
    g = grade_ladder.grade(role)
    return f"{g.short}s" if g is not None else f"{role}s"


def role_cabin_exception(
    cabin_defn: Mapping[str, Any],
    *,
    cabin: Any,
    duration_hours: Any,
    traveller_grade: str | None = None,
) -> Finding | None:
    """AIR-04 for one flight (a modifier of the AIR-CABIN rule).

    Applies when the cabin is the exception class (business), the flight is longer
    than ``role_exception_min_hours`` (> 6 h) and the AIR-CABIN tiers do not already
    permit it.  Then the traveller's grade of record decides:

    * at or above ``role_exception_role`` (SVP) -> None: permitted, in policy (the
      AIR-CABIN verdict carries the AIR-04 note);
    * a known junior grade -> ``approval_required`` naming the AIR-CABIN override
      approver: AIR-04 does not cover this traveller;
    * no grade on record (None / not on the ladder) -> ``approval_required`` by the
      rule's role: an approver must confirm the grade (never auto-approved).
    """
    status = role_exception_status(
        cabin_defn, cabin=cabin, duration_hours=duration_hours, traveller_grade=traveller_grade
    )
    if not status.applies or status.entitled is True:
        return None
    if status.hours is None or status.min_hours is None:  # pragma: no cover - applies implies set
        return None
    hours, min_hours, role, clause = status.hours, status.min_hours, status.role, status.clause
    who = _role_label(role)
    observed: dict[str, Any] = {
        "cabin_class": status.seat,
        "duration_hours": float(hours),
        "role_exception_min_hours": float(min_hours),
        "role": role,
        "traveller_grade": traveller_grade if grade_ladder.grade(traveller_grade) else None,
    }
    head = (
        f"{status.seat.capitalize()} class on a {hours:g}-hour flight: {clause} permits it on "
        f"flights over {min_hours:g} hours only for {who} and above."
    )
    if status.entitled is False:
        title = grade_ladder.title_of(traveller_grade)
        approver = _approver(cabin_defn, "override_approver")
        return Finding(
            clause_ref=clause,
            flag_type=APPROVAL_REQUIRED,
            severity="advisory",
            reason=(
                f"{head} The traveler's grade of record is {title}, so {clause} does not "
                f"cover this seat; it needs the cabin-class exception from an approver at "
                f"{approver} level (never auto-approved)."
            ),
            requires_approver=approver,
            observed=observed,
        )
    why = (
        f"The rule's grade threshold '{role}' is not on the grade ladder"
        if grade_ladder.grade(traveller_grade) is not None
        else "No grade is recorded for the traveler"
    )
    return Finding(
        clause_ref=clause,
        flag_type=APPROVAL_REQUIRED,
        severity="advisory",
        reason=(
            f"{head} {why}, so {clause} cannot be applied automatically; an approver at "
            f"{role} level must confirm the exception (never auto-approved)."
        ),
        requires_approver=role,
        observed={**observed, "could_not_verify": "traveller_grade"},
    )


# ---------------------------------------------------------------------------
# CARD-03 — card transactions reconciled within the EXP-01 deadline
# ---------------------------------------------------------------------------


def card_reconciliation(
    defn: Mapping[str, Any],
    *,
    unreconciled: Iterable[tuple[date, Any, str]],
    today: date,
) -> Finding | None:
    """CARD-03: the cardholder's corporate-card transactions not in any expense report.

    *unreconciled*: (transaction date, amount, currency) of each card transaction not
    linked to an expense.  One older than ``deadline_days`` (30; day 30 is on time,
    day 31 is overdue) -> an advisory ``unreconciled_card`` finding naming the count,
    the oldest date and the totals per currency.
    """
    clause = _clause(defn, "corpus_clause_ref", "CARD-03")
    deadline = _int_setting(defn, "deadline_days", 30)
    overdue = [
        (d, cents(a) or Decimal(0), c) for d, a, c in unreconciled if (today - d).days > deadline
    ]
    if not overdue:
        return None
    overdue.sort(key=lambda r: r[0])
    oldest = overdue[0][0]
    totals: dict[str, Decimal] = {}
    for _d, amount, currency in overdue:
        totals[currency] = totals.get(currency, Decimal(0)) + amount
    total_text = ", ".join(f"{v:,.2f} {k}" for k, v in sorted(totals.items()))
    n = len(overdue)
    noun = "transaction" if n == 1 else "transactions"
    return Finding(
        clause_ref=clause,
        flag_type=UNRECONCILED_CARD,
        severity="advisory",
        reason=(
            f"{n} corporate card {noun} ({total_text}; oldest {oldest.isoformat()}, "
            f"{_days((today - oldest).days)} ago) {'is' if n == 1 else 'are'} not yet in an "
            f"expense report. {clause}: reconcile every card transaction in an expense "
            f"report within {_days(deadline)}."
        ),
        observed={
            "overdue_count": n,
            "oldest_txn_date": oldest.isoformat(),
            "deadline_days": deadline,
            "totals": {k: str(v) for k, v in totals.items()},
        },
    )
