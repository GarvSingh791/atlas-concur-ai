"""Validation and plain-English descriptions for entry-level rule shapes (product gap 2/8).

Companion of ``policy_rule_semantics`` for what the existing validators / describers do
not know about:

* ``kind`` rules — GRND-01 rail class, GRND-03 airport parking days, GRND-05 tips
  (``ai_agents/tools/entry_rules.py``);
* ``ground_subtypes`` — a ground-transport cap that targets only rideshare/taxi, car
  rentals, rail, transit or parking;
* the LOD-02 tolerance keys of a hotel cap and the MEAL-02 / exclusion keys of a per diem.

Every sentence states what the engine does (derived from the same constants the
evaluators use).  LLM-free, total (never raises).
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from typing import Any

from ai_agents.tools import entry_rules
from ai_agents.tools.ground_transport import (
    BROAD,
    GROUND_SUBTYPES,
    SUBTYPE_LABELS,
    classify_ground_transport,
    subtype_label,
)

APPROVERS = ("manager", "VP")

# Keys the evaluators read, per kind (plus the common filter / clause keys).
_COMMON_KIND_KEYS = frozenset(
    {"kind", "corpus_clause_ref", "expense_categories", "ground_subtypes", "exception_approver"}
)
_KIND_KEYS: dict[str, frozenset[str]] = {
    entry_rules.KIND_RAIL_CLASS: _COMMON_KIND_KEYS
    | {"standard_classes", "premium_classes", "premium_min_hours", "premium_condition"},
    entry_rules.KIND_PARKING_DAYS: _COMMON_KIND_KEYS | {"max_days", "lot"},
    entry_rules.KIND_TIP_PCT: _COMMON_KIND_KEYS | {"max_tip_pct"},
}

TOLERANCE_KEYS = frozenset(entry_rules.TOLERANCE_KEYS)
PER_DIEM_EXTRA_KEYS = frozenset(
    {"partial_day_pct", "partial_day_clause_ref", "exclusions_clause_ref"}
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _str_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and v.strip() for v in value)
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_ground_subtypes(applies_to: str, definition: Mapping[str, Any]) -> list[str]:
    """Errors for a ``ground_subtypes`` filter (empty when absent or valid)."""
    if "ground_subtypes" not in definition:
        return []
    errors: list[str] = []
    subtypes = definition.get("ground_subtypes")
    if applies_to != "all" or definition.get("expense_categories") != ["ground_transport"]:
        errors.append(
            "ground_subtypes is only used with applies_to 'all' and expense_categories "
            "['ground_transport']."
        )
    if not isinstance(subtypes, list) or not _str_list(subtypes):
        errors.append(f"ground_subtypes must be a non-empty list from {list(GROUND_SUBTYPES)}.")
    elif any(s not in GROUND_SUBTYPES for s in subtypes):
        errors.append(f"ground_subtypes may only name {list(GROUND_SUBTYPES)}.")
    elif len(set(subtypes)) == len(GROUND_SUBTYPES):
        errors.append(
            "ground_subtypes names every kind of ground transport; drop it to cap all "
            "ground transport."
        )
    return errors


def validate_tolerance(applies_to: str, definition: Mapping[str, Any]) -> list[str]:
    if not TOLERANCE_KEYS & set(definition):
        return []
    errors: list[str] = []
    if applies_to != "hotel":
        errors.append("The auto-approval tolerance (LOD-02) is only used by hotel caps.")
    pct = _num(definition.get("auto_approve_over_cap_pct"))
    if pct is None or not 0 < pct <= 100:
        errors.append("auto_approve_over_cap_pct must be a percentage above 0 and up to 100.")
    ref = definition.get("auto_approve_clause_ref", "LOD-02")
    if not isinstance(ref, str) or not ref.strip():
        errors.append("auto_approve_clause_ref must be a clause id.")
    if definition.get("auto_approve_requires", "booked_through_tool") != "booked_through_tool":
        errors.append(
            "auto_approve_requires must be 'booked_through_tool' (the only condition Atlas "
            "can verify)."
        )
    return errors


def validate_per_diem_extras(definition: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if "partial_day_pct" in definition:
        pct = _num(definition.get("partial_day_pct"))
        if pct is None or not 0 < pct < 100:
            errors.append("partial_day_pct must be a percentage between 0 and 100.")
    for key in ("partial_day_clause_ref", "exclusions_clause_ref"):
        if key in definition and not (
            isinstance(definition.get(key), str) and definition[key].strip()
        ):
            errors.append(f"{key} must be a clause id.")
    return errors


def validate_kind(rule_type: str, applies_to: str, definition: Mapping[str, Any]) -> list[str]:
    """Errors for a ``kind`` rule (GRND-01 / GRND-03 / GRND-05 shapes)."""
    kind = definition.get("kind")
    if kind not in _KIND_KEYS:
        return [f"kind {kind!r} is not one of {sorted(_KIND_KEYS)}."]
    errors: list[str] = []
    expected_type = entry_rules.KIND_RULE_TYPES[str(kind)]
    if rule_type != expected_type or applies_to != "all":
        errors.append(
            f"A {kind} rule is a {expected_type} rule with applies_to 'all' "
            f"(got {rule_type} / {applies_to})."
        )
    for key in sorted(set(definition) - _KIND_KEYS[str(kind)]):
        errors.append(f"definition key {key!r} is not used by the rule engine.")
    if definition.get("expense_categories") != ["ground_transport"]:
        errors.append(f"A {kind} rule must set expense_categories to ['ground_transport'].")
    if definition.get("ground_subtypes") != [entry_rules.KIND_SUBTYPE[str(kind)]]:
        errors.append(
            f"A {kind} rule must set ground_subtypes to "
            f"['{entry_rules.KIND_SUBTYPE[str(kind)]}']."
        )
    approver = definition.get("exception_approver", "manager")
    if approver not in APPROVERS:
        errors.append("exception_approver must be 'manager' or 'VP'.")
    if kind == entry_rules.KIND_RAIL_CLASS:
        hours = _num(definition.get("premium_min_hours", 3))
        if hours is None or hours <= 0:
            errors.append("premium_min_hours must be a number of hours above 0.")
        for key in ("standard_classes", "premium_classes"):
            if key in definition and not _str_list(definition.get(key)):
                errors.append(f"{key} must be a non-empty list of class names.")
    elif kind == entry_rules.KIND_PARKING_DAYS:
        days = definition.get("max_days")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            errors.append("max_days must be a whole number of days of at least 1.")
    elif kind == entry_rules.KIND_TIP_PCT:
        pct = _num(definition.get("max_tip_pct"))
        if pct is None or not 0 < pct <= 100:
            errors.append("max_tip_pct must be a percentage above 0 and up to 100.")
    return errors


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


def subtypes_text(subtypes: Any) -> str:
    names = [subtype_label(s) for s in subtypes or [] if isinstance(s, str)]
    return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]


def ground_scope_sentence(definition: Mapping[str, Any], *, kind: bool = False) -> str:
    """Exactly which ground-transport lines a ``ground_subtypes`` rule reaches."""
    subtypes = [s for s in definition.get("ground_subtypes") or [] if isinstance(s, str)]
    named = subtypes_text(subtypes)
    others = [SUBTYPE_LABELS[s] for s in GROUND_SUBTYPES if s not in subtypes]
    not_covered = ", ".join(others)
    how = "identified from the receipt, card merchant or linked trip segment"
    if kind:
        return (
            f"Applies only to ground-transport expense lines Atlas identifies as {named} "
            f"({how}), to lines whose receipt, card or trip names another kind while any of "
            f"their words name {named}, and to every line no receipt, card or trip segment "
            "identifies at all (its flag says the kind could not be confirmed); "
            f"{not_covered} lines confirmed by that evidence are not covered."
        )
    return (
        f"Applies only to {named} ground-transport expense lines ({how}); {not_covered} "
        "lines confirmed by that evidence are not covered. A ground-transport line whose "
        "kind the receipt, card or trip does not confirm is still checked (it may be one) "
        "and its flag says so."
    )


# Known limits (ground): ``ground_worst_case`` — what clears an unidentified line.
def low_value_cleared(names: str) -> str:
    return (
        "An unidentified line below the receipt threshold that is within every other "
        f"limit, and whose own words or trip segment name nothing this rule checks ({names}), "
        "cannot breach it whatever its kind and is not sent to the approver for it, as long "
        "as the report's cleared unidentified lines together stay below that threshold; its "
        "note asks for a receipt or card match that names the kind."
    )


def describe_kind(definition: Mapping[str, Any]) -> list[str]:
    """Plain-English sentences for a ``kind`` rule."""
    kind = definition.get("kind")
    clause = definition.get("corpus_clause_ref") or ""
    approver = definition.get("exception_approver") or "manager"
    if kind == entry_rules.KIND_PARKING_DAYS:
        # GRND-03 reaches AIRPORT parking only (``entry_rules.ground_subtype_reach``).
        out = [
            "Applies only to airport parking (an airport named on the receipt or entry, or "
            "a line linked to a flight segment); hotel valet, city garages and meters are "
            "not covered, and nor are lines the receipt, card or trip segment identifies as "
            "another kind. A ground-transport line that no receipt, card or trip segment "
            "identifies at all is marked 'could not verify' unless it is shown to be airport "
            "parking (the approver checks). "
            + low_value_cleared(
                "an airport or a linked flight, a premium lot, a day count over the limit"
            )
        ]
    else:
        out = [ground_scope_sentence(definition, kind=True)]
    consequence = (
        f"A breach blocks submission until the employee adds a written justification; the "
        f"report then goes to their {approver} as a policy exception (never auto-approved)."
    )
    unverified = (
        "When the receipt or entry doesn't show what the rule needs, the line is marked "
        f"'could not verify' (advisory, never a silent pass) and goes to their {approver} "
        "for a check — it can be submitted but is never auto-approved."
    )
    if kind == entry_rules.KIND_RAIL_CLASS:
        hours = _num(definition.get("premium_min_hours", 3)) or 3.0
        condition = definition.get("premium_condition") or "cheaper than or comparable to flying"
        out.append(
            f"{clause}: Standard/Economy class for journeys under {hours:g} hours; a "
            f"First/Business ticket is an exception — under {hours:g} hours it is not "
            f"permitted, from {hours:g} hours it is permitted only if {condition}, which "
            "Atlas cannot check."
        )
        out.append(consequence)
        out.append(
            "A First/Business class named on the receipt, card or entry counts even when the "
            "linked rail segment says Standard (the receipt shows what was bought); the "
            "segment's class is used only when nothing names a premium class, and a typed "
            "'standard' never clears it. "
            + unverified
            + " "
            + low_value_cleared("rail, a ticket class")
        )
    elif kind == entry_rules.KIND_PARKING_DAYS:
        days = definition.get("max_days")
        lot = definition.get("lot") or "economy"
        out.append(
            f"{clause}: airport parking is reimbursable for up to {days} days ({lot} lot); "
            "longer stays and premium/valet/short-term airport lots are exceptions."
        )
        out.append(consequence)
        out.append(
            "Days come only from the parking receipt (OCR), or from a day count stated on "
            "the entry when it is above the limit; the linked trip's length is shown to the "
            "approver as context but is never counted as parking days. " + unverified
        )
    elif kind == entry_rules.KIND_TIP_PCT:
        pct = _num(definition.get("max_tip_pct")) or 20.0
        out.append(
            f"{clause}: a tip on a rideshare/taxi ride is reimbursable up to {pct:g}% of the "
            f"fare (tip ÷ (receipt total − tip)); exactly {pct:g}% is within. A claim that "
            "leaves the tip (or the part over the limit) out of the claimed amount is within."
        )
        out.append(consequence)
        out.append(
            "Only a tip the receipt itemizes is checked — also on a ground-transport line "
            "whose kind the receipt, card or trip does not confirm; a ride without an "
            "itemized tip is not flagged, and one that mentions a tip without an amount, or "
            "whose receipt total was not read, is marked 'could not verify' and goes to the "
            "approver (never auto-approved)."
        )
    return out


def tolerance_sentence(definition: Mapping[str, Any]) -> str | None:
    pct = _num(definition.get("auto_approve_over_cap_pct"))
    if pct is None:
        return None
    clause = definition.get("auto_approve_clause_ref") or "LOD-02"
    return (
        f"{clause}: a nightly rate up to {pct:g}% over the cap is auto-approved (advisory "
        "note only) when the stay was booked through the travel tool (the entry is linked "
        "to a hotel segment the trip planner booked on the traveler's approved trip and its "
        "receipt or card names that hotel, with the receipt in the booked city; at trip "
        "approval, a hotel segment the planner booked); above "
        f"{pct:g}%, booked elsewhere (including a hotel typed into a trip by hand), or "
        "without that receipt evidence, it is a manager exception."
    )


def per_diem_sentences(definition: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    pct = _num(definition.get("partial_day_pct"))
    if pct is not None:
        clause = definition.get("partial_day_clause_ref") or "MEAL-02"
        out.append(
            f"{clause}: {pct:g}% of the daily rate on departure and return days (the first "
            "and last day of the approved trip; a same-day trip gets one partial day); a "
            "meal not linked to an approved trip that is above the partial rate is marked "
            "'could not verify'."
        )
    exclusions = [e for e in definition.get("exclusions") or [] if isinstance(e, str)]
    if exclusions:
        clause = definition.get("exclusions_clause_ref") or "MEAL-04"
        names = " and ".join(exclusions)
        out.append(
            f"Exclusions ({names}, {clause}) are checked: itemized receipt lines included in "
            "the claimed amount are taken out of the reimbursable meal total and flagged (a "
            "manager may accept them as approved client entertainment); a claim that already "
            "leaves them out is not flagged; a mention without itemized amounts is marked "
            "'could not verify' and goes to the approver (never auto-approved)."
        )
    # One list element: describe_rule keeps at most six sentences per rule.
    return [" ".join(out)] if out else []


def is_broad(subtype: Any) -> bool:
    return subtype in (None, BROAD)


# ---------------------------------------------------------------------------
# Authoring: target the ground sub-type the intent names (product gap 8)
# ---------------------------------------------------------------------------

# An intent that names ground transport as a whole keeps the broad cap.
_GENERIC_GROUND = re.compile(
    r"\b(ground[\s-]transport\w*|all\s+(?:local\s+)?(?:transport\w*|travel)|any\s+transport\w*"
    r"|local\s+transport\w*|transportation)\b",
    re.IGNORECASE,
)


def narrow_ground_scope(intent: str, candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    """The candidate narrowed to the ground sub-types the intent names, or None (unchanged).

    Only a broad ground-transport ``amount_cap`` (applies_to 'all', expense_categories
    ['ground_transport'], no ``ground_subtypes`` / ``kind``) is narrowed, and only when
    the intent names specific kinds (rideshare, taxi, parking, train ...) with the same
    deterministic classifier the engine uses on expense lines, and does not speak of
    ground transport as a whole.  "Rideshare over $75" -> ["rideshare_taxi"]: the
    authored cap no longer reaches train tickets, rentals, transit or parking.
    """
    try:
        if not isinstance(intent, str) or _GENERIC_GROUND.search(intent):
            return None
        if candidate.get("rule_type") != "amount_cap" or candidate.get("applies_to") != "all":
            return None
        definition = candidate.get("definition")
        if not isinstance(definition, Mapping) or "kind" in definition:
            return None
        if "ground_subtypes" in definition:
            return None
        if definition.get("expense_categories") != ["ground_transport"]:
            return None
        families = classify_ground_transport(intent).families
        if not families or len(families) == len(GROUND_SUBTYPES):
            return None
        narrowed = copy.deepcopy(dict(candidate))
        narrowed["definition"] = {**dict(definition), "ground_subtypes": list(families)}
        return narrowed
    except Exception:  # noqa: BLE001 — never fatal: the candidate is used unchanged
        return None
