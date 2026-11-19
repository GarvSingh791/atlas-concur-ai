"""Option retrieval, policy evaluation, ranking, and shortlist construction (P4-T9).

Fetch candidates from InventoryTool, evaluate each against PolicyEvalTool, then
rank by four competing objectives: policy compliance, price, traveler preference,
sustainability (FR-T4/T5).  Out-of-policy options are labeled and shown but never
silently top-ranked (they are score-penalised via the policy_score component).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.process_checks import load_process_rules, notice, option_notices
from ai_agents.agents.traveller_grade import owner_grade
from ai_agents.context import AgentContext
from ai_agents.tools.inventory import InventoryIntent, InventoryOption, InventoryTool
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, PolicyVerdict
from ai_agents.tools.process_rules import POLICY_VIOLATION, Finding
from ai_agents.tools.profile import ProfileResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Annotated option — inventory data + policy verdict + rank score + explanation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedOption:
    """Inventory option decorated with policy evaluation, ranking score, and explanation."""

    # From InventoryOption
    option_id: str
    segment_type: str  # "flight" | "hotel"
    supplier: str
    amount_usd: float
    sustainability_score: int  # 1 (low-impact) – 5 (high-impact); lower is better

    # Flight-specific (None for hotels)
    cabin_class: str | None
    duration_hours: float | None
    is_direct: bool | None

    # Hotel-specific (None for flights)
    city: str | None
    nightly_rate_usd: float | None
    room_type: str | None

    # Policy verdict
    in_policy: bool  # display label; should equal rule_in_policy in normal flow
    policy_reason: str  # human-readable explanation from the verdict
    clause_refs: tuple[str, ...]  # source_clause_ref values from violated/applicable rules
    requires_approver: str | None

    # Ground-truth from PolicyVerdict (critic checks label == rule_in_policy)
    rule_in_policy: bool

    # Ranking
    rank_score: float  # higher = better; computed by _rank_score()

    # Explanation (FR-T6)
    explanation: str  # templated or LLM-enriched plain-language summary

    # Hotel stay (Issue 6): nights of the REQUESTED stay and rate x nights.
    # None when the stay length is unknown (no / same-day return date).
    nights: int | None = None
    stay_total_usd: float | None = None

    # Product gap 2 stage 2 (flights): the cheapest fare of the same cabin in THIS
    # search (AIR-06 baseline, recorded on the selection) and the process-rule notices
    # shown at selection time — PTA-02 lead time, AIR-06 lowest fare, AIR-04 SVP
    # business class.  Each notice: clause_ref, flag_type, message, requires_approver,
    # blocking.
    lowest_fare_usd: float | None = None
    policy_notices: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# EvalItem conversion
# ---------------------------------------------------------------------------


def stay_nights(departure_date: Any, return_date: Any) -> int | None:
    """Nights of the REQUESTED stay (intent dates), or None when unknown / zero.

    Never the inventory option's own check-in/out: a static fixture hotel carries
    the fixture's window, which is not the stay being planned.
    """
    try:
        start = date.fromisoformat(str(departure_date))
        end = date.fromisoformat(str(return_date))
    except (TypeError, ValueError):
        return None
    nights = (end - start).days
    return nights if nights >= 1 else None


def _option_to_eval_item(
    opt: InventoryOption, *, nights: int | None = None, traveller_grade: str | None = None
) -> EvalItem:
    """Convert an InventoryOption to a PolicyEvalTool EvalItem.

    A hotel's ``amount_usd`` is its quoted NIGHTLY rate, compared with the nightly
    cap; *nights* (the requested stay) only feeds the reason wording.
    *traveller_grade*: the requesting traveller's grade of record (AIR-04).
    """
    if opt.segment_type == "flight":
        return EvalItem(
            item_type="flight",
            amount_usd=opt.amount_usd,
            supplier=opt.supplier,
            cabin_class=opt.cabin_class,
            duration_hours=opt.duration_hours,
            ref_id=opt.option_id,
            traveller_grade=traveller_grade,
        )
    # hotel
    rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
    return EvalItem(
        item_type="hotel",
        amount_usd=rate,
        supplier=opt.supplier,
        city=opt.city,
        nights=nights,
        ref_id=opt.option_id,
        amount_is_nightly_rate=True,
        nights_source="requested",
        stay_amount_usd=round(rate * nights, 2) if nights else None,
        # LOD-02 (product gap 2): an option offered by the planner IS booked through
        # the travel tool, so <= 15% over the nightly cap is auto-approvable.
        booked_through_tool=True,
    )


# ---------------------------------------------------------------------------
# Ranking score (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _rank_score(
    in_policy: bool,
    amount_usd: float,
    sustainability_score: int,
    supplier: str,
    preferred_suppliers: list[str],
) -> float:
    """Return a composite ranking score (higher is better).

    Competing objectives (FR-T4):
    1. Policy compliance — hard advantage (+100) for in-policy options.
    2. Price            — inverse-price bonus (lower price = higher bonus).
    3. Sustainability   — lower sustainability_score = less environmental impact.
    4. Preference       — bonus when the supplier matches user preferences.

    Out-of-policy options can never exceed the baseline for any in-policy option
    (the +100 policy gap is larger than the maximum combined price + pref bonus
    for any realistic option in the seeded inventory).
    """
    policy_score = 100.0 if in_policy else 0.0
    price_score = 1000.0 / (amount_usd + 1.0)
    sustain_score = (5 - sustainability_score) * 5.0  # 0–20 points
    pref_score = 10.0 if supplier in preferred_suppliers else 0.0
    return policy_score + price_score + sustain_score + pref_score


# ---------------------------------------------------------------------------
# Templated explanation (FR-T6 deterministic path)
# ---------------------------------------------------------------------------


def _templated_explanation(
    opt: InventoryOption, in_policy: bool, policy_reason: str, nights: int | None = None
) -> str:
    """Build a plain-language explanation without LLM."""
    if opt.segment_type == "flight":
        parts: list[str] = [opt.supplier]
        if opt.cabin_class:
            parts.append(f"({opt.cabin_class})")
        if opt.is_direct:
            parts.append("direct")
        if opt.duration_hours:
            parts.append(f"{opt.duration_hours:.1f}h")
        parts.append(f"at ${opt.amount_usd:.0f}")
        policy_note = "Policy-compliant." if in_policy else f"Out of policy — {policy_reason}"
        return (
            f"{' '.join(parts)}. {policy_note} "
            f"Sustainability: {opt.sustainability_score}/5 (lower is better)."
        )
    # hotel
    parts = [opt.supplier]
    if opt.room_type:
        parts.append(f"({opt.room_type})")
    if opt.city:
        parts.append(f"in {opt.city}")
    rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
    parts.append(f"at ${rate:.0f}/night")
    if nights:
        parts.append(f"(${rate * nights:.0f} total for {nights} night{'s' if nights != 1 else ''})")
    policy_note = "Policy-compliant." if in_policy else f"Out of policy — {policy_reason}"
    return (
        f"{' '.join(parts)}. {policy_note} "
        f"Sustainability: {opt.sustainability_score}/5 (lower is better)."
    )


# ---------------------------------------------------------------------------
# Annotation helper
# ---------------------------------------------------------------------------


def _annotate(
    opt: InventoryOption,
    verdict: PolicyVerdict,
    preferred_suppliers: list[str],
    *,
    nights: int | None = None,
    findings: tuple[Finding, ...] = (),
    lowest_fare: Decimal | None = None,
) -> RankedOption:
    """Combine an InventoryOption with its PolicyVerdict into a RankedOption.

    *findings* (product gap 2 stage 2) are the option's process-rule findings: each is a
    notice; an AIR-06 breach (a ``policy_violation`` finding) also makes the option out
    of policy, with its clause cited.
    """
    clause_refs = tuple(
        v.source_clause_ref for v in verdict.verdicts if v.source_clause_ref is not None
    )
    # Primary reason: first failing rule, or first rule overall, or empty
    failing = [v for v in verdict.verdicts if not v.in_policy]
    reason = (
        failing[0].reason if failing else (verdict.verdicts[0].reason if verdict.verdicts else "")
    )
    approver = next((v.requires_approver for v in verdict.verdicts if v.requires_approver), None)
    in_policy = verdict.in_policy
    breaches = [f for f in findings if f.flag_type == POLICY_VIOLATION]
    if breaches:
        if in_policy:
            reason = breaches[0].reason
        in_policy = False
        approver = approver or breaches[0].requires_approver
    clause_refs += tuple(f.clause_ref for f in findings if f.clause_ref not in clause_refs)

    score = _rank_score(
        in_policy,
        opt.amount_usd,
        opt.sustainability_score,
        opt.supplier,
        preferred_suppliers,
    )
    hotel_nights = nights if opt.segment_type == "hotel" else None
    explanation = _templated_explanation(opt, in_policy, reason, hotel_nights)
    rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd

    return RankedOption(
        option_id=opt.option_id,
        segment_type=opt.segment_type,
        supplier=opt.supplier,
        amount_usd=opt.amount_usd,
        sustainability_score=opt.sustainability_score,
        cabin_class=opt.cabin_class,
        duration_hours=opt.duration_hours,
        is_direct=opt.is_direct,
        city=opt.city,
        nightly_rate_usd=opt.nightly_rate_usd,
        room_type=opt.room_type,
        in_policy=in_policy,
        policy_reason=reason,
        clause_refs=clause_refs,
        requires_approver=approver,
        rule_in_policy=in_policy,  # ground-truth (matches label in normal flow)
        rank_score=score,
        explanation=explanation,
        nights=hotel_nights,
        stay_total_usd=round(rate * hotel_nights, 2) if hotel_nights else None,
        lowest_fare_usd=float(lowest_fare) if lowest_fare is not None else None,
        policy_notices=tuple(notice(f) for f in findings),
    )


def _process_findings(
    options: list[InventoryOption],
    *,
    intent: Any,
    context: AgentContext,
    session: Session,
    traveller_grade: str | None = None,
) -> tuple[dict[str, list[Finding]], dict[str, Decimal]]:
    """PTA-02 / AIR-06 / AIR-04 findings and the AIR-06 baseline per FLIGHT option."""
    flights = [
        (o.option_id, o.cabin_class, o.amount_usd, o.duration_hours)
        for o in options
        if o.segment_type == "flight"
    ]
    if not flights:
        return {}, {}
    rules = load_process_rules(session, context.organization_id)
    return option_notices(
        rules,
        flights=flights,
        departure=intent.departure_date,
        origin=intent.origin,
        destination=intent.destination,
        traveller_grade=traveller_grade,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def retrieve_and_rank(
    *,
    intent: Any,  # TripIntent — imported here as Any to avoid circular imports
    context: AgentContext,
    session: Session,
    profile: ProfileResult | None = None,
) -> tuple[list[RankedOption], dict[str, Any]]:
    """Retrieve inventory candidates, evaluate policy, rank, and return shortlist.

    Returns (ranked_options, tools_used_trace).
    ranked_options is empty when no inventory matches the intent (not an error).
    All recommended options have an option_id traceable to the seeded inventory
    — the agent never fabricates options (FR-T3).
    """
    inv_tool = InventoryTool()
    eval_tool = PolicyEvalTool()
    tools_used: dict[str, Any] = {}
    all_options: list[InventoryOption] = []

    # ── Flight retrieval ─────────────────────────────────────────────────────
    if intent.include_flights and intent.origin and intent.destination and intent.departure_date:
        flight_inv = inv_tool.search(
            InventoryIntent(
                segment_type="flight",
                origin=intent.origin,
                destination=intent.destination,
                departure_date=intent.departure_date,
            ),
            context=context,
        )
        all_options.extend(flight_inv.options)
        tools_used["InventoryTool.flights"] = {
            "origin": intent.origin,
            "destination": intent.destination,
            "departure_date": intent.departure_date,
            "results": len(flight_inv.options),
        }

    # ── Hotel retrieval ──────────────────────────────────────────────────────
    if intent.include_hotels and intent.destination and intent.departure_date:
        check_out = intent.return_date or intent.departure_date
        hotel_inv = inv_tool.search(
            InventoryIntent(
                segment_type="hotel",
                city=intent.destination,
                check_in_date=intent.departure_date,
                check_out_date=check_out,
            ),
            context=context,
        )
        all_options.extend(hotel_inv.options)
        tools_used["InventoryTool.hotels"] = {
            "city": intent.destination,
            "check_in_date": intent.departure_date,
            "results": len(hotel_inv.options),
        }

    if not all_options:
        return [], tools_used

    # ── Policy evaluation ────────────────────────────────────────────────────
    nights = stay_nights(intent.departure_date, intent.return_date)
    # AIR-04: the planner plans for the requesting user (the trip's traveler_id is
    # context.user_id) — their grade of record, read from the database.
    grade = owner_grade(session, organization_id=context.organization_id, user_id=context.user_id)
    eval_items = [
        _option_to_eval_item(
            opt,
            nights=nights if opt.segment_type == "hotel" else None,
            traveller_grade=grade,
        )
        for opt in all_options
    ]
    verdicts: tuple[PolicyVerdict, ...] = eval_tool.evaluate_many(
        eval_items, context=context, session=session
    )
    tools_used["PolicyEvalTool"] = {
        "items_evaluated": len(eval_items),
        "in_policy_count": sum(1 for v in verdicts if v.in_policy),
    }

    # ── Preferred suppliers from profile ─────────────────────────────────────
    preferred_suppliers: list[str] = []
    if profile and isinstance(profile.preferences, dict):
        raw = profile.preferences.get("preferred_suppliers", [])
        if isinstance(raw, list):
            preferred_suppliers = [str(s) for s in raw]

    # ── Process rules (product gap 2 stage 2): PTA-02 / AIR-06 / AIR-04 ───────
    findings, lowest = _process_findings(
        all_options, intent=intent, context=context, session=session, traveller_grade=grade
    )

    # ── Annotate + rank ──────────────────────────────────────────────────────
    annotated = [
        _annotate(
            opt,
            verdict,
            preferred_suppliers,
            nights=nights,
            findings=tuple(findings.get(opt.option_id, ())),
            lowest_fare=lowest.get(opt.option_id),
        )
        for opt, verdict in zip(all_options, verdicts, strict=False)
    ]
    ranked = sorted(annotated, key=lambda o: o.rank_score, reverse=True)

    return ranked, tools_used
