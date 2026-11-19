"""Validation and plain-English descriptions of the process & timing rules (gap 2, stage 2).

Companion of ``policy_rule_semantics`` / ``entry_rule_semantics`` for:

* process ``kind`` rules — EXP-01 submission deadline, PTA-02 advance booking, PTA-05
  re-approval on cost increase, AIR-06 lowest logical fare, CARD-03 card reconciliation
  (``ai_agents/tools/process_rules.py``);
* the EXP-04 affidavit keys of a receipt rule and the AIR-04 role-exception keys of a
  cabin rule.

Every sentence states what the wiring (``ai_agents/agents/process_checks.py``) does.
LLM-free, total (never raises).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ai_agents.tools import grade_entitlements as ge
from ai_agents.tools import process_rules as pr
from config.constants import grades as grade_ladder

APPROVERS = ("manager", "VP", "SVP")

_COMMON = frozenset({"kind", "corpus_clause_ref", "exception_approver"})
PROCESS_KIND_KEYS: dict[str, frozenset[str]] = {
    pr.KIND_SUBMISSION_DEADLINE: frozenset(
        {"kind", "corpus_clause_ref", "deadline_days", "stale_days", "stale_approver"}
    ),
    pr.KIND_ADVANCE_BOOKING: _COMMON | {"domestic_days", "international_days"},
    pr.KIND_TRIP_CHANGE: _COMMON | {"max_increase_pct"},
    pr.KIND_LOWEST_FARE: _COMMON | {"domestic_tolerance_usd", "international_tolerance_usd"},
    pr.KIND_CARD_RECONCILIATION: frozenset({"kind", "corpus_clause_ref", "deadline_days"}),
    pr.KIND_GRADE_ENTITLEMENT: frozenset(
        {"kind", "corpus_clause_ref", "entitlement", "min_grade", "delay_exception_hours"}
    ),
}
AFFIDAVIT_KEYS = frozenset(pr.AFFIDAVIT_KEYS)
ROLE_EXCEPTION_KEYS = frozenset(pr.ROLE_EXCEPTION_KEYS)


def _whole(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _clause_ok(definition: Mapping[str, Any], key: str) -> bool:
    value = definition.get(key)
    return value is None or (isinstance(value, str) and bool(value.strip()))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_process_kind(
    rule_type: str, applies_to: str, definition: Mapping[str, Any]
) -> list[str]:
    """Errors for a process ``kind`` rule (``[]`` = the wiring enforces it as written)."""
    kind = str(definition.get("kind"))
    if kind not in PROCESS_KIND_KEYS:
        return [f"kind {kind!r} is not a process rule kind."]
    errors: list[str] = []
    expected = pr.PROCESS_KIND_SHAPES[kind]
    if (rule_type, applies_to) != expected:
        errors.append(
            f"A {kind} rule is a {expected[0]} rule with applies_to '{expected[1]}' "
            f"(got {rule_type} / {applies_to})."
        )
    for key in sorted(set(definition) - PROCESS_KIND_KEYS[kind]):
        errors.append(f"definition key {key!r} is not used by the rule engine.")
    if not _clause_ok(definition, "corpus_clause_ref"):
        errors.append("corpus_clause_ref must be a clause id.")
    for key in ("exception_approver", "stale_approver"):
        if key in definition and definition.get(key) not in APPROVERS:
            errors.append(f"{key} must be one of {list(APPROVERS)}.")
    if kind == pr.KIND_SUBMISSION_DEADLINE:
        deadline = definition.get("deadline_days", 30)
        stale = definition.get("stale_days", 60)
        if not _whole(deadline) or not _whole(stale):
            errors.append("deadline_days and stale_days must be whole numbers of days (>= 1).")
        elif stale < deadline:
            errors.append("stale_days must not be shorter than deadline_days.")
    elif kind == pr.KIND_ADVANCE_BOOKING:
        for key, default in (("domestic_days", 14), ("international_days", 21)):
            if not _whole(definition.get(key, default), minimum=0):
                errors.append(f"{key} must be a whole number of days.")
    elif kind == pr.KIND_TRIP_CHANGE:
        pct = _num(definition.get("max_increase_pct", 10))
        if pct is None or not 0 <= pct <= 1000:
            errors.append("max_increase_pct must be a percentage of at least 0.")
    elif kind == pr.KIND_LOWEST_FARE:
        for key, default in (("domestic_tolerance_usd", 150), ("international_tolerance_usd", 400)):
            amount = _num(definition.get(key, default))
            if amount is None or amount < 0:
                errors.append(f"{key} must be an amount of at least 0.")
    elif kind == pr.KIND_CARD_RECONCILIATION:
        if not _whole(definition.get("deadline_days", 30)):
            errors.append("deadline_days must be a whole number of days (>= 1).")
    elif kind == pr.KIND_GRADE_ENTITLEMENT:
        errors.extend(_grade_entitlement_errors(definition))
    return errors


def _grade_entitlement_errors(definition: Mapping[str, Any]) -> list[str]:
    """Follow-up (grades): AIR-07 / LOD-05 / NR-01 rule keys."""
    errors: list[str] = []
    entitlement = definition.get("entitlement")
    if entitlement not in ge.ENTITLEMENTS:
        errors.append("entitlement must be one of " + ", ".join(ge.ENTITLEMENTS) + ".")
    min_grade = definition.get("min_grade", grade_ladder.SENIOR_EXCEPTION_MIN_GRADE)
    if grade_ladder.grade(min_grade) is None:
        errors.append(
            "min_grade must be a grade code: " + ", ".join(grade_ladder.GRADE_CODES) + "."
        )
    if "delay_exception_hours" in definition:
        if entitlement != ge.AIRPORT_EXTRAS:
            errors.append("delay_exception_hours applies only to the airport_extras entitlement.")
        elif not _whole(definition.get("delay_exception_hours")):
            errors.append("delay_exception_hours must be a whole number of hours (>= 1).")
    return errors


def validate_affidavit(definition: Mapping[str, Any]) -> list[str]:
    """Errors for the EXP-04 keys of a receipt rule (none present -> [])."""
    if not set(definition) & AFFIDAVIT_KEYS:
        return []
    errors: list[str] = []
    amount = _num(definition.get("affidavit_max_usd"))
    if amount is None or amount < 0:
        errors.append("affidavit_max_usd must be an amount of at least 0.")
    if not _whole(definition.get("affidavit_max_per_quarter", 3), minimum=0):
        errors.append("affidavit_max_per_quarter must be a whole number.")
    if not _clause_ok(definition, "affidavit_clause_ref"):
        errors.append("affidavit_clause_ref must be a clause id.")
    return errors


def validate_role_exception(definition: Mapping[str, Any]) -> list[str]:
    """Errors for the AIR-04 keys of a cabin rule (none present -> [])."""
    if not set(definition) & ROLE_EXCEPTION_KEYS:
        return []
    errors: list[str] = []
    cabin = definition.get("role_exception_class")
    if not isinstance(cabin, str) or not cabin.strip():
        errors.append("role_exception_class must name a cabin class.")
    hours = _num(definition.get("role_exception_min_hours", 6))
    if hours is None or hours < 0:
        errors.append("role_exception_min_hours must be a number of hours.")
    for key in ("role_exception_role", "role_exception_clause_ref"):
        if not _clause_ok(definition, key):
            errors.append(f"{key} must be a non-empty text.")
    role = definition.get("role_exception_role")
    if isinstance(role, str) and role.strip() and grade_ladder.grade(role) is None:
        # AIR-04 compares the traveller's grade with this rung: it must be on the ladder.
        errors.append(
            "role_exception_role must be a grade code: " + ", ".join(grade_ladder.GRADE_CODES) + "."
        )
    return errors


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


def describe_process_kind(definition: Mapping[str, Any]) -> list[str]:
    """Plain-English sentences for a process ``kind`` rule."""
    kind = definition.get("kind")
    clause = definition.get("corpus_clause_ref") or pr.DEFAULT_CLAUSE.get(str(kind), "")
    approver = definition.get("exception_approver") or "manager"
    if kind == pr.KIND_SUBMISSION_DEADLINE:
        deadline = definition.get("deadline_days", 30)
        stale = definition.get("stale_days", 60)
        stale_approver = definition.get("stale_approver") or "VP"
        return [
            f"{clause}: an expense report is due within {deadline} days of trip completion "
            "— counted from the report's earliest expense date, or the linked approved "
            "trip's end date when that is earlier (a trip never makes an older expense "
            "look newer).",
            f"A report more than {deadline} days past that date gets an advisory "
            f"'late submission' note; one more than {stale} days old requires "
            f"{stale_approver} approval (may be treated as taxable) and is never "
            "auto-approved. A draft shows it as soon as it is opened.",
        ]
    if kind == pr.KIND_ADVANCE_BOOKING:
        dom = definition.get("domestic_days", 14)
        intl = definition.get("international_days", 21)
        return [
            f"{clause}: flights are booked at least {dom} days ahead (domestic) or {intl} "
            "days ahead (international); the trip planner shows the lead time on every "
            "flight option.",
            f"A trip whose flight departs sooner (counted from the day it is submitted) goes "
            f"to the {approver} for the documented business reason and is never "
            "auto-approved; a route Atlas cannot place as domestic or international is "
            "checked by the approver when the lead time is between the two.",
        ]
    if kind == pr.KIND_TRIP_CHANGE:
        pct = definition.get("max_increase_pct", 10)
        return [
            f"{clause}: an approved or under-review trip is never changed in place — a "
            "change made in the trip planner becomes a new draft linked to it.",
            f"When that draft's estimated cost is more than {pct}% above the approved "
            f"estimate it must be re-approved: it goes to the {approver} with the increase "
            "named and is never auto-approved.",
        ]
    if kind == pr.KIND_LOWEST_FARE:
        dom = definition.get("domestic_tolerance_usd", 150)
        intl = definition.get("international_tolerance_usd", 400)
        return [
            f"{clause}: a flight's fare must be within ${dom} (domestic) / ${intl} "
            "(international) of the cheapest option of the same cabin in the same "
            "trip-planner search; a route of unknown country uses the domestic tolerance.",
            f"A fare above that is out of policy (shown on the option, and the trip goes to "
            f"the {approver}); a flight typed into a trip by hand was never priced against a "
            "search and is marked 'could not verify'.",
        ]
    if kind == pr.KIND_GRADE_ENTITLEMENT:
        return _describe_grade_entitlement(definition)
    if kind == pr.KIND_CARD_RECONCILIATION:
        days = definition.get("deadline_days", 30)
        return [
            f"{clause}: every corporate card transaction must be in an expense report within "
            f"{days} days of the transaction date.",
            "Older card transactions not linked to any expense are listed as an advisory "
            "note on the cardholder's expense reports (they are not blocked).",
        ]
    return []


def _describe_grade_entitlement(definition: Mapping[str, Any]) -> list[str]:
    entitlement = str(definition.get("entitlement"))
    if entitlement not in ge.ENTITLEMENTS:
        return []
    clause = definition.get("corpus_clause_ref") or ge.DEFAULT_CLAUSE[entitlement]
    min_grade = str(definition.get("min_grade") or grade_ladder.SENIOR_EXCEPTION_MIN_GRADE)
    who = ge.grade_label(min_grade)
    first = (
        f"{clause}: {ge.ALLOWANCE[entitlement]} are reimbursable only for {who} and above, "
        "decided from the expense owner's grade of record (set by a finance admin on the "
        "Users page)."
    )
    if entitlement == ge.AIRPORT_EXTRAS:
        hours = definition.get("delay_exception_hours", ge.DEFAULT_DELAY_HOURS)
        first += (
            f" The clause's other exception — a flight delay of more than {hours} hours — "
            "cannot be checked by Atlas (no delay data): the claimant states it in a "
            "justification."
        )
    return [
        first,
        "A line is recognised from the receipt's OCR words, the card merchant or the "
        "entry's own words (words can only add scrutiny). At or above that grade it is "
        "in policy with a note; a junior grade is a policy violation (a justification "
        f"lets it be submitted) and an unknown grade needs an approver — both only an "
        f"approver at {min_grade} level or above may decide (never auto-approved).",
    ]


def modifier_sentences(rule_type: str, definition: Mapping[str, Any]) -> list[str]:
    """The EXP-04 / AIR-04 sentence of a receipt / cabin rule carrying those keys."""
    if rule_type == "receipt_required" and pr.affidavit_configured(definition):
        clause = definition.get("affidavit_clause_ref") or "EXP-04"
        amount = definition.get("affidavit_max_usd")
        count = definition.get("affidavit_max_per_quarter", 3)
        return [
            f"{clause}: a line with no receipt at all may instead carry a signed "
            f"missing-receipt affidavit with a written explanation, up to ${amount} and at "
            f"most {count} per quarter (by the expense date's calendar quarter); it can then "
            "be submitted but always goes to the approver. Above that amount, or beyond the "
            "quarterly count, the receipt is required."
        ]
    if rule_type == "cabin_class" and "role_exception_class" in definition:
        clause = definition.get("role_exception_clause_ref") or "AIR-04"
        cabin = definition.get("role_exception_class")
        hours = definition.get("role_exception_min_hours", 6)
        role = definition.get("role_exception_role") or "SVP"
        return [
            f"{clause}: {role}s and above may book {cabin} class on flights over {hours} "
            "hours. The traveler's grade of record (set by a finance admin on the Users "
            "page) decides: at or above that grade the flight is in policy; a junior grade "
            "needs the cabin-class exception above; with no grade recorded the flight is "
            f"flagged for an approver at {role} level (never auto-approved)."
        ]
    return []
