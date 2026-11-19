"""What a structured policy rule ACTUALLY does in the rule engine (Issue 5).

validate_rule_definition — the definition shapes the engine enforces as written. A shape
                           it would silently mis-enforce (an unreachable tier, an
                           ignored key, a target no expense line ever reaches) is an
                           error.  Total: never raises.
describe_rule            — plain-English sentences describing what PolicyEvalTool +
                           presubmit actually do with the rule.  Scope, figures and
                           consequences are DERIVED by running the engine's own code
                           (``build_eval_item``, ``PolicyEvalTool.rule_applies`` /
                           ``evaluate_candidate`` / ``resolve_tier`` and
                           ``presubmit.verdicts_to_flags``) on probe items, so the text
                           cannot drift from enforcement.  Total: never raises.
offers_justification     — whether a written justification unblocks a breach of the rule.
intent_scope_conflict    — deterministic backstop for known intent → target drift.
sanitize_reason          — bounded, control-free reason text for the client / audit.
RULE_VOCABULARY_GLOSSARY — the rule vocabulary taught to the authoring LLM.

LLM-free and read-only.  Never imports backend.*.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ai_agents.agents import entry_rule_semantics as entry_sem
from ai_agents.agents import process_rule_semantics as process_sem
from ai_agents.agents.expense_agent.categorize import DraftEntry, category_for_expense_type
from ai_agents.agents.expense_agent.presubmit import build_eval_item, verdicts_to_flags
from ai_agents.tools.entry_rules import rule_kind
from ai_agents.tools.policy_eval import (
    EvalItem,
    PolicyEvalTool,
    RuleVerdict,
    format_cabin_classes,
)
from ai_agents.tools.process_rules import process_kind
from ai_agents.tools.spend_policy import (
    MILEAGE_CHECKS,
    SPEND_RULE_TYPES,
    TEAM_MEAL_MIN_ATTENDEES,
    to_decimal,
)
from database.models.expense import ExpenseCategory
from database.models.policy_rule import PolicyAppliesTo, PolicyRuleType

logger = logging.getLogger(__name__)

RULE_TYPES: tuple[str, ...] = tuple(e.value for e in PolicyRuleType)
APPLIES_TO: tuple[str, ...] = tuple(e.value for e in PolicyAppliesTo)
EXPENSE_CATEGORIES: tuple[str, ...] = tuple(e.value for e in ExpenseCategory)
CABIN_CLASSES: tuple[str, ...] = ("economy", "premium_economy", "business", "first")
APPROVERS: frozenset[str] = frozenset({"manager", "VP"})

_MAX_SENTENCES = 6
_MAX_ERRORS = 6
GENERIC_DESCRIPTION = (
    "Atlas could not summarise this rule's definition; review the raw definition "
    "before relying on it."
)
_ENGINE = PolicyEvalTool()

# ---------------------------------------------------------------------------
# Probes — synthetic expense lines run through the engine's own builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProbeSegment:
    """Duck-typed TripSegment for ``build_eval_item`` (only the attributes it reads)."""

    details: dict[str, Any] = field(default_factory=dict)
    location: str | None = None
    supplier: str | None = None
    start_datetime: str | None = None
    end_datetime: str | None = None


def _probe_entry(category: str, amount: float = 100.0) -> DraftEntry:
    return DraftEntry(
        category=category,
        amount=amount,
        currency="USD",
        expense_date="2026-01-05",
        vendor=None,
        description=None,
        status="matched",
        match_source=None,
        trip_segment_id=None,
        is_itemized=False,
        primary_receipt_id=None,
        source_card_txn_id=None,
        sibling_group_id=None,
    )


# Expense categories the categorizer files car-rental and train receipts under (derived
# from categorize.py, not restated: Atlas files both as ground transport today).
_CAR_RENTAL_CATEGORY = category_for_expense_type("car") or "other"
_RAIL_CATEGORY = category_for_expense_type("train") or "other"

# (reach key, expense category, segment details or None, label).  Segment details are
# what the product actually stores: no planner, form or seed records a car segment's
# ``vehicle_class`` (only a raw POST /trips payload could), so a car-rental line is
# probed with a product-shaped car segment (no vehicle class) — "car_classed" is the
# hypothetical vehicle-class path that car rules depend on.
_REACH: tuple[tuple[str, str, dict[str, Any] | None, str], ...] = (
    ("airfare", "airfare", None, "airfare"),
    ("lodging", "lodging", None, "lodging"),
    ("meals", "meals", None, "meal"),
    ("ground_transport", "ground_transport", None, "taxi, rideshare, transit and parking"),
    ("car_rental", _CAR_RENTAL_CATEGORY, {}, "car-rental"),
    ("rail", _RAIL_CATEGORY, None, "train-ticket"),
    (
        "car_classed",
        "ground_transport",
        {"vehicle_class": "Economy"},
        "car-rental lines whose trip car segment records a vehicle class (Atlas never "
        "records one today)",
    ),
    ("other", "other", None, "other"),
)
_REACH_CATEGORY = {key: cat for key, cat, _, _ in _REACH}
_REACH_LABEL = {key: label for key, _, _, label in _REACH}


def _reach_item(key: str, amount: float = 100.0) -> EvalItem:
    for k, cat, details, _ in _REACH:
        if k == key:
            seg = _ProbeSegment(details=dict(details)) if details is not None else None
            return build_eval_item(_probe_entry(cat, amount), segment=seg)
    raise KeyError(key)


def expense_reach(rule_type: Any, applies_to: Any, definition: Any) -> tuple[str, ...]:
    """Which kinds of expense line the engine evaluates this rule for (reach keys)."""
    out = []
    for key, _, _, _ in _REACH:
        if PolicyEvalTool.rule_applies(rule_type, applies_to, definition, _reach_item(key)):
            out.append(key)
    return tuple(out)


@lru_cache(maxsize=1)
def category_filterable() -> frozenset[str]:
    """Expense categories an ``expense_categories`` filter can actually match.

    Only lines the engine evaluates as general receipts keep their category on the
    EvalItem (airfare / lodging / meals are evaluated as flight / hotel / meals).
    """
    out = set()
    for cat in EXPENSE_CATEGORIES:
        item = build_eval_item(_probe_entry(cat), segment=None)
        if item.expense_category == cat:
            out.add(cat)
    return frozenset(out)


def _planning_reach(rule_type: str, applies_to: str, definition: Any) -> tuple[str, ...]:
    """Trip-planning option types (the planner only ranks flights and hotels)."""
    return tuple(
        t
        for t in ("flight", "hotel")
        if PolicyEvalTool.rule_applies(
            rule_type,
            applies_to,
            definition,
            EvalItem(item_type=t),  # type: ignore[arg-type]
        )
    )


def _verdict(
    rule_type: str, applies_to: str, definition: Mapping[str, Any], item: EvalItem
) -> RuleVerdict | None:
    try:
        return _ENGINE.evaluate_candidate(rule_type, applies_to, dict(definition), item)
    except Exception as exc:  # noqa: BLE001 — a description is never fatal
        logger.debug("describe_rule: probe evaluation failed: %s", exc)
        return None


@dataclass(frozen=True)
class Outcome:
    """What a FAILING verdict does to an expense line (presubmit.verdicts_to_flags)."""

    flagged: bool
    blocking: bool
    justification_unblocks: bool
    flag_type: str | None


def expense_outcome(verdict: RuleVerdict, category: str, *, has_receipt: bool = True) -> Outcome:
    flags, _ = verdicts_to_flags(
        [verdict], entry_ref="probe", has_receipt=has_receipt, category=category
    )
    if not flags:
        return Outcome(False, False, False, None)
    blocking = any(f.severity == "blocking" for f in flags)
    justified, _ = verdicts_to_flags(
        [verdict],
        entry_ref="probe",
        has_receipt=has_receipt,
        category=category,
        justification="Probe business justification.",
    )
    unblocks = blocking and not any(f.severity == "blocking" for f in justified)
    return Outcome(True, blocking, unblocks, flags[0].flag_type)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _enum_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _money(value: Any) -> str:
    f = _num(value)
    if f is None:
        return "?"
    return f"${f:,.0f}" if f.is_integer() else f"${f:,.2f}"


def _join(parts: Iterable[str], last: str = "and") -> str:
    items = [p for p in parts if p]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def _valid_tiers(tiers: Any) -> list[dict[str, Any]]:
    return [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []


def _tier_places(tier: Mapping[str, Any], limit: int = 3) -> str:
    cities = [c for c in (tier.get("cities") or []) if isinstance(c, str) and c.strip()]
    if not cities and isinstance(tier.get("aliases"), dict):
        cities = [str(k) for k in tier["aliases"]]
    shown = ", ".join(cities[:limit])
    return f"{shown}, …" if len(cities) > limit else shown


def _is_city_tier(tier: Mapping[str, Any]) -> bool:
    return bool(tier.get("cities")) or bool(tier.get("aliases"))


def _tier_number(tier: Mapping[str, Any]) -> tuple[int, str]:
    label = str(tier.get("label") or tier.get("tier") or "")
    match = re.search(r"(\d+)", label)
    return (int(match.group(1)) if match else 10_000, label)


def tier_breakdown(tiers: Any, value_key: str, unit: str) -> str | None:
    """'Tier 1 (New York, San Francisco, …): $300/night; …; any other city (Tier 3): $160/night'.

    The "any other city" tier is the one the engine resolves for an unlisted city.
    """
    valid = [t for t in _valid_tiers(tiers) if _num(t.get(value_key)) is not None]
    if not valid:
        return None
    fallback = PolicyEvalTool.resolve_tier(valid, "Atlas Unlisted Probe City", value_key)
    city_tiers = sorted((t for t in valid if _is_city_tier(t)), key=_tier_number)
    parts = [
        f"{PolicyEvalTool.tier_label(t)} ({_tier_places(t)}): {_money(t[value_key])}{unit}"
        for t in city_tiers
    ]
    if fallback is not None:
        label = PolicyEvalTool.tier_label(fallback)
        other = "any other city" if city_tiers else "every city"
        named = f" ({label})" if city_tiers and _tier_number(fallback)[0] != 10_000 else ""
        parts.append(f"{other}{named}: {_money(fallback[value_key])}{unit}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# validate_rule_definition
# ---------------------------------------------------------------------------

_TARGETS: dict[str, frozenset[str]] = {
    "amount_cap": frozenset({"hotel", "flight", "car", "all"}),
    "per_diem": frozenset({"meals"}),
    "cabin_class": frozenset({"flight"}),
    "preferred_supplier": frozenset({"flight", "hotel"}),
    "category_allow": frozenset({"car"}),
    "receipt_required": frozenset({"all"}),
    # Product gap 2 (policy2)
    "per_head_cap": frozenset({"meals", "all"}),
    "gift_disclosure": frozenset({"all"}),
    "mileage": frozenset({"all"}),
}

_KEYS: dict[str, frozenset[str]] = {
    "amount_cap": frozenset(
        {
            "currency",
            "per",
            "tiers",
            "exception_approver",
            "expense_categories",
            "basis",
            "corpus_clause_ref",
            # product gap 8 / 2: ground sub-type filter and the LOD-02 tolerance.
            "ground_subtypes",
            *entry_sem.TOLERANCE_KEYS,
        }
    ),
    "per_diem": frozenset(
        {
            "currency",
            "per",
            "rates",
            "exclusions",
            "group_meal_threshold",
            "group_meal_approver",
            "corpus_clause_ref",
            *entry_sem.PER_DIEM_EXTRA_KEYS,  # MEAL-02 / MEAL-04 (product gap 2)
        }
    ),
    "cabin_class": frozenset({"tiers", "override_approver", *process_sem.ROLE_EXCEPTION_KEYS}),
    "preferred_supplier": frozenset(
        {"preferred_suppliers", "within_pct", "exception_approver", "condition"}
    ),
    "category_allow": frozenset(
        {"allowed_classes", "excluded_classes", "conditional_classes", "exception_approver"}
    ),
    "receipt_required": frozenset(
        {
            "threshold_usd",
            "always_required_categories",
            "consequence",
            *process_sem.AFFIDAVIT_KEYS,  # EXP-04 (product gap 2 stage 2)
        }
    ),
    # Product gap 2 (policy2)
    "per_head_cap": frozenset(
        {
            "currency",
            "per",
            "tiers",
            "min_attendees",
            "expense_categories",
            "exception_approver",
            "corpus_clause_ref",
        }
    ),
    "gift_disclosure": frozenset(
        {
            "currency",
            "threshold_usd",
            "expense_categories",
            "disclosure_categories",
            "government_official_approver",
            "corpus_clause_ref",
        }
    ),
    "mileage": frozenset(
        {
            "check",
            "currency",
            "rate_per_mile",
            "rate_basis",
            "max_one_way_miles",
            "exception_approver",
            "expense_categories",
            "corpus_clause_ref",
        }
    ),
}

_TIER_KEYS_CITY = frozenset({"tier", "label", "description", "cities", "aliases", "default"})
_TIER_KEYS_FLAT = frozenset({"tier", "label", "description"})


def _target_error(rt: str, at: str) -> str:
    if at == "rail":
        return (
            "Atlas has no rail expense category and no rail trip options, so a rail rule "
            "would never be enforced."
        )
    if rt == "amount_cap" and at == "meals":
        return "Meal limits must be a per_diem rule (applies_to 'meals'), not an amount_cap."
    if rt == "preferred_supplier":
        return (
            f"Atlas does not record a supplier on {at} expense lines, so every line would "
            "fail a preferred-supplier rule; only flight and hotel are supported."
        )
    if rt == "receipt_required":
        return (
            "Receipt rules must apply to all expense lines (applies_to 'all'); use "
            "always_required_categories to name categories."
        )
    allowed = ", ".join(sorted(_TARGETS[rt]))
    return f"A {rt} rule can only apply to: {allowed} (got {at!r})."


def _approver_error(defn: Mapping[str, Any], key: str) -> str | None:
    if key not in defn:
        return None
    value = defn.get(key)
    if value is None or (isinstance(value, str) and value in APPROVERS):
        return None
    return f"{key} must be 'manager', 'VP' or null (got {value!r})."


def _str_list(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(v, str) and v.strip() for v in value)
    )


def _tier_errors(tiers: Any, value_key: str, noun: str, *, by_city: bool) -> list[str]:
    if not isinstance(tiers, list) or not tiers:
        return [f"{noun} must be a non-empty list."]
    errors: list[str] = []
    allowed = (_TIER_KEYS_CITY if by_city else _TIER_KEYS_FLAT) | {value_key}
    names: list[str] = []
    for i, tier in enumerate(tiers, start=1):
        where = f"{noun}[{i}]"
        if not isinstance(tier, dict):
            errors.append(f"{where} must be an object.")
            continue
        for key in sorted(set(tier) - allowed):
            hint = " (city tiers are only supported for hotel caps)" if key == "cities" else ""
            errors.append(f"{where} key {key!r} is not used by the rule engine{hint}.")
        name = tier.get("tier")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where} needs a 'tier' name.")
        else:
            names.append(name)
        value = _num(tier.get(value_key))
        if value is None or value <= 0:
            errors.append(f"{where} {value_key} must be a number greater than 0.")
        if "cities" in tier and not _str_list(tier.get("cities")):
            errors.append(f"{where} cities must be a non-empty list of city names.")
        aliases = tier.get("aliases")
        if "aliases" in tier and not (
            isinstance(aliases, dict) and all(_str_list(v) for v in aliases.values())
        ):
            errors.append(f"{where} aliases must map a city to a list of alternative names.")
        if "default" in tier and not isinstance(tier.get("default"), bool):
            errors.append(f"{where} default must be true or false.")
        for key in ("label", "description"):
            if key in tier and not isinstance(tier.get(key), str):
                errors.append(f"{where} {key} must be text.")
    if len(set(names)) != len(names):
        errors.append(f"{noun} tier names must be unique.")
    if errors:
        return errors
    if not by_city:
        if len(tiers) != 1:
            errors.append(
                f"{noun} must have exactly one tier: these lines carry no city, so only "
                "the lowest tier would ever be applied."
            )
        return errors
    defaults = [t for t in tiers if t.get("default") is True]
    if len(defaults) > 1:
        errors.append(f"Only one of the {noun} may be the default (default: true).")
    if any(_is_city_tier(t) for t in defaults):
        errors.append(f"The default tier of {noun} must not list cities.")
    fallback = [t for t in tiers if not _is_city_tier(t)]
    if not fallback:
        errors.append(
            f"One of the {noun} must list no cities and be marked default: true — it applies "
            "to every other location."
        )
    elif len(fallback) > 1:
        errors.append(
            f"Only one of the {noun} may omit cities: any other tier without cities would "
            "never be applied."
        )
    elif len(tiers) > 1 and not (
        fallback[0].get("default") is True or fallback[0].get("tier") == "domestic"
    ):
        errors.append(
            f"The tier without cities must be marked default: true, otherwise cities that "
            f"are not listed would get the lowest of the {noun} instead."
        )
    return errors


def _validate_amount_cap(at: str, defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if defn.get("currency", "USD") != "USD":
        errors.append("currency must be 'USD': amounts are compared in US dollars.")
    per = defn.get("per")
    if at == "hotel":
        if per not in (None, "night"):
            errors.append("A hotel cap is per night (per: 'night').")
        errors.extend(_tier_errors(defn.get("tiers"), "cap", "tiers", by_city=True))
        if "basis" in defn and defn.get("basis") != "room_plus_tax":
            errors.append("basis may only be 'room_plus_tax' (the nightly amount incl. tax).")
    else:
        if per not in (None, "item", "day"):
            errors.append(
                f"per {per!r} is not supported for a {at} cap: each line's amount is "
                "compared on its own (use 'item')."
            )
        errors.extend(_tier_errors(defn.get("tiers"), "cap", "tiers", by_city=False))
        if "basis" in defn:
            errors.append("basis is only used for hotel caps.")
    if at == "all":
        cats = defn.get("expense_categories")
        allowed = category_filterable()
        if cats is None:
            errors.append(
                "An amount_cap for 'all' must list expense_categories (e.g. "
                "['ground_transport']); a single cap across every expense type isn't supported."
            )
        elif not _str_list(cats):
            errors.append("expense_categories must be a non-empty list of categories.")
        else:
            bad = [c for c in cats if c not in allowed]
            if bad:
                errors.append(
                    f"expense_categories may only name {_join(sorted(allowed), 'or')}; "
                    f"{_join(bad)} lines are evaluated through their own target "
                    "(flight, hotel or meals)."
                )
    elif "expense_categories" in defn:
        errors.append("expense_categories is only used with applies_to 'all'.")
    err = _approver_error(defn, "exception_approver")
    if err:
        errors.append(err)
    return errors


def _validate_per_diem(defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if defn.get("currency", "USD") != "USD":
        errors.append("currency must be 'USD': amounts are compared in US dollars.")
    if defn.get("per") not in (None, "day"):
        errors.append("A meal per diem is per day (per: 'day').")
    rates = defn.get("rates")
    if PolicyEvalTool._is_tiered_rates(rates):
        errors.extend(_tier_errors(rates, "amount", "rates", by_city=True))
    elif not isinstance(rates, list) or not rates:
        errors.append("rates must be a non-empty list.")
    else:
        regions: list[str] = []
        for i, rate in enumerate(rates, start=1):
            if not isinstance(rate, dict) or set(rate) - {"region", "amount"}:
                errors.append(f"rates[{i}] must be {{'region', 'amount'}} or a city tier.")
                continue
            amount = _num(rate.get("amount"))
            if amount is None or amount <= 0:
                errors.append(f"rates[{i}] amount must be a number greater than 0.")
            region = rate.get("region")
            if region == "international":
                errors.append(
                    "Atlas cannot tell whether a meal was international, so an "
                    "'international' rate would never be applied — use city tiers instead."
                )
            elif region != "domestic":
                errors.append(f"rates[{i}] region must be 'domestic' (got {region!r}).")
            regions.append(str(region))
        if len(set(regions)) != len(regions):
            errors.append("rates must not repeat a region.")
    if "exclusions" in defn and not _str_list(defn.get("exclusions"), allow_empty=True):
        errors.append("exclusions must be a list of text items.")
    if "group_meal_threshold" in defn:
        threshold = defn.get("group_meal_threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 2:
            errors.append("group_meal_threshold must be a whole number of at least 2.")
    if "group_meal_approver" in defn and defn.get("group_meal_approver") not in (None, "manager"):
        errors.append("group_meal_approver must be 'manager' or null.")
    return errors


def _validate_cabin(defn: Mapping[str, Any]) -> list[str]:
    tiers = defn.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return ["tiers must be a non-empty list of duration bands."]
    errors: list[str] = []
    bands: list[tuple[float, float | None]] = []
    for i, tier in enumerate(tiers, start=1):
        where = f"tiers[{i}]"
        if not isinstance(tier, dict):
            errors.append(f"{where} must be an object.")
            continue
        for key in sorted(set(tier) - {"min_hours", "max_hours", "allowed_classes"}):
            errors.append(f"{where} key {key!r} is not used by the rule engine.")
        classes = tier.get("allowed_classes")
        if not isinstance(classes, list) or not _str_list(classes):
            errors.append(f"{where} allowed_classes must list cabins from {list(CABIN_CLASSES)}.")
        elif any(c not in CABIN_CLASSES for c in classes):
            errors.append(f"{where} allowed_classes must list cabins from {list(CABIN_CLASSES)}.")
        lo = _num(tier.get("min_hours", 0)) if "min_hours" in tier else 0.0
        hi = _num(tier.get("max_hours")) if "max_hours" in tier else None
        if lo is None or lo < 0 or ("max_hours" in tier and (hi is None or hi <= lo)):
            errors.append(f"{where} needs min_hours >= 0 and max_hours > min_hours.")
            continue
        bands.append((lo, hi))
    if errors:
        return errors
    bands.sort(key=lambda b: b[0])
    expected = 0.0
    for idx, (lo, hi) in enumerate(bands):
        if lo != expected:
            errors.append(
                f"Duration bands must cover every flight length without gaps or overlaps "
                f"(a band should start at {expected:g}h, not {lo:g}h); flights in a gap "
                "would have no allowed cabin at all."
            )
            break
        if hi is None:
            if idx != len(bands) - 1:
                errors.append("Only the last duration band may be open-ended.")
            break
        expected = hi
    else:
        errors.append(
            "The last duration band must be open-ended (no max_hours), otherwise longer "
            "flights would have no allowed cabin at all."
        )
    err = _approver_error(defn, "override_approver")
    if err:
        errors.append(err)
    return errors


def _validate_supplier(at: str, defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not _str_list(defn.get("preferred_suppliers")):
        errors.append("preferred_suppliers must be a non-empty list of supplier names.")
    if "within_pct" in defn and defn.get("within_pct") is not None:
        pct = _num(defn.get("within_pct"))
        if pct is None or not 0 <= pct <= 100:
            errors.append("within_pct must be a percentage between 0 and 100.")
    if "condition" in defn:
        if at != "hotel":
            errors.append("condition is only used by hotel preferred-supplier rules.")
        elif not (isinstance(defn.get("condition"), str) and defn["condition"].strip()):
            errors.append("condition must be a short text.")
    err = _approver_error(defn, "exception_approver")
    if err:
        errors.append(err)
    return errors


def _validate_category_allow(defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = defn.get("allowed_classes")
    if not _str_list(allowed):
        errors.append("allowed_classes must be a non-empty list of vehicle classes.")
    for key in ("excluded_classes",):
        if key in defn and not _str_list(defn.get(key), allow_empty=True):
            errors.append(f"{key} must be a list of vehicle classes.")
    conditional = defn.get("conditional_classes", [])
    if not isinstance(conditional, list) or not all(
        isinstance(c, dict)
        and set(c) <= {"class", "condition"}
        and isinstance(c.get("class"), str)
        and c["class"].strip()
        for c in conditional
    ):
        errors.append("conditional_classes must be a list of {'class', 'condition'} objects.")
    if not errors:
        seen: dict[str, str] = {}
        lists = (
            ("allowed_classes", allowed or []),
            ("excluded_classes", defn.get("excluded_classes") or []),
            ("conditional_classes", [c["class"] for c in conditional]),
        )
        for key, values in lists:
            for v in values:
                if v in seen and seen[v] != key:
                    errors.append(f"Vehicle class {v!r} is listed in both {seen[v]} and {key}.")
                seen[v] = key
    err = _approver_error(defn, "exception_approver")
    if err:
        errors.append(err)
    return errors


def _validate_receipt(defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    threshold = _num(defn.get("threshold_usd"))
    if threshold is None or threshold < 0:
        errors.append("threshold_usd must be a number of at least 0.")
    cats = defn.get("always_required_categories", [])
    if not _str_list(cats, allow_empty=True) or any(c not in EXPENSE_CATEGORIES for c in cats):
        errors.append(
            f"always_required_categories must list categories from {list(EXPENSE_CATEGORIES)}."
        )
    if defn.get("consequence", "flagged_for_review") != "flagged_for_review":
        errors.append(
            "consequence must be 'flagged_for_review': a missing receipt always blocks "
            "submission and no other consequence is supported."
        )
    return errors


# --- Product gap 2 (policy2): per_head_cap / gift_disclosure / mileage ----------


def _spend_categories_error(defn: Mapping[str, Any], allowed: frozenset[str]) -> str | None:
    cats = defn.get("expense_categories")
    if not isinstance(cats, list) or not _str_list(cats) or any(c not in allowed for c in cats):
        return f"expense_categories must list categories from {sorted(allowed)}."
    return None


def _validate_per_head_cap(at: str, defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if defn.get("currency", "USD") != "USD":
        errors.append("currency must be 'USD': amounts are compared in US dollars.")
    if defn.get("per", "head") != "head":
        errors.append("A per-head cap is per attendee (per: 'head').")
    errors.extend(_tier_errors(defn.get("tiers"), "cap", "tiers", by_city=True))
    if at == "meals":
        minimum = defn.get("min_attendees", TEAM_MEAL_MIN_ATTENDEES)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
            errors.append("min_attendees must be a whole number of at least 2 (a team meal).")
        if "expense_categories" in defn:
            errors.append("expense_categories is only used with applies_to 'all'.")
    else:
        err = _spend_categories_error(defn, frozenset({"entertainment"}))
        if err:
            errors.append(err)
        if "min_attendees" in defn:
            errors.append("min_attendees is only used for team meals (applies_to 'meals').")
    err = _approver_error(defn, "exception_approver")
    if err:
        errors.append(err)
    return errors


def _validate_gift_disclosure(defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if defn.get("currency", "USD") != "USD":
        errors.append("currency must be 'USD': amounts are compared in US dollars.")
    threshold = _num(defn.get("threshold_usd"))
    if threshold is None or threshold < 0:
        errors.append("threshold_usd must be a number of at least 0.")
    err = _spend_categories_error(defn, frozenset({"gift", "entertainment"}))
    if err:
        errors.append(err)
    raw = defn.get("expense_categories")
    cats: list[Any] = raw if isinstance(raw, list) and _str_list(raw) else []
    disclosure = defn.get("disclosure_categories", ["gift"])
    if (
        not isinstance(disclosure, list)
        or not _str_list(disclosure)
        or any(c not in cats for c in disclosure)
    ):
        errors.append("disclosure_categories must be a subset of expense_categories.")
    approver = defn.get("government_official_approver", "Legal/Compliance")
    if not isinstance(approver, str) or not approver.strip():
        errors.append("government_official_approver must name who pre-approves.")
    return errors


def _validate_mileage(defn: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    check = defn.get("check")
    if check not in MILEAGE_CHECKS:
        return [f"check must be one of {list(MILEAGE_CHECKS)}."]
    err = _spend_categories_error(defn, frozenset({"mileage"}))
    if err:
        errors.append(err)
    allowed = {
        "rate": {"rate_per_mile", "rate_basis", "currency"},
        "commute_offset": set(),
        "air_cap": {"max_one_way_miles", "exception_approver"},
    }[check]
    common = {"check", "expense_categories", "corpus_clause_ref"}
    for key in sorted(set(defn) - allowed - common):
        errors.append(f"definition key {key!r} is not used by a {check} mileage rule.")
    if check == "rate":
        if defn.get("currency", "USD") != "USD":
            errors.append("currency must be 'USD': the rate is US dollars per mile.")
        rate = to_decimal(defn.get("rate_per_mile"))
        if rate is None or rate <= 0 or rate > 10:
            errors.append("rate_per_mile must be a dollar amount per mile above 0 (e.g. '0.70').")
    if check == "air_cap":
        limit = _num(defn.get("max_one_way_miles"))
        if limit is None or limit <= 0:
            errors.append("max_one_way_miles must be a number of miles greater than 0.")
        err = _approver_error(defn, "exception_approver")
        if err:
            errors.append(err)
    return errors


def _validate(rule_type: Any, applies_to: Any, definition: Any) -> list[str]:
    rt, at = _enum_str(rule_type), _enum_str(applies_to)
    if rt not in RULE_TYPES:
        return [f"rule_type {rt!r} is not one of {list(RULE_TYPES)}."]
    if at not in APPLIES_TO:
        return [f"applies_to {at!r} is not one of {list(APPLIES_TO)}."]
    if not isinstance(definition, Mapping):
        return ["definition must be a JSON object."]
    if process_kind(definition) is not None:
        # Product gap 2 stage 2: process rules (EXP-01, PTA-02, PTA-05, AIR-06, CARD-03).
        return process_sem.validate_process_kind(rt, at, definition)
    if "kind" in definition:
        # Product gap 2: entry-level rules (GRND-01 / GRND-03 / GRND-05).
        return entry_sem.validate_kind(rt, at, definition)
    if at not in _TARGETS[rt]:
        return [_target_error(rt, at)]
    errors = [
        f"definition key {key!r} is not used by the rule engine (it would be silently ignored)."
        for key in sorted(set(definition) - _KEYS[rt])
    ]
    if "corpus_clause_ref" in definition and not isinstance(
        definition.get("corpus_clause_ref"), str
    ):
        errors.append("corpus_clause_ref must be a clause id.")
    if rt == "amount_cap":
        errors.extend(_validate_amount_cap(at, definition))
        errors.extend(entry_sem.validate_ground_subtypes(at, definition))
        errors.extend(entry_sem.validate_tolerance(at, definition))
    elif rt == "per_diem":
        errors.extend(_validate_per_diem(definition))
        errors.extend(entry_sem.validate_per_diem_extras(definition))
    elif rt == "cabin_class":
        errors.extend(_validate_cabin(definition))
        errors.extend(process_sem.validate_role_exception(definition))
    elif rt == "preferred_supplier":
        errors.extend(_validate_supplier(at, definition))
    elif rt == "category_allow":
        errors.extend(_validate_category_allow(definition))
    elif rt == "receipt_required":
        errors.extend(_validate_receipt(definition))
        errors.extend(process_sem.validate_affidavit(definition))
    elif rt == "per_head_cap":
        errors.extend(_validate_per_head_cap(at, definition))
    elif rt == "gift_disclosure":
        errors.extend(_validate_gift_disclosure(definition))
    elif rt == "mileage":
        errors.extend(_validate_mileage(definition))
    return errors


def validate_rule_definition(rule_type: Any, applies_to: Any, definition: Any) -> tuple[str, ...]:
    """Human-readable problems with a rule; ``()`` means the engine enforces it as written.

    Total: any unexpected input yields an error tuple, never an exception.
    """
    try:
        return tuple(_validate(rule_type, applies_to, definition)[:_MAX_ERRORS])
    except Exception as exc:  # noqa: BLE001 — totality: never raise on legacy JSON
        logger.warning("validate_rule_definition: unexpected definition shape (%s)", exc)
        return ("The rule definition could not be validated.",)


# ---------------------------------------------------------------------------
# describe_rule
# ---------------------------------------------------------------------------


def _consequence(outcome: Outcome | None, what: str) -> str | None:
    if outcome is None:
        return None
    if not outcome.flagged:
        return f"{what} is noted but never flagged or blocked."
    if outcome.blocking and outcome.justification_unblocks:
        return (
            f"{what} blocks submission until the employee adds a written justification; "
            "the report then always goes to their manager as a policy exception "
            "(never auto-approved)."
        )
    if outcome.blocking:
        return f"{what} blocks submission; a written justification does not unblock it."
    return f"{what} is flagged for the approver as advisory only and never blocks submission."


_GROUND_KEYS = ("ground_transport", "car_rental", "rail")


def _ground_kinds(reach: tuple[str, ...]) -> str:
    """The ground-transport kinds a rule reaches, in reading order (engine-derived)."""
    kinds: list[str] = []
    if "ground_transport" in reach:
        kinds += ["taxi", "rideshare"]
    if "car_rental" in reach:
        kinds.append("car rentals")
    if "rail" in reach:
        kinds.append("train tickets")
    if "ground_transport" in reach:
        kinds += ["transit", "parking"]
    return _join(kinds)


def _scope(rt: str, at: str, defn: Mapping[str, Any], reach: tuple[str, ...]) -> str:
    if "ground_subtypes" in defn and "ground_transport" in reach:
        return entry_sem.ground_scope_sentence(defn)
    if not reach:
        return "It is not applied to any expense line."
    if set(reach) == set(_REACH_LABEL):
        return "Applies to every expense line."
    if at == "all" and "ground_transport" in reach and "expense_categories" in defn:
        extra = " and other" if "other" in reach else ""
        return (
            f"Applies to every ground-transport{extra} expense line — "
            f"{_ground_kinds(reach)} alike (Atlas files all of these as ground transport; "
            "name ground_subtypes to target only one kind)."
        )
    if reach == ("car_classed",):
        return (
            "Applies only to car-rental expense lines linked to a trip car segment that "
            "records a vehicle class. Atlas's trip planner and expense forms never record a "
            "vehicle class, so in practice no expense line is checked against it (car-rental "
            "receipts are filed as ground transport); taxi, rideshare, transit and parking "
            "expenses are not covered."
        )
    labels: list[str] = []
    grouped = False
    for key in reach:
        if key in _GROUND_KEYS:
            if not grouped:
                labels.append(f"ground-transport ({_ground_kinds(reach)})")
                grouped = True
            continue
        labels.append(_REACH_LABEL[key])
    sentence = f"Applies to {_join(labels)} expense lines"
    if "car_classed" in reach and "ground_transport" not in reach:
        sentence += "; taxi, rideshare, transit and parking expenses are not covered"
    return sentence + "."


# Trip segments re-checked at approval that trip planning never shows (car / rail).
_APPROVAL_SEGMENTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("car", "car-rental segments that record a vehicle class", {"vehicle_class": "Economy"}),
    ("car", "car-rental segments (each at its full rental amount)", {}),
    ("rail", "rail segments", None),
)


def _approval_sentence(rt: str, at: str, defn: Mapping[str, Any]) -> str | None:
    """Trip segments (beyond flights / hotels) this rule is checked on at trip approval."""
    if rt == "receipt_required":  # a plan carries no receipts (has_receipt=True)
        return None
    from ai_agents.agents.approval_agent.evaluate import _SEGMENT_CATEGORY  # noqa: PLC0415

    labels = []
    for seg_type, label, details in _APPROVAL_SEGMENTS:
        category = _SEGMENT_CATEGORY.get(seg_type, "other")
        item = build_eval_item(
            _probe_entry(category), segment=_ProbeSegment(details=dict(details or {}))
        )
        if PolicyEvalTool.rule_applies(rt, at, defn, item):
            labels.append(label)
    if _APPROVAL_SEGMENTS[1][1] in labels:  # every car-rental segment — one label says it
        labels = [x for x in labels if x != _APPROVAL_SEGMENTS[0][1]]
    if not labels:
        return None
    return f"When a trip is submitted for approval, its {_join(labels)} are checked too."


def _a(noun: str) -> str:
    return f"an {noun}" if noun[:1].lower() in "aeiou" else f"a {noun}"


def _trip_sentence(
    rt: str, at: str, defn: Mapping[str, Any], fail_item: EvalItem | None
) -> str | None:
    planning = _planning_reach(rt, at, defn)
    if not planning:
        return None
    names = _join(f"{t} options" for t in planning)
    verdict = _verdict(rt, at, defn, fail_item) if fail_item is not None else None
    if verdict is not None and not verdict.in_policy:
        return (
            f"In trip planning, {names} that break it are marked out of policy, and a trip's "
            "segments are checked again when it is submitted for approval."
        )
    return f"In trip planning it is shown on {names} as advice only."


def _amount_cap(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    tiers = _valid_tiers(defn.get("tiers"))
    lowest = PolicyEvalTool.resolve_tier(tiers, None, "cap")
    out = [_scope(rt, at, defn, reach)]
    fail_key = next((k for k in reach), None)
    fail_item = None
    if lowest is not None and _num(lowest.get("cap")) is not None:
        cap = float(lowest["cap"])
        if at == "hotel":
            out.append(f"Nightly cap by hotel city — {tier_breakdown(tiers, 'cap', '')}.")
            out.append(
                "Lodging lines are compared per night when the stay length is verified "
                "(approved itinerary or receipt), otherwise at the full line amount; if the "
                f"hotel city can't be verified the lowest cap ({_money(cap)}) applies."
            )
            tolerance = entry_sem.tolerance_sentence(defn)
            if tolerance:
                out.append(tolerance)
        else:
            not_daily = (
                " (it is not divided by the number of days)" if defn.get("per") == "day" else ""
            )
            out.append(f"Each line's full amount is compared with a {_money(cap)} cap{not_daily}.")
        if fail_key is not None:
            fail_item = _reach_item(fail_key, amount=cap + 1)
    outcome = None
    if fail_item is not None and fail_key is not None:
        verdict = _verdict(rt, at, defn, fail_item)
        if verdict is not None and not verdict.in_policy:
            outcome = expense_outcome(verdict, _REACH_CATEGORY[fail_key])
    out.append(_consequence(outcome, "On expense reports, a line over the cap") or "")
    out.append(_trip_sentence(rt, at, defn, fail_item) or "")
    out.append(_approval_sentence(rt, at, defn) or "")
    return out


def _per_diem(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    out = [_scope(rt, at, defn, reach)]
    rates = defn.get("rates")
    tiered = PolicyEvalTool._is_tiered_rates(rates)
    probe = _reach_item("meals", amount=1.0)
    in_policy = _verdict(rt, at, defn, probe)
    rate = _num((in_policy.observed or {}).get("rate")) if in_policy is not None else None
    if tiered:
        out.append(f"Daily meal limit by city — {tier_breakdown(rates, 'amount', '/day')}.")
    elif rate is not None:
        out.append(
            f"Each line is compared with the domestic rate of {_money(rate)} per day (Atlas "
            "does not determine a meal's region)."
        )
    if rate is not None:
        lowest = f" the lowest rate ({_money(rate)}) applies" if tiered else ""
        city = f"; if the city can't be verified{lowest}" if tiered else ""
        out.append(
            "Meals linked to an approved trip are pooled over the trip's days and the daily "
            f"average is compared; other meal lines are compared one line at a time{city}."
        )
        verdict = _verdict(rt, at, defn, _reach_item("meals", amount=rate + 1))
        if verdict is not None and not verdict.in_policy:
            out.append(
                _consequence(
                    expense_outcome(verdict, "meals"), "On expense reports, a day over the limit"
                )
                or ""
            )
    out.extend(entry_sem.per_diem_sentences(defn))
    caveats = []
    if "group_meal_threshold" in defn:
        caveats.append(
            f"group-meal approval ({defn.get('group_meal_threshold')}+ attendees; attendee "
            "counts aren't recorded on expense lines)"
        )
    if caveats:
        out.append(f"Not checked automatically: {_join(caveats)}.")
    return out


def _hours_band(lo: float, hi: float | None) -> str:
    if hi is None:
        return f"{lo:g}h and over" if lo else "any length"
    return f"under {hi:g}h" if lo == 0 else f"{lo:g}–{hi:g}h"


def _cabin(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    out = [_scope(rt, at, defn, reach)]
    bands = []
    for tier in _valid_tiers(defn.get("tiers")):
        lo = _num(tier.get("min_hours", 0)) or 0.0
        hi = _num(tier.get("max_hours")) if "max_hours" in tier else None
        classes = tier.get("allowed_classes")
        if isinstance(classes, list):
            bands.append((lo, hi, format_cabin_classes([str(c) for c in classes])))
    bands.sort(key=lambda b: b[0])
    if bands:
        out.append(
            "Allowed cabins by flight duration — "
            + "; ".join(f"{_hours_band(lo, hi)}: {cls}" for lo, hi, cls in bands)
            + "."
        )
    fail_item = None
    for hours in (1.0, 4.0, 7.0, 11.0, 15.0):
        for cabin in reversed(CABIN_CLASSES):
            item = dataclasses.replace(
                _reach_item("airfare"), cabin_class=cabin, duration_hours=hours
            )
            verdict = _verdict(rt, at, defn, item)
            if verdict is not None and not verdict.in_policy:
                fail_item = item
                break
        if fail_item is not None:
            break
    if fail_item is not None:
        verdict = _verdict(rt, at, defn, fail_item)
        if verdict is not None:
            approver = verdict.requires_approver
            what = "On expense reports, a flight in a cabin that isn't allowed"
            sentence = _consequence(expense_outcome(verdict, "airfare"), what) or ""
            if approver:
                sentence = sentence[:-1] + f" (an exception needs {approver} approval)."
            out.append(sentence)
    unchecked = _verdict(rt, at, defn, _reach_item("airfare"))
    if unchecked is not None and not unchecked.observed:
        flags, _ = verdicts_to_flags(
            [unchecked], entry_ref="probe", has_receipt=True, category="airfare"
        )
        if flags and flags[0].flag_type == "not_evaluated":
            out.append(
                "An airfare line is only checked when it is linked to an approved flight "
                "segment that records the cabin and duration; otherwise it is marked not "
                "evaluated."
            )
    out.append(_trip_sentence(rt, at, defn, fail_item) or "")
    return out


def _supplier(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    out = [_scope(rt, at, defn, reach)]
    names = [str(s) for s in defn.get("preferred_suppliers") or [] if isinstance(s, str)]
    noun = {"flight": "airlines", "hotel": "hotel brands"}.get(at, "suppliers")
    if names:
        out.append(f"Preferred {noun}: {_join(names)}.")
    category = {"flight": "airfare", "hotel": "lodging"}.get(at)
    if category is None:
        return out
    key = "airfare" if category == "airfare" else "lodging"
    fail_item = dataclasses.replace(_reach_item(key), supplier="Atlas Unlisted Probe Supplier")
    verdict = _verdict(rt, at, defn, fail_item)
    if verdict is not None:
        out.append(
            _consequence(
                expense_outcome(verdict, category),
                f"On expense reports, {_a(category)} line with a non-preferred supplier",
            )
            or ""
        )
    unverified = _verdict(rt, at, defn, _reach_item(key))
    if unverified is not None and not unverified.in_policy:
        outcome = expense_outcome(unverified, category)
        if outcome.blocking and not outcome.justification_unblocks:
            out.append(
                "A line whose supplier can't be verified from a receipt, card transaction or "
                "approved itinerary blocks submission and can't be justified."
            )
    pct = _num(defn.get("within_pct"))
    if pct is not None:
        out.append(f"The {pct:g}% price allowance is not applied automatically.")
    out.append(_trip_sentence(rt, at, defn, fail_item) or "")
    return out


def _category_allow(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    out = [_scope(rt, at, defn, reach)]
    allowed = [str(c) for c in defn.get("allowed_classes") or []]
    excluded = [str(c) for c in defn.get("excluded_classes") or []]
    conditional = [
        f"{c.get('class')} ({c.get('condition') or 'conditional'})"
        for c in defn.get("conditional_classes") or []
        if isinstance(c, dict)
    ]
    parts = []
    if allowed:
        parts.append(f"allowed: {_join(allowed)}")
    if conditional:
        parts.append(f"needs approval: {_join(conditional)}")
    if excluded:
        approver = defn.get("exception_approver")
        parts.append(
            f"excluded: {_join(excluded)}" + (f" ({approver} exception)" if approver else "")
        )
    parts.append("any other class is out of policy")
    out.append("Vehicle classes — " + "; ".join(parts) + ".")
    item = dataclasses.replace(_reach_item("car_classed"), vehicle_class="Atlas Probe Class")
    verdict = _verdict(rt, at, defn, item)
    if verdict is not None and not verdict.in_policy and "car_classed" in reach:
        out.append(
            _consequence(
                expense_outcome(verdict, "ground_transport"),
                "On expense reports, a rental in a class that isn't allowed",
            )
            or ""
        )
    out.append(_approval_sentence(rt, at, defn) or "")
    return out


def _receipt(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    reach = expense_reach(rt, at, defn)
    out = [_scope(rt, at, defn, reach)]
    threshold = _num(defn.get("threshold_usd"))
    if threshold is None:
        return out
    out.append(
        f"A receipt is required for lines of {_money(threshold)} or more (for lodging and "
        "trip-linked meals the nightly / daily figure is compared)."
    )
    cats = [c for c in defn.get("always_required_categories") or [] if isinstance(c, str)]
    effective, ignored = [], []
    for cat in cats:
        key = next((k for k, c, d, _ in _REACH if c == cat and d is None), None)
        if key is None:
            continue
        below = max(threshold - 1.0, 0.0) if threshold > 0 else 0.0
        verdict = _verdict(rt, at, defn, _reach_item(key, amount=below))
        if verdict is not None and verdict.observed.get("receipt_required") is True:
            effective.append(cat.replace("_", " "))
        else:
            ignored.append(cat.replace("_", " "))
    if effective:
        out.append(f"{_join(effective).capitalize()} lines always need a receipt.")
    if ignored and threshold > 0:
        out.append(
            f"The always-required list also names {_join(ignored)}, but those lines under "
            f"{_money(threshold)} are not checked (they are evaluated without their category)."
        )
    key = reach[0] if reach else None
    if key is not None:
        verdict = _verdict(rt, at, defn, _reach_item(key, amount=threshold + 1))
        if verdict is not None:
            outcome = expense_outcome(verdict, _REACH_CATEGORY[key], has_receipt=False)
            out.append(_consequence(outcome, "A line without a covering receipt") or "")
    return out


def _kind_describer(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    return entry_sem.describe_kind(defn)


def _process_describer(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    return process_sem.describe_process_kind(defn)


# --- Product gap 2 (policy2) describers -------------------------------------
# The consequence sentence is DERIVED like every other rule's: a failing probe
# line (built through presubmit.build_eval_item with the entry's spend fields)
# is evaluated by the engine and run through verdicts_to_flags.


def _spend_failing_probe(rt: str, at: str, defn: Mapping[str, Any]) -> tuple[EvalItem, str] | None:
    """(a line that breaks the rule, its category) — None when none can be built."""
    cats = [c for c in defn.get("expense_categories") or [] if isinstance(c, str)]
    entry: DraftEntry | None = None
    if rt == "per_head_cap":
        lowest = PolicyEvalTool.resolve_tier(_valid_tiers(defn.get("tiers")), None, "cap")
        cap = _num((lowest or {}).get("cap"))
        if cap is None:
            return None
        category = "meals" if at == "meals" else (cats[0] if cats else "entertainment")
        heads = defn.get("min_attendees", TEAM_MEAL_MIN_ATTENDEES) if at == "meals" else 1
        heads = heads if isinstance(heads, int) and not isinstance(heads, bool) else 2
        entry = dataclasses.replace(
            _probe_entry(category, cap * heads + 1), attendee_count=max(heads, 1)
        )
    elif rt == "gift_disclosure":
        threshold = _num(defn.get("threshold_usd"))
        disclosure = defn.get("disclosure_categories") or ["gift"]
        if threshold is None or not _str_list(disclosure):
            return None
        entry = _probe_entry(disclosure[0], threshold + 1)
    elif rt == "mileage":
        check = defn.get("check")
        limit = _num(defn.get("max_one_way_miles")) or 0.0
        trip: dict[str, Any] = {"distance": "10", "unit": "mi", "commute_offset": "0"}
        amount = 10_000.0  # never equals billable miles x rate
        if check == "commute_offset":
            trip.pop("commute_offset")
        elif check == "air_cap":
            trip["distance"] = str(limit + 1)
        entry = dataclasses.replace(_probe_entry("mileage", amount), mileage=trip)
    if entry is None:
        return None
    return build_eval_item(entry, segment=None), entry.category


def _spend_outcome(rt: str, at: str, defn: Mapping[str, Any]) -> Outcome | None:
    probe = _spend_failing_probe(rt, at, defn)
    if probe is None:
        return None
    item, category = probe
    if not PolicyEvalTool.rule_applies(rt, at, defn, item):
        return None
    verdict = _verdict(rt, at, defn, item)
    if verdict is None or not verdict.observed:
        return None
    return expense_outcome(verdict, category)


def _tier_caps(defn: Mapping[str, Any]) -> str:
    return tier_breakdown(defn.get("tiers"), "cap", "/head") or ""


def _per_head(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    if at == "meals":
        minimum = defn.get("min_attendees", TEAM_MEAL_MIN_ATTENDEES)
        out = [
            f"Applies to team meals: meals lines that record {minimum} or more attendees "
            "(the claimant included). A team meal is checked against this cap instead of "
            "the claimant's own per diem."
        ]
    else:
        out = ["Applies to client entertainment expense lines."]
    caps = _tier_caps(defn)
    out.append(
        "A line may not exceed the per-head cap of its city tier times the number of "
        f"attendees{': ' + caps if caps else ''}. With no verified city the lowest tier "
        "applies; with no attendee count, one person."
    )
    out.append(_consequence(_spend_outcome(rt, at, defn), "A line over the cap") or "")
    return out


def _gift(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    threshold = _num(defn.get("threshold_usd"))
    approver = defn.get("government_official_approver") or "Legal/Compliance"
    out = ["Applies to gift and client entertainment expense lines."]
    if threshold is not None:
        out.append(
            f"A gift over {_money(threshold)} needs a complete disclosure: recipient, "
            "business purpose and whether the recipient is a government official."
        )
        out.append(
            _consequence(_spend_outcome(rt, at, defn), "A gift without its disclosure") or ""
        )
    out.append(
        f"A gift or entertainment involving a government official needs {approver} "
        "pre-approval, which Atlas cannot verify: the approver is asked to confirm it and "
        "the report is never auto-approved."
    )
    return out


def _mileage(rt: str, at: str, defn: Mapping[str, Any]) -> list[str]:
    check = defn.get("check")
    out = ["Applies to mileage expense lines (personal vehicle)."]
    what = "A mileage line that breaks it"
    if check == "rate":
        rate = to_decimal(defn.get("rate_per_mile"))
        out.append(
            f"Atlas computes the amount as business miles x ${rate}/mile; the claimant "
            "cannot set it."
        )
        out.append(
            "Atlas cannot verify the route distance, so every mileage line goes to the "
            "manager to confirm it (never auto-approved)."
        )
        what = "A line whose amount differs (e.g. after a rate change)"
    elif check == "commute_offset":
        out.append(
            "Normal commute miles must be recorded and are deducted from the claim; Atlas "
            "cannot verify the commute, so the manager confirms the offset."
        )
        what = "A line without a commute offset"
    elif check == "air_cap":
        limit = _num(defn.get("max_one_way_miles"))
        if limit is not None:
            out.append(
                f"Trips over {limit:g} miles each way are capped at the lowest logical "
                "airfare unless air travel was impractical; Atlas has no airfare to compare."
            )
        what = "A longer trip"
    out.append(_consequence(_spend_outcome(rt, at, defn), what) or "")
    out.append("Mileage lines never need a receipt.")
    return out


_DESCRIBERS: dict[str, Callable[[str, str, Mapping[str, Any]], list[str]]] = {
    "amount_cap": _amount_cap,
    "per_diem": _per_diem,
    "cabin_class": _cabin,
    "preferred_supplier": _supplier,
    "category_allow": _category_allow,
    "receipt_required": _receipt,
    "per_head_cap": _per_head,
    "gift_disclosure": _gift,
    "mileage": _mileage,
}


def describe_rule(rule_type: Any, applies_to: Any, definition: Any) -> tuple[str, ...]:
    """Plain-English sentences (at most 6) describing what the engine does with this rule.

    Total: a definition it cannot describe yields one generic sentence, never an
    exception (GET /policy-rules renders every stored row, including legacy JSON).
    """
    try:
        rt, at = _enum_str(rule_type), _enum_str(applies_to)
        builder = _DESCRIBERS.get(rt)
        if builder is None or at not in APPLIES_TO or not isinstance(definition, Mapping):
            return (GENERIC_DESCRIPTION,)
        if process_kind(definition) is not None:
            builder = _process_describer
        elif rule_kind(definition) is not None:
            builder = _kind_describer
        sentences = [s for s in builder(rt, at, definition) if s and s.strip()]
        sentences.extend(process_sem.modifier_sentences(rt, definition))
        problems = validate_rule_definition(rt, at, definition)
        if problems:
            sentences.insert(
                0, f"Warning: the rule engine can't enforce this rule as written — {problems[0]}"
            )
        return tuple(sentences[:_MAX_SENTENCES]) or (GENERIC_DESCRIPTION,)
    except Exception as exc:  # noqa: BLE001 — totality: a summary is never fatal
        logger.warning("describe_rule: could not describe rule (%s)", exc)
        return (GENERIC_DESCRIPTION,)


def offers_justification(rule_type: Any, applies_to: Any, definition: Any) -> bool:
    """True when a written justification unblocks an expense line that breaks this rule.

    Derived from the engine: a failing probe line is run through ``verdicts_to_flags``
    with and without a justification.  False on any doubt.
    """
    try:
        rt, at = _enum_str(rule_type), _enum_str(applies_to)
        if not isinstance(definition, Mapping) or validate_rule_definition(rt, at, definition):
            return False
        if rt in SPEND_RULE_TYPES:  # product gap 2: probes carry the spend fields
            spend = _spend_outcome(rt, at, definition)
            return spend is not None and spend.blocking and spend.justification_unblocks
        for key in expense_reach(rt, at, definition):
            for item in _failing_items(rt, at, definition, key):
                verdict = _verdict(rt, at, definition, item)
                if verdict is None or verdict.in_policy:
                    continue
                outcome = expense_outcome(verdict, _REACH_CATEGORY[key])
                return outcome.blocking and outcome.justification_unblocks
        return False
    except Exception:  # noqa: BLE001
        return False


def _failing_items(rt: str, at: str, defn: Mapping[str, Any], key: str) -> list[EvalItem]:
    base = _reach_item(key)
    if rt == "amount_cap":
        lowest = PolicyEvalTool.resolve_tier(_valid_tiers(defn.get("tiers")), None, "cap")
        cap = _num((lowest or {}).get("cap"))
        return [dataclasses.replace(base, amount_usd=cap + 1)] if cap is not None else []
    if rt == "per_diem":
        return [dataclasses.replace(base, amount_usd=10_000.0)]
    if rt == "preferred_supplier":
        return [dataclasses.replace(base, supplier="Atlas Unlisted Probe Supplier")]
    if rt == "cabin_class":
        return [
            dataclasses.replace(base, cabin_class=c, duration_hours=h)
            for h in (1.0, 4.0, 7.0, 11.0)
            for c in reversed(CABIN_CLASSES)
        ]
    if rt == "category_allow":
        return [dataclasses.replace(base, vehicle_class="Atlas Probe Class")]
    return []


# ---------------------------------------------------------------------------
# Intent ↔ target backstop
# ---------------------------------------------------------------------------

# Unambiguous ground-transport nouns.
_GROUND_TERMS = re.compile(
    r"\b(ride[\s-]?shar\w*|ride[\s-]?hail\w*|taxi\w*|cabs?|uber|lyft|ground[\s-]transport\w*"
    r"|shuttle\w*|limo\w*|car[\s-]service\w*)\b",
    re.IGNORECASE,
)
# Words that are ground transport only when the intent is not about another target
# ("a flight with a transit over 8 hours", "hotel stays including parking").
_WEAK_GROUND_TERMS = re.compile(r"\b(transit|parking)\b", re.IGNORECASE)
_OTHER_TARGET_TERMS = re.compile(
    r"\b(flights?|fl[yi]\w*|air\w*|cabins?|hotels?|lodging|stays?|nights?|rooms?|meals?"
    r"|per[\s-]?diem|dinners?|lunch\w*|breakfast\w*)\b",
    re.IGNORECASE,
)
_JUSTIFY_TERMS = re.compile(
    r"\b(justif\w*|written\s+(?:business\s+)?(?:reason|explanation))", re.IGNORECASE
)
# "no / not / never / without ... justification" asks for NO justification step.
_NEGATION = re.compile(r"\b(no|not|never|without|cannot|can't|isn't|aren't|nor)\b", re.IGNORECASE)


def _names_ground_transport(text: str) -> bool:
    if _GROUND_TERMS.search(text):
        return True
    return bool(_WEAK_GROUND_TERMS.search(text)) and not _OTHER_TARGET_TERMS.search(text)


def _asks_for_justification(text: str) -> bool:
    """True when the intent asks for a justification step (not one negated nearby)."""
    for match in _JUSTIFY_TERMS.finditer(text):
        before = text[max(0, match.start() - 40) : match.start()]
        window = re.split(r"[.;:!?]", before)[-1].split()[-4:]
        if not _NEGATION.search(" ".join(window)):
            return True
    return False


def intent_scope_conflict(intent: str, candidate: Mapping[str, Any]) -> str | None:
    """A reason the candidate cannot mean what the intent says, or None.

    * the intent names taxi / rideshare / transit / parking-type spend but the rule
      does not reach ground-transport expense lines (e.g. a ``car`` rule, which only
      covers car-rental segments that record a vehicle class);
    * the intent asks for a written justification but a breach of the rule cannot be
      unblocked by one (derived from the engine via ``offers_justification``).
    """
    try:
        text = intent if isinstance(intent, str) else ""
        rt = _enum_str(candidate.get("rule_type"))
        at = _enum_str(candidate.get("applies_to"))
        defn = candidate.get("definition")
        if _names_ground_transport(text) and "ground_transport" not in expense_reach(rt, at, defn):
            return (
                "Taxi, rideshare, transit and parking are ground-transport expense lines; "
                "this rule never reaches them (car rules only cover car-rental segments that "
                "record a vehicle class). "
                "Express it as an amount_cap with applies_to 'all' and expense_categories "
                "['ground_transport']."
            )
        if _asks_for_justification(text) and not offers_justification(rt, at, defn):
            return (
                "Only spending caps, meal per diems and airline preferred-supplier rules with a "
                "manager (or no) exception approver let an employee unblock a line with a "
                "written justification; this rule has no justification step."
            )
        return None
    except Exception:  # noqa: BLE001 — fail closed: an unreadable candidate conflicts
        return "The drafted rule could not be checked against the description."


# ---------------------------------------------------------------------------
# Reason sanitizing
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏  ]")
_VERDICT_PREFIX = re.compile(r"^\W*(?:UN)?FAITHFUL\b[\s.:;,\-–—]*", re.IGNORECASE)


def sanitize_reason(text: Any, limit: int = 300) -> str:
    """Plain, single-line, bounded reason text (rendered as text, never HTML)."""
    if not isinstance(text, str):
        return ""
    cleaned = _CONTROL.sub(" ", text).replace("**", "").replace("`", "")
    cleaned = " ".join(cleaned.split())
    cleaned = _VERDICT_PREFIX.sub("", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


# ---------------------------------------------------------------------------
# Prompt glossary
# ---------------------------------------------------------------------------

RULE_VOCABULARY_GLOSSARY = """\
What each target (applies_to) reaches in Atlas:
- flight: airfare expense lines and flight options in trip planning.
- hotel: lodging expense lines (compared per night) and hotel options in trip planning.
- meals: meal expense lines.
- car: ONLY car-rental lines linked to a trip car segment that records a vehicle class —
  Atlas's planner and forms never record one, so car rules almost never fire, and on expense
  reports they are advisory (never blocking). Taxi, rideshare, transit and parking are NOT car.
- all: every expense line. An amount_cap on "all" MUST carry "expense_categories" naming the
  categories it covers: "ground_transport" and/or "other". Atlas files taxi, rideshare,
  Uber/Lyft, car rentals, train/rail tickets, transit and parking ALL as ground_transport, so
  a ground_transport cap covers every one of them UNLESS it also names "ground_subtypes"
  (only with expense_categories ["ground_transport"]): any of "rideshare_taxi", "car_rental",
  "rail", "transit", "parking". Use ground_subtypes ["rideshare_taxi"] for a taxi/rideshare
  limit, ["parking"] for a parking limit, and omit it for "all ground transport".
- rail: not supported as a target (train tickets are ground_transport expense lines) — never
  use it.

What happens when a line breaks a rule (the consequence):
- amount_cap, per_diem, preferred_supplier (flight) with exception_approver "manager" or null:
  the line blocks submission until the employee writes a business justification; the report
  then goes to their manager as an exception. This is the ONLY way to express "needs a
  written justification" or "needs manager approval".
- the same rules with exception_approver "VP": blocks; a justification does not unblock it.
- cabin_class: blocks (exception via override_approver). category_allow: advisory only.
- receipt_required: a missing receipt blocks submission; there is no justification step.
- preferred_supplier on hotel with a "condition" key: advice only, never flagged."""
