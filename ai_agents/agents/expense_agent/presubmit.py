"""Pre-submit policy check for the Expense Agent (P4-T10, FR-X5, M-A3).

Builds EvalItem per DraftEntry, calls PolicyEvalTool.evaluate_many,
and converts RuleVerdict lists into PreSubmitFlag instances.

Three load-bearing collisions (C1, C2, C3) implemented exactly per spec.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents import grade_checks, process_checks
from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.traveller_grade import owner_grade
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.evidence import (
    OCR_SNAPSHOT_KEY,
    backing_expense_ids,
    ocr_snapshot,
    receipt_ids_by_entry,
)
from ai_agents.receipt_pipeline.retention import RECEIPT_RETAINED_ACTION
from ai_agents.tools import entry_rules
from ai_agents.tools.fx_policy import (
    CURRENCY_UNVERIFIED_FLAG,
    FX_NOT_COMPARED,
    FX_STATUS_KEY,
    FX_UNSUPPORTED,
    with_item_currency,
)
from ai_agents.tools.policy_eval import (
    EvalItem,
    PolicyEvalTool,
    RuleVerdict,
    normalize_supplier_name,
)
from ai_agents.tools.spend_policy import (
    PER_HEAD_CAP,
    SPEND_CATEGORIES,
    spend_facts,
    team_meal_cap_minimum,
)
from config.constants.city_names import same_city
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.audit_log import AuditLog
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseCategory
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule, PolicyRuleStatus, PolicyRuleType
from database.models.receipt import Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreSubmitFlag:
    """A pre-submit policy flag for a draft entry."""

    entry_ref: str  # vendor or index-based ref
    flag_type: str  # "policy_violation" | "missing_receipt" | "approval_required" | ...
    rule_id: str | None
    rule_type: str | None
    source_clause_ref: str | None
    reason: str
    requires_approver: str | None
    observed: dict
    severity: str  # "blocking" | "advisory"
    # C7R: the persisted Expense id when the entry exists (None for an unsaved
    # draft, a trip segment or a what-if item).  entry_ref is a readable label
    # (usually the vendor), so two same-vendor lines are only distinguishable
    # by entry_id.
    entry_id: str | None = None
    # C7R UAT: True when the claimant may resolve this flag with a written
    # justification (a policy_violation of a JUSTIFIABLE_RULE_TYPES rule).
    justifiable: bool = False
    # The entry's justification when it was applied to this flag — the flag is
    # then advisory (submit allowed) but still shown to the approver.
    justification: str | None = None
    # Issue 6: the line is within a cap only at the claimant's UNVERIFIED stay
    # length / city ("nights", "days", "city", "nights_and_city", "days_and_city").
    # The flag is then advisory (submit allowed) but stays a policy_violation, so
    # approval-time evaluation escalates it to the approver — never auto-approved.
    pending_verification: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreSubmitFlag:
        """Inverse of ``to_dict`` (a stored snapshot flag back to a flag)."""
        names = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in names}
        values["observed"] = dict(values.get("observed") or {})
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_ref": self.entry_ref,
            "entry_id": self.entry_id,
            "flag_type": self.flag_type,
            "rule_id": self.rule_id,
            "rule_type": self.rule_type,
            "source_clause_ref": self.source_clause_ref,
            "reason": self.reason,
            "requires_approver": self.requires_approver,
            "observed": self.observed,
            "severity": self.severity,
            "justifiable": self.justifiable,
            "justification": self.justification,
            "pending_verification": self.pending_verification,
        }


# UAT2: RECEIPT-REQ issue for an attached receipt whose total is not a machine
# reading (illegible / withheld, typed by the claimant, or guessed by the gap-fill
# LLM).  verdicts_to_flags turns it into an ADVISORY missing_receipt flag.
RECEIPT_UNVERIFIED = "the attached receipt has no machine-read amount to verify the claim"

# UAT2: an attached receipt whose date is far from the claimed date (advisory).
RECEIPT_MISMATCH = "receipt_mismatch"

# Product gap 2: a line in policy only through a clause's own allowance (e.g. LOD-02,
# <= 15% over the nightly cap booked through the travel tool -> auto-approved).
# Advisory and NOT escalating: the clause itself auto-approves it.
POLICY_ADVISORY = "policy_advisory"
_RECEIPT_DATE_TOLERANCE_DAYS = 3

# Flag types that must reach a HUMAN at approval time whatever their pre-submit
# severity: approval-time evaluation (approval_agent.evaluate._is_blocking) treats
# them as blocking, so a report carrying one is never auto-approved.
ESCALATING_FLAG_TYPES: frozenset[str] = frozenset(
    {"missing_receipt", "possible_duplicate", RECEIPT_MISMATCH, CURRENCY_UNVERIFIED_FLAG}
)


# Rule types whose violation is a SOFT exception: policy itself provides for a
# MANAGER signing off on it ("requires manager pre-approval", "written
# justification"), so a claimant's justification moves the flag from blocking to
# advisory — the report can be submitted and the approver decides (C7R UAT).
#
# Deliberately NOT justifiable (stay blocking whatever the claimant writes):
#   * receipt_required -> missing_receipt: the fix is evidence (attach a receipt
#     that covers the claim), not prose;
#   * category_allow: a prohibited category / vehicle class is never
#     reimbursable — change the category or remove the entry;
#   * cabin_class (AIR-CABIN): an upgrade needs VP-level pre-approval. A flag
#     that names an approver above manager is routed by grade (phase 12: SVP /
#     VP routing), so a claimant's justification must never downgrade it to a
#     manager-level advisory;
#   * possible_duplicate: not a rule verdict; a second claim for one purchase;
#   * any unknown / future rule type (fail closed).
# ``is_justifiable`` adds per-verdict guards (approver level, evidence).
# A justified flag keeps flag_type "policy_violation", so approval-time
# evaluation still treats it as an exception (never auto-approved) and the
# approver UI shows it as an exception together with the justification.
JUSTIFIABLE_RULE_TYPES: frozenset[str] = frozenset(
    {"amount_cap", "per_diem", "preferred_supplier", PER_HEAD_CAP}
)

# The only approval tier Atlas routes an exception to (the submitter's manager).
_JUSTIFIABLE_APPROVERS: frozenset[str] = frozenset({"manager"})


def is_justifiable(verdict: RuleVerdict) -> bool:
    """True when a written justification may soften this FAILING verdict.

    Fails closed:
      * the rule type must be in ``JUSTIFIABLE_RULE_TYPES``;
      * a named exception approver above manager (e.g. "VP") → not justifiable;
      * an UNVERIFIED supplier (``observed["supplier_verified"] is False`` — no
        supplier could be read from the evidence, AIR-PREF) → not justifiable:
        like a missing receipt, the fix is evidence, not prose.
    """
    observed = verdict.observed if isinstance(verdict.observed, dict) else {}
    # Product gap 2: an entry-level rule (GRND-01 rail class, MEAL-04 exclusion, ...)
    # marks a breach a manager may accept with a justification explicitly.
    if (
        verdict.rule_type not in JUSTIFIABLE_RULE_TYPES
        and observed.get(entry_rules.JUSTIFIABLE) is not True
    ):
        return False
    approver = verdict.requires_approver
    if approver and approver.strip().lower() not in _JUSTIFIABLE_APPROVERS:
        return False
    return observed.get("supplier_verified") is not False


def pending_verification(verdict: RuleVerdict) -> str | None:
    """What must be verified for this FAILING verdict to be within its cap (Issue 6).

    Set by PolicyEvalTool when the line is within a cap only at the claimant's own
    (unverified) nights / days / city: "nights", "days", "city", "nights_and_city"
    or "days_and_city".  None otherwise.  Callers must still gate on
    ``is_justifiable`` (an above-manager rule is never softened).
    """
    observed = verdict.observed if isinstance(verdict.observed, dict) else {}
    if observed.get("within_cap_if_verified") is not True:
        return None
    unverified = observed.get("unverified")
    if (
        not isinstance(unverified, list)
        or not unverified
        or not all(u in ("nights", "days", "city") for u in unverified)
    ):
        return None
    return "_and_".join(unverified)


def clean_justification(value: str | None) -> str | None:
    """The stripped justification, or None when blank."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _entry_ref(entry: DraftEntry, idx: int) -> str:
    return entry.vendor or f"entry_{idx}"


def entry_label(entry: DraftEntry) -> str:
    """Approver-friendly name for an entry in flag reasons (C7R).

    The vendor when there is one, else "<Category> <amount> <currency> on <date>"
    — never an internal id / UUID.
    """
    vendor = (entry.vendor or "").strip()
    if vendor:
        return vendor
    category = str(entry.category or "expense").replace("_", " ").capitalize()
    amount = _coerce_float(entry.amount)
    amount_text = f"{amount:.2f}" if amount is not None else "?"
    currency = f" {entry.currency}" if entry.currency else ""
    when = f" on {entry.expense_date}" if entry.expense_date else ""
    return f"{category} {amount_text}{currency}{when}"


def verdicts_to_flags(
    verdicts: list[RuleVerdict],
    *,
    entry_ref: str,
    has_receipt: bool,
    category: str,
    receipt_issue: str | None = None,
    entry_label: str | None = None,
    entry_id: str | None = None,
    justification: str | None = None,
) -> tuple[list[PreSubmitFlag], int]:
    """Convert a list of RuleVerdicts for ONE entry into PreSubmitFlags.

    This is the canonical per-verdict → flag mapping, factored out of
    run_presubmit so it can be reused by single-entry callers (e.g. the
    receipt-upload auto-draft path). Behavior is identical to the loop body
    previously inlined in run_presubmit.

    Collisions handled:
      C3: receipt_required → never a policy_violation; derive missing_receipt.
      C2: ground_transport + applies_to="car" → advisory, never blocking.

    ``entry_label`` (default: ``entry_ref``) names the entry in reason text.
    ``entry_id`` (the persisted Expense id, when there is one) is stamped on every
    flag so a client can attribute it to exactly one line (C7R).
    ``justification`` (the entry's written justification, C7R UAT): a non-empty
    value makes each policy_violation of a ``JUSTIFIABLE_RULE_TYPES`` rule
    advisory instead of blocking and records the text on the flag.  Hard blocks
    (missing receipt, category_allow, unknown rule types) are unaffected.
    A justifiable violation that is within its cap at the claimant's unverified
    nights / days / city (``pending_verification``, Issue 6) is advisory too —
    it stays a policy_violation, so the approver must confirm it.

    Returns:
        (flags, failing_verdict_count) where failing_verdict_count is the number
        of in_policy=False verdicts that produced a policy_violation flag (feeds
        the M-A3 guard in run_presubmit).
    """
    flags: list[PreSubmitFlag] = []
    failing_verdict_count = 0
    ref = entry_ref
    label = entry_label or entry_ref
    justified_text = clean_justification(justification)

    currency_flagged = False
    for verdict in verdicts:
        # Product gap 8: an amount in an unsupported currency could not be compared
        # with a cap — one advisory, ESCALATING flag per entry (never a silent pass).
        if verdict.observed.get(FX_STATUS_KEY) == FX_UNSUPPORTED:
            if not currency_flagged:
                currency_flagged = True
                flags.append(
                    PreSubmitFlag(
                        entry_ref=ref,
                        flag_type=CURRENCY_UNVERIFIED_FLAG,
                        rule_id=verdict.rule_id,
                        rule_type=verdict.rule_type,
                        source_clause_ref=verdict.source_clause_ref,
                        reason=(
                            f"Could not verify {label} against policy limits: currency "
                            f"{verdict.observed.get('currency')} is not supported. "
                            "Your approver will review it."
                        ),
                        requires_approver=None,
                        observed=verdict.observed,
                        severity="advisory",
                        entry_id=entry_id,
                    )
                )
            # Integration: only the bare "could not compare" verdict stops here; a rule
            # that still decided something (ENT-04 judged as an unknown amount) goes on
            # to its own flag below.
            if verdict.rule_type != "receipt_required" and verdict.observed.get(FX_NOT_COMPARED):
                continue
        # C3: receipt_required → never a policy_violation; derive missing_receipt flag
        if verdict.rule_type == "receipt_required":
            receipt_required = verdict.observed.get("receipt_required") is True
            if receipt_required and not has_receipt and receipt_issue == RECEIPT_UNVERIFIED:
                # UAT2: a receipt IS attached but its total cannot be machine-read
                # (illegible, or typed by the claimant).  Prose cannot fix that and
                # neither can another upload of the same paper, so it must not be a
                # permanent submit dead-end: advisory for submit, but still a
                # missing_receipt flag (ESCALATING_FLAG_TYPES) — never auto-approved;
                # a human compares the image with the claim.
                flags.append(
                    PreSubmitFlag(
                        entry_ref=ref,
                        flag_type="missing_receipt",
                        rule_id=verdict.rule_id,
                        rule_type=verdict.rule_type,
                        source_clause_ref=verdict.source_clause_ref,
                        reason=(
                            f"Receipt attached for {label} but its total could not be "
                            f"verified ({receipt_issue}) — your approver will review. "
                            f"Rule: {verdict.source_clause_ref or verdict.rule_id}."
                        ),
                        requires_approver=None,
                        observed={**verdict.observed, "receipt_unverified": True},
                        severity="advisory",
                        entry_id=entry_id,
                    )
                )
                continue
            if receipt_required and not has_receipt:
                flags.append(
                    PreSubmitFlag(
                        entry_ref=ref,
                        flag_type="missing_receipt",
                        rule_id=verdict.rule_id,
                        rule_type=verdict.rule_type,
                        source_clause_ref=verdict.source_clause_ref,
                        reason=(
                            f"Receipt required for {label} but "
                            f"{receipt_issue or 'no receipt attached'}. "
                            f"Rule: {verdict.source_clause_ref or verdict.rule_id}."
                        ),
                        requires_approver=None,
                        observed=verdict.observed,
                        severity="blocking",
                        entry_id=entry_id,
                    )
                )
            continue

        # C2: ground_transport GROUND rules → advisory never blocking
        if category == "ground_transport" and verdict.applies_to == "car":
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type=(
                        "approval_required" if verdict.requires_approver else "policy_violation"
                    ),
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=verdict.reason + " [advisory: vehicle_class unknown]",
                    requires_approver=verdict.requires_approver,
                    observed=verdict.observed,
                    severity="advisory",
                    entry_id=entry_id,
                )
            )
            continue

        if not verdict.in_policy:
            failing_verdict_count += 1
            justifiable = is_justifiable(verdict)
            applied = justified_text if justifiable else None
            pending = pending_verification(verdict) if justifiable else None
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type="policy_violation",
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=verdict.reason,
                    requires_approver=verdict.requires_approver,
                    observed=verdict.observed,
                    severity="advisory" if (applied or pending) else "blocking",
                    entry_id=entry_id,
                    justifiable=justifiable,
                    justification=applied,
                    pending_verification=pending,
                )
            )
        elif verdict.observed.get(entry_rules.ADVISORY):
            # Product gap 2: in policy only through a clause's own allowance (LOD-02:
            # <= 15% over the nightly cap, booked through the tool) — shown, never blocking.
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type=POLICY_ADVISORY,
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=verdict.reason,
                    requires_approver=None,
                    observed=verdict.observed,
                    severity="advisory",
                    entry_id=entry_id,
                )
            )
        elif verdict.requires_approver:
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type="approval_required",
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=verdict.reason,
                    requires_approver=verdict.requires_approver,
                    observed=verdict.observed,
                    severity="advisory",
                    entry_id=entry_id,
                )
            )
        elif verdict.observed.get(entry_rules.COULD_NOT_VERIFY):
            # Product gap 2: the rule applies but the evidence it needs is missing —
            # an honest "could not verify" advisory (never a silent pass).  Review
            # round 4: when the reason promises an approver will check the evidence
            # (MEAL-04 keyword only, GRND-01 / 03 / 05 unverified) the flag carries
            # that approver, so the approval agent never auto-approves past it.
            # MEAL-02's unverified travel day promises no check and stays plain.
            check = verdict.observed.get(entry_rules.APPROVER_CHECK)
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type="not_evaluated",
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=verdict.reason,
                    requires_approver=check if isinstance(check, str) and check else None,
                    observed=verdict.observed,
                    severity="advisory",
                    entry_id=entry_id,
                )
            )
        elif not verdict.observed:
            # Skip verdict (observed=={}) → not_evaluated advisory
            flags.append(
                PreSubmitFlag(
                    entry_ref=ref,
                    flag_type="not_evaluated",
                    rule_id=verdict.rule_id,
                    rule_type=verdict.rule_type,
                    source_clause_ref=verdict.source_clause_ref,
                    reason=f"Rule {verdict.rule_type} could not be evaluated: {verdict.reason}",
                    requires_approver=None,
                    observed={},
                    severity="advisory",
                    entry_id=entry_id,
                )
            )

    return flags, failing_verdict_count


def _is_hotel_segment(segment: object | None) -> bool:
    """True only for a HOTEL trip segment (LOD-02: a stay booked through the tool).

    Accepts the ORM enum (``SegmentType.hotel``) or its plain value; anything else —
    no segment, a flight / car / rail segment, an unknown type — is not (fail closed).
    """
    if segment is None:
        return False
    seg_type = getattr(segment, "segment_type", None)
    return getattr(seg_type, "value", seg_type) == "hotel"


def is_planner_booked_hotel(segment: object | None, trip: object | None) -> bool:
    """True only for a HOTEL segment the trip PLANNER booked (LOD-02, review round 4).

    The trip must carry the server-set planner ``recommendation_context`` (POST /trips
    always writes None; no API lets a client set it) and the segment — of THAT trip —
    must name, in ``details.option_id``, one of the options the planner selected for
    it.  ``details`` alone proves nothing: it is client-writable on POST /trips.
    Anything else (a typed-in segment, a non-hotel segment, a foreign option) -> False.
    """
    if not _is_hotel_segment(segment) or trip is None:
        return False
    trip_id = getattr(trip, "id", None)
    if trip_id is None or getattr(segment, "trip_id", None) != trip_id:
        return False
    ctx = getattr(trip, "recommendation_context", None)
    if not isinstance(ctx, dict) or ctx.get("agent_task") != "trip_planning":
        return False
    selected = ctx.get("selected_option_ids")
    if not isinstance(selected, list):
        return False
    details = getattr(segment, "details", None)
    option_id = details.get("option_id") if isinstance(details, dict) else None
    if not isinstance(option_id, str) or not option_id.strip():
        return False
    return option_id in {str(x) for x in selected if x}


def build_eval_item(entry: DraftEntry, *, segment: object | None = None, idx: int = 0) -> EvalItem:
    """Build an EvalItem for a DraftEntry, implementing C1/C2 collision logic.

    C1: Hotel lodging amount_usd = per-night rate (amount / nights).
    C2: ground_transport → "car" ONLY if vehicle_class is known; else "receipt".
    """
    category = entry.category

    if category == "airfare":
        cabin_class = None
        duration_hours = None
        supplier = None
        if segment is not None:
            details = getattr(segment, "details", {}) or {}
            cabin_class = details.get("cabin_class")
            duration_hours = details.get("duration_hours")
            supplier = getattr(segment, "supplier", None)
        return EvalItem(
            item_type="flight",
            amount_usd=entry.amount,
            supplier=supplier,
            cabin_class=cabin_class,
            duration_hours=duration_hours,
            ref_id=_entry_ref(entry, idx),
        )

    if category == "lodging":
        # C1: per-night rate
        nights: int | None = None
        city: str | None = None
        if segment is not None:
            details = getattr(segment, "details", {}) or {}
            nights = details.get("nights")
            city = getattr(segment, "location", None)
            if nights is None:
                # Try to compute from datetime span
                start_str = getattr(segment, "start_datetime", None)
                end_str = getattr(segment, "end_datetime", None)
                if start_str and end_str:
                    try:
                        from datetime import date as _date

                        s = _date.fromisoformat(str(start_str)[:10])
                        e = _date.fromisoformat(str(end_str)[:10])
                        computed = (e - s).days
                        if computed > 0:
                            nights = computed
                    except (ValueError, TypeError):
                        pass
        nights_unknown = nights in (0, None)
        if nights_unknown:
            # Advisory flag handled separately — evaluate full amount
            amount_usd = entry.amount
        else:
            amount_usd = entry.amount / nights  # type: ignore[operator]
        return EvalItem(
            item_type="hotel",
            amount_usd=amount_usd,
            city=city,
            nights=nights,
            ref_id=_entry_ref(entry, idx),
            # LOD-02 (review round 4): never granted here.  Only the trusted call sites
            # set booked_through_tool — the planner ranking, trip approval for a
            # planner-booked hotel segment (is_planner_booked_hotel) and
            # build_entry_eval_item after the receipt hotel + city evidence check.
        )

    if category == "meals":
        return EvalItem(
            item_type="meals",
            amount_usd=entry.amount,
            ref_id=_entry_ref(entry, idx),
            item_text=entry_context.join_text(entry.vendor, entry.description),
            spend=entry_spend_facts(entry),
        )

    if category in SPEND_CATEGORIES:
        # Product gap 2: mileage / entertainment / gift reach applies_to="all"
        # rules only (their own spend rules + RECEIPT-REQ, which exempts mileage).
        return EvalItem(
            item_type="receipt",
            amount_usd=entry.amount,
            expense_category=category,
            ref_id=_entry_ref(entry, idx),
            spend=entry_spend_facts(entry),
        )

    if category == "ground_transport":
        # C2: only map to "car" if vehicle_class is genuinely known
        vehicle_class: str | None = None
        if segment is not None:
            details = getattr(segment, "details", {}) or {}
            vehicle_class = details.get("vehicle_class")
        if vehicle_class:
            return EvalItem(
                item_type="car",
                amount_usd=entry.amount,
                vehicle_class=vehicle_class,
                ref_id=_entry_ref(entry, idx),
            )
        # No vehicle_class → evaluate as receipt (gets applies_to=all rules only)
        return EvalItem(
            item_type="receipt",
            amount_usd=entry.amount,
            expense_category="ground_transport",
            ref_id=_entry_ref(entry, idx),
            # Product gap 8: the ground sub-type (rideshare / rail / parking ...).
            **_ground_item_fields(entry, segment),
        )

    # other
    return EvalItem(
        item_type="receipt",
        amount_usd=entry.amount,
        expense_category="other",
        ref_id=_entry_ref(entry, idx),
    )


def _segment_hours(segment: object | None) -> float | None:
    start = getattr(segment, "start_datetime", None)
    end = getattr(segment, "end_datetime", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
        return None
    return round((end - start).total_seconds() / 3600, 2)


def _ground_item_fields(
    entry: DraftEntry,
    segment: object | None,
    *,
    extra_texts: Sequence[str | None] = (),
    vendor_edited: bool = False,
) -> dict[str, Any]:
    """Ground-transport EvalItem fields (product gap 8) from the entry's words + segment.

    ``extra_texts``: the card merchant and the words the RECEIPT printed (OCR vendor and
    line items, ``entry_context.receipt_texts``) — evidence the claimant cannot retype,
    so they are the TRUSTED texts; the entry's vendor / description are the claimant's.
    """
    seg_type = getattr(getattr(segment, "segment_type", None), "value", None) or getattr(
        segment, "segment_type", None
    )
    details = getattr(segment, "details", None) or {}
    if not isinstance(details, dict):
        details = {}
    seg_class = next(
        (
            details[k]
            for k in ("travel_class", "fare_class", "class", "cabin_class")
            if isinstance(details.get(k), str)
        ),
        None,
    )
    hours = details.get("duration_hours")
    seg_hours = (
        float(hours) if isinstance(hours, int | float) and not isinstance(hours, bool) else None
    )
    return entry_context.ground_fields(
        entry.vendor,
        entry.description,
        trusted_texts=tuple(extra_texts),
        segment_type=str(seg_type) if isinstance(seg_type, str) else None,
        segment_hours=seg_hours if seg_hours else _segment_hours(segment),
        segment_class=seg_class,
        vendor_edited=vendor_edited,
    )


def _receipt_words(fields: dict[str, Any], provenance: Any) -> tuple[str | None, ...]:
    """Trusted receipt words for the ground classifier: the words ``with_receipt_evidence``
    captured, else the OCR line-item descriptions."""
    captured = fields.get(entry_context.RECEIPT_WORDS_KEY)
    if isinstance(captured, tuple | list):
        return tuple(w for w in captured if isinstance(w, str))
    return tuple(d for d, _ in entry_context.receipt_line_items(fields, provenance) or ())


def entry_spend_facts(entry: DraftEntry) -> Any:
    """Product gap 2: the entry's attendees / disclosure / mileage + exact amount."""
    return spend_facts(
        category=entry.category,
        amount=entry.amount,
        attendee_count=entry.attendee_count,
        disclosure=entry.disclosure,
        mileage=entry.mileage,
        currency=entry.currency,
    )


def _with_spend(entry: DraftEntry, item: EvalItem) -> EvalItem:
    """Attach the spend facts (product gap 2) to an item built from evidence.

    A client-entertainment line has no verified city (it is not a trip meal), so —
    like a meals line — the claimant's description / vendor ride along as an
    UNVERIFIED city claim (wording / escalation only, never clears ENT-02).
    """
    updates: dict[str, Any] = {"spend": entry_spend_facts(entry)}
    if item.expense_category == "entertainment" and item.city is None:
        text = _claim_text(entry)
        if text and not item.claimed_city_text:
            updates["claimed_city_text"] = text
    return dataclasses.replace(item, **updates)


def _is_team_meal_entry(entry: DraftEntry, cover_minimum: int | None) -> bool:
    """A meals line an active per-head cap governs instead of the per diem.

    *cover_minimum* is ``team_meal_cap_minimum`` of the org's active rules; None
    (no per-head cap for meals) -> never a team meal: the line stays in the
    per-diem day total whatever attendee count it claims (fail closed).
    """
    count = entry.attendee_count
    return (
        cover_minimum is not None
        and entry.category == "meals"
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= cover_minimum
    )


def _team_meal_cover_minimum(context: AgentContext, session: Session) -> int | None:
    """``team_meal_cap_minimum`` of the org's ACTIVE rules (org-scoped via the policy).

    None on a lookup failure: team meals then count toward the per-diem total
    (the more restrictive outcome)."""
    stmt = (
        sa.select(PolicyRule.rule_type, PolicyRule.applies_to, PolicyRule.definition)
        .join(Policy, PolicyRule.policy_id == Policy.id)
        .where(
            Policy.organization_id == context.organization_id,
            PolicyRule.status == PolicyRuleStatus.active,
            PolicyRule.rule_type == PolicyRuleType.per_head_cap,
        )
    )
    try:
        with session.begin_nested():
            rows = session.execute(stmt).all()
    except Exception as exc:  # noqa: BLE001 — fail closed: team meals stay in the total
        logger.warning("presubmit: team-meal cap lookup failed: %s", type(exc).__name__)
        return None
    return team_meal_cap_minimum((r[0], r[1], r[2]) for r in rows)


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion for OCR-extracted numeric fields."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# A per-night figure is derived from money amounts read to the cent.
_NIGHTLY_TOLERANCE = 0.005


@dataclass(frozen=True)
class _LodgingNightly:
    amount_usd: float | None  # the room + tax figure HOTEL-CAP compares by default
    room_only_usd: float | None  # room subtotal / nights (legacy ``room_only`` basis)
    implied_nights: int | None  # stay length implied by the OCR per-night rate


# The longest stay an OCR per-night rate may imply (security review): a tiny rate
# must never turn a folio into hundreds of "nights" and shrink the nightly figure.
MAX_IMPLIED_NIGHTS = 30


def _implied_nights(rate: float, base: float) -> int | None:
    """Nights for which *rate* x nights == *base* to the cent per night, else None.

    Only a plausible stay (1..MAX_IMPLIED_NIGHTS) counts; anything else returns
    None so the caller compares the full line amount (fail closed).
    """
    if rate <= 0 or base <= 0 or rate * MAX_IMPLIED_NIGHTS < base - _NIGHTLY_TOLERANCE:
        return None
    n = round(base / rate)
    if 1 <= n <= MAX_IMPLIED_NIGHTS and abs(n * rate - base) <= _NIGHTLY_TOLERANCE * n:
        return int(n)
    return None


def _lodging_nightly(
    amount: float | None,
    *,
    nights: int | None,
    per_night_rate: float | None,
    subtotal: float | None,
) -> _LodgingNightly:
    """The hotel per-night figures for a lodging line (UAT2 Issue I).

    HOTEL-CAP's canonical ``basis`` is ``room_plus_tax`` (LOD-01: the nightly cap
    covers room + tax), so ``amount_usd`` is the line total incl. tax / nights.  The
    receipt's room rate / room subtotal are ROOM-ONLY figures: they never lower it.

      * nights known -> amount / nights;
      * nights unknown -> the OCR per-night rate only supplies a stay length when
        it divides the room subtotal (or else the total) exactly, to the cent per
        night: amount / implied nights (>= the rate itself).  Otherwise the full
        line amount is compared (fail closed);
      * ``room_only_usd`` (only for a legacy ``room_only`` cap) = subtotal / nights,
        only for a consistent subtotal (0 < subtotal <= amount); else None, and
        the evaluator falls back to ``amount_usd``.

    With no line amount at all, the OCR per-night rate is the only figure there is.
    """
    rate = per_night_rate if per_night_rate is not None and per_night_rate > 0 else None
    if amount is None:
        return _LodgingNightly(amount_usd=rate, room_only_usd=None, implied_nights=None)
    sub = subtotal if subtotal is not None and 0 < subtotal <= amount + _NIGHTLY_TOLERANCE else None
    implied: int | None = None
    if not nights and rate is not None:
        for base in (sub, amount):
            if base is not None:
                implied = _implied_nights(rate, base)
                if implied is not None:
                    break
    divisor = nights or implied
    if not divisor:
        return _LodgingNightly(amount_usd=amount, room_only_usd=None, implied_nights=None)
    return _LodgingNightly(
        amount_usd=amount / divisor,
        room_only_usd=sub / divisor if sub is not None else None,
        implied_nights=implied,
    )


def build_eval_item_from_fields(
    fields: dict[str, Any],
    *,
    category: str,
    ref_id: str,
    provenance: dict[str, Any] | None = None,
) -> EvalItem:
    """``_eval_item_from_fields`` tagged with ``fields["currency"]`` (product gap 8)."""
    item = _eval_item_from_fields(fields, category=category, ref_id=ref_id, provenance=provenance)
    return with_item_currency(item, fields.get("currency"))


def _eval_item_from_fields(
    fields: dict[str, Any],
    *,
    category: str,
    ref_id: str,
    provenance: dict[str, Any] | None = None,
) -> EvalItem:
    """Build an EvalItem directly from a receipt's extracted_fields dict.

    *provenance* is the receipt's field_provenance map; when given, the vendor
    only feeds ``supplier`` if it was read deterministically from the receipt.

    Used by the receipt-upload auto-draft path (no DraftEntry/segment objects
    available). Mirrors build_eval_item's per-category routing, but sources its
    values from the OCR field dict:

      amount        ← fields["amount"]
      city          ← fields["location"]  (hotel amount_cap tier resolution)
      supplier      ← fields["vendor"]    (airfare/lodging only — preferred_supplier)
      nights        ← fields["nights"]       (lodging EXTRA key, when present)
      per_night_rate ← fields["per_night_rate"] (lodging EXTRA key, when present)
      subtotal      ← fields["subtotal"]     (OCR EXTRA key: the pre-tax room total)

    C1 (lodging): amount_usd is the PER-NIGHT figure — see ``_lodging_nightly``
    (UAT2 Issue I: the line total incl. tax / nights, the canonical HOTEL-CAP
    ``room_plus_tax`` basis; the OCR room-only rate never lowers it).
    """
    amount = _coerce_float(fields.get("amount"))
    location = fields.get("location")
    city = location if isinstance(location, str) and location else None
    # The receipt vendor is the airline / hotel brand for airfare and lodging —
    # feeds the preferred_supplier rules (AIR-PREF / HOTEL-PREF). Trust boundary:
    # only a vendor READ from the receipt (provenance 'deterministic') may clear
    # the rule. A vendor guessed by LLM gap-fill ('ai_inferred') or typed by the
    # claimant ('user_edited') is not evidence, so the item is evaluated without a
    # supplier (blocking for AIR-PREF -> approver review).
    vendor = fields.get("vendor")
    vendor_trusted = provenance is None or provenance.get("vendor") == "deterministic"
    supplier = (
        vendor.strip() if vendor_trusted and isinstance(vendor, str) and vendor.strip() else None
    )
    # An untrusted vendor is still reported (reason text only) as what was claimed.
    claimed_supplier = (
        vendor.strip() if supplier is None and isinstance(vendor, str) and vendor.strip() else None
    )

    if category == "lodging":
        nights_raw = fields.get("nights")
        nights: int | None
        try:
            nights = int(nights_raw) if nights_raw is not None else None
        except (TypeError, ValueError):
            nights = None

        if nights is not None and nights < 1:
            nights = None
        # The receipt's room rate / subtotal only describe an OCR-read total: once
        # the total was typed / edited / inferred (or they were) they are dropped.
        prov = provenance or {}
        amount_read = prov.get("amount", _DETERMINISTIC) == _DETERMINISTIC

        def _ocr_extra(key: str) -> float | None:
            if not amount_read or prov.get(key, _DETERMINISTIC) != _DETERMINISTIC:
                return None
            return _coerce_float(fields.get(key))

        nightly = _lodging_nightly(
            amount,
            nights=nights,
            per_night_rate=_ocr_extra("per_night_rate"),
            subtotal=_ocr_extra("subtotal"),
        )
        return EvalItem(
            item_type="hotel",
            amount_usd=nightly.amount_usd,
            supplier=supplier,
            city=city,
            nights=nights,
            ref_id=ref_id,
            claimed_supplier=claimed_supplier,
            # Wording only: a per-night figure is never described as the full amount.
            amount_is_nightly_rate=nightly.implied_nights is not None,
            stay_amount_usd=amount if nightly.implied_nights is not None else None,
            room_only_nightly_usd=nightly.room_only_usd,
        )

    if category == "airfare":
        return EvalItem(
            item_type="flight",
            amount_usd=amount,
            supplier=supplier,
            ref_id=ref_id,
            claimed_supplier=claimed_supplier,
        )

    if category == "meals":
        return EvalItem(
            item_type="meals",
            amount_usd=amount,
            ref_id=ref_id,
            # Product gap 2: exclusions (MEAL-04) read the receipt's own line items.
            item_text=entry_context.join_text(vendor),
            line_items=entry_context.receipt_line_items(fields, provenance),
            # Review round 3: the receipt's own total (``with_receipt_evidence``), never
            # the claim — a claim may leave the wine out.
            receipt_total_usd=_coerce_float(fields.get(entry_context.RECEIPT_TOTAL_KEY)),
            receipt_shared=fields.get(entry_context.RECEIPT_SHARED_KEY) is True,
        )

    if category == "ground_transport":
        # No vehicle_class available from a receipt → evaluate as receipt
        # (applies_to="all" rules only; mirrors build_eval_item's C2 fallback).
        return EvalItem(
            item_type="receipt",
            amount_usd=amount,
            expense_category="ground_transport",
            ref_id=ref_id,
            line_items=entry_context.receipt_line_items(fields, provenance),
            tip_usd=entry_context.receipt_tip(fields, provenance),
            parking_days=entry_context.receipt_parking_days(fields, provenance),
            receipt_total_usd=_coerce_float(fields.get(entry_context.RECEIPT_TOTAL_KEY)),
            receipt_shared=fields.get(entry_context.RECEIPT_SHARED_KEY) is True,
            # Security review: ``vendor`` here may be the entry's (typed) vendor, so it
            # is the claimant's word; only what ``with_receipt_evidence`` captured from
            # the receipt itself (plus its OCR line items) is trusted.
            **entry_context.ground_fields(
                vendor if isinstance(vendor, str) else None,
                trusted_texts=_receipt_words(fields, provenance),
                vendor_edited=fields.get(entry_context.RECEIPT_VENDOR_LOST_KEY) is True,
            ),
        )

    # other (product gap 2: a manual-only spend category keeps its own category)
    return EvalItem(
        item_type="receipt",
        amount_usd=amount,
        expense_category=category if category in SPEND_CATEGORIES else "other",
        ref_id=ref_id,
    )


class PolicyEvaluationIncomplete(Exception):
    """Strict-mode signal: the policy engine did not evaluate every entry."""


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def same_supplier(a: Any, b: Any) -> bool:
    """True when two non-blank vendor strings name the same supplier (C7R).

    Compares the normalized brand key of ``policy_eval.normalize_supplier_name``
    (case, punctuation and generic / legal tokens such as AIRLINES, AIR LINES,
    AIRWAYS, INC, CORP, CO removed), so the receipt's 'UNITED' or
    'UNITED AIRLINES INC' equals the claimed 'United Airlines'.  Token-exact, never
    substring: 'UNITEDX', 'UNITED BISTRO' and 'SKYBRIDGE AIR' do not match.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    a_key, b_key = normalize_supplier_name(a.strip()), normalize_supplier_name(b.strip())
    return bool(a_key) and a_key == b_key


# A segment's supplier / city / nights are typed by whoever created the trip
# (POST /trips lets an employee create a DRAFT trip with free-text segments).
# Only an itinerary that a HUMAN approver signed off on may enrich an entry's
# policy evaluation: the trip must be approved / completed AND its approval row
# must carry a human ``approved`` decision.  An AUTO-approved trip (moved to
# ``approved`` by the agent with no human looking at the claimant-typed nights /
# city / supplier), a legacy trip with no approval row (e.g. self-approved via
# the pre-C7-02 PATCH), and draft / pending / rejected / cancelled trips are all
# ignored.
_TRUSTED_TRIP_STATUSES = frozenset({TripStatus.approved, TripStatus.completed})


def _trip_human_approved(trip: Trip, *, session: Session) -> bool:
    """True iff the trip's approval row records a HUMAN ``approved`` decision.

    ``auto_approved`` (agent decision) does not count.  Lookup failure → False
    (fail closed: the segment is then not trusted).
    """
    try:
        approval_id = session.scalar(
            sa.select(Approval.id)
            .where(
                Approval.organization_id == trip.organization_id,
                Approval.subject_type == ApprovalSubjectType.trip,
                Approval.subject_id == trip.id,
                Approval.current_status == ApprovalStatus.approved,
            )
            .limit(1)
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("presubmit: trip approval lookup failed for %s: %s", trip.id, exc)
        return False
    return approval_id is not None


def _load_owned_segment(
    segment_id: Any,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> TripSegment | None:
    """Load the entry's trip segment iff it is on *owner_id*'s APPROVED/COMPLETED
    trip in this org whose approval was decided by a human.

    Anything else (missing, other org, someone else's trip, a draft / pending /
    rejected / cancelled trip, an auto-approved or approval-less trip, lookup
    error) → None, so the entry is evaluated
    from its own fields instead — never from claimant-typed itinerary data that
    nobody signed off on.
    """
    sid = _as_uuid(segment_id)
    if sid is None:
        return None
    try:
        segment = session.get(TripSegment, sid)
        if not isinstance(segment, TripSegment):
            return None
        trip = session.get(Trip, segment.trip_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("presubmit: segment lookup failed for %s: %s", sid, exc)
        return None
    if not isinstance(trip, Trip) or trip.organization_id != context.organization_id:
        return None
    if owner_id is not None and trip.traveler_id != owner_id:
        return None
    if trip.status not in _TRUSTED_TRIP_STATUSES:
        return None
    if not _trip_human_approved(trip, session=session):
        return None
    return segment


def _load_owned_receipt(
    receipt_id: Any,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> Receipt | None:
    rid = _as_uuid(receipt_id)
    if rid is None:
        return None
    try:
        receipt = session.get(Receipt, rid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("presubmit: receipt lookup failed for %s: %s", rid, exc)
        return None
    if not isinstance(receipt, Receipt) or receipt.organization_id != context.organization_id:
        return None
    if owner_id is not None and receipt.uploaded_by != owner_id:
        return None
    return receipt


def _load_owned_card_txn(
    txn_id: Any,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> CardTransaction | None:
    tid = _as_uuid(txn_id)
    if tid is None:
        return None
    try:
        txn = session.get(CardTransaction, tid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("presubmit: card txn lookup failed for %s: %s", tid, exc)
        return None
    if not isinstance(txn, CardTransaction) or txn.organization_id != context.organization_id:
        return None
    if owner_id is not None and txn.user_id != owner_id:
        return None
    return txn


_DETERMINISTIC = "deterministic"
_USER_EDITED = "user_edited"
_AI_INFERRED = "ai_inferred"
# Receipt-amount provenances that are not a reading of the receipt image (C7R).
_NOT_RECEIPT_EVIDENCE = frozenset({_USER_EDITED, _AI_INFERRED})


@dataclass(frozen=True)
class _EntryEvidence:
    """Facts about an entry read from records the claimant cannot type into.

    vendors: deterministic OCR receipt vendor and/or the card-transaction
        merchant (both owned by the report owner, same org).
    city / nights: deterministic OCR receipt location / OCR-extracted nights.
    vendor_edited / city_edited: the linked receipt's vendor / location was
        overwritten by the claimant (PATCH /receipts → 'user_edited').  The
        original OCR value is gone, so it may have contradicted the trip
        segment; the segment value is then NOT trusted for that field.
    vendor_lost: the vendor was edited AND no OCR snapshot of it is left (legacy row):
        the ground classifier has no machine reading of the vendor at all.
    """

    vendors: tuple[str, ...] = ()
    city: str | None = None
    nights: int | None = None
    vendor_edited: bool = False
    city_edited: bool = False
    vendor_lost: bool = False


def _positive_int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _same_city(a: str, b: str) -> bool:
    """Compare the city part ("New York, NY" ~ "new york" ~ "NEW YORK NY 10017").

    Shared normalisation (``config.constants.city_names``): an OCR'd receipt
    location without a comma must not read as a different city from the segment
    and so drop the verified city to the lowest tier.  Exact on the city part —
    aliases ("NYC") are a tier concern, never a same-city match here."""
    return same_city(a, b)


# ---------------------------------------------------------------------------
# Issue 6 — claimed stay length (description) and claimed (untrusted) segments
# ---------------------------------------------------------------------------

_COUNT_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
}
_COUNT_TOKEN = r"(\d{1,2}|" + "|".join(_COUNT_WORDS) + r")"
# "3 nights", "3-night", "three nights" — never "289.00/night" or "per night".
NIGHTS_RE = re.compile(rf"\b{_COUNT_TOKEN}\s*-?\s*nights?\b", re.IGNORECASE)
DAYS_RE = re.compile(rf"\b{_COUNT_TOKEN}\s*-?\s*days?\b", re.IGNORECASE)
MAX_CLAIMED_NIGHTS = 60
MAX_CLAIMED_DAYS = 31
MAX_CLAIMED_PARKING_DAYS = 60


def claimed_count(text: Any, pattern: re.Pattern[str], maximum: int) -> int | None:
    """The single stay length stated in *text* ("3 nights" -> 3), else None.

    Values outside [1, maximum] are ignored; two different values ("2 nights +
    1 night") are ambiguous -> None.  A CLAIM only: it never clears a rule.
    """
    if not isinstance(text, str) or not text:
        return None
    values: set[int] = set()
    for match in pattern.finditer(text):
        token = match.group(1).lower()
        n = int(token) if token.isdigit() else _COUNT_WORDS[token]
        if 1 <= n <= maximum:
            values.add(n)
    return values.pop() if len(values) == 1 else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def segment_nights(segment: Any) -> int | None:
    """Nights of a hotel segment: details.nights, else check-in/out, else its datetimes."""
    if segment is None:
        return None
    details = getattr(segment, "details", None) or {}
    if not isinstance(details, dict):
        details = {}
    nights = _positive_int(details.get("nights"))
    if nights:
        return nights
    for start_raw, end_raw in (
        (details.get("check_in_date"), details.get("check_out_date")),
        (getattr(segment, "start_datetime", None), getattr(segment, "end_datetime", None)),
    ):
        start, end = _as_date(start_raw), _as_date(end_raw)
        if start and end and (end - start).days > 0:
            return (end - start).days
    return None


def segment_days(segment: Any) -> int | None:
    """Calendar days a segment spans (inclusive): 15:00 Mon -> 11:00 Thu = 4 days."""
    if segment is None:
        return None
    start = _as_date(getattr(segment, "start_datetime", None))
    end = _as_date(getattr(segment, "end_datetime", None))
    if start is None or end is None or end < start:
        return None
    return (end - start).days + 1


def _load_claimed_segment(
    segment_id: Any,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> TripSegment | None:
    """The entry's segment on *owner_id*'s own trip in this org, WITHOUT the trust checks.

    Its nights / city are what the claimant planned (an unapproved or auto-approved
    trip): CLAIMS — wording and escalation only, never a verified value.
    """
    sid = _as_uuid(segment_id)
    if sid is None:
        return None
    try:
        segment = session.get(TripSegment, sid)
        if not isinstance(segment, TripSegment):
            return None
        trip = session.get(Trip, segment.trip_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("presubmit: claimed-segment lookup failed for %s: %s", sid, exc)
        return None
    if not isinstance(trip, Trip) or trip.organization_id != context.organization_id:
        return None
    if owner_id is not None and trip.traveler_id != owner_id:
        return None
    return segment


def _claim_text(entry: DraftEntry) -> str | None:
    """The claimant's free text that may name a city (description / vendor)."""
    parts = [t.strip() for t in (entry.description, entry.vendor) if isinstance(t, str)]
    text = " | ".join(p for p in parts if p)
    return text or None


def _with_claims(entry: DraftEntry, item: EvalItem, *, claimed_segment: Any) -> EvalItem:
    """Attach the claimant's UNVERIFIED stay length / city to a hotel or meals item.

    Hotel: claimed nights = the smaller of the description's "N nights" and the
    nights of the entry's own but untrusted segment (only when no nights were
    verified).  Meals: claimed days from the description (or the untrusted
    segment's days) when no days were verified.  City (both, only when no city was
    verified): the untrusted segment's location as ``claimed_city`` and the
    description / vendor as ``claimed_city_text``.  Wording / escalation only.
    """
    updates: dict[str, Any] = {}
    if item.item_type == "hotel":
        if not _positive_int(item.nights) and not item.amount_is_nightly_rate:
            nights = [
                n
                for n in (
                    claimed_count(entry.description, NIGHTS_RE, MAX_CLAIMED_NIGHTS),
                    segment_nights(claimed_segment),
                )
                if n
            ]
            if nights:
                updates["claimed_nights"] = min(nights)
    elif item.item_type == "meals":
        if not _positive_int(item.days):
            days = [
                d
                for d in (
                    claimed_count(entry.description, DAYS_RE, MAX_CLAIMED_DAYS),
                    segment_days(claimed_segment),
                )
                if d
            ]
            if days:
                updates["claimed_days"] = min(days)
    else:
        return item
    if item.city is None:
        location = getattr(claimed_segment, "location", None)
        if not item.claimed_city and isinstance(location, str) and location.strip():
            updates["claimed_city"] = location.strip()
        text = _claim_text(entry)
        if text:
            updates["claimed_city_text"] = text
    return dataclasses.replace(item, **updates) if updates else item


def _trip_meal_scope(
    segment: TripSegment, *, session: Session
) -> tuple[set[uuid.UUID], int] | None:
    """(segment ids, covered calendar days) of the WHOLE trip *segment* belongs to.

    Meal per diem is per travel DAY, not per segment: meals linked to overlapping
    segments of one trip (a hotel plus a flight on its first day) must share one
    allowance for that day.  Days are the UNION of the calendar days covered by the
    trip's dated segments, so overlaps are counted once (security review, Issue 6).
    None when the trip's segments cannot be read (-> fail closed: the line is
    compared with one day's rate).
    """
    try:
        with session.begin_nested():
            segments = list(
                session.scalars(
                    sa.select(TripSegment).where(TripSegment.trip_id == segment.trip_id)
                ).all()
            )
    except Exception as exc:  # noqa: BLE001 — fail closed: no per-day average
        logger.warning("presubmit: trip meal scope lookup failed: %s", type(exc).__name__)
        return None
    if not any(getattr(s, "id", None) == segment.id for s in segments):
        segments.append(segment)
    covered: set[date] = set()
    for seg in segments:
        start = _as_date(getattr(seg, "start_datetime", None))
        end = _as_date(getattr(seg, "end_datetime", None))
        if start is None or end is None or end < start:
            continue
        covered.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    if not covered:
        return None
    return {seg.id for seg in segments}, len(covered)


def _trip_span_days(segment: TripSegment, *, session: Session) -> int | None:
    """Calendar days of the approved trip *segment* belongs to, from the TRIP's own start /
    end dates (inclusive) — not the union of its segments' dates (security review round
    3).  GRND-03 reason context only, never parking days.  None when unreadable."""
    try:
        trip = session.get(Trip, segment.trip_id)
    except Exception as exc:  # noqa: BLE001 — context only: omit it
        logger.warning("presubmit: trip span lookup failed: %s", type(exc).__name__)
        return None
    if not isinstance(trip, Trip):
        return None
    start, end = _as_date(trip.start_date), _as_date(trip.end_date)
    if start is None or end is None or end < start:
        return None
    return (end - start).days + 1


def _segment_meal_total(
    entry: DraftEntry,
    segment_ids: set[uuid.UUID],
    *,
    peers: Sequence[DraftEntry],
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> float | None:
    """Every meals amount its traveler claimed against ANY of *segment_ids* — all
    segments of one trip (Issue 6; see ``_trip_meal_scope``).

    The trip's covered days give a per-DAY allowance; it must cover ALL meals
    lines linked to that trip's segments — this entry, the other lines being evaluated with
    it (``peers``) and the owner's persisted lines on any non-rejected report —
    never each line separately (that would multiply the allowance per line).
    None when the total cannot be established (-> the line is evaluated alone
    against one day's rate: fail closed).
    """
    if owner_id is None:
        return None
    amount = _coerce_float(entry.amount)
    if amount is None:
        return None
    total = amount
    cover = _team_meal_cover_minimum(context, session)
    known_ids: set[uuid.UUID] = set()
    own_id = _as_uuid(entry.entry_id)
    if own_id is not None:
        known_ids.add(own_id)
    for peer in peers:
        if peer is entry:
            continue
        peer_id = _as_uuid(peer.entry_id)
        if peer_id is not None:
            if peer_id in known_ids:
                continue
            known_ids.add(peer_id)
        if (
            peer.category == "meals"
            and not _is_team_meal_entry(peer, cover)  # product gap 2: MEAL-05, not per diem
            and _as_uuid(peer.trip_segment_id) in segment_ids
        ):
            if not _same_currency(peer.currency, entry.currency):
                return None  # product gap 8: never add amounts in different currencies
            peer_amount = _coerce_float(peer.amount)
            if peer_amount is None:
                return None
            total += peer_amount
    # Product gap 8: a persisted meals line in another currency cannot be summed
    # with this one -> NULL -> no per-day average (fail closed: one day's rate).
    foreign_lines = sa.func.count().filter(
        sa.func.upper(sa.func.trim(Expense.currency)) != str(entry.currency or "").strip().upper()
    )
    stmt = (
        sa.select(
            sa.case(
                (foreign_lines > 0, sa.null()),
                else_=sa.func.coalesce(sa.func.sum(Expense.amount), 0),
            )
        )
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            Expense.organization_id == context.organization_id,
            ExpenseReport.organization_id == context.organization_id,
            ExpenseReport.owner_id == owner_id,
            ExpenseReport.status != ExpenseReportStatus.rejected,
            Expense.trip_segment_id.in_(segment_ids),
            Expense.category == ExpenseCategory.meals,
        )
    )
    if cover is not None:
        # Product gap 2: a team meal an active per-head cap governs (MEAL-05) never
        # counts toward the claimant's own per-diem day total.
        stmt = stmt.where(sa.or_(Expense.attendee_count.is_(None), Expense.attendee_count < cover))
    if known_ids:
        stmt = stmt.where(Expense.id.notin_(known_ids))
    try:
        with session.begin_nested():
            others = session.scalar(stmt)
    except Exception as exc:  # noqa: BLE001 — fail closed: no per-day average
        logger.warning("presubmit: segment meal total lookup failed: %s", type(exc).__name__)
        return None
    if others is None:
        return None  # a line in another currency (product gap 8)
    other_total = _coerce_float(others)
    return None if other_total is None else total + other_total


def _same_currency(a: Any, b: Any) -> bool:
    return str(a or "").strip().upper() == str(b or "").strip().upper()


def card_merchant_brand(merchant: str) -> str:
    """The brand part of a card-statement merchant descriptor (C7R).

    Card feeds pad the merchant name with a ticket / reference number and often a
    location after it ('UNITED AIRLINES 0162345678 HOUSTON TX').  Everything from the
    first whitespace-separated word that contains a digit onwards is descriptor
    noise, so the brand is the words before it ('UNITED AIRLINES').  A descriptor
    with no digit, or one that STARTS with a digit, is returned unchanged.

    Deliberately narrow: no prefix / substring matching and no city stripping, so
    'UNITED NIGERIA AIRLINES' stays a different supplier than 'UNITED' (fail closed).
    """
    words = merchant.split()
    for i, word in enumerate(words):
        if any(ch.isdigit() for ch in word):
            return " ".join(words[:i]) if i > 0 else merchant.strip()
    return " ".join(words)


def _verified_supplier(
    claimed: str | None, evidence_vendors: list[str]
) -> tuple[str | None, str | None]:
    """(supplier, conflicting_evidence) from the trusted records (C7R).

    The supplier is the evidence vendor iff every trusted record names the same
    supplier and the claim is blank or names it too.  Otherwise the supplier is
    unverified (None) and the second value lists what the records say (None when
    there are no records at all).
    """
    if not evidence_vendors:
        return None, None
    first = evidence_vendors[0]
    consistent = all(same_supplier(first, v) for v in evidence_vendors[1:])
    if consistent and (claimed is None or same_supplier(claimed, first)):
        return first, None
    distinct: list[str] = []
    for v in evidence_vendors:
        if not any(same_supplier(v, d) for d in distinct):
            distinct.append(v)
    return None, " / ".join(distinct)


def _fields_eval_item(
    entry: DraftEntry,
    *,
    idx: int,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> tuple[EvalItem, _EntryEvidence]:
    """EvalItem from the entry's own fields (no segment) — upload-path semantics.

    * amount / category / vendor come from the entry (what is being claimed);
    * location / nights / per-night rate come from the entry's linked receipt,
      and ONLY when the receipt read them itself: location must have
      field_provenance 'deterministic' (an 'ai_inferred' location is grounded on
      the claimant's own itinerary and a 'user_edited' one is typed by them);
      nights must be an OCR value (no provenance entry, or 'deterministic');
      the OCR per-night rate is used only while the receipt amount is still the
      deterministic OCR amount AND equals the claimed amount.  Otherwise city is
      unknown (→ lowest, domestic cap) and the full amount is evaluated;
    * ``supplier`` comes ONLY from trusted evidence — a vendor READ from the
      linked receipt (field_provenance 'deterministic') and/or the merchant on
      the owner's linked card transaction.  When all such records name the same
      supplier (``same_supplier``: 'UNITED' == 'United Airlines') and the claimed
      vendor is blank or names that supplier too, the evidence vendor is the
      supplier.  A typed / edited / AI-inferred vendor is not evidence: with no
      trusted record, or records that contradict the claim or each other, the
      supplier is None (AIR-PREF stays BLOCKING) and the claim / conflicting
      evidence ride along in ``claimed_supplier`` / ``supplier_evidence`` so the
      flag reason is honest (C7R) instead of "Supplier ''";
    * an unverified receipt location rides along as ``claimed_city`` (reason
      text only — it never selects a cap tier).

    Also returns the entry's evidence (used to cross-check a trip segment).
    """
    fields: dict[str, Any] = {"amount": entry.amount}
    evidence_vendors: list[str] = []
    claimed_city: str | None = None
    evidence_city: str | None = None
    evidence_nights: int | None = None
    vendor_edited = False
    vendor_lost = False
    city_edited = False
    receipt_words: tuple[str, ...] = ()

    receipt = _load_owned_receipt(
        entry.primary_receipt_id, context=context, session=session, owner_id=owner_id
    )
    if receipt is not None:
        rfields: dict[str, Any] = receipt.extracted_fields or {}
        rprov: dict[str, Any] = receipt.field_provenance or {}
        vendor_edited = rprov.get("vendor") == _USER_EDITED
        city_edited = rprov.get("location") == _USER_EDITED
        location = rfields.get("location")
        if (
            rprov.get("location") == _DETERMINISTIC
            and isinstance(location, str)
            and location.strip()
        ):
            evidence_city = location.strip()
            fields["location"] = evidence_city
        elif isinstance(location, str) and location.strip():
            claimed_city = location.strip()
        if rprov.get("nights", _DETERMINISTIC) == _DETERMINISTIC:
            evidence_nights = _positive_int(rfields.get("nights"))
            if evidence_nights is not None:
                fields["nights"] = evidence_nights
        # The OCR per-night rate / room subtotal only describe the claimed amount
        # if the claim still equals the (OCR-read, unedited) receipt total;
        # otherwise derive amount / nights.  Each must itself be an OCR value.
        receipt_amount = _coerce_float(rfields.get("amount"))
        entry_amount = _coerce_float(entry.amount)
        claim_is_ocr_total = (
            rprov.get("amount") == _DETERMINISTIC
            and receipt_amount is not None
            and entry_amount is not None
            and abs(receipt_amount - entry_amount) < 0.005
        )
        for extra in ("per_night_rate", "subtotal"):
            if (
                claim_is_ocr_total
                and rfields.get(extra) is not None
                and rprov.get(extra, _DETERMINISTIC) == _DETERMINISTIC
            ):
                fields[extra] = rfields[extra]
        rvendor = rfields.get("vendor")
        if rprov.get("vendor") == _DETERMINISTIC and isinstance(rvendor, str) and rvendor.strip():
            evidence_vendors.append(rvendor.strip())
        # Product gap 2: receipt line items / tip / parking days READ by OCR.
        line_items = entry_context.receipt_line_items(rfields, rprov)
        if line_items:
            fields["line_items"] = [{"description": d, "amount": a} for d, a in line_items]
        tip = entry_context.receipt_tip(rfields, rprov)
        if tip is not None:
            fields["tip"] = tip
        parking_days = entry_context.receipt_parking_days(rfields, rprov)
        if parking_days is not None:
            fields["parking_days"] = parking_days
        # Product gap 8: what the receipt itself printed also classifies the line.
        # Review round 5: a vendor the claimant edited is replaced by the OCR snapshot
        # (the machine reading the correction keeps); with no snapshot the vendor
        # evidence is lost and the line cannot be verified.
        receipt_words = entry_context.receipt_texts(
            rfields, rprov, grounding_context=receipt.grounding_context
        )
        vendor_lost = vendor_edited and (
            entry_context.snapshot_vendor(receipt.grounding_context) is None
        )
        # Review round 3: the receipt's own total (partial claims: MEAL-04, GRND-05).
        fields[entry_context.RECEIPT_TOTAL_KEY] = entry_context.receipt_total(rfields, rprov)
        # A receipt that also backs other lines (itemized siblings) is not this line's
        # alone: the rest of it may be claimed elsewhere (fail closed).
        fields[entry_context.RECEIPT_SHARED_KEY] = bool(
            entry.is_itemized or entry.sibling_group_id or len(backing_expense_ids(receipt)) > 1
        )

    merchant_text: str | None = None
    if entry.source_card_txn_id:
        txn = _load_owned_card_txn(
            entry.source_card_txn_id, context=context, session=session, owner_id=owner_id
        )
        if txn is not None and isinstance(txn.merchant, str) and txn.merchant.strip():
            # Compare the merchant's brand, not the ticket number / city padding.
            evidence_vendors.append(card_merchant_brand(txn.merchant))
            merchant_text = txn.merchant.strip()

    claimed_vendor = (entry.vendor or "").strip() or None
    supplier, supplier_evidence = _verified_supplier(claimed_vendor, evidence_vendors)
    if supplier is not None:
        fields["vendor"] = supplier
    item = build_eval_item_from_fields(
        fields,
        category=entry.category,
        ref_id=_entry_ref(entry, idx),
        provenance={"vendor": _DETERMINISTIC if supplier is not None else "unverified"},
    )
    claims: dict[str, Any] = {}
    if item.item_type in ("flight", "hotel"):
        if supplier is None:
            claims["claimed_supplier"] = claimed_vendor
            claims["supplier_evidence"] = supplier_evidence
        if item.city is None:
            claims["claimed_city"] = claimed_city
    if item.item_type == "meals":
        # Issue 6: MEALS-PD is tiered by city — the same trust rule as HOTEL-CAP
        # (only a location READ from the receipt selects the tier).
        if evidence_city is not None:
            claims["city"] = evidence_city
        else:
            claims["claimed_city"] = claimed_city
    if any(v is not None for v in claims.values()):
        item = dataclasses.replace(item, **claims)
    if item.item_type == "meals":
        item = dataclasses.replace(
            item, item_text=entry_context.join_text(entry.vendor, entry.description, merchant_text)
        )
    elif item.expense_category == "ground_transport":
        # Product gap 8: classify from the entry's words, the card merchant AND the
        # receipt's own OCR words — a renamed vendor ("Amtrak" typed on an Uber receipt)
        # is then ambiguous -> the broad category, which a rideshare cap still reaches.
        item = dataclasses.replace(
            item,
            **_ground_item_fields(
                entry,
                None,
                extra_texts=(merchant_text, *receipt_words),
                vendor_edited=vendor_lost,
            ),
        )
        if "parking" in item.ground_candidates and item.parking_days is None:
            stated = claimed_count(entry.description, DAYS_RE, MAX_CLAIMED_PARKING_DAYS)
            if stated is not None:
                item = dataclasses.replace(item, claimed_days=stated)
    return item, _EntryEvidence(
        vendors=tuple(evidence_vendors),
        city=evidence_city,
        nights=evidence_nights,
        vendor_edited=vendor_edited,
        city_edited=city_edited,
        vendor_lost=vendor_lost,
    )


def draft_entries_for_expenses(
    expenses: list[Expense],
    *,
    session: Session,
    organization_id: uuid.UUID,
) -> list[DraftEntry]:
    """Persisted expense rows -> DraftEntry, linking each row's receipt / card txn (C7R).

    The ONE mapping used by both the approval-time re-evaluation and
    POST /policy/evaluate, so both see the same evidence: EVERY attached receipt
    (``receipt_ids``, newest first; the newest is ``primary_receipt_id``, UAT2 —
    a correct receipt attached after a wrong one must count; a receipt shared
    with the entry as an itemized sibling comes after its own) and the first
    linked card transaction of each entry, within the organization.  Ownership /
    provenance are enforced later by ``build_entry_eval_item`` /
    ``receipt_support_issue``.
    """
    if not expenses:
        return []
    entry_ids = [e.id for e in expenses]
    # Own receipts newest first, then a receipt SHARED with this entry (itemized
    # siblings split from one folio — UAT2 hardening, ``evidence``).
    receipts_by_entry = receipt_ids_by_entry(
        session, organization_id=organization_id, entry_ids=entry_ids
    )
    txn_by_entry: dict[uuid.UUID, uuid.UUID] = {}
    for t_id, t_exp in session.execute(
        sa.select(CardTransaction.id, CardTransaction.expense_id)
        .where(
            CardTransaction.expense_id.in_(entry_ids),
            CardTransaction.organization_id == organization_id,
        )
        .order_by(CardTransaction.created_at, CardTransaction.id)
    ).all():
        txn_by_entry.setdefault(t_exp, t_id)

    drafts: list[DraftEntry] = []
    for e in expenses:
        receipt_ids = receipts_by_entry.get(e.id, [])
        receipt_id = receipt_ids[0] if receipt_ids else None
        txn_id = txn_by_entry.get(e.id)
        drafts.append(
            DraftEntry(
                category=getattr(e.category, "value", str(e.category)),
                amount=float(str(e.amount)),
                currency=e.currency,
                expense_date=str(e.expense_date),
                vendor=e.vendor,
                description=e.description,
                status=getattr(e.status, "value", str(e.status)),
                match_source=getattr(e.match_source, "value", None),
                trip_segment_id=str(e.trip_segment_id) if e.trip_segment_id else None,
                is_itemized=bool(e.is_itemized),
                primary_receipt_id=str(receipt_id) if receipt_id else None,
                source_card_txn_id=str(txn_id) if txn_id else None,
                sibling_group_id=None,
                entry_id=str(e.id),
                justification=clean_justification(e.justification),
                receipt_ids=tuple(str(r) for r in receipt_ids),
                receipt_affidavit_at=(
                    e.receipt_affidavit_at.isoformat()
                    if isinstance(e.receipt_affidavit_at, datetime)
                    else None
                ),
                attendee_count=e.attendee_count,
                disclosure=e.disclosure if isinstance(e.disclosure, dict) else None,
                mileage=e.mileage if isinstance(e.mileage, dict) else None,
            )
        )
    return drafts


# A receipt backs a claim only if the claim does not exceed what the receipt
# says was paid.  The slack covers a tip / card surcharge added on top of the
# printed total (the card charge, not the receipt, sets the claimed amount).
_RECEIPT_AMOUNT_SLACK = 0.25


def entry_receipt_ids(entry: DraftEntry) -> list[str]:
    """Every receipt id linked to *entry*, newest first (``primary_receipt_id`` first)."""
    ids = [r for r in entry.receipt_ids if r]
    if entry.primary_receipt_id and entry.primary_receipt_id not in ids:
        ids.insert(0, entry.primary_receipt_id)
    return ids


def receipt_total_covers(receipt_total: float | Decimal, claimed: float | Decimal) -> bool:
    """True when a receipt total backs a *claimed* amount (within the tip slack).

    The one rule RECEIPT-REQ applies per receipt; the approval detail reuses it to
    mark a receipt that does not cover its line (UAT2 defect F).
    """
    return float(claimed) <= float(receipt_total) * (1 + _RECEIPT_AMOUNT_SLACK) + 0.01


def _single_receipt_issue(receipt: Receipt | None, entry: DraftEntry) -> str | None:
    """Why ONE linked receipt does not back *entry*'s claim (None = it does)."""
    if receipt is None:
        return "the attached receipt could not be verified"
    rfields: dict[str, Any] = receipt.extracted_fields or {}
    rprov: dict[str, Any] = receipt.field_provenance or {}
    receipt_amount = _coerce_float(rfields.get("amount"))
    if (
        rprov.get("amount") in _NOT_RECEIPT_EVIDENCE
        or receipt_amount is None
        or receipt_amount <= 0
    ):
        return RECEIPT_UNVERIFIED
    entry_amount = _coerce_float(entry.amount)
    if entry_amount is None or not receipt_total_covers(receipt_amount, entry_amount):
        return (
            f"the attached receipt ({receipt_amount:.2f}) does not cover the "
            f"claimed amount ({entry_amount if entry_amount is not None else '?'})"
        )
    return None


def receipt_support_issue(
    entry: DraftEntry,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> str | None:
    """Why the entry's linked receipts do NOT satisfy RECEIPT-REQ (None = they do).

    A receipt id alone is not evidence: POST /receipts?expense_id= lets the owner
    attach ANY receipt (e.g. a $3 coffee) to an entry.  A receipt counts only
    if it is the owner's receipt in this org and its own amount — read from the
    image, not typed by the claimant ('user_edited') nor guessed by the gap-fill
    LLM, which never sees the image ('ai_inferred', C7R) — covers the claimed amount
    (within the tip slack).  Itemized siblings each claim part of one receipt,
    so a claim BELOW the receipt total is fine.

    UAT2: an entry may carry several receipts (a correct one attached after a
    wrong one).  ANY covering receipt satisfies the rule.  Otherwise an attached
    receipt whose total is not a machine reading yields ``RECEIPT_UNVERIFIED``
    (an advisory, escalating flag — a human reviews it); otherwise the newest
    receipt's reason is returned (blocking).
    """
    ids = entry_receipt_ids(entry)
    if not ids:
        return "no receipt attached"
    issues: list[str] = []
    for rid in ids:
        receipt = _load_owned_receipt(rid, context=context, session=session, owner_id=owner_id)
        issue = _single_receipt_issue(receipt, entry)
        if issue is None:
            return None
        issues.append(issue)
    if RECEIPT_UNVERIFIED in issues:
        return RECEIPT_UNVERIFIED
    return issues[0]


def _receipt_date(fields: Any) -> date | None:
    value = fields.get("date") if isinstance(fields, dict) else None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _date_typed(receipt: Receipt) -> bool:
    """True when the receipt's date was typed by the claimant (PATCH /receipts)."""
    return (receipt.field_provenance or {}).get("date") == _USER_EDITED


def receipt_mismatch_issue(
    entry: DraftEntry,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> str | None:
    """Why the entry's receipt looks like it is for ANOTHER purchase (None = no sign).

    UAT2 (H3): ``receipt_support_issue`` only checks that the receipt amount covers
    the claim, so one receipt could back an entry dated weeks away.  When EVERY
    dated receipt of the entry is more than three days from the claimed date the
    entry gets an ADVISORY ``receipt_mismatch`` flag: never a submit dead-end (a
    hotel folio is dated at checkout, a multi-day meals receipt spans days) but it
    escalates at approval time (``ESCALATING_FLAG_TYPES``).

    UAT2 review: a receipt date the claimant TYPED (``user_edited``) is not
    evidence — it cannot be checked against the claim and it could hide a re-used
    receipt (``receipt_reuse_issue`` cannot match on it) — so any attached
    receipt with a typed date is the same advisory, escalating flag.
    """
    ids = entry_receipt_ids(entry)
    if not ids:
        return None
    try:
        claimed: date | None = date.fromisoformat(str(entry.expense_date)[:10])
    except (TypeError, ValueError):
        claimed = None
    dated: list[tuple[date, str | None]] = []
    typed: list[tuple[date | None, str | None]] = []
    for rid in ids:
        receipt = _load_owned_receipt(rid, context=context, session=session, owner_id=owner_id)
        if receipt is None:
            continue
        fields = receipt.extracted_fields or {}
        rdate = _receipt_date(fields)
        if _date_typed(receipt):
            typed.append((rdate, _receipt_vendor(fields)))
        if rdate is not None:
            dated.append((rdate, _receipt_vendor(fields)))
    issues: list[str] = []
    if (
        claimed is not None
        and dated
        and not any(
            abs((rdate - claimed).days) <= _RECEIPT_DATE_TOLERANCE_DAYS for rdate, _ in dated
        )
    ):
        rdate, rvendor = dated[0]
        source = f" from {rvendor}" if rvendor else ""
        issues.append(
            f"the attached receipt{source} is dated {rdate.isoformat()}, "
            f"{abs((rdate - claimed).days)} days from the claimed date {claimed.isoformat()}"
        )
    if typed:
        tdate, tvendor = typed[0]
        source = f" from {tvendor}" if tvendor else ""
        shown = f" ({tdate.isoformat()})" if tdate is not None else ""
        issues.append(
            f"the date{shown} on the attached receipt{source} was entered by hand, "
            "so it could not be checked against the claim"
        )
    return "; ".join(issues) if issues else None


def receipt_mismatch_flag(
    entry: DraftEntry, *, entry_ref: str, issue: str, check: str = "date"
) -> PreSubmitFlag:
    """The ADVISORY ``receipt_mismatch`` flag (UAT2): submit allowed, never auto-approved.

    ``check`` records why: ``date`` (receipt date far from / typed against the
    claim), ``possible_reuse`` (UAT2 hardening: a WEAK match — the receipt's total
    equals another claim's receipt but a typed / unread date or vendor can't
    confirm or rule out that it is the same paper receipt) or ``kept_for_audit``
    (the receipt matches one kept for audit after its line was removed from a
    reviewed report).
    """
    return PreSubmitFlag(
        entry_ref=entry_ref,
        flag_type=RECEIPT_MISMATCH,
        rule_id=None,
        rule_type=None,
        source_clause_ref=None,
        reason=(
            f"Check the receipt for {entry_label(entry)}: {issue}. "
            "Your approver will compare the receipt with the claim."
        ),
        requires_approver=None,
        observed={"receipt_check": check},
        severity="advisory",
        entry_id=entry.entry_id,
    )


POSSIBLE_DUPLICATE = "possible_duplicate"


def _receipt_vendor(fields: Any) -> str | None:
    vendor = fields.get("vendor") if isinstance(fields, dict) else None
    return vendor.strip() if isinstance(vendor, str) and vendor.strip() else None


def _same_vendor_names(a: set[str], b: set[str]) -> bool:
    """True when two lines may name the same supplier (duplicate detection, C7R).

    Each side is every vendor name known for the line: the claimed vendor and the
    vendor read from its receipt.  A side with NO name at all matches anything
    (fail closed: blanking the claimed vendor must not hide a duplicate), and
    otherwise any pair naming the same supplier matches (``same_supplier``, so
    'United Airlines Inc' == 'UNITED'), or the same text ignoring case.
    """
    if not a or not b:
        return True
    return any(x.lower() == y.lower() or same_supplier(x, y) for x in a for y in b)


DUPLICATE_CHECK_FAILED = (
    "the duplicate-claim check could not be completed, so a human must confirm "
    "this is not a second claim for the same purchase"
)


def duplicate_claim_issue(
    entry: DraftEntry,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> str | None:
    """Why *entry* looks like a second claim for the same purchase (None = it does not).

    C7R: nothing else stops one purchase being claimed twice — re-uploading the same
    receipt drafts a new entry (into the same inbox or a new one), and smart-match
    can pair it with a fresh card transaction (the staging card-feed top-up creates
    one per demo template), so the duplicate even arrives "card-corroborated".  An
    auto-approved report (under the threshold) is never seen by a human, so this
    flag is what routes such a claim to one.

    A persisted entry is a possible duplicate when the SAME owner has ANOTHER
    expense in this org — on another report that was not rejected (a rejected
    claim may be re-filed) OR on the same report — with the same date, amount and
    currency and a matching supplier (``_same_vendor_names``: the claimed vendor
    and the receipt-read vendor of each line are compared by normalized supplier
    key; a line with no vendor name at all matches).  Only persisted entries
    (``entry_id``) are checked.  A lookup error FAILS CLOSED: it returns
    ``DUPLICATE_CHECK_FAILED`` (a blocking flag) rather than "no duplicate".
    The reason names the other report by title — never an internal id.
    """
    entry_uuid = _as_uuid(entry.entry_id)
    if entry_uuid is None or owner_id is None:
        return None
    try:
        amount = Decimal(str(entry.amount))
        expense_date = date.fromisoformat(str(entry.expense_date)[:10])
    except (InvalidOperation, ValueError, TypeError):
        return None
    stmt = (
        sa.select(
            Expense.id,
            Expense.vendor,
            Expense.report_id,
            ExpenseReport.title,
            ExpenseReport.status,
        )
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            Expense.organization_id == context.organization_id,
            ExpenseReport.organization_id == context.organization_id,
            ExpenseReport.owner_id == owner_id,
            Expense.id != entry_uuid,
            Expense.expense_date == expense_date,
            Expense.amount == amount,
            Expense.currency == entry.currency,
            ExpenseReport.status != ExpenseReportStatus.rejected,
        )
        .order_by(Expense.created_at, Expense.id)
        .limit(50)
    )
    try:
        with session.begin_nested():
            own_report_id = session.scalar(
                sa.select(Expense.report_id).where(
                    Expense.id == entry_uuid,
                    Expense.organization_id == context.organization_id,
                )
            )
            rows = session.execute(stmt).all()
            receipt_rows = (
                session.execute(
                    sa.select(Receipt.expense_id, Receipt.extracted_fields).where(
                        Receipt.organization_id == context.organization_id,
                        Receipt.expense_id.in_([entry_uuid, *(r[0] for r in rows)]),
                    )
                ).all()
                if rows
                else []
            )
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the evaluation
        logger.warning("presubmit: duplicate-claim lookup failed: %s", type(exc).__name__)
        return DUPLICATE_CHECK_FAILED
    if not rows:
        return None

    names: dict[Any, set[str]] = {}
    for expense_id, fields in receipt_rows:
        rv = _receipt_vendor(fields)
        if rv:
            names.setdefault(expense_id, set()).add(rv)

    def _names(expense_id: Any, vendor: Any) -> set[str]:
        out = set(names.get(expense_id, set()))
        if isinstance(vendor, str) and vendor.strip():
            out.add(vendor.strip())
        return out

    mine = _names(entry_uuid, entry.vendor)
    matches = [r for r in rows if _same_vendor_names(mine, _names(r[0], r[1]))]
    if not matches:
        return None
    # Prefer naming another report (the more informative reason).
    matches.sort(key=lambda r: r[2] == own_report_id)
    other_id, other_vendor, report_id, title, status = matches[0]
    what = _duplicate_basis(entry.vendor, mine, other_vendor, _names(other_id, other_vendor))
    if report_id == own_report_id:
        return f"{what} is also claimed on another line of this report"
    status_value = getattr(status, "value", status)
    return f"{what} is already claimed on report '{title}' ({status_value})"


def _duplicate_basis(my_vendor: Any, mine: set[str], other_vendor: Any, theirs: set[str]) -> str:
    """Honest wording of WHY two lines matched (UAT2: never "same vendor" when it differs)."""
    if not mine or not theirs:
        return "a line with the same date and amount (one of the two lines names no vendor)"
    a = my_vendor.strip() if isinstance(my_vendor, str) else ""
    b = other_vendor.strip() if isinstance(other_vendor, str) else ""
    if a and b and (a.lower() == b.lower() or same_supplier(a, b)):
        return "a line with the same vendor, date and amount"
    return "a line with the same date and amount whose receipt names the same supplier"


@dataclass(frozen=True)
class _ReceiptSig:
    """What a receipt READS (UAT2 reuse check) — its MACHINE reading, never a typed value.

    From the immutable OCR snapshot (``evidence.ocr_snapshot``) when the row has
    one; a legacy row (processed before snapshots) falls back to its current
    fields whose provenance is not ``user_edited`` / ``ai_inferred``.  A field
    is None when it was not machine-read.
    """

    day: date | None
    amount: float | None
    vendor: str | None


@dataclass(frozen=True)
class _ReceiptEvidence:
    """One receipt row for the reuse check: its machine reading + every total it shows.

    ``totals`` also holds the CURRENT total whatever its provenance (a typed
    total is untrusted — it can never prove two receipts differ — but a typed
    total equal to another receipt's is still a signal: the WEAK match).
    """

    receipt_id: Any
    sig: _ReceiptSig
    totals: frozenset[Decimal]


_STRONG = "strong"
_WEAK = "weak"
_CENT = Decimal("0.01")


def _cents(value: Any) -> Decimal | None:
    amount = _coerce_float(value)
    if amount is None or amount <= 0:
        return None
    try:
        return Decimal(str(amount)).quantize(_CENT)
    except InvalidOperation:
        return None


def _machine_reading(fields: Any, provenance: Any, grounding: Any) -> _ReceiptSig:
    snapshot = ocr_snapshot(grounding)
    if snapshot is not None:
        source: dict[str, Any] = snapshot
        untrusted: set[str] = set()
    else:
        source = fields if isinstance(fields, dict) else {}
        prov: dict[str, Any] = provenance if isinstance(provenance, dict) else {}
        untrusted = {
            k for k in ("amount", "date", "vendor") if prov.get(k) in _NOT_RECEIPT_EVIDENCE
        }
    amount = None if "amount" in untrusted else _coerce_float(source.get("amount"))
    return _ReceiptSig(
        day=None if "date" in untrusted else _receipt_date(source),
        amount=amount if amount is not None and amount > 0 else None,
        vendor=None if "vendor" in untrusted else _receipt_vendor(source),
    )


def _receipt_evidence(
    receipt_id: Any, fields: Any, provenance: Any, grounding: Any
) -> _ReceiptEvidence | None:
    sig = _machine_reading(fields, provenance, grounding)
    current = fields.get("amount") if isinstance(fields, dict) else None
    totals = {c for c in (_cents(sig.amount), _cents(current)) if c is not None}
    if not totals:
        return None
    return _ReceiptEvidence(receipt_id=receipt_id, sig=sig, totals=frozenset(totals))


def _reuse_strength(a: _ReceiptEvidence, b: _ReceiptEvidence) -> str | None:
    """STRONG / WEAK / None: do two receipt rows read as the SAME paper receipt?

    Machine readings that CONTRADICT each other (two read dates that differ, two
    read suppliers that differ) prove two purchases → None.  Otherwise:

    * STRONG (blocking duplicate): the same machine-read total (to the cent)
      and either the same read date (a side with no read vendor matches — fail
      closed) or the same read supplier;
    * WEAK (advisory, always escalates): only the total links them — the same
      read total with no read date / supplier to confirm it, or a total known to
      both rows where a side's total is typed / unread (legacy rows and
      ``user_edited`` values can't be compared any further) — including two
      different read totals while a typed total matches one of them (revision:
      an OCR misread "corrected" to the original's total).  Never silently
      passes, never auto-approves.
    """
    x, y = a.sig, b.sig
    if x.day is not None and y.day is not None and x.day != y.day:
        return None
    if x.vendor and y.vendor and not _same_vendor_names({x.vendor}, {y.vendor}):
        return None
    both_read = x.amount is not None and y.amount is not None
    if both_read and _cents(x.amount) == _cents(y.amount):
        if x.day is not None and y.day is not None:
            return _STRONG
        if x.vendor and y.vendor:
            return _STRONG
        return _WEAK
    # Only the totals link them: a typed / unread / legacy total on one side
    # equals a total on the other.  No machine reading contradicts it (ruled out
    # above), so it can't be dismissed — even when the two READ totals differ
    # (an OCR misread of the copy, corrected by typing the original's total).
    return _WEAK if a.totals & b.totals else None


def _weak_basis(a: _ReceiptEvidence, b: _ReceiptEvidence) -> str:
    """Why a WEAK match can't be confirmed or ruled out — reviewer-facing text."""
    x, y = a.sig, b.sig
    read_totals_agree = (
        x.amount is not None and y.amount is not None and _cents(x.amount) == _cents(y.amount)
    )
    if not read_totals_agree:
        return "a total was typed by hand or not read from the receipt"
    return "its date or vendor was typed by hand or not read"


def _receipt_text(ev: _ReceiptEvidence | None) -> str:
    if ev is None:
        return "the attached receipt"
    sig = ev.sig
    parts = [p for p in (sig.vendor, sig.day.isoformat() if sig.day else None) if p]
    total = _cents(sig.amount) if sig.amount is not None else min(ev.totals)
    parts.append(f"{total:.2f}")
    return f"the attached receipt ({', '.join(parts)})"


def _numeric_sql(txt: Any) -> Any:
    """A JSON text value as NUMERIC(…, 2), NULL when it is not a plain number."""
    return sa.case(
        (
            txt.op("~")(r"^\s*-?[0-9]+(\.[0-9]+)?\s*$"),
            sa.func.round(sa.cast(sa.func.trim(txt), sa.Numeric), 2),
        ),
        else_=sa.null(),
    )


def _receipt_amount_sql() -> Any:
    """The receipt's CURRENT total (any provenance) as NUMERIC(…, 2)."""
    return _numeric_sql(Receipt.extracted_fields["amount"].astext)


def _snapshot_amount_sql() -> Any:
    """The receipt's OCR-snapshot total (the machine reading) as NUMERIC(…, 2)."""
    return _numeric_sql(Receipt.grounding_context[(OCR_SNAPSHOT_KEY, "amount")].astext)


@dataclass
class _ReuseFindings:
    """What ``_receipt_reuse_scan`` found for one entry (None = nothing)."""

    strong: str | None = None  # blocking possible_duplicate
    strong_basis: str = "receipt date+total+vendor"
    weak: str | None = None  # advisory, escalating receipt_mismatch (possible_reuse)
    kept: str | None = None  # advisory, escalating receipt_mismatch (kept_for_audit)


def _share_issue(
    session: Session,
    *,
    context: AgentContext,
    own_report_id: Any,
    my_rows: Sequence[Receipt],
    evidence_by_id: dict[Any, _ReceiptEvidence],
) -> str | None:
    """Why a receipt row SHARED by several entries is used beyond what it supports.

    UAT2 hardening (itemized siblings): one receipt row may back several entries
    only while every one of them is on THIS report and their summed amounts are
    covered by the receipt's machine-read total.  Re-validated on every run, so a
    later edit / move that breaks the split is flagged.
    """
    for receipt in my_rows:
        backing = backing_expense_ids(receipt)
        if len(backing) < 2:
            continue
        members = session.execute(
            sa.select(Expense.amount, Expense.report_id, ExpenseReport.title, ExpenseReport.status)
            .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
            .where(
                Expense.id.in_(backing),
                Expense.organization_id == context.organization_id,
                ExpenseReport.organization_id == context.organization_id,
            )
        ).all()
        if len(members) < 2:
            continue
        ev = evidence_by_id.get(receipt.id)
        text = _receipt_text(ev)
        elsewhere = [m for m in members if m[1] != own_report_id]
        if elsewhere:
            _amount, _rid, title, status = elsewhere[0]
            return (
                f"{text} also backs a line on report '{title}' "
                f"({getattr(status, 'value', status)}) — one receipt can only be split "
                "across lines of the same report"
            )
        claimed = sum((Decimal(str(m[0])) for m in members), Decimal("0"))
        cap = _cents(ev.sig.amount) if ev is not None and ev.sig.amount is not None else None
        if cap is None:
            return (
                f"{text} is split across {len(members)} lines of this report, but its "
                "total was not read from the receipt, so the split can't be checked"
            )
        if claimed > cap:
            return (
                f"{text} is split across {len(members)} lines of this report totalling "
                f"{claimed:.2f}, more than the receipt's total {cap:.2f}"
            )
    return None


def _receipt_reuse_scan(
    entry: DraftEntry,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> _ReuseFindings:
    """Is *entry*'s evidence a receipt that already backs ANOTHER claim? (UAT2 hardening)

    Each receipt of the entry is compared (``_reuse_strength``) with the owner's
    receipts linked to OTHER live (not rejected) entries and with receipts KEPT
    FOR AUDIT; the SQL prefilter matches on the current total OR the OCR-snapshot
    total, so retyping a total can't drop a candidate.  A row shared by several
    entries is checked against its split (``_share_issue``).  A lookup error
    fails closed (``DUPLICATE_CHECK_FAILED``, blocking).
    """
    out = _ReuseFindings()
    entry_uuid = _as_uuid(entry.entry_id)
    ids = entry_receipt_ids(entry)
    if entry_uuid is None or owner_id is None or not ids:
        return out
    my_rows: list[Receipt] = []
    mine: list[_ReceiptEvidence] = []
    for rid in ids:
        receipt = _load_owned_receipt(rid, context=context, session=session, owner_id=owner_id)
        if receipt is None:
            continue
        my_rows.append(receipt)
        ev = _receipt_evidence(
            receipt.id,
            receipt.extracted_fields,
            receipt.field_provenance,
            receipt.grounding_context,
        )
        if ev is not None:
            mine.append(ev)
    if not my_rows:
        return out
    evidence_by_id = {ev.receipt_id: ev for ev in mine}
    totals = sorted({t for ev in mine for t in ev.totals})
    my_ids = [r.id for r in my_rows]
    current_sql, snapshot_sql = _receipt_amount_sql(), _snapshot_amount_sql()
    by_total = sa.or_(current_sql.in_(totals), snapshot_sql.in_(totals))
    try:
        with session.begin_nested():
            own_report_id = session.scalar(
                sa.select(Expense.report_id).where(
                    Expense.id == entry_uuid,
                    Expense.organization_id == context.organization_id,
                )
            )
            shared = _share_issue(
                session,
                context=context,
                own_report_id=own_report_id,
                my_rows=my_rows,
                evidence_by_id=evidence_by_id,
            )
            rows: list[Any] = []
            kept_rows: list[Any] = []
            if totals:
                rows = list(
                    session.execute(
                        sa.select(
                            Receipt.id,
                            Receipt.extracted_fields,
                            Receipt.field_provenance,
                            Receipt.grounding_context,
                            Expense.report_id,
                            ExpenseReport.title,
                            ExpenseReport.status,
                        )
                        .join(Expense, Receipt.expense_id == Expense.id)
                        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
                        .where(
                            Receipt.organization_id == context.organization_id,
                            Receipt.uploaded_by == owner_id,
                            Receipt.id.notin_(my_ids),
                            Expense.organization_id == context.organization_id,
                            ExpenseReport.organization_id == context.organization_id,
                            ExpenseReport.owner_id == owner_id,
                            Expense.id != entry_uuid,
                            ExpenseReport.status != ExpenseReportStatus.rejected,
                            by_total,
                        )
                        .order_by(Receipt.created_at, Receipt.id)
                        .limit(100)
                    ).all()
                )
                # Receipts KEPT FOR AUDIT (UAT2 review): detached when their line
                # was deleted from a reviewed report — no longer linked to any
                # entry, but still evidence a reviewer saw.
                kept_rows = list(
                    session.execute(
                        sa.select(
                            Receipt.id,
                            Receipt.extracted_fields,
                            Receipt.field_provenance,
                            Receipt.grounding_context,
                            AuditLog.after_state,
                        )
                        .join(
                            AuditLog,
                            sa.and_(
                                AuditLog.entity_id == Receipt.id,
                                AuditLog.entity_type == "receipt",
                                AuditLog.action == RECEIPT_RETAINED_ACTION,
                                AuditLog.organization_id == Receipt.organization_id,
                            ),
                        )
                        .where(
                            Receipt.organization_id == context.organization_id,
                            Receipt.uploaded_by == owner_id,
                            Receipt.expense_id.is_(None),
                            Receipt.id.notin_(my_ids),
                            by_total,
                        )
                        .order_by(Receipt.created_at, Receipt.id)
                        .limit(100)
                    ).all()
                )

            def _best(theirs: _ReceiptEvidence) -> tuple[str, _ReceiptEvidence] | None:
                found: tuple[str, _ReceiptEvidence] | None = None
                for ev in mine:
                    strength = _reuse_strength(ev, theirs)
                    if strength == _STRONG:
                        return strength, ev
                    if strength == _WEAK and found is None:
                        found = (strength, ev)
                return found

            strong_hits: list[tuple[Any, Any, Any, _ReceiptEvidence]] = []
            weak_hits: list[tuple[Any, Any, Any, _ReceiptEvidence, _ReceiptEvidence]] = []
            for rid, fields, prov, grounding, report_id, title, status in rows:
                theirs = _receipt_evidence(rid, fields, prov, grounding)
                match = _best(theirs) if theirs is not None else None
                if match is not None and theirs is not None:
                    if match[0] == _STRONG:
                        strong_hits.append((report_id, title, status, match[1]))
                    else:
                        weak_hits.append((report_id, title, status, match[1], theirs))
            kept_hit: tuple[Any, _ReceiptEvidence] | None = None
            for rid, fields, prov, grounding, after in kept_rows:
                theirs = _receipt_evidence(rid, fields, prov, grounding)
                match = _best(theirs) if theirs is not None else None
                if match is not None:
                    kept_hit = ((after or {}).get("report_id"), match[1])
                    break
            kept_title: str | None = None
            if kept_hit is not None and _as_uuid(kept_hit[0]) is not None:
                kept_title = session.scalar(
                    sa.select(ExpenseReport.title).where(
                        ExpenseReport.id == _as_uuid(kept_hit[0]),
                        ExpenseReport.organization_id == context.organization_id,
                        ExpenseReport.owner_id == owner_id,
                    )
                )
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the evaluation
        logger.warning("presubmit: receipt-reuse lookup failed: %s", type(exc).__name__)
        out.strong = DUPLICATE_CHECK_FAILED
        return out

    if strong_hits:
        strong_hits.sort(key=lambda h: h[0] == own_report_id)
        report_id, title, status, ev = strong_hits[0]
        if report_id == own_report_id:
            out.strong = (
                f"{_receipt_text(ev)} also backs another line of this report — to split one "
                "receipt across lines, attach that same receipt to each line instead of "
                "uploading it again"
            )
        else:
            out.strong = (
                f"{_receipt_text(ev)} already backs a line on report '{title}' "
                f"({getattr(status, 'value', status)})"
            )
    elif shared is not None:
        out.strong = shared
        out.strong_basis = "shared receipt split"
    elif weak_hits:
        weak_hits.sort(key=lambda h: h[0] == own_report_id)
        report_id, title, status, ev, theirs = weak_hits[0]
        where = (
            "the receipt on another line of this report"
            if report_id == own_report_id
            else f"the one on report '{title}' ({getattr(status, 'value', status)})"
        )
        out.weak = (
            f"{_receipt_text(ev)} may be the same receipt as {where} — the totals match, but "
            f"{_weak_basis(ev, theirs)}, so this can't be ruled out"
        )
    if kept_hit is not None:
        where = f" from report '{kept_title}'" if kept_title else ""
        out.kept = (
            f"{_receipt_text(kept_hit[1])} matches a receipt kept for audit after its line "
            f"was removed{where} during review — check this is not that line re-filed"
        )
    return out


def receipt_reuse_issue(
    entry: DraftEntry,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> str | None:
    """Why *entry*'s receipt is evidence already backing ANOTHER claim (UAT2, H3).

    Re-uploading one paper receipt creates a new receipt row each time, and the
    entry it backs (or the receipt's own fields, on a never-submitted draft) can
    then be retyped, which defeats the entry-level duplicate check.  The OCR
    snapshot of each copy still reads the same: a STRONG match (``_reuse_strength``)
    with a receipt linked to ANOTHER live (not rejected) entry of the same owner,
    or a shared receipt used beyond its total / across reports, is one purchase
    claimed twice.  A lookup error fails closed (``DUPLICATE_CHECK_FAILED``).
    """
    return _receipt_reuse_scan(entry, context=context, session=session, owner_id=owner_id).strong


def evidence_flags(
    entry: DraftEntry,
    *,
    entry_ref: str,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> list[PreSubmitFlag]:
    """Non-rule flags derived from the entry's evidence, shared by every policy path.

    * ``possible_duplicate`` (BLOCKING): the same purchase claimed on another line
      (``duplicate_claim_issue``), or — when no such line exists — the entry's
      receipt STRONGLY matches one backing another live claim, or a shared
      receipt is used beyond its total / across reports (``receipt_reuse_issue``);
    * ``receipt_mismatch`` (ADVISORY, escalates): the receipt's date is far from
      the claim's or was typed by the claimant (``receipt_mismatch_issue``); the
      receipt WEAKLY matches another claim's (same total, but a typed / unread
      date or vendor can't confirm or rule it out — UAT2 hardening); and/or it
      reads the same as one KEPT FOR AUDIT after its line was deleted from a
      reviewed report (UAT2 review).  Not blocking: the claimant may be right and
      a kept receipt can never be removed — a blocking flag would be a permanent
      dead-end — but a human always sees it (never auto-approved).
    """
    flags: list[PreSubmitFlag] = []
    duplicate = duplicate_claim_issue(entry, context=context, session=session, owner_id=owner_id)
    found = _receipt_reuse_scan(entry, context=context, session=session, owner_id=owner_id)
    if duplicate is not None:
        flags.append(duplicate_claim_flag(entry, entry_ref=entry_ref, issue=duplicate))
    elif found.strong is not None:
        flags.append(
            duplicate_claim_flag(
                entry, entry_ref=entry_ref, issue=found.strong, basis=found.strong_basis
            )
        )
    weak = found.weak if duplicate is None and found.strong is None else None
    mismatch = receipt_mismatch_issue(entry, context=context, session=session, owner_id=owner_id)
    issues = [i for i in (found.kept, weak, mismatch) if i]
    if issues:
        check = "kept_for_audit" if found.kept else "possible_reuse" if weak else "date"
        flags.append(
            receipt_mismatch_flag(
                entry,
                entry_ref=entry_ref,
                issue="; ".join(issues),
                check=check,
            )
        )
    return flags


def duplicate_claim_flag(
    entry: DraftEntry,
    *,
    entry_ref: str,
    issue: str,
    basis: str = "supplier+date+amount+currency",
) -> PreSubmitFlag:
    """The blocking ``possible_duplicate`` flag for *entry* (C7R).

    Blocking, so the report can never be auto-approved: a human decides whether
    it is a genuine second purchase.  ``basis`` records which check matched.
    """
    return PreSubmitFlag(
        entry_ref=entry_ref,
        flag_type=POSSIBLE_DUPLICATE,
        rule_id=None,
        rule_type=None,
        source_clause_ref=None,
        reason=f"Possible duplicate claim for {entry_label(entry)}: {issue}.",
        requires_approver=None,
        observed={"duplicate_check": basis},
        severity="blocking",
        entry_id=entry.entry_id,
    )


def build_entry_eval_item(
    entry: DraftEntry,
    *,
    idx: int,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
    peers: Sequence[DraftEntry] = (),
) -> EvalItem:
    """Build the EvalItem for one entry the way every policy path should (C7-15).

    Prefers the entry's real trip segment (supplier / cabin / nights / city from
    the owner's APPROVED or COMPLETED itinerary); missing segment data is filled
    from the entry's own fields; with no usable segment the entry's fields are
    used outright.

    The itinerary is a plan; receipts and card transactions are what was
    actually spent.  Where they disagree the evidence wins, conservatively:
      * flight supplier — if a deterministic receipt vendor / card merchant
        exists and the segment supplier matches none of them, the segment
        supplier is dropped (supplier = the evidence-trusted entry vendor, or
        None → AIR-PREF blocks);
      * hotel city — conflicting receipt vs segment city → city unknown (the
        domestic cap, the lowest tier);
      * a receipt vendor / location the claimant overwrote (PATCH /receipts →
        'user_edited') destroyed the OCR value that might have contradicted the
        segment, so it counts as a contradiction: the segment supplier is kept
        only if an independent deterministic vendor (card merchant) confirms
        it, and the segment city is dropped (lowest cap);
      * hotel nights — the smaller of the receipt and segment nights (a higher
        per-night rate);
      * meals city — the segment / receipt city with the same contradiction rules
        as a hotel; meals days — the trip's covered days (union of its segments), applied to
        the TOTAL of every meals line linked to any segment of that trip
        (``_trip_meal_scope`` / ``_segment_meal_total``, Issue 6).

    Anything the claimant states but nothing verifies (nights / days in the
    description, an untrusted segment's dates / city, a city named in the
    description / vendor) rides along as a claim (``_with_claims``).
    """
    fields_item, evidence = _fields_eval_item(
        entry, idx=idx, context=context, session=session, owner_id=owner_id
    )
    segment = _load_owned_segment(
        entry.trip_segment_id, context=context, session=session, owner_id=owner_id
    )
    if segment is None:
        claimed_segment = _load_claimed_segment(
            entry.trip_segment_id, context=context, session=session, owner_id=owner_id
        )
        return _with_spend(entry, _with_claims(entry, fields_item, claimed_segment=claimed_segment))

    item = build_eval_item(entry, segment=segment, idx=idx)
    updates: dict[str, Any] = {}
    if item.item_type == "flight":
        # AIR-04 (known limits): the OWNER's grade of record, from the database.
        updates["traveller_grade"] = owner_grade(
            session, organization_id=context.organization_id, user_id=owner_id
        )
        segment_supplier_confirmed = any(same_supplier(item.supplier, v) for v in evidence.vendors)
        segment_supplier_contradicted = not segment_supplier_confirmed and (
            bool(evidence.vendors) or evidence.vendor_edited
        )
        if segment_supplier_contradicted or not item.supplier:
            updates["supplier"] = fields_item.supplier
            if fields_item.supplier is None:
                # Honest reason text (C7R): what was claimed vs what the records say.
                updates["claimed_supplier"] = fields_item.claimed_supplier or (
                    (item.supplier or "").strip() or None
                )
                updates["supplier_evidence"] = fields_item.supplier_evidence or (
                    " / ".join(evidence.vendors) if evidence.vendors else None
                )
    if item.item_type == "hotel":
        city = item.city
        if evidence.city_edited:
            city = None
        elif evidence.city:
            city = evidence.city if not city or _same_city(city, evidence.city) else None
        if city != item.city:
            updates["city"] = city
        if city is None:
            updates["claimed_city"] = item.city or fields_item.claimed_city
        nights_candidates = [n for n in (_positive_int(item.nights), evidence.nights) if n]
        nights = min(nights_candidates) if nights_candidates else None
        if nights != item.nights:
            updates["nights"] = nights
            updates["amount_usd"] = entry.amount / nights if nights else entry.amount
        # Issue I: the receipt's room-only figure (legacy room_only cap basis) only
        # applies over the SAME verified nights it was computed for.
        if nights and fields_item.nights == nights:
            updates["room_only_nightly_usd"] = fields_item.room_only_nightly_usd
        # LOD-02 (review round 2): the tool booking is the segment's hotel.  A receipt /
        # card vendor that names a different supplier — or a receipt vendor the claimant
        # overwrote, or a booking with no supplier to compare — means this stay is not
        # shown to be that booking, so the tolerance does not apply (fail closed, like
        # the flight-supplier contradiction above).
        # Security review (round 3): the tolerance needs POSITIVE evidence that this is
        # the booked stay — a receipt / card vendor naming the segment's hotel AND a
        # receipt location (OCR, not edited) in the segment's city.  No evidence, or
        # either one missing or contradicting -> the manager exception stays.
        # Review round 4: and the segment must be one the PLANNER booked — a hotel the
        # traveler typed into POST /trips was never booked through the tool.
        try:
            seg_trip = session.get(Trip, segment.trip_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("presubmit: trip lookup failed for LOD-02: %s", exc)
            seg_trip = None
        if is_planner_booked_hotel(segment, seg_trip):
            seg_supplier = getattr(segment, "supplier", None)
            seg_city = getattr(segment, "location", None)
            hotel_ok = bool(seg_supplier) and any(
                same_supplier(seg_supplier, v) for v in evidence.vendors
            )
            city_ok = (
                not evidence.city_edited
                and bool(evidence.city)
                and isinstance(seg_city, str)
                and bool(seg_city.strip())
                and _same_city(seg_city, evidence.city or "")
            )
            if hotel_ok and city_ok:
                updates["booked_through_tool"] = True
    if item.item_type == "meals":
        segment_city = getattr(segment, "location", None) or None
        city = segment_city
        if evidence.city_edited:
            city = None
        elif evidence.city:
            city = evidence.city if not city or _same_city(city, evidence.city) else None
        if city:
            updates["city"] = city
        else:
            updates["claimed_city"] = segment_city or fields_item.claimed_city
        scope = _trip_meal_scope(segment, session=session)
        days = scope[1] if scope else None
        if scope and days:
            total = _segment_meal_total(
                entry, scope[0], peers=peers, context=context, session=session, owner_id=owner_id
            )
            if total is not None:
                updates["days"] = days
                updates["amount_usd"] = total / days
                updates["stay_amount_usd"] = round(total, 2)
                # MEAL-02 (product gap 2): the trip's first / last covered day are its
                # departure / return days (a same-day trip has one, counted once).
                updates["travel_days"] = entry_rules.travel_day_count(days)
        updates["line_items"] = fields_item.line_items
        updates["item_text"] = fields_item.item_text
        updates["receipt_total_usd"] = fields_item.receipt_total_usd
        updates["receipt_shared"] = fields_item.receipt_shared
        # The trip's per-day average replaces ``amount_usd``: keep THIS line's claim so
        # its exclusions are measured against what it claims (review round 3).
        updates["line_claim_usd"] = entry.amount
    if item.item_type == "receipt" and item.expense_category == "ground_transport":
        # The trusted words (card merchant, the receipt's OCR words) were gathered by
        # ``_fields_eval_item``; the entry's own words stay the claimant's.
        updates.update(
            _ground_item_fields(
                entry,
                segment,
                extra_texts=fields_item.ground_trusted_texts,
                vendor_edited=evidence.vendor_lost,
            )
        )
        updates["line_items"] = fields_item.line_items
        updates["tip_usd"] = fields_item.tip_usd
        updates["receipt_total_usd"] = fields_item.receipt_total_usd
        updates["receipt_shared"] = fields_item.receipt_shared
        updates["parking_days"] = fields_item.parking_days
        updates["claimed_days"] = fields_item.claimed_days
        if updates.get("airport_parking") and fields_item.parking_days is None:
            # GRND-03: only the RECEIPT's parking days decide a breach.  The trip's
            # length is NOT how long the car was parked — it is reason context only
            # ("the linked trip spans 9 days; the receipt does not say ...").
            span = _trip_span_days(segment, session=session)
            if span is not None:
                updates["trip_days"] = span
    item = dataclasses.replace(item, **updates) if updates else item
    return _with_spend(entry, _with_claims(entry, item, claimed_segment=None))


def memo_owner_grade(
    session: Session, context: AgentContext, owner_id: uuid.UUID | None
) -> Callable[[], str | None]:
    """The OWNER's grade of record, looked up at most once per report (follow-up, grades)."""
    cache: list[str | None] = []

    def _get() -> str | None:
        if not cache:
            cache.append(
                owner_grade(session, organization_id=context.organization_id, user_id=owner_id)
            )
        return cache[0]

    return _get


def grade_line_flags(
    entry: DraftEntry,
    ref: str,
    process: process_checks.ProcessRules,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
    grade_of_owner: Callable[[], str | None],
    strict: bool,
) -> list[PreSubmitFlag]:
    """Follow-up (grades): the AIR-07 / LOD-05 / NR-01 flags of one line."""
    rules = grade_checks.grade_rules(process)
    if not rules:
        return []
    try:
        raw = grade_checks.entry_grade_flags(
            entry,
            entry_ref=ref,
            rules=rules,
            context=context,
            session=session,
            owner_id=owner_id,
            grade_of_owner=grade_of_owner,
        )
    except Exception as exc:
        if strict:
            raise
        logger.warning("presubmit: grade checks failed: %s", type(exc).__name__)
        return []
    return [PreSubmitFlag.from_dict(f) for f in raw]


def run_presubmit(
    report: DraftReport,
    *,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None = None,
    strict: bool = False,
) -> tuple[list[PreSubmitFlag], dict[str, list[RuleVerdict]]]:
    """Run pre-submit policy check for all entries in the report.

    Each entry is evaluated against its REAL trip segment when it links one that
    belongs to *owner_id* (default: the acting user) in the caller's org; with no
    usable segment it falls back to the entry's own fields — the same
    ``build_eval_item_from_fields`` path the receipt upload uses (C7-15).

    Args:
        owner_id: The report owner whose trip segments / receipts / card
            transactions may enrich the entries.  Defaults to ``context.user_id``.
        strict: When True (approval-time re-evaluation, C7-05) an evaluation
            failure RAISES instead of silently producing zero flags, so the
            caller can treat it as "not policy-compliant".

    Returns:
        (flags, verdicts_index) where verdicts_index maps entry_ref → list[RuleVerdict].
    """
    entries = report.entries
    if not entries:
        return [], {}

    # Build EvalItems, resolving the entry's real trip segment when one exists
    # and otherwise falling back to the entry's own fields (C7-15).
    eval_items: list[EvalItem] = []
    effective_owner = owner_id if owner_id is not None else context.user_id

    for idx, entry in enumerate(entries):
        try:
            item = build_entry_eval_item(
                entry,
                idx=idx,
                context=context,
                session=session,
                owner_id=effective_owner,
                peers=entries,
            )
        except Exception as exc:
            # Malformed segment/receipt data must never crash assembly; strict
            # (approval) mode surfaces it so the caller escalates instead.
            if strict:
                raise
            logger.warning("presubmit: eval-item build failed for entry %d: %s", idx, exc)
            item = build_eval_item(entry, segment=None, idx=idx)
        # Product gap 8: caps are compared in the policy currency (fx_policy).
        eval_items.append(with_item_currency(item, entry.currency))

    # Evaluate all items
    try:
        policy_verdicts = PolicyEvalTool().evaluate_many(
            eval_items, context=context, session=session
        )
    except Exception as exc:
        if strict:
            raise
        logger.warning("ExpenseAgent: PolicyEvalTool.evaluate_many failed: %s", exc)
        policy_verdicts = tuple()

    if strict and len(policy_verdicts) != len(entries):
        raise PolicyEvaluationIncomplete(
            f"Policy evaluation returned {len(policy_verdicts)} verdicts for "
            f"{len(entries)} entries."
        )

    # Build verdicts index
    verdicts_index: dict[str, list[RuleVerdict]] = {}
    for pv in policy_verdicts:
        ref = pv.item.ref_id or ""
        verdicts_index[ref] = list(pv.verdicts)

    # Build flags
    flags: list[PreSubmitFlag] = []
    failing_verdict_count = 0
    # Product gap 2 stage 2: process & timing rules (EXP-01 / EXP-04 / CARD-03).
    process = process_checks.load_process_rules(session, context.organization_id)
    owner_grade_once = memo_owner_grade(session, context, effective_owner)

    for idx, (entry, pv) in enumerate(zip(entries, policy_verdicts, strict=False)):
        ref = _entry_ref(entry, idx)
        # C7 review: a linked receipt id is not enough — the receipt must be the
        # owner's and its machine-read amount must cover the claim.
        receipt_issue = receipt_support_issue(
            entry, context=context, session=session, owner_id=effective_owner
        )

        entry_flags, entry_failing = verdicts_to_flags(
            list(pv.verdicts),
            entry_ref=ref,
            has_receipt=receipt_issue is None,
            category=entry.category,
            receipt_issue=receipt_issue,
            entry_label=entry_label(entry),
            entry_id=entry.entry_id,
            justification=entry.justification,
        )
        if process.receipt:
            entry_flags = apply_affidavit(
                entry_flags,
                entry,
                process=process,
                context=context,
                session=session,
                owner_id=effective_owner,
            )
        flags.extend(entry_flags)
        failing_verdict_count += entry_failing

        # Follow-up (grades): AIR-07 / LOD-05 / NR-01 from the OWNER's grade of record.
        flags.extend(
            grade_line_flags(
                entry, ref, process, context, session, effective_owner, owner_grade_once, strict
            )
        )

        # C7R: a second claim for the same purchase is never auto-approved; UAT2:
        # nor is a receipt reused for another claim or one dated far from it.
        flags.extend(
            evidence_flags(
                entry, entry_ref=ref, context=context, session=session, owner_id=effective_owner
            )
        )

    # Product gap 2 stage 2: report-level process rules — EXP-01 (submission deadline /
    # stale report) and CARD-03 (card transactions not reconciled within the deadline).
    try:
        for raw in process_checks.report_flags(
            session,
            organization_id=context.organization_id,
            owner_id=effective_owner,
            trip_id=report.trip_id,
            entry_dates=[e.expense_date for e in entries],
            rules=process,
        ):
            flags.append(PreSubmitFlag.from_dict(raw))
    except Exception as exc:
        if strict:
            raise
        logger.warning("presubmit: process checks failed: %s", type(exc).__name__)

    # M-A3 internal guard: count policy_violation flags >= failing verdicts
    pv_flag_count = sum(1 for f in flags if f.flag_type == "policy_violation")
    if pv_flag_count < failing_verdict_count:
        raise AssertionError(
            f"M-A3 violation: {pv_flag_count} policy_violation flags < "
            f"{failing_verdict_count} failing verdicts"
        )

    blocking_count = sum(1 for f in flags if f.severity == "blocking")
    advisory_count = sum(1 for f in flags if f.severity == "advisory")
    logger.info(
        "ExpenseAgent presubmit: %d entries, %d blocking, %d advisory flags",
        len(entries),
        blocking_count,
        advisory_count,
    )

    return flags, verdicts_index


def apply_affidavit(
    flags: list[PreSubmitFlag],
    entry: DraftEntry,
    *,
    process: process_checks.ProcessRules,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
) -> list[PreSubmitFlag]:
    """EXP-04 (product gap 2 stage 2): the missing-receipt affidavit on a line with NO receipt.

    Only a BLOCKING ``missing_receipt`` flag of a line that has no receipt linked at all is
    touched (an attached receipt that does not cover the claim is not a lost receipt).  A
    signed, explained affidavit within the clause's amount and quarterly count makes it
    advisory (still an escalating flag type — never auto-approved); otherwise it stays
    blocking and its reason says why, citing EXP-04.
    """
    return apply_affidavit_fields(
        flags,
        process=process,
        context=context,
        session=session,
        owner_id=owner_id,
        receipt_linked=bool(entry_receipt_ids(entry)),
        entry_id=entry.entry_id,
        amount=entry.amount,
        expense_date=entry.expense_date,
        signed_at=entry.receipt_affidavit_at,
        justification=entry.justification,
        currency=entry.currency,
    )


def affidavit_fields(entry: DraftEntry, *, owner_id: uuid.UUID) -> dict[str, Any]:
    """The EXP-04 inputs of *entry* as a JSON-safe dict (the /policy/evaluate unit)."""
    return {
        "owner_id": str(owner_id),
        "receipt_linked": bool(entry_receipt_ids(entry)),
        "entry_id": entry.entry_id,
        "amount": entry.amount,
        "expense_date": entry.expense_date,
        "signed_at": entry.receipt_affidavit_at,
        "justification": entry.justification,
        "currency": entry.currency,
    }


def apply_affidavit_fields(
    flags: list[PreSubmitFlag],
    *,
    process: process_checks.ProcessRules,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
    receipt_linked: bool,
    entry_id: str | None,
    amount: Any,
    expense_date: Any,
    signed_at: Any,
    justification: str | None,
    currency: Any = None,
) -> list[PreSubmitFlag]:
    """``apply_affidavit`` on explicit fields (shared with POST /policy/evaluate)."""
    if receipt_linked or not process.receipt:
        return flags
    out: list[PreSubmitFlag] = []
    for flag in flags:
        if flag.flag_type == "missing_receipt" and flag.severity == "blocking":
            flag = PreSubmitFlag.from_dict(
                process_checks.affidavit_flag(
                    flag.to_dict(),
                    session=session,
                    rules=process,
                    organization_id=context.organization_id,
                    owner_id=owner_id,
                    entry_id=entry_id,
                    amount=amount,
                    expense_date=expense_date,
                    signed_at=signed_at,
                    justification=justification,
                    currency=currency,
                )
            )
        out.append(flag)
    return out


def build_flags_snapshot(flags: list[PreSubmitFlag], entry_count: int) -> dict[str, Any]:
    """Build the flags snapshot dict for storage in ExpenseReport.pre_submit_flags."""
    blocking_count = sum(1 for f in flags if f.severity == "blocking")
    advisory_count = sum(1 for f in flags if f.severity == "advisory")
    return {
        "flags": [f.to_dict() for f in flags],
        "evaluated_entry_count": entry_count,
        "blocking_count": blocking_count,
        "advisory_count": advisory_count,
    }
