"""Currency normalisation for policy evaluation (product gap 8).

The ONE place where an entry / receipt amount meets a money cap is
``PolicyEvalTool._evaluate_rule``; it calls :func:`evaluate_with_fx` around the
per-rule evaluators.  For a money rule (``amount_cap``, ``per_diem``,
``receipt_required``) and an item that carries a ``currency`` different from the
rule's own currency, the item's amounts are converted with the static reference
table (``config.constants.fx_rates``) BEFORE the cap is compared, and the verdict
reason quotes the original amount, the converted amount and the rate
("EUR 220.00 ≈ USD 238.70 at 1.085").

Fail closed, never silent:
  * an UNSUPPORTED currency is never treated as USD.  ``amount_cap`` / ``per_diem``
    return an advisory "could not verify, currency not supported" verdict
    (``observed[FX_STATUS_KEY] == FX_UNSUPPORTED``) — presubmit turns it into an
    escalating ``currency_unverified`` flag, so the report is never auto-approved;
    ``receipt_required`` assumes the receipt IS required (the threshold cannot be
    checked).
  * an item with no currency, or already in the rule's currency, is evaluated
    exactly as before (byte-for-byte: no conversion, no reason change).

Integration (policy tracks): EVERY money figure the item carries is converted with
the same rate — the line amount, the receipt's own total, its tip and line items, the
line's own claim and the spend facts' amount — so the entry-level rules (MEAL-04,
GRND-05, LOD-02, MEAL-02) and the spend rules (MEAL-05 / ENT-02 per-head caps, the
ENT-04 gift threshold) never compare a foreign amount with a USD figure.
``per_head_cap`` with an unsupported currency is the same could-not-verify advisory;
``gift_disclosure`` with an unsupported currency is judged as an UNKNOWN amount (the
disclosure is required — fail closed) and carries the fx status, so the line also
gets the escalating ``currency_unverified`` flag.

Target currency = the rule's own ``definition.currency`` (the org's policy
currency; "USD" when absent).  ``receipt_required`` thresholds are
``threshold_usd`` — USD by definition.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from config.constants.fx_rates import (
    RATES_AS_OF,
    RATES_SOURCE,
    Conversion,
    UnknownCurrency,
    conversion,
    normalize_currency,
)

if TYPE_CHECKING:
    from ai_agents.tools.policy_eval import EvalItem, RuleVerdict
    from database.models.policy_rule import PolicyRule

MONEY_RULE_TYPES: frozenset[str] = frozenset(
    {"amount_cap", "per_diem", "receipt_required", "per_head_cap", "gift_disclosure"}
)
FX_STATUS_KEY = "fx_status"
FX_UNSUPPORTED = "currency_not_supported"
# Set on the verdict that only says "could not compare" (no rule outcome behind it).
FX_NOT_COMPARED = "fx_not_compared"
CURRENCY_UNVERIFIED_FLAG = "currency_unverified"

# EvalItem amount fields that are money in the item's currency.
_AMOUNT_FIELDS: tuple[str, ...] = (
    "amount_usd",
    "stay_amount_usd",
    "room_only_nightly_usd",
    # Entry-level rules (product gap 2): the receipt's own figures and the line's claim.
    "receipt_total_usd",
    "tip_usd",
    "line_claim_usd",
)


def item_currency(item: Any) -> str | None:
    """The currency the item's amounts are in: its own tag, else the spend facts'."""
    raw = getattr(item, "currency", None)
    if raw is None:
        spend = getattr(item, "spend", None)
        raw = getattr(spend, "currency", None) if spend is not None else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def _rule_type(rule: Any) -> str:
    rt = rule.rule_type
    return str(getattr(rt, "value", rt))


def rule_currency(rule: Any) -> str:
    """The currency a money rule's caps are written in (upper-cased)."""
    if _rule_type(rule) == "receipt_required":
        return "USD"  # ``threshold_usd``
    defn = rule.definition if isinstance(rule.definition, dict) else {}
    code = defn.get("currency") or "USD"
    return str(code).strip().upper()


def with_item_currency(item: EvalItem, currency: object) -> EvalItem:
    """*item* tagged with the currency its amounts are in (None/blank -> unchanged)."""
    if not isinstance(currency, str) or not currency.strip():
        return item
    return dataclasses.replace(item, currency=currency.strip().upper())


def _verdict(rule: Any, *, reason: str, observed: dict[str, Any]) -> RuleVerdict:
    from ai_agents.tools.policy_eval import RuleVerdict  # noqa: PLC0415 — import cycle

    return RuleVerdict(
        rule_id=str(rule.id),
        rule_type=_rule_type(rule),
        applies_to=str(getattr(rule.applies_to, "value", rule.applies_to)),
        source_clause_ref=rule.source_clause_ref,
        in_policy=True,
        reason=reason,
        requires_approver=None,
        observed=observed,
    )


def _unsupported(rule: Any, item: EvalItem, currency: str, target: str) -> RuleVerdict:
    amount = item.amount_usd
    shown = f"{currency} {amount:.2f}" if amount is not None else currency
    observed: dict[str, Any] = {
        FX_STATUS_KEY: FX_UNSUPPORTED,
        FX_NOT_COMPARED: True,
        "currency": currency,
        "policy_currency": target,
        "original_amount": amount,
    }
    if _rule_type(rule) == "receipt_required":
        # Fail closed: the USD threshold cannot be checked, so a receipt is required.
        observed.update(
            {
                "receipt_required": True,
                "expense_category": item.expense_category or "",
            }
        )
        reason = (
            f"Receipt required: {shown} could not be converted to {target} "
            f"(currency not supported), so the receipt threshold could not be checked."
        )
    else:
        reason = (
            f"Could not verify {shown} against the {target} limit: currency not "
            f"supported. Your approver will review it."
        )
    return _verdict(rule, reason=reason, observed=observed)


def _convert_item(item: EvalItem, source: str, target: str) -> tuple[EvalItem, Conversion | None]:
    updates: dict[str, Any] = {"currency": target}
    headline: Conversion | None = None
    for name in _AMOUNT_FIELDS:
        value = getattr(item, name, None)
        if value is None:
            continue
        conv = conversion(Decimal(str(value)), source, target)
        updates[name] = float(conv.converted_amount)
        if name == "amount_usd":
            headline = conv
    line_items = getattr(item, "line_items", None)
    if line_items:
        updates["line_items"] = tuple(
            (desc, float(conversion(Decimal(str(amt)), source, target).converted_amount))
            if isinstance(amt, int | float) and not isinstance(amt, bool)
            else (desc, amt)
            for desc, amt in line_items
        )
    spend = getattr(item, "spend", None)
    if spend is not None:
        amount = getattr(spend, "amount", None)
        spend_updates: dict[str, Any] = {"currency": target}
        if amount is not None:
            conv = conversion(amount, source, target)
            spend_updates["amount"] = conv.converted_amount
            headline = headline or conv
        updates["spend"] = dataclasses.replace(spend, **spend_updates)
    return dataclasses.replace(item, **updates), headline


def convert_for_rule(rule: PolicyRule, item: EvalItem) -> EvalItem:
    """*item* with its money in *rule*'s currency when that conversion is possible;
    unchanged otherwise (no currency, same currency, or an unsupported code — the rule's
    own verdict then carries the could-not-verify outcome)."""
    raw = item_currency(item)
    if raw is None:
        return item
    target = rule_currency(rule)
    if raw.strip().upper() == target:
        return item
    try:
        source = normalize_currency(raw)
        normalize_currency(target)
    except UnknownCurrency:
        return item
    return _convert_item(item, source, target)[0]


def _unsupported_gift(
    rule: PolicyRule,
    item: EvalItem,
    evaluate: Callable[[PolicyRule, EvalItem], RuleVerdict],
    currency: str,
    target: str,
) -> RuleVerdict:
    """ENT-04 with an unsupported currency: the amount is UNKNOWN in the threshold's
    currency, so the disclosure is required (fail closed) — and the fx status rides on
    the verdict so the line also gets the escalating ``currency_unverified`` flag."""
    spend = getattr(item, "spend", None)
    unknown = (
        dataclasses.replace(item, spend=dataclasses.replace(spend, amount=None))
        if spend is not None
        else item
    )
    verdict = evaluate(rule, unknown)
    return dataclasses.replace(
        verdict,
        reason=(
            f"{verdict.reason} [{currency} could not be converted to {target}: currency "
            "not supported, so the amount was treated as unknown.]"
        ),
        observed={
            **verdict.observed,
            FX_STATUS_KEY: FX_UNSUPPORTED,
            "currency": currency,
            "policy_currency": target,
            "original_amount": item.amount_usd,
        },
    )


def evaluate_with_fx(
    rule: PolicyRule,
    item: EvalItem,
    evaluate: Callable[[PolicyRule, EvalItem], RuleVerdict],
) -> RuleVerdict:
    """Evaluate *rule* on *item* with its amounts in the rule's currency."""
    raw = item_currency(item)
    if raw is None or _rule_type(rule) not in MONEY_RULE_TYPES:
        return evaluate(rule, item)
    target = rule_currency(rule)
    if str(raw).strip().upper() == target:
        return evaluate(rule, item)  # already in the cap's currency: unchanged
    try:
        source = normalize_currency(raw)
        normalize_currency(target)
    except UnknownCurrency:
        if _rule_type(rule) == "gift_disclosure":
            return _unsupported_gift(rule, item, evaluate, str(raw).strip().upper(), target)
        return _unsupported(rule, item, str(raw).strip().upper(), target)

    converted, headline = _convert_item(item, source, target)
    verdict = evaluate(rule, converted)
    if headline is None or not verdict.observed:
        return verdict  # nothing compared (skip verdict): nothing to explain
    note = f"{headline.describe()} ({RATES_SOURCE}, as of {RATES_AS_OF})"
    return dataclasses.replace(
        verdict,
        reason=f"{verdict.reason} [{note}]",
        observed={**verdict.observed, "fx": headline.as_dict()},
    )
