"""Worst-case check for UNCONFIRMED ground-transport lines (known limits, ground).

Background (product gaps, policy review round 5): a ground line whose kind (rideshare /
taxi / rental / rail / transit / parking) no TRUSTED evidence names — no receipt OCR
word, no card merchant, a segment alone never counts — is *unconfirmed*: every sub-type
is a candidate, so every ``kind`` rule (GRND-01 rail class, GRND-03 airport parking,
GRND-05 tips) reaches it and returns "could not verify" with an approver check.  That
closed a policy bypass but sent every typed-only $14 "Uber" to a human.

This module keeps the bypass closed and drops the harmless escalations.  For an
unconfirmed line it asks: *whatever kind this line really is, could any candidate rule
be breached?*

Amount — the line must be within every limit that reaches it:
  * the policy's own evidence floor: RECEIPT-REQ says no receipt is needed (amount,
    converted to USD by the engine's FX helper, below ``threshold_usd``; a category
    that always needs a receipt, an unknown amount or an unsupported currency fail
    closed).  Below that floor the policy accepts the claim with no document at all,
    so it cannot demand a document proving the ride's kind; and whatever the line
    really is, no more than that amount is at stake;
  * every money cap / rule that reaches the line (authored sub-type caps "rideshare cap
    $20" reach unconfirmed lines): no verdict on the line may fail, need an approver or
    lack an FX conversion.
Shape — per kind rule, nothing the line's own words or segment say may put it at risk:
  * GRND-01 rail class: rail is not named by any word or the segment, and no class is
    named (a premium class named anywhere is evaluated as today);
  * GRND-03 airport parking: the line is not airport-linked (no airport named, no
    flight segment linked — otherwise it may be airport parking and the day count is at
    issue, whether or not a parking word was typed), no premium-lot word ("valet",
    "short-term" ...), no stated day count over the limit;
  * GRND-05 tips: never cleared here — its could-not-verify only ever comes from a tip
    the words or the receipt mention, which is a fact the approver must check.

Total — clearing is bounded per evaluation (one report): a line is cleared only while
the cleared lines' total, including it, stays below the receipt threshold
(``ClearingBudget``); later lines escalate as before.

A rule that passes all of these is CLEARED: its verdict becomes plainly in policy; the first
cleared verdict carries a single low-key advisory ("ground transport, category
unverified; within every ground-transport limit") when nothing else on the line still
needs an approver.  A rule that fails either keeps its could-not-verify / approver
check, and its reason names why it could not be cleared.  Both carry the entry-time hint
"add a receipt or card match that names the kind of ride to confirm it".

Invariants (tests: ``ai_agents/tests/test_kl_ground_worst_case.py``):
  * VERIFIED lines and lines whose receipt / card contradicts the typed words are never
    touched (only ``ground_transport.unconfirmed`` lines are);
  * typed words never narrow: ``ground_named`` holds every family ANY word or the segment
    names, and a named family keeps its rule at risk;
  * a receipt vendor the claimant overwrote with no OCR snapshot (``ground_named`` None)
    is never cleared;
  * without an active RECEIPT-REQ rule in the evaluated set nothing is cleared (fail
    closed) — so a single-rule evaluation is exactly the pre-change verdict.

Pure and deterministic; no DB, no LLM.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from ai_agents.tools.entry_rules import (
    ADVISORY,
    APPROVER_CHECK,
    COULD_NOT_VERIFY,
    KIND_PARKING_DAYS,
    KIND_RAIL_CLASS,
    rule_kind,
)
from ai_agents.tools.fx_policy import FX_STATUS_KEY, FX_UNSUPPORTED
from ai_agents.tools.ground_transport import PARKING, RAIL, premium_parking, unconfirmed

if TYPE_CHECKING:
    from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
    from database.models.policy_rule import PolicyRule

# Observed markers.
CLEARED = "ground_worst_case_cleared"  # the rule could not be breached whatever the kind
AT_RISK = "ground_at_risk"  # why an unconfirmed line's rule could not be cleared
CONFIRM_HINT = "confirm_ground_type"  # show "add a receipt or card match" at entry time

HINT_TEXT = "Add a receipt or card match that names the kind of ride to confirm the type."

# The could-not-verify outcomes this check may clear, per kind rule.
_RELAXABLE: dict[str, frozenset[str]] = {
    KIND_RAIL_CLASS: frozenset({"travel_class"}),
    # "airport_parking": not shown to be airport parking; "parking_days": an airport is
    # named / a flight segment is linked, days unknown (``_shape_risk`` decides).
    KIND_PARKING_DAYS: frozenset({"airport_parking", "parking_days"}),
}
_CENT = Decimal("0.01")


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(_CENT)
    except (InvalidOperation, ValueError):
        return None


def _usd(d: Decimal) -> str:
    return f"${d:,.2f}"


def _is_unconfirmed(item: Any) -> bool:
    return getattr(item, "expense_category", None) == "ground_transport" and unconfirmed(
        getattr(item, "ground_candidates", ()) or (),
        getattr(item, "ground_verified", False) is True,
    )


def _candidate(rule: Any, verdict: Any) -> str | None:
    """The kind of a could-not-verify verdict this check may clear, else None."""
    kind = rule_kind(getattr(rule, "definition", None)) if rule is not None else None
    if kind is None or kind not in _RELAXABLE or not verdict.in_policy:
        return None
    if verdict.observed.get(COULD_NOT_VERIFY) not in _RELAXABLE[kind]:
        return None
    if not verdict.observed.get(APPROVER_CHECK):
        return None
    return kind


def _escalates(verdict: Any) -> bool:
    """A verdict that sends the line to a human (or could not compare money)."""
    obs = verdict.observed or {}
    return (
        not verdict.in_policy
        or bool(verdict.requires_approver)
        or bool(obs.get(COULD_NOT_VERIFY) and obs.get(APPROVER_CHECK))
        or obs.get(FX_STATUS_KEY) == FX_UNSUPPORTED
    )


@dataclass(frozen=True)
class _Floor:
    ok: bool
    why: str
    amount_usd: Decimal | None = None
    threshold_usd: Decimal | None = None


def _receipt_floor(item: Any, others: Sequence[Any]) -> _Floor:
    """Below RECEIPT-REQ's threshold in every receipt rule on the line (fail closed)."""
    amount = _money(getattr(item, "amount_usd", None))
    if amount is None or amount <= 0:
        return _Floor(False, "its amount is unknown")
    floors = [v for v in others if v.rule_type == "receipt_required"]
    if not floors:
        return _Floor(False, "no receipt threshold applies to it")
    worst: _Floor | None = None
    for v in floors:
        obs = v.observed or {}
        # An unsupported currency arrives as receipt_required True (fx_policy, fail closed).
        usd = _money(obs.get("amount_usd"))
        threshold = _money(obs.get("threshold_usd"))
        if obs.get("receipt_required") is not False or usd is None or threshold is None:
            if usd is not None and threshold is not None and usd >= threshold:
                why = f"at {_usd(usd)} it is not below the {_usd(threshold)} receipt threshold"
            else:
                why = "the receipt rule requires a receipt for it"
            return _Floor(False, why)
        if worst is None or (worst.threshold_usd is not None and threshold < worst.threshold_usd):
            worst = _Floor(True, "", usd, threshold)
    return worst or _Floor(False, "no receipt threshold applies to it")


def _shape_risk(kind: str, item: Any, verdict: Any) -> str | None:
    """Why the line's own words / segment keep *kind*'s rule at risk, else None."""
    named = getattr(item, "ground_named", None)
    if named is None:
        return "the receipt's own vendor reading was overwritten"
    if kind == KIND_RAIL_CLASS:
        if RAIL in named:
            return "the entry's words or its trip segment say it is rail"
        if getattr(item, "travel_class", None):
            return "a ticket class is named"
        return None
    if kind == KIND_PARKING_DAYS:
        if getattr(item, "airport_parking", False) is True:
            # Airport-linked (an airport named, or a flight segment linked —
            # ``entry_context.ground_fields``): the line may be airport parking and
            # the day count is at issue, whether or not a parking word was typed
            # (integration security fix: typed words never narrow scrutiny, and a
            # typed "Uber to the airport" proves nothing about what was paid for).
            if PARKING in named:
                return "the entry says it is airport parking"
            return "it is linked to an airport or a flight, so it may be airport parking"
        lot = premium_parking(getattr(item, "item_text", None))
        if lot:
            return f"the entry names a {lot} lot"
        claimed = getattr(item, "claimed_days", None)
        max_days = verdict.observed.get("max_days")
        if isinstance(claimed, int) and (not isinstance(max_days, int) or claimed > max_days):
            return f"{claimed} days are stated"
        return None
    return "this rule is never cleared without evidence"


def _clauses(pairs: Iterable[tuple[Any, Any]]) -> str:
    refs = sorted({str(v.source_clause_ref) for _, v in pairs if v.source_clause_ref})
    return ", ".join(refs)


@dataclass
class ClearingBudget:
    """The running total of unconfirmed ground lines cleared in ONE evaluation
    (``PolicyEvalTool.evaluate_many`` — one report's lines, in report order).

    Integration security fix: each small typed-only ride alone is below the
    receipt floor, but many of them must not each pass.  A line is cleared only
    while the cleared total INCLUDING it stays below its RECEIPT-REQ threshold,
    so at most the receipt-free amount is at stake per report (and therefore per
    trip day as well: a day's lines are a subset of the report's).  The rest
    escalate exactly as an uncleared line does.
    """

    cleared_usd: Decimal = Decimal("0")


def relax(
    pairs: Sequence[tuple[PolicyRule | None, RuleVerdict]],
    item: EvalItem,
    budget: ClearingBudget | None = None,
) -> tuple[RuleVerdict, ...]:
    """The verdicts of *pairs* (rule, verdict) after the worst-case check on *item*.

    Anything but an unconfirmed ground line comes back unchanged (same objects).
    *budget* bounds the total cleared across the lines of one evaluation (a fresh
    budget when None: the line is judged alone).
    """
    if budget is None:
        budget = ClearingBudget()
    verdicts = tuple(v for _, v in pairs)
    if not _is_unconfirmed(item):
        return verdicts
    cands = [(i, _candidate(r, v)) for i, (r, v) in enumerate(pairs)]
    targets = {i: k for i, k in cands if k is not None}
    if not targets:
        return verdicts
    others = [v for i, v in enumerate(verdicts) if i not in targets]
    floor = _receipt_floor(item, others)
    over = [
        v
        for v in others
        if v.rule_type != "receipt_required"
        and (not v.in_policy or (v.observed or {}).get(FX_STATUS_KEY) == FX_UNSUPPORTED)
    ]
    amount_risk: str | None = None
    if not floor.ok:
        amount_risk = floor.why
    elif over:
        amount_risk = f"the line is over another limit ({_clauses((None, v) for v in over)})"
    elif (
        floor.amount_usd is not None
        and floor.threshold_usd is not None
        and budget.cleared_usd + floor.amount_usd >= floor.threshold_usd
    ):
        amount_risk = (
            f"together with the {_usd(budget.cleared_usd)} of unconfirmed ground lines "
            f"already cleared on this report it reaches the {_usd(floor.threshold_usd)} "
            "receipt threshold"
        )

    out = list(verdicts)
    cleared: list[int] = []
    for i, kind in targets.items():
        v = verdicts[i]
        why = amount_risk or _shape_risk(kind, item, v)
        if why is None:
            cleared.append(i)
            continue
        out[i] = dataclasses.replace(
            v,
            reason=f"{v.reason} It was not cleared as a low-value ride because {why}. "
            f"{HINT_TEXT}",
            observed={**v.observed, AT_RISK: why, CONFIRM_HINT: True},
        )
    if not cleared:
        return tuple(out)

    still_escalates = any(_escalates(out[i]) for i in range(len(out)) if i not in cleared)
    refs = _clauses((None, verdicts[i]) for i in cleared)
    for n, i in enumerate(sorted(cleared)):
        v = verdicts[i]
        observed = {
            k: val for k, val in v.observed.items() if k not in (COULD_NOT_VERIFY, APPROVER_CHECK)
        }
        observed.update(
            {
                CLEARED: True,
                CONFIRM_HINT: True,
                "ground_subtype_unconfirmed": True,
                "amount_checked_usd": float(floor.amount_usd or 0),
                "receipt_threshold_usd": float(floor.threshold_usd or 0),
            }
        )
        note = n == 0 and not still_escalates
        if note:
            observed[ADVISORY] = True
        out[i] = dataclasses.replace(
            v,
            in_policy=True,
            requires_approver=None,
            reason=(
                "Ground transport, category unverified: no receipt, card or trip segment "
                f"names what kind of ride this is, but at {_usd(floor.amount_usd or _CENT)} "
                f"it is below the {_usd(floor.threshold_usd or _CENT)} receipt threshold and "
                f"within every ground-transport limit whatever kind it is ({refs}), so it "
                f"does not need an approver check. {HINT_TEXT}"
            ),
            observed=observed,
        )
    if not line_escalates(out):
        # Only a line that no human will look at uses up the budget.
        budget.cleared_usd += floor.amount_usd or Decimal("0")
    return tuple(out)


# ---------------------------------------------------------------------------
# Review-load metric
# ---------------------------------------------------------------------------


def line_escalates(verdicts: Iterable[Any]) -> bool:
    """True when any policy verdict sends the line to a human approver."""
    return any(_escalates(v) for v in verdicts)


@dataclass(frozen=True)
class ReviewLoad:
    lines: int
    escalated_before: int  # round-5 behaviour: no worst-case check
    escalated_after: int  # with the worst-case check


def review_load(
    items: Sequence[EvalItem],
    rules: Sequence[PolicyRule],
    *,
    tool: PolicyEvalTool | None = None,
) -> ReviewLoad:
    """How many of *items* a policy evaluation escalates, before / after this check.

    "Before" evaluates each applicable rule on its own (``_evaluate_all`` of a one-rule
    list is exactly the per-rule verdict: no receipt floor -> nothing is cleared);
    "after" is the engine's real, all-rules evaluation.  Deterministic, no DB.
    """
    if tool is None:
        from ai_agents.tools.policy_eval import PolicyEvalTool  # noqa: PLC0415 — cycle

        tool = PolicyEvalTool()
    before = after = 0
    for item in items:
        applicable = tool._filter_applicable(list(rules), item)
        raw = [v for r in applicable for v in tool._evaluate_all([r], item)]
        before += line_escalates(raw)
        after += line_escalates(tool._evaluate_all(applicable, item))
    return ReviewLoad(lines=len(items), escalated_before=before, escalated_after=after)
