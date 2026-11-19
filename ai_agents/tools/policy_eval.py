"""PolicyEvalTool — canonical deterministic policy rule evaluator (P4-T4).

This is the SINGLE source of truth for rule evaluation.  The backend
/policy/evaluate endpoint (P4-T13) will delegate INTO this tool, not the reverse.

Design:
  - Reads ACTIVE policy_rules via an injected Session + JOIN to policies for
    org-scoping (policy_rules has no organization_id column — scoped via policy_id FK).
  - Evaluates a structured EvalItem against the fetched rules deterministically.
  - Returns typed PolicyVerdict + per-rule RuleVerdicts carrying source_clause_ref
    for downstream Citation construction by the caller.
  - LLM-FREE: no provider calls.
  - READ-ONLY: no INSERT/UPDATE/DELETE.

Six rule_types handled: cabin_class, amount_cap, per_diem, preferred_supplier,
category_allow, receipt_required.

SUSTAIN clause produces no verdict — structural guarantee: no policy_rules row
has source_clause_ref="SUSTAIN" (SUSTAIN has no structured rule per policy_content.py).

Downstream seam: verdicts feed Policy Agent (P4-T8); backend delegates (P4-T13).
source_clause_ref → Citation(kind=clause_ref, ref=source_clause_ref, source="policy_rules").
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.context import AgentContext
from ai_agents.tools import entry_rules, ground_worst_case, process_rules
from ai_agents.tools.base import ToolError
from ai_agents.tools.fx_policy import convert_for_rule, evaluate_with_fx
from ai_agents.tools.spend_policy import (
    GIFT_DISCLOSURE,
    MILEAGE,
    PER_HEAD_CAP,
    RECEIPT_EXEMPT_CATEGORIES,
    RECEIPT_EXEMPT_CLAUSE,
    SPEND_RULE_TYPES,
    SpendFacts,
    SpendOutcome,
    evaluate_gift_disclosure,
    evaluate_mileage,
    evaluate_per_head_cap,
    spend_rule_applies,
    team_meal_cap_minimum,
    team_meal_covered,
)
from config.constants import grades as grade_ladder
from config.constants.city_names import city_keys, norm_city
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalItem:
    """Structured representation of an item to evaluate against policy.

    item_type maps to PolicyAppliesTo values ("flight", "hotel", "car", "meals",
    "rail", "receipt").  "receipt" matches applies_to="all" only (RECEIPT-REQ rule).
    ref_id is echoed into the verdict for traceability (option id / segment id).
    """

    item_type: Literal["flight", "hotel", "rail", "car", "meals", "receipt"]
    # Amount semantics depend on item_type:
    #   flight/rail/car/receipt → total amount; hotel → per-night rate
    amount_usd: float | None = None
    supplier: str | None = None  # airline / hotel brand
    city: str | None = None  # for hotel amount_cap tier resolution
    region: Literal["domestic", "international"] | None = None  # for per_diem
    cabin_class: str | None = None  # flight
    duration_hours: float | None = None  # flight (hours, gate-to-gate)
    vehicle_class: str | None = None  # car
    nights: int | None = None  # hotel (informational)
    expense_category: str | None = None  # receipt: "airfare"/"lodging"/"meals"/…
    headcount: int | None = None  # meals: number of attendees
    ref_id: str | None = None  # echo-through (option_id / segment_id)
    # Verification context (C7R) — only used to word honest flag reasons; never
    # used to CLEAR a rule.  When ``supplier`` / ``city`` could not be verified
    # from trusted evidence they are None and these carry what was claimed.
    claimed_supplier: str | None = None  # claimant-stated supplier (unverified)
    supplier_evidence: str | None = None  # what trusted records name instead (conflict)
    claimed_city: str | None = None  # claimant-stated / unverified hotel city
    # Hotel only (wording): True when ``amount_usd`` is the per-night rate printed on
    # the receipt (it may be set without ``nights``); ``stay_amount_usd`` is then the
    # full stay total, so a reason never calls the nightly rate "the full amount".
    amount_is_nightly_rate: bool = False
    stay_amount_usd: float | None = None
    # Hotel only (UAT2 Issue I): ``amount_usd`` is the ROOM + TAX per-night figure —
    # the canonical HOTEL-CAP ``basis: room_plus_tax`` (LOD-01), also applied when a
    # rule names no / an unknown basis (fail closed).  This is the ROOM-ONLY figure
    # (trusted room subtotal / nights), compared only by a legacy ``room_only`` cap
    # and only when it does not exceed ``amount_usd``.
    room_only_nightly_usd: float | None = None
    # Issue 6 — stay length / location context.  VERIFIED values change what is
    # compared (``nights`` for hotels; ``days`` for meals, ``amount_usd`` is then the
    # per-day average and ``stay_amount_usd`` the total).  CLAIMED values (typed by
    # the claimant: "3 nights" in the description, an unapproved trip's dates /
    # city, a city named in the description or vendor) NEVER clear a rule: a line
    # within a cap only at the claimed figures stays a violation, marked
    # ``within_cap_if_verified`` so the approver confirms it (see
    # presubmit.verdicts_to_flags).
    nights_source: str | None = None  # "itinerary" | "receipt" | "requested" (wording)
    claimed_nights: int | None = None  # hotel: unverified stay length (>= 1)
    days: int | None = None  # meals: verified days covered by stay_amount_usd (>= 1)
    claimed_days: int | None = None  # meals: unverified days (>= 1)
    claimed_city_text: str | None = None  # free text that may NAME a city (desc / vendor)
    # Product gap 8: ISO currency of the amounts above (None = the policy's own
    # currency).  Money rules convert to the rule currency first (fx_policy).
    currency: str | None = None
    # Product gap 2 / 8 — entry-level rules (see ai_agents/tools/entry_rules.py).
    # LOD-02: the stay was booked through the Atlas travel tool (a planner option or a
    # segment of the traveler's approved trip).  Only then may a rate <= 15% over the
    # nightly cap be auto-approved; never set from claimant text.
    booked_through_tool: bool = False
    # MEAL-02: how many of the verified ``days`` are departure / return (travel) days.
    travel_days: int | None = None
    # Free text of the entry (vendor / description / card merchant) — exclusion keywords
    # (MEAL-04 alcohol) and ground-transport classification.  Wording / detection only.
    item_text: str | None = None
    # OCR line items READ from the receipt: ((description, amount), ...).
    line_items: tuple[tuple[str, float], ...] | None = None
    # Ground transport: classifier sub-type (ground_transport.GROUND_SUBTYPES or the broad
    # "ground_transport"), rail class ("first" / "business" / "standard"), tip and
    # verified parking days.  ``duration_hours`` doubles as the rail journey length.
    ground_subtype: str | None = None
    travel_class: str | None = None
    tip_usd: float | None = None
    parking_days: int | None = None
    # GRND-03 reaches AIRPORT parking only (airport words / code, or a flight segment).
    airport_parking: bool = False
    # Covered days of the linked approved trip — reason CONTEXT only, never parking days.
    trip_days: int | None = None
    # Review round 3 — partial claims.  The receipt's own total READ by OCR (never the
    # claimed amount): a claimant may claim only part of a receipt (the food without the
    # wine, the fare without the tip), so MEAL-04 / GRND-05 compare the claim with it.
    receipt_total_usd: float | None = None
    # The receipt also backs OTHER lines (itemized siblings / a shared receipt): the
    # rest of the receipt may be claimed elsewhere, so nothing on it is assumed to be
    # "left out of the claim" (fail closed).
    receipt_shared: bool = False
    # Meals with verified ``days``: THIS line's claimed amount (``amount_usd`` is then the
    # trip's per-day average), so its exclusions are measured against its own claim.
    line_claim_usd: float | None = None
    # Ground transport trust (security review): families named by any word / segment,
    # and whether TRUSTED evidence (OCR vendor / line items, card merchant, trusted
    # segment type) alone names exactly one family that the claimant's words agree with.
    ground_candidates: tuple[str, ...] = ()
    ground_verified: bool = False
    ground_trusted_texts: tuple[str, ...] = ()
    # Families named by words / segment before widening (``ground_worst_case``); None =
    # unknown or evidence destroyed -> an unconfirmed line is never cleared as low-value.
    ground_named: tuple[str, ...] | None = None
    # Product gap 2 (policy2): attendees / gift disclosure / mileage trip and the
    # entry's exact amount, for the per_head_cap / gift_disclosure / mileage rules.
    spend: SpendFacts | None = None
    # AIR-04 (known limits): the trip / expense OWNER's grade of record, read from
    # ``users.grade`` by the server-side caller (``ai_agents/agents/traveller_grade.py``).
    # Never taken from a request: POST /policy/evaluate's what-if fields drop it.
    traveller_grade: str | None = None


# ---------------------------------------------------------------------------
# Supplier-name normalization (preferred_supplier matching)
# ---------------------------------------------------------------------------

# Generic corporate / industry tokens dropped before comparing supplier names, so
# OCR'd receipt vendors ("UNITED", "United Airlines Inc.", "Lufthansa German
# Airlines") line up with the policy's canonical brand names ("United Airlines",
# "Delta Air Lines", "Marriott International"), including legal-entity forms
# ("Deutsche Lufthansa AG").  Brand-bearing words such as
# "air" on its own ("Air Canada") are deliberately NOT dropped.
_SUPPLIER_STOP_TOKENS: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "llc",
        "ltd",
        "limited",
        "plc",
        "ag",
        "gmbh",
        "sa",
        "deutsche",
        "airlines",
        "airline",
        "airways",
        "international",
        "intl",
        "hotels",
        "hotel",
        "resorts",
        "resort",
        "the",
        "and",
    }
)
# Multi-token generic phrases removed before single-token filtering.
_SUPPLIER_STOP_PHRASES: tuple[tuple[str, ...], ...] = (("air", "lines"),)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def format_cabin_classes(classes: list[str]) -> str:
    """Human-readable cabin list for policy reasons: ['economy', 'premium_economy']
    -> "economy or premium economy" (never a Python list repr)."""
    names = [str(c).replace("_", " ") for c in classes]
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def normalize_supplier_name(name: str | None) -> tuple[str, ...]:
    """Normalize a supplier/brand name to a tuple of comparison tokens.

    Lowercases, treats every non-alphanumeric run as a separator (punctuation,
    "&", "."), removes generic phrases ("air lines") and generic tokens
    (inc/corp/llc/airlines/international/hotels/...).  If stripping would leave
    nothing (e.g. the name is literally "Airlines"), the un-stripped tokens are
    kept so the name never collapses to an empty key.
    """
    if not name:
        return ()
    raw = tuple(t for t in _NON_ALNUM.split(name.lower()) if t)
    tokens = list(raw)
    for phrase in _SUPPLIER_STOP_PHRASES:
        n = len(phrase)
        i = 0
        while i <= len(tokens) - n:
            if tuple(tokens[i : i + n]) == phrase:
                del tokens[i : i + n]
            else:
                i += 1
    stripped = tuple(t for t in tokens if t not in _SUPPLIER_STOP_TOKENS)
    return stripped or raw


def supplier_matches_preferred(
    supplier: str | None, preferred: list[str], *, allow_prefix: bool = False
) -> bool:
    """True when *supplier* names one of the *preferred* brands.

    Matching is on normalized names (case, punctuation and generic/legal tokens
    such as "Airlines", "Inc." or "AG" removed), token-based and never substring:
    "UNITED", "United Airlines Inc." and "Deutsche Lufthansa AG" all normalize to
    the preferred brand key, while "UnitedHealth" does not. An exact string match
    always counts (backward compatible with the planner/inventory names).

    By default the normalized names must be EQUAL, so a different carrier that
    merely shares a first word ("United Nigeria Airlines") never satisfies a
    blocking rule. ``allow_prefix=True`` additionally accepts a supplier whose
    tokens START with the brand key ("Marriott New York" -> "Marriott"); use it
    only for advisory, non-blocking rules.
    """
    if not supplier:
        return False
    if supplier in preferred:
        return True
    sup_key = normalize_supplier_name(supplier)
    if not sup_key:
        return False
    for pref in preferred:
        pref_key = normalize_supplier_name(str(pref))
        if not pref_key:
            continue
        if sup_key == pref_key:
            return True
        if allow_prefix and sup_key[: len(pref_key)] == pref_key:
            return True
    return False


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleVerdict:
    """Result of applying one policy rule to one EvalItem.

    source_clause_ref → Citation(kind=clause_ref, ref=source_clause_ref, source="policy_rules").
    """

    rule_id: str  # PolicyRule.id (UUID as str)
    rule_type: str  # PolicyRuleType value
    applies_to: str  # PolicyAppliesTo value
    source_clause_ref: str | None  # citation pointer; None for programmatic rules
    in_policy: bool
    reason: str  # deterministic human-readable explanation
    requires_approver: str | None  # "VP" / "manager" / None
    observed: dict  # what was observed (cabin_class, amount, etc.)


@dataclass(frozen=True)
class PolicyVerdict:
    """Aggregated policy evaluation result for one EvalItem.

    in_policy = all(v.in_policy for v in verdicts).
    An item with zero applicable rules → in_policy=True, empty verdicts.
    SUSTAIN clause never appears in verdicts (no rule row references it).

    source → Citation.source when building Citation objects in the calling agent.
    """

    item: EvalItem
    in_policy: bool
    verdicts: tuple[RuleVerdict, ...]
    evaluated_rule_count: int
    source: str = "policy_rules"


# ---------------------------------------------------------------------------
# Claimed stay length / city (Issue 6) — helpers shared by amount_cap / per_diem
# ---------------------------------------------------------------------------


def _count(value: Any) -> int | None:
    """A usable day / night count: an int >= 1 (bools, 0, negatives -> None)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _usd(value: float) -> str:
    """'$300' for whole dollars, '$300.50' otherwise (the corpus' own style)."""
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


@dataclass(frozen=True)
class _Claims:
    """What the claimant's UNVERIFIED stay length / city would change for a failing line.

    ``unverified`` names the claims needed for the line to be within a cap
    (("nights",), ("city",), ("nights", "city"), or the "days" variants); empty when
    the line exceeds the cap even at the claimed figures.
    """

    claimed_count: int | None
    unit_amount: float
    claimed: tuple[dict, str] | None
    unverified: tuple[str, ...]
    observed: dict


def _assess_claims(
    *,
    amount: float,
    limit: float,
    claimed_count: int | None,
    count_key: str,
    claimed: tuple[dict, str] | None,
    value_key: str,
) -> _Claims | None:
    if claimed_count is None and claimed is None:
        return None
    unit = amount / claimed_count if claimed_count else amount
    claimed_limit = float(claimed[0][value_key]) if claimed is not None else None
    unverified: tuple[str, ...]
    if claimed_count and unit <= limit:
        unverified = (count_key,)
    elif claimed_limit is not None and amount <= claimed_limit:
        unverified = ("city",)
    elif claimed_count and claimed_limit is not None and unit <= claimed_limit:
        unverified = (count_key, "city")
    else:
        unverified = ()
    observed: dict = {}
    if claimed_count:
        observed[f"claimed_{count_key}"] = claimed_count
        unit_key = "claimed_nightly_usd" if count_key == "nights" else "claimed_daily_usd"
        observed[unit_key] = round(unit, 2)
    if claimed is not None:
        ctier, cname = claimed
        observed["claimed_city"] = cname
        observed["claimed_tier"] = ctier.get("tier")
        if ctier.get("label"):
            observed["claimed_tier_label"] = ctier["label"]
        observed["claimed_cap" if value_key == "cap" else "claimed_rate"] = claimed_limit
    observed["within_cap_if_verified"] = bool(unverified)
    if unverified:
        observed["unverified"] = list(unverified)
    return _Claims(
        claimed_count=claimed_count,
        unit_amount=round(unit, 2),
        claimed=claimed,
        unverified=unverified,
        observed=observed,
    )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class PolicyEvalTool:
    """Canonical deterministic policy rule evaluator (LLM-free, read-only).

    No constructor dependencies — pure deterministic logic + injected Session.
    """

    def evaluate(
        self,
        item: EvalItem,
        *,
        context: AgentContext,
        session: Session,
    ) -> PolicyVerdict:
        """Evaluate one item against active org-scoped policy rules."""
        rules = self._load_active_rules(context, session)
        applicable = self._filter_applicable(rules, item)
        verdicts = self._evaluate_all(applicable, item)
        overall = all(v.in_policy for v in verdicts)
        return PolicyVerdict(
            item=item,
            in_policy=overall,
            verdicts=verdicts,
            evaluated_rule_count=len(applicable),
        )

    def evaluate_many(
        self,
        items: list[EvalItem],
        *,
        context: AgentContext,
        session: Session,
    ) -> tuple[PolicyVerdict, ...]:
        """Evaluate multiple items against the same rule set (single DB fetch)."""
        rules = self._load_active_rules(context, session)
        results = []
        # Known limits (ground, integration fix): one clearing budget for all items.
        budget = ground_worst_case.ClearingBudget()
        for item in items:
            applicable = self._filter_applicable(rules, item)
            verdicts = self._evaluate_all(applicable, item, budget)
            overall = all(v.in_policy for v in verdicts)
            results.append(
                PolicyVerdict(
                    item=item,
                    in_policy=overall,
                    verdicts=verdicts,
                    evaluated_rule_count=len(applicable),
                )
            )
        return tuple(results)

    def evaluate_candidate(
        self,
        rule_type: str,
        applies_to: str,
        definition: dict,
        probe_item: EvalItem,
    ) -> RuleVerdict:
        """Evaluate a candidate rule definition without persisting it.

        Used by the rule-authoring assist (Cap 3) to validate that a
        draft definition is evaluable by PolicyEvalTool before persisting.

        Builds a transient in-memory PolicyRule and dispatches to _evaluate_rule.
        Returns the RuleVerdict. If the definition is malformed, returns the
        skip-verdict (observed={}) — caller must check for observed={} to detect
        a non-evaluable definition.
        """
        import uuid as _uuid  # noqa: PLC0415

        transient = PolicyRule(
            id=_uuid.uuid4(),
            policy_id=_uuid.UUID(int=0),  # sentinel; never persisted
            rule_type=PolicyRuleType(rule_type),
            applies_to=PolicyAppliesTo(applies_to),
            definition=definition,
            source_clause_ref=None,
            status=PolicyRuleStatus.draft,
        )
        return self._evaluate_rule(transient, probe_item)

    # ------------------------------------------------------------------
    # DB access (org-scoped via JOIN — policy_rules has no org column)
    # ------------------------------------------------------------------

    def _load_active_rules(self, context: AgentContext, session: Session) -> list[PolicyRule]:
        stmt = (
            sa.select(PolicyRule)
            .join(Policy, PolicyRule.policy_id == Policy.id)
            .where(
                Policy.organization_id == context.organization_id,
                PolicyRule.status == PolicyRuleStatus.active,
            )
        )
        return list(session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # Applicability filter
    # ------------------------------------------------------------------

    def _filter_applicable(self, rules: list[PolicyRule], item: EvalItem) -> list[PolicyRule]:
        # Product gap 2 (policy2): a TEAM meal is governed by MEAL-05's per-head cap
        # instead of the claimant's own per diem — but only when THIS rule set has an
        # active per-head cap that reaches the line.  Without one the per diem stays
        # (fail closed: a typed attendee count never switches every meal rule off).
        cover = team_meal_cap_minimum((r.rule_type, r.applies_to, r.definition) for r in rules)
        team_meal = team_meal_covered(item.item_type, item.spend, cover)
        return [
            rule
            for rule in rules
            if self.rule_applies(rule.rule_type, rule.applies_to, rule.definition, item)
            and not (team_meal and rule.rule_type == PolicyRuleType.per_diem)
        ]

    @staticmethod
    def rule_applies(rule_type: Any, applies_to: Any, definition: Any, item: EvalItem) -> bool:
        """Whether a rule (by type / target / definition) is evaluated for *item* at all.

        * ``applies_to="all"`` reaches every item, EXCEPT an ``amount_cap`` that carries
          an ``expense_categories`` filter (Issue 5): it reaches only items whose
          ``expense_category`` is listed.  Items without a category (trip options,
          flight / hotel / meal / car-rental expense lines) are never reached, so
          such a cap produces no verdict for them (no flag, no ``not_evaluated``).
          A malformed filter (not a list of strings) reaches nothing — the
          activation validator refuses such a rule.
        * an ``applies_to="all"`` rule with a ``ground_subtypes`` filter (product gap 8)
          reaches only ground-transport lines of those sub-types (an authored cap also
          reaches lines Atlas could not classify — see
          ``entry_rules.ground_subtype_reach``).
        * any other target reaches only the items of that type.
        * a PROCESS rule (EXP-01, PTA-02, PTA-05, AIR-06, CARD-03 — product gap 2 stage 2)
          reaches no item: the report / trip / planner wiring runs it
          (``ai_agents.agents.process_checks``).
        """
        if process_rules.is_process_rule(definition):
            return False
        at = getattr(applies_to, "value", applies_to)
        # Product gap 2 (policy2): the spend rules reach only the lines they govern.
        # (A TEAM meal leaves the per diem only in ``_filter_applicable``, which sees
        # the whole rule set and so knows whether a per-head cap covers it.)
        spend_rt = getattr(rule_type, "value", rule_type)
        if spend_rt in SPEND_RULE_TYPES:
            return spend_rule_applies(
                str(spend_rt),
                str(at),
                definition,
                item_type=item.item_type,
                expense_category=item.expense_category,
                facts=item.spend,
            )
        if at == PolicyAppliesTo.all.value:
            rt = getattr(rule_type, "value", rule_type)
            if rt == PolicyRuleType.amount_cap.value and isinstance(definition, dict):
                if "expense_categories" in definition:
                    cats = definition.get("expense_categories")
                    if not isinstance(cats, list):
                        return False
                    if item.expense_category is None or item.expense_category not in [
                        c for c in cats if isinstance(c, str)
                    ]:
                        return False
            if isinstance(definition, dict):
                # Product gap 8: a ground sub-type filter (rideshare / rail / parking ...).
                reach = entry_rules.ground_subtype_reach(definition, item)
                if reach is not None:
                    return reach
            return True
        return bool(at == item.item_type)

    # ------------------------------------------------------------------
    # Per-rule evaluation dispatch
    # ------------------------------------------------------------------

    def _evaluate_all(
        self,
        rules: list[PolicyRule],
        item: EvalItem,
        budget: ground_worst_case.ClearingBudget | None = None,
    ) -> tuple[RuleVerdict, ...]:
        """Every verdict for *item*: one per rule, plus an explicit exclusion verdict for a
        rule whose ``exclusions`` the item contains (MEAL-04 alcohol, product gap 2)."""
        out: list[tuple[PolicyRule | None, RuleVerdict]] = []
        for rule in rules:
            out.append((rule, self._evaluate_rule(rule, item)))
            extra = self._exclusion_verdict(rule, item)
            if extra is not None:
                out.append((None, extra))
        # Known limits (ground): an unconfirmed ground line within every limit whatever
        # its kind is not sent to the approver by the kind rules (ground_worst_case).
        # *budget* bounds the total cleared across one evaluate_many call (a report).
        return ground_worst_case.relax(out, item, budget)

    def _exclusion_verdict(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict | None:
        defn = rule.definition
        if not isinstance(defn, dict) or "exclusions" not in defn:
            return None
        try:
            # Integration: the receipt's figures in the rule's currency (same rate as the
            # per-diem compare), so the excluded / reimbursable amounts it states match.
            outcome = entry_rules.exclusion_outcome(defn, convert_for_rule(rule, item))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning("PolicyEvalTool: rule %s exclusion check error: %s", rule.id, exc)
            return None
        return self._outcome_verdict(rule, outcome) if outcome is not None else None

    @staticmethod
    def _outcome_verdict(rule: PolicyRule, outcome: entry_rules.Outcome) -> RuleVerdict:
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=outcome.clause_ref or rule.source_clause_ref,
            in_policy=outcome.in_policy,
            reason=outcome.reason,
            requires_approver=outcome.requires_approver if not outcome.in_policy else None,
            observed=outcome.observed,
        )

    def _evaluate_rule(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        # Product gap 8: amounts are converted to the rule's currency first.
        return evaluate_with_fx(rule, item, self._evaluate_rule_in_policy_currency)

    def _evaluate_rule_in_policy_currency(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        rt = rule.rule_type
        try:
            if entry_rules.rule_kind(rule.definition) is not None:
                # Product gap 2: GRND-01 / GRND-03 / GRND-05 entry-level rules.
                return self._outcome_verdict(rule, entry_rules.evaluate_kind(rule.definition, item))
            if rt == PolicyRuleType.cabin_class:
                return self._eval_cabin_class(rule, item)
            if rt == PolicyRuleType.amount_cap:
                return self._eval_amount_cap(rule, item)
            if rt == PolicyRuleType.per_diem:
                return self._eval_per_diem(rule, item)
            if rt == PolicyRuleType.preferred_supplier:
                return self._eval_preferred_supplier(rule, item)
            if rt == PolicyRuleType.category_allow:
                return self._eval_category_allow(rule, item)
            if rt == PolicyRuleType.receipt_required:
                return self._eval_receipt_required(rule, item)
            if rt.value in SPEND_RULE_TYPES:
                return self._eval_spend_rule(rule, item)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("PolicyEvalTool: rule %s evaluation error: %s", rule.id, exc)
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rt.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=True,
                reason=f"Rule evaluation skipped due to unexpected definition shape: {exc}",
                requires_approver=None,
                observed={},
            )
        raise ToolError(
            f"Unknown rule_type {rt!r} on rule {rule.id}.",
            code="POLICY_EVAL_TOOL",
        )

    # ------------------------------------------------------------------
    # cabin_class (AIR-CABIN)
    # ------------------------------------------------------------------

    def _eval_cabin_class(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        duration = item.duration_hours
        cabin = item.cabin_class

        if duration is None or cabin is None:
            return self._skip_verdict(rule, "cabin_class or duration_hours not provided")

        allowed: list[str] = []
        for tier in defn.get("tiers", []):
            min_h = tier.get("min_hours", 0.0)
            max_h = tier.get("max_hours")
            if max_h is None:
                if duration >= min_h:
                    allowed = tier["allowed_classes"]
                    break
            elif min_h <= duration < max_h:
                allowed = tier["allowed_classes"]
                break

        in_policy = cabin in allowed
        observed = {"duration_hours": duration, "cabin_class": cabin, "allowed": allowed}
        role_note = self._role_exception_note(defn, item) if not in_policy else None
        if role_note is not None and role_note[0]:
            in_policy, reason, approver = True, role_note[1], None
            observed.update(role_note[2])
        elif in_policy:
            reason = f"Cabin class {cabin!r} is permitted for {duration}h flight."
            approver = None
        else:
            approver = defn.get("override_approver")
            reason = (
                f"Cabin class {cabin!r} is NOT permitted for {duration}h flight. "
                f"Allowed: {format_cabin_classes(allowed)}. "
                f"Requires {approver or 'approver'} override."
            )
            if role_note is not None:
                reason = f"{reason} {role_note[1]}"
                observed.update(role_note[2])
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=in_policy,
            reason=reason,
            requires_approver=approver if not in_policy else None,
            observed=observed,
        )

    @staticmethod
    def _role_exception_note(defn: Any, item: EvalItem) -> tuple[bool, str, dict] | None:
        """AIR-04 on an AIR-CABIN miss: (entitled, sentence, observed) or None.

        None when AIR-04 does not reach the flight (another cabin, too short, no AIR-04
        keys) or its keys are malformed (fail closed: the cabin violation stands).
        Entitled only for a grade of record at or above the rule's role.
        """
        if not isinstance(defn, dict):
            return None
        try:
            status = process_rules.role_exception_status(
                defn,
                cabin=item.cabin_class,
                duration_hours=item.duration_hours,
                traveller_grade=item.traveller_grade,
            )
        except ValueError:
            return None
        if not status.applies:
            return None
        title = grade_ladder.title_of(item.traveller_grade)
        observed = {
            "role_exception": status.clause,
            "traveller_grade": item.traveller_grade if title is not None else None,
        }
        if status.entitled:
            return (
                True,
                f"Cabin class {item.cabin_class!r} is permitted for {item.duration_hours}h "
                f"flight by {status.clause}: the traveler's grade of record ({title}) is "
                f"{status.role} or above.",
                observed,
            )
        if status.entitled is False:
            return (
                False,
                f"{status.clause} (business class for {status.role}s and above) does not "
                f"apply: the traveler's grade of record is {title}.",
                observed,
            )
        why = (
            f"the rule's grade threshold '{status.role}' is not on the grade ladder"
            if title is not None
            else "no grade is recorded for the traveler"
        )
        return (
            False,
            f"{status.clause} (business class for {status.role}s and above) could not be "
            f"applied: {why}.",
            observed,
        )

    # ------------------------------------------------------------------
    # City-tier resolution (HOTEL-CAP tiers, MEALS-PD tiered rates)
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_city(name: str) -> str:
        """Casefold, drop dots ('D.C.' -> 'dc'), collapse punctuation / whitespace."""
        return norm_city(name)

    @staticmethod
    def _city_keys(city: str) -> set[str]:
        """Comparable keys of a location (``config.constants.city_names.city_keys``,
        the ONE helper every city consumer shares): its city part ('New York, NY' ->
        'new york'), its first two parts ('Washington, DC, USA' -> 'washington dc'),
        the whole, and each without a trailing US ZIP / state code ('New York NY',
        'NEW YORK NY 10017' -> 'new york')."""
        return city_keys(city)

    @classmethod
    def _tier_names(cls, tier: dict) -> list[tuple[str, str]]:
        """(normalized name, display city) for every city and alias a tier lists."""
        names: list[tuple[str, str]] = []
        for city in tier.get("cities") or []:
            if isinstance(city, str) and city.strip():
                names.append((cls._norm_city(city), city))
        aliases = tier.get("aliases")
        if isinstance(aliases, dict):
            for canonical, alts in aliases.items():
                for alt in alts if isinstance(alts, list) else []:
                    if isinstance(alt, str) and alt.strip():
                        names.append((cls._norm_city(alt), str(canonical)))
        return [(n, d) for n, d in names if n]

    @classmethod
    def _match_city_tier(
        cls, tiers: list[dict], city: str, value_key: str
    ) -> tuple[dict, str] | None:
        """The tier that LISTS *city* (exact city / alias match, never a substring:
        'New York City' only matches where it is listed as an alias).  A city listed
        in several tiers resolves to the lowest value (fail closed)."""
        keys = cls._city_keys(city)
        matches: list[tuple[dict, str]] = []
        for tier in tiers:
            for norm, display in cls._tier_names(tier):
                if norm in keys:
                    matches.append((tier, display))
                    break
        if not matches:
            return None
        return min(matches, key=lambda m: float(m[0][value_key]))

    @classmethod
    def _resolve_tier(cls, tiers: Any, city: str | None, value_key: str) -> dict | None:
        """Order-independent tier for *city* (``value_key``: "cap" / "amount").

        * a listed city -> its tier;
        * no (verified) city -> the LOWEST tier (conservative);
        * an unlisted city -> the ``default`` tier, else the legacy ``domestic``
          tier, else the lowest.
        The order of *tiers* never matters, so legacy definitions (tier1 / domestic /
        intl_*) resolve exactly as before and canonical ones (tier3 default first)
        resolve the same whichever order they are stored in.
        """
        valid = [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []
        if not valid:
            return None
        if city:
            match = cls._match_city_tier(valid, city, value_key)
            if match is not None:
                return match[0]
            for tier in valid:
                if tier.get("default") is True:
                    return tier
            for tier in valid:
                if tier.get("tier") == "domestic":
                    return tier
        return min(valid, key=lambda t: float(t[value_key]))

    @classmethod
    def _claimed_city_tier(
        cls, tiers: Any, item: EvalItem, value_key: str
    ) -> tuple[dict, str] | None:
        """The tier of the city the claimant NAMED (unverified; wording / escalation only).

        A structured claimed location (unverified receipt location, unapproved trip
        segment) wins; otherwise a listed city / alias named as a whole word in the
        free text (description / vendor).  Several cities in the text -> the lowest.
        """
        valid = [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []
        if not valid:
            return None
        if item.claimed_city:
            return cls._match_city_tier(valid, item.claimed_city, value_key)
        if not item.claimed_city_text:
            return None
        padded = f" {cls._norm_city(item.claimed_city_text)} "
        matches: list[tuple[dict, str]] = []
        for tier in valid:
            for norm, display in cls._tier_names(tier):
                if f" {norm} " in padded:
                    matches.append((tier, display))
                    break
        if not matches:
            return None
        return min(matches, key=lambda m: float(m[0][value_key]))

    @staticmethod
    def _tier_label(tier: dict) -> str:
        return str(tier.get("label") or tier.get("tier") or "default")

    # Public read-only views of the tier resolution, so rule descriptions
    # (policy_rule_semantics.describe_rule) state exactly what the evaluator picks.
    @classmethod
    def resolve_tier(cls, tiers: Any, city: str | None, value_key: str) -> dict | None:
        """The tier ``_eval_amount_cap`` / ``_eval_per_diem_tiered`` would apply."""
        return cls._resolve_tier(tiers, city, value_key)

    @classmethod
    def tier_label(cls, tier: dict) -> str:
        return cls._tier_label(tier)

    # ------------------------------------------------------------------
    # amount_cap (HOTEL-CAP)
    # ------------------------------------------------------------------

    @staticmethod
    def _room_only_amount(defn: Any, item: EvalItem) -> float | None:
        """The room-only nightly figure a legacy ``basis: room_only`` hotel cap compares.

        None (compare ``amount_usd``, the room + tax figure) unless the rule says
        ``room_only`` AND the item carries a room-only figure within
        (0, amount_usd] — a larger or non-positive one is inconsistent evidence.
        """
        if item.item_type != "hotel" or defn.get("basis") != "room_only":
            return None
        room_only = item.room_only_nightly_usd
        amount = item.amount_usd
        if room_only is None or amount is None or not 0 < room_only <= amount:
            return None
        return room_only

    def _eval_amount_cap(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        room_only = self._room_only_amount(defn, item)
        amount = room_only if room_only is not None else item.amount_usd

        if amount is None:
            return self._skip_verdict(rule, "amount_usd not provided")

        tiers = defn.get("tiers", [])
        tier = self._resolve_tier(tiers, item.city, "cap")
        if tier is None:
            return self._skip_verdict(rule, "no matching tier in amount_cap definition")

        cap = float(tier["cap"])
        currency = defn.get("currency", "USD")
        in_policy = amount <= cap
        observed = {
            "amount_usd": amount,
            "cap": cap,
            "currency": currency,
            "tier": tier.get("tier"),
            "city": item.city,
        }
        if tier.get("label"):
            observed["tier_label"] = tier["label"]
        if room_only is not None:
            observed["basis"] = "room_only"
            observed["room_plus_tax_usd"] = item.amount_usd
        if in_policy:
            reason = (
                f"Amount {amount} {currency} is within cap {cap} {currency}"
                f" (tier: {self._tier_label(tier)})."
            )
            approver = None
        else:
            approver = defn.get("exception_approver")
            # LOD-02 (product gap 2): within the tolerance AND booked through the travel
            # tool -> auto-approved (an advisory, never an exception).  Beyond it the
            # verdict below is unchanged (LOD-02 "> 15% requires manager approval").
            tolerance = (
                entry_rules.lodging_tolerance(
                    defn, amount=amount, cap=cap, booked_through_tool=item.booked_through_tool
                )
                if item.item_type == "hotel"
                else None
            )
            if tolerance is not None and tolerance.auto_approved:
                return self._lodging_tolerance_verdict(
                    rule, item, observed, tolerance, amount=amount, cap=cap, tier=tier
                )
            claims = (
                self._hotel_claims(tiers, item, amount=amount, cap=cap)
                if item.item_type == "hotel"
                else None
            )
            if claims is not None:
                observed.update(claims.observed)
            reason = self._amount_cap_violation_reason(
                item,
                amount=amount,
                cap=cap,
                currency=currency,
                tier=tier,
                approver=approver,
                claims=claims,
                room_only=room_only is not None,
            )
            if tolerance is not None and tolerance.within:
                observed.update(self._tolerance_observed(tolerance))
                reason += (
                    f" It is {entry_rules.pct_text(tolerance.over_pct)} over the cap"
                    f" — within the {tolerance.tolerance_pct:g}% {tolerance.clause_ref} "
                    "tolerance, which is auto-approved only for stays booked through the Atlas "
                    "travel tool (a hotel on an approved trip); this stay was not, so it stays "
                    "an exception."
                )
            note = (
                entry_rules.unclassified_note(defn, item) if item.item_type == "receipt" else None
            )
            if note:
                reason += f" {note}"
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=in_policy,
            reason=reason,
            requires_approver=approver if not in_policy else None,
            observed=observed,
        )

    @staticmethod
    def _tolerance_observed(tolerance: entry_rules.Tolerance) -> dict:
        return {
            "over_cap_pct": float(tolerance.over_pct.quantize(entry_rules.CENT)),
            "tolerance_pct": float(tolerance.tolerance_pct),
            "within_tolerance": tolerance.within,
            "booked_through_tool": tolerance.booked_through_tool,
            "tolerance_clause_ref": tolerance.clause_ref,
        }

    def _lodging_tolerance_verdict(
        self,
        rule: PolicyRule,
        item: EvalItem,
        observed: dict,
        tolerance: entry_rules.Tolerance,
        *,
        amount: float,
        cap: float,
        tier: dict,
    ) -> RuleVerdict:
        """LOD-02: <= tolerance over the cap and booked through the tool -> auto-approved."""
        currency = rule.definition.get("currency", "USD")
        where = f" (city: {item.city})" if item.city else ""
        reason = (
            f"Nightly rate {amount:.2f} {currency} is "
            f"{entry_rules.pct_text(tolerance.over_pct)} over the {self._tier_label(tier)} cap "
            f"{cap:.2f} {currency} per night{where} — within the {tolerance.tolerance_pct:g}% "
            f"{tolerance.clause_ref} tolerance for stays booked through the travel tool, so it "
            "is auto-approved (no exception needed)."
        )
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=tolerance.clause_ref,
            in_policy=True,
            reason=reason,
            requires_approver=None,
            observed={
                **observed,
                **self._tolerance_observed(tolerance),
                entry_rules.ADVISORY: "auto_approved_within_tolerance",
            },
        )

    def _hotel_claims(
        self, tiers: Any, item: EvalItem, *, amount: float, cap: float
    ) -> _Claims | None:
        """Would this over-cap hotel line be within a cap at its CLAIMED nights / city?

        Only for a failing verdict, and only from claims (verified nights / city are
        already in *amount* / *cap*).  Never clears the rule.
        """
        claimed_n = None
        if not _count(item.nights) and not item.amount_is_nightly_rate:
            claimed_n = _count(item.claimed_nights)
        claimed = None
        if not item.city:
            match = self._claimed_city_tier(tiers, item, "cap")
            if match is not None and float(match[0]["cap"]) > cap:
                claimed = match
        return _assess_claims(
            amount=amount,
            limit=cap,
            claimed_count=claimed_n,
            count_key="nights",
            claimed=claimed,
            value_key="cap",
        )

    def _amount_cap_violation_reason(
        self,
        item: EvalItem,
        *,
        amount: float,
        cap: float,
        currency: str,
        tier: dict,
        approver: str | None,
        claims: _Claims | None = None,
        room_only: bool = False,
    ) -> str:
        """Approver-friendly cap-violation reason that is honest about what is unverified.

        A hotel item without a verified city is capped at the conservative (lowest)
        tier and one without verified nights is evaluated at the full stay amount;
        both stay violations.  When the claimant's own (unverified) nights / city
        would bring it within a cap, the reason states the computed per-night figure
        and that cap, and that the approver must confirm it (Issue 6).
        """
        tier_name = self._tier_label(tier)
        requires = f"Requires {approver or 'approver'} pre-approval."
        if item.item_type != "hotel":
            return (
                f"Amount {amount:.2f} {currency} exceeds the {tier_name} cap "
                f"{cap:.2f} {currency}. {requires}"
            )
        nights = _count(item.nights)
        claimed_n = claims.claimed_count if claims else None
        if room_only:
            count = f" ({_plural(nights, 'night')})" if nights else ""
            basis = f"Nightly room rate {amount:.2f} {currency} excl. tax{count}"
        elif nights:
            basis = f"Nightly rate {amount:.2f} {currency} ({_plural(nights, 'night')})"
        elif item.amount_is_nightly_rate:
            if item.nights_source == "requested":
                basis = f"Nightly rate {amount:.2f} {currency} (quoted per-night rate)"
            else:
                total = (
                    f"; stay total {item.stay_amount_usd:.2f} {currency}"
                    if item.stay_amount_usd is not None
                    else ""
                )
                basis = (
                    f"Nightly rate {amount:.2f} {currency} (per-night rate on the receipt{total})"
                )
        elif claimed_n and claims is not None:
            basis = (
                f"Hotel amount {amount:.2f} {currency} over the "
                f"{_plural(claimed_n, 'night')} stated on the entry (not verified) is "
                f"{claims.unit_amount:.2f} {currency} per night"
            )
        else:
            basis = f"Hotel amount {amount:.2f} {currency}"
        where = f" (city: {item.city})" if item.city else ""
        claimed_text = f" (claimed: {item.claimed_city!r})" if item.claimed_city else ""
        lowest = f"the lowest cap ({tier_name}, {cap:.2f} {currency} per night)"
        city_unverified = (
            f"Hotel city could not be verified from a receipt or approved itinerary"
            f"{claimed_text}, so {lowest} was applied"
        )
        nights_unverified = (
            "The number of nights could not be verified from a receipt or approved itinerary."
        )
        parts: list[str] = []
        if claims is not None and claims.unverified:
            if "city" in claims.unverified and claims.claimed is not None:
                ctier, cname = claims.claimed
                parts.append(
                    f"{basis} — within the {self._tier_label(ctier)} {cname} cap of "
                    f"{_usd(float(ctier['cap']))}/night if the city is confirmed."
                )
                parts.append(f"{city_unverified}.")
            else:
                parts.append(
                    f"{basis} — within the {tier_name} cap {cap:.2f} {currency} per night{where}."
                )
                if not item.city:
                    parts.append(f"{city_unverified}.")
            if claimed_n:
                parts.append(nights_unverified)
            confirm = {
                ("nights",): "the stay length",
                ("city",): "the hotel city",
                ("nights", "city"): "the stay length and the hotel city",
            }.get(tuple(claims.unverified), "the stated details")
            parts.append(f"Your approver must confirm {confirm}.")
        else:
            verb = ", which exceeds" if claimed_n else " exceeds"
            parts.append(
                f"{basis}{verb} the {tier_name} cap {cap:.2f} {currency} per night{where}."
            )
            if not item.city:
                parts.append(f"{city_unverified} instead of a city-specific cap.")
                if claims is not None and claims.claimed is not None:
                    ctier, cname = claims.claimed
                    parts.append(
                        f"It would also exceed the {self._tier_label(ctier)} {cname} cap of "
                        f"{_usd(float(ctier['cap']))}/night."
                    )
                else:
                    parts.append(
                        "Attach the hotel folio or link the stay to its approved trip "
                        "to apply the city's cap."
                    )
            if claimed_n:
                parts.append(nights_unverified)
            elif not nights and not item.amount_is_nightly_rate:
                parts.append(
                    "Number of nights could not be verified, so the full amount was "
                    "compared with the nightly cap. Add the number of nights to the "
                    'description (e.g. "3 nights"), link the stay to its approved trip, '
                    "or attach the hotel folio."
                )
        parts.append(requires)
        return " ".join(parts)

    # ------------------------------------------------------------------
    # per_diem (MEALS-PD)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_tiered_rates(rates: Any) -> bool:
        return isinstance(rates, list) and any(
            isinstance(r, dict) and ("tier" in r or "cities" in r or "default" in r) for r in rates
        )

    def _eval_per_diem(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        amount = item.amount_usd

        if amount is None:
            return self._skip_verdict(rule, "amount_usd not provided")
        # Product gap 2: QUANTIFIED exclusions (MEAL-04 alcohol read from receipt line
        # items) are not reimbursable — compare the meal amount without them.  The
        # exclusion itself is reported by its own verdict (``_exclusion_verdict``).
        amount = entry_rules.net_of_exclusions(defn, item, amount)

        if self._is_tiered_rates(defn.get("rates")):
            return self._eval_per_diem_tiered(rule, item, amount)

        # Legacy region-keyed rates (domestic / international) — unchanged.
        region = item.region or "domestic"
        rate: float | None = None
        for r in defn.get("rates", []):
            if r.get("region") == region:
                rate = float(r["amount"])
                break
        if rate is None:
            rate = float(defn["rates"][0]["amount"])

        currency = defn.get("currency", "USD")
        in_policy = amount <= rate
        requires_approver: str | None = None
        observed: dict = {"amount_usd": amount, "rate": rate, "region": region}

        if in_policy:
            reason = (
                f"Meal amount {amount} {currency} is within"
                f" {region} per-diem {rate} {currency}."
            )
        else:
            reason = (
                f"Meal amount {amount} {currency} exceeds" f" {region} per-diem {rate} {currency}."
            )
            requires_approver = None

        reason, requires_approver = self._group_meal(defn, item, observed, reason)
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=in_policy,
            reason=reason,
            requires_approver=requires_approver,
            observed=observed,
        )

    @staticmethod
    def _group_meal(
        defn: dict, item: EvalItem, observed: dict, reason: str
    ) -> tuple[str, str | None]:
        headcount = item.headcount or 0
        group_threshold = defn.get("group_meal_threshold", 3)
        if headcount >= group_threshold:
            observed["headcount"] = headcount
            observed["group_meal_threshold"] = group_threshold
            group_approver = defn.get("group_meal_approver", "manager")
            reason += f" Group meal ({headcount} attendees) requires {group_approver} approval."
            return reason, group_approver
        return reason, None

    def _eval_per_diem_tiered(self, rule: PolicyRule, item: EvalItem, amount: float) -> RuleVerdict:
        """MEALS-PD by city tier (canonical MEAL-01 shape).

        ``amount_usd`` is the per-day figure: the line amount, or — with verified
        ``days`` — the per-day average of ``stay_amount_usd``.  No verified city ->
        the lowest tier.  Claimed days / city never clear the rule (see EvalItem).
        """
        defn = rule.definition
        rates = defn.get("rates")
        row = self._resolve_tier(rates, item.city, "amount")
        if row is None:
            return self._skip_verdict(rule, "no matching rate in per_diem definition")
        rate = float(row["amount"])
        currency = defn.get("currency", "USD")
        label = self._tier_label(row)
        days = _count(item.days)
        partial_pct = entry_rules.partial_day_pct(defn)
        if partial_pct is not None and days and item.travel_days is not None:
            # MEAL-02 (product gap 2): verified trip days -> full rate on full days,
            # partial_pct of it on departure / return days.
            return self._eval_partial_days(
                rule, item, amount, rate=rate, row=row, days=days, pct=partial_pct
            )
        in_policy = amount <= rate
        observed: dict = {
            "amount_usd": amount,
            "rate": rate,
            "tier": row.get("tier"),
            "city": item.city,
        }
        if row.get("label"):
            observed["tier_label"] = row["label"]
        if days:
            observed["days"] = days
            if item.stay_amount_usd is not None:
                observed["total_usd"] = item.stay_amount_usd
        where = f" (city: {item.city})" if item.city else ""
        if days:
            total = item.stay_amount_usd if item.stay_amount_usd is not None else amount * days
            basis = (
                f"Daily meal average {amount:.2f} {currency} "
                f"({total:.2f} {currency} over {_plural(days, 'day')})"
            )
        else:
            basis = f"Meal amount {amount:.2f} {currency}"

        unverified_day: tuple[str, str] | None = None
        if in_policy:
            reason = f"{basis} is within the {label} per diem {rate:.2f} {currency} per day{where}."
            unverified_day = self._unverified_travel_day(defn, amount=amount, rate=rate)
            if unverified_day is not None:
                clause, partial = unverified_day
                reason += (
                    f" Atlas could not verify whether it falls on a departure or return day, "
                    f"when {clause} allows {partial_pct:g}% of the per diem ({partial} "
                    f"{currency}); link the meal to its approved trip so the day can be checked."
                )
                observed[entry_rules.COULD_NOT_VERIFY] = "travel_day"
                observed["partial_day_pct"] = float(partial_pct or 0)
        else:
            claimed_d = None if days else _count(item.claimed_days)
            claimed = None
            if not item.city:
                match = self._claimed_city_tier(rates, item, "amount")
                if match is not None and float(match[0]["amount"]) > rate:
                    claimed = match
            claims = _assess_claims(
                amount=amount,
                limit=rate,
                claimed_count=claimed_d,
                count_key="days",
                claimed=claimed,
                value_key="amount",
            )
            if claims is not None:
                observed.update(claims.observed)
                if claimed_d:
                    basis = (
                        f"Meal amount {amount:.2f} {currency} over the "
                        f"{_plural(claimed_d, 'day')} stated on the entry (not verified) is "
                        f"{claims.unit_amount:.2f} {currency} per day"
                    )
            claimed_text = f" (claimed: {item.claimed_city!r})" if item.claimed_city else ""
            city_unverified = (
                f"Meal city could not be verified from a receipt or approved itinerary"
                f"{claimed_text}, so the lowest per diem ({label}, {rate:.2f} {currency} "
                "per day) was applied."
            )
            days_unverified = (
                "The number of days could not be verified from a receipt or approved itinerary."
            )
            parts: list[str] = []
            if claims is not None and claims.unverified:
                if "city" in claims.unverified and claims.claimed is not None:
                    ctier, cname = claims.claimed
                    parts.append(
                        f"{basis} — within the {self._tier_label(ctier)} {cname} per diem of "
                        f"{_usd(float(ctier['amount']))}/day if the city is confirmed."
                    )
                else:
                    parts.append(
                        f"{basis} — within the {label} per diem {rate:.2f} {currency} "
                        f"per day{where}."
                    )
                if not item.city:
                    parts.append(city_unverified)
                if claimed_d:
                    parts.append(days_unverified)
                confirm = {
                    ("days",): "the number of days",
                    ("city",): "the city",
                    ("days", "city"): "the number of days and the city",
                }.get(tuple(claims.unverified), "the stated details")
                parts.append(f"Your approver must confirm {confirm}.")
            else:
                verb = ", which exceeds" if claimed_d else " exceeds"
                parts.append(
                    f"{basis}{verb} the {label} per diem {rate:.2f} {currency} per day{where}."
                )
                if not item.city:
                    parts.append(city_unverified)
                    if claims is not None and claims.claimed is not None:
                        ctier, cname = claims.claimed
                        parts.append(
                            f"It would also exceed the {self._tier_label(ctier)} {cname} per "
                            f"diem of {_usd(float(ctier['amount']))}/day."
                        )
                if claimed_d:
                    parts.append(days_unverified)
                elif not days:
                    parts.append(
                        "If it covers several days, state the number of days in the "
                        'description (e.g. "3 days").'
                    )
            reason = " ".join(parts)

        reason, requires_approver = self._group_meal(defn, item, observed, reason)
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=unverified_day[0] if unverified_day else rule.source_clause_ref,
            in_policy=in_policy,
            reason=reason,
            requires_approver=requires_approver,
            observed=observed,
        )

    @staticmethod
    def _unverified_travel_day(defn: dict, *, amount: float, rate: float) -> tuple[str, str] | None:
        """MEAL-02 cannot be checked without the trip's dates: (clause, partial rate) when a
        line within the full rate would be OVER the partial-day rate, else None."""
        pct = entry_rules.partial_day_pct(defn)
        if pct is None:
            return None
        partial = entry_rules.per_diem_allowance(rate, days=1, travel_days=1, pct=pct)
        a = entry_rules.cents(amount)
        if a is None or a <= partial:
            return None
        clause = defn.get("partial_day_clause_ref")
        return (clause if isinstance(clause, str) and clause else "MEAL-02", f"{partial:.2f}")

    def _eval_partial_days(
        self,
        rule: PolicyRule,
        item: EvalItem,
        amount: float,
        *,
        rate: float,
        row: dict,
        days: int,
        pct: Any,
    ) -> RuleVerdict:
        """MEAL-01 + MEAL-02 over a verified trip: the pooled meal total against
        rate x full days + pct% x rate x departure/return days (Decimal, to the cent)."""
        defn = rule.definition
        currency = defn.get("currency", "USD")
        label = self._tier_label(row)
        travel = max(0, min(int(item.travel_days or 0), days))
        full = days - travel
        allowance = entry_rules.per_diem_allowance(rate, days=days, travel_days=travel, pct=pct)
        full_allowance = entry_rules.per_diem_allowance(rate, days=days, travel_days=0, pct=pct)
        total = entry_rules.pooled_total(defn, item, amount, days=days)
        partial_rate = entry_rules.per_diem_allowance(rate, days=1, travel_days=1, pct=pct)
        clause = defn.get("partial_day_clause_ref")
        partial_clause = clause if isinstance(clause, str) and clause else "MEAL-02"
        in_policy = total <= allowance
        where = f" (city: {item.city})" if item.city else ""
        observed: dict = {
            "amount_usd": amount,
            "rate": rate,
            "tier": row.get("tier"),
            "city": item.city,
            "days": days,
            "full_days": full,
            "travel_days": travel,
            "partial_day_pct": float(pct),
            "partial_day_rate": float(partial_rate),
            "allowance_usd": float(allowance),
            "total_usd": float(total),
        }
        if row.get("label"):
            observed["tier_label"] = row["label"]
        parts = []
        if full:
            parts.append(f"{_plural(full, 'full day')} at {rate:.2f}")
        if travel:
            same_day = " (same-day trip)" if days == 1 else ""
            parts.append(
                f"{_plural(travel, 'departure/return day')}{same_day} at {float(pct):g}% = "
                f"{partial_rate:.2f}"
            )
        breakdown = " + ".join(parts)
        basis = f"Meals {total:.2f} {currency} over {_plural(days, 'day')}"
        if in_policy:
            reason = (
                f"{basis} are within the {label} allowance {allowance:.2f} {currency}{where}: "
                f"{breakdown} ({partial_clause})."
            )
        else:
            reason = (
                f"{basis} exceed the {label} allowance {allowance:.2f} {currency}{where}: "
                f"{breakdown} ({partial_clause})."
            )
        reason, requires_approver = self._group_meal(defn, item, observed, reason)
        # A breach caused only by the partial-day rate cites MEAL-02 itself.
        cite = rule.source_clause_ref
        if not in_policy and total <= full_allowance:
            cite = partial_clause
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=cite,
            in_policy=in_policy,
            reason=reason,
            requires_approver=requires_approver,
            observed=observed,
        )

    # ------------------------------------------------------------------
    # preferred_supplier (AIR-PREF, HOTEL-PREF)
    # ------------------------------------------------------------------

    def _eval_preferred_supplier(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        supplier = (item.supplier or "").strip()
        preferred = defn.get("preferred_suppliers", [])
        # A missing/blank supplier is deliberately NOT a skip verdict: it falls
        # through as "not in the preferred list" — blocking for AIR-PREF, a silent
        # in-policy advisory for HOTEL-PREF. This is pinned by the M-A3 gold label
        # m_a3_flight_empty_supplier_violation (violation-recall hard gate = 1.0).
        # The receipt-upload path now passes the OCR vendor through, so AIR-PREF
        # only blocks there when OCR yields no vendor at all.  Caveat: the vendor
        # may be a travel agency (e.g. "Expedia") rather than the carrier, which
        # still reads as non-preferred and needs an approver/exception.
        # HOTEL-PREF is advisory ("should") — non-blocking.
        is_advisory = rule.applies_to == PolicyAppliesTo.hotel and "condition" in defn
        # Prefix (sub-brand / property name) matching only for the advisory rule;
        # a blocking rule needs an exact normalized brand match.
        in_preferred = supplier_matches_preferred(supplier, preferred, allow_prefix=is_advisory)
        observed: dict = {"supplier": supplier, "preferred_suppliers": preferred}
        if not supplier:
            # C7R: record WHY there is no supplier (never print an empty name).
            observed["supplier_verified"] = False
            if item.claimed_supplier:
                observed["claimed_supplier"] = item.claimed_supplier
            if item.supplier_evidence:
                observed["supplier_evidence"] = item.supplier_evidence
        preferred_text = ", ".join(str(p) for p in preferred) or "none configured"

        if in_preferred:
            reason = f"Supplier {supplier!r} is in the preferred list."
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rule.rule_type.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=True,
                reason=reason,
                requires_approver=None,
                observed=observed,
            )

        if is_advisory:
            not_preferred = (
                f"Supplier {supplier!r} is not in the preferred list ({preferred_text})."
                if supplier
                else self._unverified_supplier_text(item)
            )
            reason = f"{not_preferred} Preferred booking is advised but not required."
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rule.rule_type.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=True,  # advisory — not blocking
                reason=reason,
                requires_approver=None,
                observed=observed,
            )

        approver = defn.get("exception_approver")
        if supplier:
            reason = (
                f"Supplier {supplier!r} is NOT in the preferred list ({preferred_text}). "
                f"Requires {approver or 'approver'} pre-approval."
            )
        else:
            # Fail closed: an unverifiable supplier is still a blocking verdict
            # (M-A3 gold label m_a3_flight_empty_supplier_violation), but the
            # reason states what is actually wrong.
            reason = (
                f"{self._unverified_supplier_text(item)} Preferred suppliers: "
                f"{preferred_text}. Requires {approver or 'approver'} review."
            )
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=False,
            reason=reason,
            requires_approver=approver,
            observed=observed,
        )

    @staticmethod
    def _unverified_supplier_text(item: EvalItem) -> str:
        """Why an item has no usable supplier (claimed-but-unverified / conflict / none)."""
        kind = {"flight": "Airfare", "hotel": "Hotel", "rail": "Rail", "car": "Car rental"}.get(
            item.item_type, "Supplier"
        )
        claimed = (item.claimed_supplier or "").strip()
        evidence = (item.supplier_evidence or "").strip()
        if claimed and evidence:
            return (
                f"{kind} supplier could not be verified: the claim names {claimed!r} but the "
                f"receipt / card transaction shows {evidence!r}."
            )
        if claimed:
            return (
                f"{kind} supplier could not be verified from a receipt, card transaction or "
                f"approved itinerary (claimed: {claimed!r})."
            )
        if evidence:
            return f"{kind} supplier could not be verified: the records disagree ({evidence})."
        return (
            f"{kind} supplier is unknown: no vendor was given and none could be verified "
            "from a receipt, card transaction or approved itinerary."
        )

    # ------------------------------------------------------------------
    # category_allow (GROUND-CLASS)
    # ------------------------------------------------------------------

    def _eval_category_allow(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        vc = item.vehicle_class or ""
        allowed_classes: list[str] = defn.get("allowed_classes", [])
        excluded_classes: list[str] = defn.get("excluded_classes", [])
        conditional_classes: list[dict] = defn.get("conditional_classes", [])
        observed = {
            "vehicle_class": vc,
            "allowed_classes": allowed_classes,
            "excluded_classes": excluded_classes,
        }

        if vc in allowed_classes:
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rule.rule_type.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=True,
                reason=f"Vehicle class {vc!r} is in the allowed list.",
                requires_approver=None,
                observed=observed,
            )

        if vc in excluded_classes:
            approver = defn.get("exception_approver")
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rule.rule_type.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=False,
                reason=(
                    f"Vehicle class {vc!r} is in the excluded list. "
                    f"Requires {approver or 'approver'} pre-approval."
                ),
                requires_approver=approver,
                observed=observed,
            )

        # Conditional classes (e.g. Intermediate — allowed with 3+ colleagues)
        for cond in conditional_classes:
            if cond.get("class") == vc:
                condition = cond.get("condition", "condition not met")
                # Condition cannot be verified from the item alone → default OUT + note
                approver = defn.get("exception_approver")
                return RuleVerdict(
                    rule_id=str(rule.id),
                    rule_type=rule.rule_type.value,
                    applies_to=rule.applies_to.value,
                    source_clause_ref=rule.source_clause_ref,
                    in_policy=False,
                    reason=(
                        f"Vehicle class {vc!r} is conditionally allowed ({condition}). "
                        "Condition cannot be verified automatically — requires approval."
                    ),
                    requires_approver=approver,
                    observed={**observed, "condition": condition},
                )

        # Unknown class — not explicitly allowed
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=False,
            reason=f"Vehicle class {vc!r} is not in the allowed or conditional list.",
            requires_approver=None,
            observed=observed,
        )

    # ------------------------------------------------------------------
    # receipt_required (RECEIPT-REQ, applies_to=all)
    # ------------------------------------------------------------------

    def _eval_receipt_required(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        threshold = float(defn.get("threshold_usd", 25))
        always_required: list[str] = defn.get("always_required_categories", [])
        amount = item.amount_usd or 0.0
        category = item.expense_category or ""
        observed = {
            "amount_usd": amount,
            "threshold_usd": threshold,
            "expense_category": category,
        }
        if category in RECEIPT_EXEMPT_CATEGORIES:
            return self._receipt_exempt_verdict(rule, category, observed)

        receipt_required = amount >= threshold or category in always_required

        if not receipt_required:
            return RuleVerdict(
                rule_id=str(rule.id),
                rule_type=rule.rule_type.value,
                applies_to=rule.applies_to.value,
                source_clause_ref=rule.source_clause_ref,
                in_policy=True,
                reason=(
                    f"Receipt not required (amount {amount} < {threshold}"
                    f" and category {category!r} not in always-required list)."
                ),
                requires_approver=None,
                observed={**observed, "receipt_required": False},
            )

        # For prospective options (no receipt yet) → informational, not blocking.
        # For receipt items → the receipt is being evaluated; treated as in-policy
        # advisory since we don't track whether a receipt is attached at this layer.
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=True,
            reason=(
                f"Receipt required (amount {amount} >= {threshold} or category {category!r} "
                "in always-required list). Ensure receipt is attached."
            ),
            requires_approver=None,
            observed={**observed, "receipt_required": True},
        )

    # ------------------------------------------------------------------
    # Product gap 2 (policy2): per_head_cap (MEAL-05, ENT-02), gift_disclosure
    # (ENT-04), mileage (MIL-01..03) and the REF-08 receipt exemption.  The
    # Decimal logic lives in ai_agents/tools/spend_policy.py; this section only
    # resolves city tiers and wraps the outcomes into RuleVerdicts.
    # ------------------------------------------------------------------

    def _receipt_exempt_verdict(
        self, rule: PolicyRule, category: str, observed: dict
    ) -> RuleVerdict:
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=True,
            reason=(
                f"Receipt not required: {category} is documented by its route and "
                f"distance, not a receipt ({RECEIPT_EXEMPT_CLAUSE})."
            ),
            requires_approver=None,
            observed={
                **observed,
                "receipt_required": False,
                "exempt_category": category,
                "exemption_clause": RECEIPT_EXEMPT_CLAUSE,
            },
        )

    def _eval_spend_rule(self, rule: PolicyRule, item: EvalItem) -> RuleVerdict:
        defn = rule.definition
        if not isinstance(defn, dict):
            return self._skip_verdict(rule, "definition is not an object")
        rt = rule.rule_type.value
        outcome: SpendOutcome | None
        if rt == PER_HEAD_CAP:
            tiers = defn.get("tiers", [])
            tier = self._resolve_tier(tiers, item.city, "cap")
            if tier is None:
                return self._skip_verdict(rule, "no matching tier in per_head_cap definition")
            claimed = None if item.city else self._claimed_city_tier(tiers, item, "cap")
            outcome = evaluate_per_head_cap(
                defn,
                item.spend,
                tier=tier,
                city=item.city,
                claimed_tier=claimed,
                clause_ref=rule.source_clause_ref,
            )
        elif rt == GIFT_DISCLOSURE:
            outcome = evaluate_gift_disclosure(defn, item.spend, clause_ref=rule.source_clause_ref)
        elif rt == MILEAGE:
            outcome = evaluate_mileage(defn, item.spend, clause_ref=rule.source_clause_ref)
        else:  # pragma: no cover — guarded by the dispatcher
            outcome = None
        if outcome is None:
            return self._skip_verdict(rule, f"{rt} could not be evaluated for this line")
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rt,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref or outcome.clause_ref,
            in_policy=outcome.in_policy,
            reason=outcome.reason,
            requires_approver=outcome.requires_approver,
            observed=outcome.observed,
        )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _skip_verdict(self, rule: PolicyRule, reason: str) -> RuleVerdict:
        return RuleVerdict(
            rule_id=str(rule.id),
            rule_type=rule.rule_type.value,
            applies_to=rule.applies_to.value,
            source_clause_ref=rule.source_clause_ref,
            in_policy=True,
            reason=f"Rule skipped: {reason}.",
            requires_approver=None,
            observed={},
        )
