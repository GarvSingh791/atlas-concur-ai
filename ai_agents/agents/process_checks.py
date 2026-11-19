"""Process & timing policy checks — wiring of ``ai_agents.tools.process_rules`` (gap 2, stage 2).

The evaluators are pure; this module loads the organization's ACTIVE rules, gathers
the trusted facts each clause needs from the database and turns every ``Finding`` into
a flag dict in the ``PreSubmitFlag.to_dict`` shape (callers convert / merge it):

* ``report_flags``      — EXP-01 (deadline / stale) and CARD-03 (unreconciled card
                          transactions) for one expense report (pre-submit, approval,
                          read time);
* ``affidavit_flag``    — EXP-04 on a line whose receipt is missing;
* ``trip_flags``        — PTA-02, AIR-04, AIR-06, PTA-05 for one trip at approval;
* ``option_notices``    — PTA-02 / AIR-04 / AIR-06 notices for planner options;
* ``selection_change``  — PTA-05 when a planner selection changes an approved trip.

Every rule is optional: a clause whose rule is not active in the organization is not
checked (finance controls it on the policy page).  ``today_utc`` is the one clock —
tests monkeypatch it.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.traveller_grade import owner_grade
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.evidence import receipt_ids_by_entry
from ai_agents.tools import process_rules as pr
from ai_agents.tools.policy_eval import PolicyEvalTool
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.policy_rule import PolicyRuleType
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment

logger = logging.getLogger(__name__)

REPORT_REF = "Report"
CARD_REF = "Corporate card"
TRIP_REF = "Trip"
PROCESS_SOURCE = "process_rules"


def today_utc() -> date:
    """The reference day of every process check (UTC).  Tests monkeypatch this."""
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleRef:
    rule_id: str
    rule_type: str
    definition: Mapping[str, Any]


@dataclass(frozen=True)
class ProcessRules:
    """The active rules of one organization that drive the process checks."""

    by_kind: dict[str, tuple[RuleRef, ...]] = field(default_factory=dict)
    receipt: tuple[RuleRef, ...] = ()  # RECEIPT-REQ rules carrying the EXP-04 keys
    cabin: tuple[RuleRef, ...] = ()  # AIR-CABIN rules carrying the AIR-04 keys

    def of(self, kind: str) -> tuple[RuleRef, ...]:
        return self.by_kind.get(kind, ())

    @property
    def empty(self) -> bool:
        return not (self.by_kind or self.receipt or self.cabin)


def load_process_rules(session: Session, organization_id: uuid.UUID | None) -> ProcessRules:
    """Active process rules + EXP-04 / AIR-04 modifiers of *organization_id* (org-scoped).

    Loaded through ``PolicyEvalTool._load_active_rules`` — the ONE query every rule
    consumer shares (active rules of the caller's organization's policies).
    """
    if organization_id is None:
        return ProcessRules()
    context = AgentContext(organization_id=organization_id, granted_permission_keys=frozenset())
    by_kind: dict[str, list[RuleRef]] = {}
    receipt: list[RuleRef] = []
    cabin: list[RuleRef] = []
    rules = sorted(
        PolicyEvalTool()._load_active_rules(context, session),
        key=lambda r: (str(getattr(r, "created_at", "") or ""), str(r.id)),
    )
    for rule in rules:
        definition = rule.definition
        if not isinstance(definition, Mapping):
            continue
        rule_type = getattr(rule.rule_type, "value", rule.rule_type)
        ref = RuleRef(str(rule.id), str(rule_type), definition)
        kind = pr.process_kind(definition)
        if kind is not None:
            by_kind.setdefault(kind, []).append(ref)
        elif rule_type == PolicyRuleType.receipt_required.value and pr.affidavit_configured(
            definition
        ):
            receipt.append(ref)
        elif rule_type == PolicyRuleType.cabin_class.value and "role_exception_class" in definition:
            cabin.append(ref)
    return ProcessRules(
        by_kind={k: tuple(v) for k, v in by_kind.items()},
        receipt=tuple(receipt),
        cabin=tuple(cabin),
    )


def flag_dict(
    finding: pr.Finding,
    *,
    rule: RuleRef | None,
    entry_ref: str,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """A ``PreSubmitFlag.to_dict``-shaped flag for *finding* (cites its clause).

    QA round: a PROCESS rule's flag carries the rule's process ``kind`` as its
    ``rule_type`` (e.g. ``card_reconciliation``), never the column type the row is
    stored under (``PROCESS_KIND_SHAPES``: ``category_allow`` / ``amount_cap``) —
    otherwise the finance "Rule: category not allowed" filter counted CARD-03 lines.
    A modifier of an ordinary rule (EXP-04 on RECEIPT-REQ, AIR-04 on AIR-CABIN)
    keeps that rule's type.
    """
    rule_type: str | None = None
    if rule is not None:
        rule_type = pr.process_kind(rule.definition) or rule.rule_type
    return {
        "entry_ref": entry_ref,
        "entry_id": entry_id,
        "flag_type": finding.flag_type,
        "rule_id": rule.rule_id if rule else None,
        "rule_type": rule_type,
        "source_clause_ref": finding.clause_ref,
        "reason": finding.reason,
        "requires_approver": finding.requires_approver,
        "observed": {**finding.observed, "process_rule": True},
        "severity": finding.severity,
        "justifiable": False,
        "justification": None,
        "pending_verification": None,
    }


def _safe(rule: RuleRef, fn: Any, *args: Any, **kwargs: Any) -> pr.Finding | None:
    """Run one evaluator; a malformed definition yields an honest not_evaluated finding."""
    try:
        result: pr.Finding | None = fn(*args, **kwargs)
        return result
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning("process_checks: rule %s could not be evaluated: %s", rule.rule_id, exc)
        clause = rule.definition.get("corpus_clause_ref") or pr.DEFAULT_CLAUSE.get(
            str(rule.definition.get("kind")), "policy"
        )
        return pr.Finding(
            clause_ref=str(clause),
            flag_type=pr.NOT_EVALUATED,
            severity="advisory",
            reason=f"Rule {clause} could not be evaluated: its definition is malformed.",
            observed={"could_not_verify": "definition"},
        )


# ---------------------------------------------------------------------------
# Expense reports — EXP-01 and CARD-03
# ---------------------------------------------------------------------------


# Only a trip the organization approved (or that has been completed) has trustworthy
# dates: draft / pending / rejected / cancelled trip dates are whatever the claimant typed.
_ANCHOR_TRIP_STATUSES = (TripStatus.approved, TripStatus.completed)


def report_completion(
    session: Session,
    *,
    trip_id: Any,
    entry_dates: Iterable[Any],
    owner_id: uuid.UUID | None,
    organization_id: uuid.UUID,
    today: date | None = None,
) -> tuple[date | None, str]:
    """(completion day, basis) the EXP-01 deadline runs from.

    The clock starts at the EARLIER of the report's earliest expense date and the
    linked trip's end date — never later than either (integration, security review):
    a trip link can make the deadline earlier (a line dated after the trip ended is
    still due from the trip's end) but never make an older expense look newer (a fare
    booked weeks ahead of the trip is due from its own date).

    The trip's end date is used only when it can be trusted: the trip is the OWNER's in
    the organization, it is approved or completed, and it has already ended (end date
    <= today).  Draft / pending / rejected / cancelled trip dates are whatever the
    claimant typed, so they never move the clock.
    """
    day = today if today is not None else today_utc()
    days = [d for d in (pr.as_date(x) for x in entry_dates) if d is not None]
    earliest: tuple[date | None, str] = (min(days), "the earliest expense") if days else (None, "")
    tid = _as_uuid(trip_id)
    if tid is None or owner_id is None or not days:
        return earliest
    row = session.execute(
        sa.select(Trip.start_date, Trip.end_date).where(
            Trip.id == tid,
            Trip.organization_id == organization_id,
            Trip.traveler_id == owner_id,
            Trip.status.in_(_ANCHOR_TRIP_STATUSES),
        )
    ).first()
    if row is None:
        return earliest
    end_day = pr.as_date(row[1])
    if end_day is None or end_day > day:
        return earliest
    first_line = min(days)
    if first_line < end_day:
        return first_line, "the earliest expense"
    return end_day, "trip completion"


def report_flags(
    session: Session,
    *,
    organization_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    trip_id: Any,
    entry_dates: Sequence[Any],
    rules: ProcessRules | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """EXP-01 (late / stale) and CARD-03 (overdue card transactions) for one report."""
    rules = rules if rules is not None else load_process_rules(session, organization_id)
    day = today if today is not None else today_utc()
    out: list[dict[str, Any]] = []
    deadline_rules = rules.of(pr.KIND_SUBMISSION_DEADLINE)
    if deadline_rules and entry_dates:
        completed, basis = report_completion(
            session,
            trip_id=trip_id,
            entry_dates=entry_dates,
            owner_id=owner_id,
            organization_id=organization_id,
            today=day,
        )
        for rule in deadline_rules:
            finding = _safe(
                rule,
                pr.submission_deadline,
                rule.definition,
                completed_on=completed,
                today=day,
                basis=basis,
            )
            if finding is not None:
                out.append(flag_dict(finding, rule=rule, entry_ref=REPORT_REF))
    card_rules = rules.of(pr.KIND_CARD_RECONCILIATION)
    if card_rules and owner_id is not None:
        rows = session.execute(
            sa.select(CardTransaction.txn_date, CardTransaction.amount, CardTransaction.currency)
            .where(
                CardTransaction.organization_id == organization_id,
                CardTransaction.user_id == owner_id,
                CardTransaction.expense_id.is_(None),
                CardTransaction.txn_date < day,
            )
            .order_by(CardTransaction.txn_date, CardTransaction.id)
            .limit(1000)
        ).all()
        txns = [
            (d, amount, str(currency or "USD"))
            for d, amount, currency in ((pr.as_date(r[0]), r[1], r[2]) for r in rows)
            if d is not None
        ]
        for rule in card_rules:
            finding = _safe(
                rule, pr.card_reconciliation, rule.definition, unreconciled=txns, today=day
            )
            if finding is not None:
                out.append(flag_dict(finding, rule=rule, entry_ref=CARD_REF))
    return out


def merge_report_flags(
    snapshot: Mapping[str, Any] | None, fresh: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """*snapshot* with its stored report-level process flags replaced by *fresh* ones.

    Read time (a draft nobody edited for weeks must still show it is late / stale):
    every stored EXP-01 / CARD-03 flag is dropped, the fresh ones appended, counts
    recomputed.  Nothing else in the snapshot changes; no snapshot and nothing fresh
    stays None.
    """
    if not isinstance(snapshot, Mapping):
        if not fresh:
            return None
        snapshot = {}
    base = dict(snapshot)
    kept = [
        f
        for f in (base.get("flags") or [])
        if isinstance(f, dict)
        and not (
            f.get("entry_ref") in (REPORT_REF, CARD_REF)
            and (f.get("observed") or {}).get("process_rule")
        )
    ]
    flags = [*kept, *fresh]
    base["flags"] = flags
    base["blocking_count"] = sum(1 for f in flags if f.get("severity") == "blocking")
    base["advisory_count"] = sum(1 for f in flags if f.get("severity") == "advisory")
    return base


# ---------------------------------------------------------------------------
# EXP-04 — missing-receipt affidavit
# ---------------------------------------------------------------------------


def affidavits_before(
    session: Session,
    *,
    organization_id: uuid.UUID,
    owner_id: uuid.UUID,
    entry_id: uuid.UUID | None,
    expense_day: date,
    signed_at: datetime,
    ceiling: Decimal | None = None,
    receipt_defn: Mapping[str, Any] | None = None,
) -> int:
    """How many affidavits IN USE the owner signed EARLIER for the same calendar quarter.

    Quarter = calendar quarter of the expense date.  Order = (signed time, entry id),
    so an affidavit keeps its place: a later one never pushes an earlier one out.
    Every report status counts (an affidavit once used stays used).

    An earlier signed affidavit is IN USE (takes one of the quarter's slots) only when it
    stands in for a REQUIRED receipt:

    * its line needs a receipt under RECEIPT-REQ (*receipt_defn*: an always-required
      category, or the amount in USD at / above ``threshold_usd``; mileage never) —
      an affidavit signed on a $10 coffee replaces nothing (integration, EXP-04 blocker);
    * its amount in USD is within *ceiling* (``affidavit_max_usd``): above it an
      affidavit is never accepted (stage 2 review round 2);
    * it is not FREED: a receipt frees the slot only when it is a VALIDATED receipt
      (read by OCR, its machine-read total covering the line and its date matching the
      claim — the pre-submit evidence rules) on a LOCKED line (its report is no longer a
      draft).  A junk receipt, or any receipt on a draft line, frees nothing.  (Attaching
      a receipt to a draft line withdraws the signature altogether — see
      ``backend.app.modules.expense.process_flags``.)

    A line whose currency cannot be converted counts (fail closed: never fewer slots in
    use than there may be).
    """
    start, end = pr.quarter_bounds(expense_day)
    earlier = Expense.receipt_affidavit_at < signed_at
    if entry_id is not None:
        earlier = sa.or_(
            earlier,
            sa.and_(Expense.receipt_affidavit_at == signed_at, Expense.id < entry_id),
        )
    stmt = (
        sa.select(
            Expense.id,
            Expense.amount,
            Expense.currency,
            Expense.category,
            ExpenseReport.status,
        )
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            ExpenseReport.organization_id == organization_id,
            Expense.organization_id == organization_id,
            ExpenseReport.owner_id == owner_id,
            Expense.receipt_affidavit_at.is_not(None),
            Expense.expense_date >= start,
            Expense.expense_date < end,
            earlier,
        )
    )
    if entry_id is not None:
        stmt = stmt.where(Expense.id != entry_id)
    rows = session.execute(stmt).all()
    if not rows:
        return 0
    linked = receipt_ids_by_entry(
        session, organization_id=organization_id, entry_ids=[r[0] for r in rows]
    )
    locked_with_receipt = [
        r[0] for r in rows if linked.get(r[0]) and _status_value(r[4]) != "draft"
    ]
    freed = _validated_receipt_lines(
        session,
        organization_id=organization_id,
        owner_id=owner_id,
        entry_ids=locked_with_receipt,
    )

    def in_use(eid: Any, amount: Any, currency: Any, category: Any) -> bool:
        amount_usd, convertible = pr.amount_in_usd(amount, currency)
        if not convertible:
            return True
        if receipt_defn is not None and not pr.receipt_needed(
            receipt_defn, amount_usd=amount_usd, category=category
        ):
            return False
        if ceiling is not None and (amount_usd is None or amount_usd > ceiling):
            return False
        return eid not in freed

    return sum(1 for eid, amount, ccy, cat, _st in rows if in_use(eid, amount, ccy, cat))


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


def _validated_receipt_lines(
    session: Session,
    *,
    organization_id: uuid.UUID,
    owner_id: uuid.UUID,
    entry_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """The *entry_ids* backed by a VALIDATED receipt: read by OCR (processed) and passing
    the pre-submit evidence rules (machine-read total covers the claim, receipt date
    matches it, never a typed date).  Fail closed: an error validates nothing."""
    if not entry_ids:
        return set()
    # Local import: presubmit imports this module (layering within ai_agents).
    from ai_agents.agents.expense_agent import presubmit  # noqa: PLC0415

    try:
        expenses = list(
            session.scalars(
                sa.select(Expense).where(
                    Expense.id.in_(list(entry_ids)),
                    Expense.organization_id == organization_id,
                )
            ).all()
        )
        drafts = presubmit.draft_entries_for_expenses(
            expenses, session=session, organization_id=organization_id
        )
        context = AgentContext(
            organization_id=organization_id,
            user_id=owner_id,
            granted_permission_keys=frozenset(),
        )
        out: set[uuid.UUID] = set()
        for expense, draft in zip(expenses, drafts, strict=True):
            processed = [
                rid
                for rid in presubmit.entry_receipt_ids(draft)
                if _receipt_processed(session, rid, organization_id=organization_id)
            ]
            if not processed:
                continue
            draft = dataclasses.replace(
                draft, receipt_ids=tuple(processed), primary_receipt_id=processed[0]
            )
            support = presubmit.receipt_support_issue(
                draft, context=context, session=session, owner_id=owner_id
            )
            mismatch = presubmit.receipt_mismatch_issue(
                draft, context=context, session=session, owner_id=owner_id
            )
            if support is None and mismatch is None:
                out.add(expense.id)
        return out
    except Exception as exc:  # noqa: BLE001 — fail closed: nothing is freed
        logger.warning("process_checks: receipt validation failed (%s)", type(exc).__name__)
        return set()


def _receipt_processed(session: Session, receipt_id: Any, *, organization_id: uuid.UUID) -> bool:
    rid = _as_uuid(receipt_id)
    if rid is None:
        return False
    receipt = session.get(Receipt, rid)
    return (
        isinstance(receipt, Receipt)
        and receipt.organization_id == organization_id
        and _status_value(receipt.ocr_status) == OcrStatus.processed.value
    )


def affidavit_flag(
    flag: Mapping[str, Any],
    *,
    session: Session,
    rules: ProcessRules,
    organization_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    entry_id: str | None,
    amount: Any,
    expense_date: Any,
    signed_at: Any,
    justification: str | None,
    currency: Any = None,
) -> dict[str, Any]:
    """EXP-04 applied to one BLOCKING ``missing_receipt`` flag of a line with NO receipt.

    Without an EXP-04 modifier the flag is returned unchanged.  The flag keeps its
    entry / rule ids; its clause, severity, reason and observed come from the
    affidavit finding (appended to the RECEIPT-REQ reason, which names the line).
    """
    if not rules.receipt:
        return dict(flag)
    rule = rules.receipt[0]
    signed = _as_datetime(signed_at)
    day = pr.as_date(expense_date)
    prior = 0
    quarter = pr.quarter_label(day) if day is not None else "this quarter"
    if signed is not None and day is not None and owner_id is not None:
        prior = affidavits_before(
            session,
            organization_id=organization_id,
            owner_id=owner_id,
            entry_id=_as_uuid(entry_id),
            expense_day=day,
            signed_at=signed,
            ceiling=pr.affidavit_ceiling(rule.definition),
            receipt_defn=rule.definition,
        )
    finding = _safe(
        rule,
        pr.affidavit,
        rule.definition,
        amount=amount,
        currency=currency,
        signed=signed is not None and day is not None,
        justification=justification,
        prior_in_quarter=prior,
        quarter=quarter,
    )
    if finding is None:  # pragma: no cover — affidavit() always decides
        return dict(flag)
    out = dict(flag)
    out.update(
        {
            "flag_type": finding.flag_type,
            # Unsigned: the receipt rule still decides (EXP-04 is named as the way out);
            # once signed, EXP-04 decides whether the affidavit stands.
            "source_clause_ref": (
                finding.clause_ref
                if finding.observed.get("affidavit_signed")
                else flag.get("source_clause_ref")
            ),
            "reason": f"{flag.get('reason') or ''} {finding.reason}".strip(),
            "severity": finding.severity,
            "observed": {
                **(flag.get("observed") or {}),
                **finding.observed,
            },
        }
    )
    return out


# ---------------------------------------------------------------------------
# Trips — PTA-02, AIR-04, AIR-06, PTA-05 at approval time
# ---------------------------------------------------------------------------


def _seg_type(seg: Any) -> str:
    st = getattr(seg, "segment_type", None)
    return str(getattr(st, "value", st))


def _details(obj: Any) -> dict[str, Any]:
    d = getattr(obj, "details", None)
    return d if isinstance(d, dict) else {}


def planner_context(trip: Any) -> dict[str, Any] | None:
    """The trip's server-set planner context (POST /trips writes None), else None."""
    ctx = getattr(trip, "recommendation_context", None)
    if isinstance(ctx, dict) and ctx.get("agent_task") == "trip_planning":
        return ctx
    return None


def planner_lowest_fare(trip: Any, segment: Any) -> Decimal | None:
    """The search's lowest comparable fare the PLANNER recorded for this flight segment.

    Trusted only when the trip carries the planner context and the segment's option is
    one the planner selected for it (``details`` alone is client-writable).
    """
    ctx = planner_context(trip)
    if ctx is None:
        return None
    option_id = _details(segment).get("option_id")
    selected = ctx.get("selected_option_ids")
    if not isinstance(option_id, str) or not isinstance(selected, list):
        return None
    if option_id not in {str(x) for x in selected if x}:
        return None
    for snap in ctx.get("selected_options") or []:
        if isinstance(snap, dict) and snap.get("option_id") == option_id:
            return pr.cents(snap.get("lowest_fare_usd"))
    return None


def planner_selected(trip: Any, segment: Any) -> bool:
    """The segment is an option the PLANNER selected for this trip (server-written
    context; the same gate as ``planner_lowest_fare`` / ``is_planner_booked_hotel``)."""
    ctx = planner_context(trip)
    if ctx is None:
        return False
    option_id = _details(segment).get("option_id")
    selected = ctx.get("selected_option_ids")
    return (
        isinstance(option_id, str)
        and isinstance(selected, list)
        and option_id in {str(x) for x in selected if x}
    )


def trusted_departure(trip: Any, segment: Any) -> date | None:
    """PTA-02's departure day for a flight segment (integration, security review).

    A planner-selected segment departs when the planner's option says
    (``start_datetime`` came from the inventory).  Any other segment's date — and the
    trip's start date — were typed by the traveler (POST /trips), so the EARLIER of the
    two counts: a far-future segment date cannot lengthen the lead time, and a segment
    dated outside the trip window can only shorten it.
    """
    seg_day = pr.as_date(getattr(segment, "start_datetime", None))
    trip_day = pr.as_date(getattr(trip, "start_date", None))
    if seg_day is not None and planner_selected(trip, segment):
        return seg_day
    days = [d for d in (seg_day, trip_day) if d is not None]
    return min(days) if days else None


def _segments_total(segments: Sequence[Any]) -> Decimal | None:
    total = Decimal(0)
    for s in segments:
        amount = pr.cents(getattr(s, "amount", None))
        if amount is None:
            return None
        total += amount
    return total


def trip_estimate(trip: Any, segments: Sequence[Any]) -> Decimal | None:
    """max(estimated_cost, sum of segment amounts) — never below what the plan holds."""
    values = [v for v in (pr.cents(trip.estimated_cost), _segments_total(segments)) if v]
    return max(values) if values else None


def effective_change_estimate(
    new_estimate: Any, new_types: Iterable[str], prior_segments: Sequence[Any]
) -> Decimal | None:
    """PTA-05: the cost of the trip AS CHANGED, not just of the re-selected legs.

    A planner change of an approved / under-review trip is a new draft holding only the
    legs picked again; the approved trip's legs of every OTHER type are kept (the same
    rule the in-place UAT2 update applies: a selection replaces the leg of its type).
    Effective = new estimate + those kept legs.  None (could not verify — the approver
    checks) when the new estimate or a kept leg is unpriced / not in USD.
    """
    new = pr.cents(new_estimate)
    if new is None:
        return None
    replaced = {str(t) for t in new_types}
    total = new
    for seg in prior_segments:
        if _seg_type(seg) in replaced:
            continue
        amount = pr.cents(getattr(seg, "amount", None))
        currency = str(getattr(seg, "currency", None) or "USD").upper()
        if amount is None or currency != "USD":
            return None
        total += amount
    return total


def _trip_label(trip: Any) -> str:
    start = pr.as_date(trip.start_date)
    return (
        f"the trip {trip.origin or '?'} → {trip.primary_destination or '?'}"
        f"{f' ({start.isoformat()})' if start else ''}"
    )


def changed_trip(session: Session, trip: Trip) -> Trip | None:
    """The approved / under-review trip this planner trip CHANGES (PTA-05), if any.

    Only the planner records the link (``recommendation_context.changes_trip_id``, set
    when a selection could not change an approved / pending trip of the same route); the
    trip must be the same traveler's, in the same organization.
    """
    ctx = planner_context(trip)
    tid = _as_uuid(ctx.get("changes_trip_id")) if ctx else None
    if tid is None or tid == trip.id:
        return None
    return session.scalars(
        sa.select(Trip).where(
            Trip.id == tid,
            Trip.organization_id == trip.organization_id,
            Trip.traveler_id == trip.traveler_id,
        )
    ).first()


def trip_flags(
    trip: Trip,
    segments: Sequence[TripSegment],
    *,
    session: Session,
    rules: ProcessRules | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """PTA-02 / AIR-04 / AIR-06 per flight segment and PTA-05 for the trip."""
    rules = rules if rules is not None else load_process_rules(session, trip.organization_id)
    if rules.empty:
        return []
    day = today if today is not None else today_utc()
    international = pr.is_international(trip.origin, trip.primary_destination)
    # AIR-04: the TRIP OWNER's grade of record (never the submitter's or a request's).
    grade = (
        owner_grade(session, organization_id=trip.organization_id, user_id=trip.traveler_id)
        if rules.cabin
        else None
    )
    out: list[dict[str, Any]] = []
    for seg in segments:
        if _seg_type(seg) != "flight":
            continue
        ref = f"segment_{seg.id}"
        details = _details(seg)
        departure = trusted_departure(trip, seg)
        for rule in rules.of(pr.KIND_ADVANCE_BOOKING):
            finding = _safe(
                rule,
                pr.advance_booking,
                rule.definition,
                departure=departure,
                booked_on=day,
                international=international,
            )
            if finding is not None:
                out.append(flag_dict(finding, rule=rule, entry_ref=ref))
        for rule in rules.of(pr.KIND_LOWEST_FARE):
            finding = _safe(
                rule,
                pr.lowest_fare,
                rule.definition,
                fare=getattr(seg, "amount", None),
                lowest=planner_lowest_fare(trip, seg),
                international=international,
            )
            if finding is not None:
                out.append(flag_dict(finding, rule=rule, entry_ref=ref))
        for rule in rules.cabin:
            finding = _safe(
                rule,
                pr.role_cabin_exception,
                rule.definition,
                cabin=details.get("cabin_class"),
                duration_hours=details.get("duration_hours"),
                traveller_grade=grade,
            )
            if finding is not None:
                out.append(flag_dict(finding, rule=rule, entry_ref=ref))
    change_rules = rules.of(pr.KIND_TRIP_CHANGE)
    if change_rules:
        prior = changed_trip(session, trip)
        if prior is not None:
            prior_segments = list(
                session.scalars(sa.select(TripSegment).where(TripSegment.trip_id == prior.id)).all()
            )
            baseline = pr.cents(prior.estimated_cost) or _segments_total(prior_segments)
            for rule in change_rules:
                finding = _safe(
                    rule,
                    pr.trip_change,
                    rule.definition,
                    approved_estimate=baseline,
                    new_estimate=effective_change_estimate(
                        trip_estimate(trip, segments),
                        {_seg_type(s) for s in segments},
                        prior_segments,
                    ),
                    approved_label=_trip_label(prior),
                )
                if finding is not None:
                    flag = flag_dict(finding, rule=rule, entry_ref=TRIP_REF)
                    flag["related_subject_id"] = str(prior.id)
                    out.append(flag)
    return out


# ---------------------------------------------------------------------------
# Planner — notices at selection time
# ---------------------------------------------------------------------------


def notice(finding: pr.Finding) -> dict[str, Any]:
    return {
        "clause_ref": finding.clause_ref,
        "flag_type": finding.flag_type,
        "message": finding.reason,
        "requires_approver": finding.requires_approver,
        "blocking": finding.severity == "blocking",
    }


def option_notices(
    rules: ProcessRules,
    *,
    flights: Sequence[tuple[str, str | None, Any, Any]],
    departure: Any,
    origin: Any,
    destination: Any,
    today: date | None = None,
    traveller_grade: str | None = None,
) -> tuple[dict[str, list[pr.Finding]], dict[str, Decimal]]:
    """PTA-02 / AIR-06 / AIR-04 findings per flight option of ONE planner search.

    *traveller_grade*: the requesting traveller's grade of record (``owner_grade``);
    None = unknown, so AIR-04 is never granted automatically.
    *flights*: (option_id, cabin_class, fare, duration_hours).  Returns (findings by
    option id, lowest comparable fare by option id — recorded on the selection so the
    approval-time AIR-06 check compares against the same search).
    """
    lowest = pr.lowest_comparable_fares((oid, cabin, fare) for oid, cabin, fare, _h in flights)
    out: dict[str, list[pr.Finding]] = {}
    if rules.empty:
        return out, lowest
    day = today if today is not None else today_utc()
    international = pr.is_international(origin, destination)
    dep = pr.as_date(departure)
    for oid, cabin, fare, hours in flights:
        found: list[pr.Finding] = []
        for rule in rules.of(pr.KIND_ADVANCE_BOOKING):
            f = _safe(
                rule,
                pr.advance_booking,
                rule.definition,
                departure=dep,
                booked_on=day,
                international=international,
            )
            if f is not None:
                found.append(f)
        for rule in rules.of(pr.KIND_LOWEST_FARE):
            f = _safe(
                rule,
                pr.lowest_fare,
                rule.definition,
                fare=fare,
                lowest=lowest.get(oid),
                international=international,
            )
            if f is not None:
                found.append(f)
        for rule in rules.cabin:
            f = _safe(
                rule,
                pr.role_cabin_exception,
                rule.definition,
                cabin=cabin,
                duration_hours=hours,
                traveller_grade=traveller_grade,
            )
            if f is not None:
                found.append(f)
        if found:
            out[oid] = found
    return out, lowest


def selection_change(
    session: Session,
    *,
    organization_id: uuid.UUID,
    previous: Trip,
    new_estimate: Any,
    new_types: Iterable[str],
) -> tuple[bool, pr.Finding | None]:
    """PTA-05 at selection time: a selection changing an approved / pending trip.

    *new_types*: the segment types the selection re-picked — the approved trip's legs
    of the other types are kept and counted (``effective_change_estimate``).
    Returns (checked, finding): checked is False when no PTA-05 rule is active.
    """
    rules = load_process_rules(session, organization_id)
    change_rules = rules.of(pr.KIND_TRIP_CHANGE)
    if not change_rules:
        return False, None
    prior_segments = list(
        session.scalars(sa.select(TripSegment).where(TripSegment.trip_id == previous.id)).all()
    )
    baseline = pr.cents(previous.estimated_cost) or _segments_total(prior_segments)
    for rule in change_rules:
        finding = _safe(
            rule,
            pr.trip_change,
            rule.definition,
            approved_estimate=baseline,
            new_estimate=effective_change_estimate(new_estimate, new_types, prior_segments),
            approved_label=_trip_label(previous),
        )
        if finding is not None:
            return True, finding
    return True, None


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


__all__ = [
    "CARD_REF",
    "PROCESS_SOURCE",
    "REPORT_REF",
    "TRIP_REF",
    "ProcessRules",
    "RuleRef",
    "affidavit_flag",
    "flag_dict",
    "load_process_rules",
    "merge_report_flags",
    "option_notices",
    "report_flags",
    "selection_change",
    "today_utc",
    "trip_flags",
]
