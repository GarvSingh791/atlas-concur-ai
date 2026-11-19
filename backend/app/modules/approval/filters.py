"""Finance review filters (Product gap 6, PRD US-F2 AC1) — approvals + expense reports.

US-F2 AC1: "I can filter expense reports/trips by exception type, status,
department, and date."  Status filters already existed; this module adds the
rest as ALLOW-LISTED, server-side predicates that compose with the existing
deny-by-default ``parse_filters`` allow-lists (unknown keys stay a 400) and are
always applied AFTER ``scope_query`` — they only ever AND a predicate onto an
already tenant+role-scoped statement, so they can narrow but never widen.

Kept free of the approval/expense *services* (both import this module) so
there is no import cycle: it depends only on models, core errors and the agent
flag constants.

Exception types
---------------
The canonical set is defined from what the data actually carries.  Two JSONB
documents hold policy exceptions, both shaped ``{"flags": [ {...}, ... ]}``:

  * ``expense_reports.pre_submit_flags`` — the pre-submit policy check
    (``PreSubmitFlag.to_dict``: ``flag_type`` + ``rule_type`` + severity, ...);
  * ``approvals.risk_summary`` — the Approval Agent's snapshot (the report's
    stored flags + fresh flags, or a trip's per-segment rows
    ``{"segment_id", "in_policy", "policy_notes"}`` + trip-level flags).

An exception type is therefore one of
  * a ``flag_type`` emitted by a producer (pre-submit check / approval agent),
  * a policy ``rule_type`` (``PolicyRuleType``) carried on a rule-verdict flag,
    or a process rule's ``kind`` (CARD-03 ``card_reconciliation``, ...) — read
    through ``effective_rule_type`` so legacy process flags stored under their
    column type (``category_allow`` / ``amount_cap``) never count as that type,
  * ``out_of_policy_segment`` — a trip segment row with ``in_policy: false``
    (trip rows carry no ``flag_type``).

Matching uses JSONB containment (``@>``) with the fragment bound as a parameter
(never string-built SQL).  ``exception_types_of`` is the Python twin used to
label rows (list items + CSV export) so the label a row shows and the filter
that finds it can never disagree.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from ai_agents.agents.approval_agent.evaluate import (
    LINES_REMOVED_FLAG,
    OPEN_RETURNED_REPORT_FLAG,
    POSSIBLE_REFILE_FLAG,
    POSSIBLE_SPLIT_TRIP_FLAG,
    REFILED_REJECTED_FLAG,
    REFILED_RETURNED_FLAG,
)
from ai_agents.agents.expense_agent.presubmit import (
    POLICY_ADVISORY,
    POSSIBLE_DUPLICATE,
    RECEIPT_MISMATCH,
)
from ai_agents.tools import process_rules as pr
from ai_agents.tools.fx_policy import CURRENCY_UNVERIFIED_FLAG
from backend.app.core.errors import BadRequestError
from database.models.approval import Approval, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.policy_rule import PolicyRuleType
from database.models.trip import Trip

# ---------------------------------------------------------------------------
# Canonical exception types
# ---------------------------------------------------------------------------

KIND_FLAG = "flag"
KIND_RULE = "rule"
KIND_SEGMENT = "segment"

OUT_OF_POLICY_SEGMENT = "out_of_policy_segment"


@dataclass(frozen=True)
class ExceptionType:
    """One filterable exception type: its value, display label and JSON matcher."""

    value: str
    label: str
    kind: str  # "flag" | "rule" | "segment"

    @property
    def flag_fragment(self) -> dict[str, Any]:
        """The single-flag JSON object a matching ``flags`` element contains."""
        if self.kind == KIND_RULE:
            return {"rule_type": self.value}
        if self.kind == KIND_SEGMENT:
            return {"in_policy": False}
        return {"flag_type": self.value}

    @property
    def containment(self) -> dict[str, Any]:
        """The ``{"flags": [fragment]}`` document used with JSONB ``@>``."""
        return {"flags": [self.flag_fragment]}


_FLAG_TYPES: tuple[tuple[str, str], ...] = (
    ("policy_violation", "Policy violation"),
    ("missing_receipt", "Missing receipt"),
    ("approval_required", "Pre-approval required"),
    ("not_evaluated", "Not evaluated"),
    (RECEIPT_MISMATCH, "Receipt mismatch"),
    (POSSIBLE_DUPLICATE, "Possible duplicate"),
    (POSSIBLE_SPLIT_TRIP_FLAG, "Possible split trip"),
    (OPEN_RETURNED_REPORT_FLAG, "Open returned report"),
    (REFILED_REJECTED_FLAG, "Re-filed rejected claim"),
    (REFILED_RETURNED_FLAG, "Re-filed withdrawn return"),
    # Phase 10: a re-typed line resembling one of a withdrawn return (advisory).
    (POSSIBLE_REFILE_FLAG, "Possible re-filed claim"),
    (LINES_REMOVED_FLAG, "Lines removed since return"),
    # Product gap 8 (fxcard): a foreign amount with no usable rate is escalated.
    (CURRENCY_UNVERIFIED_FLAG, "Currency not verified"),
    # Product gap 2 (policy tracks): an in-policy outcome through a clause's own
    # allowance (LOD-02 tolerance), EXP-01 late submission, CARD-03 overdue card lines.
    (POLICY_ADVISORY, "Policy advisory"),
    (pr.LATE_SUBMISSION, "Late submission"),
    (pr.UNRECONCILED_CARD, "Unreconciled card transactions"),
)

_RULE_LABELS: dict[str, str] = {
    PolicyRuleType.amount_cap.value: "Rule: amount cap",
    PolicyRuleType.category_allow.value: "Rule: category not allowed",
    PolicyRuleType.cabin_class.value: "Rule: cabin class",
    PolicyRuleType.per_diem.value: "Rule: per diem",
    PolicyRuleType.preferred_supplier.value: "Rule: preferred supplier",
    PolicyRuleType.receipt_required.value: "Rule: receipt required",
    # Product gap 2 (policy2): MEAL-05 / ENT-02, ENT-04, MIL-01..03.
    PolicyRuleType.per_head_cap.value: "Rule: per-head cap",
    PolicyRuleType.gift_disclosure.value: "Rule: gift disclosure",
    PolicyRuleType.mileage.value: "Rule: mileage",
}

# QA round: the PROCESS rules (ai_agents/tools/process_rules.py) are stored under an
# existing column type (``PROCESS_KIND_SHAPES``: category_allow / amount_cap) but are
# their own kind of rule.  Their flags carry the process ``kind`` as ``rule_type``
# (``process_checks.flag_dict``) and are filtered / labelled under it.
_PROCESS_RULE_LABELS: dict[str, str] = {
    pr.KIND_SUBMISSION_DEADLINE: "Rule: submission deadline",
    pr.KIND_ADVANCE_BOOKING: "Rule: advance booking",
    pr.KIND_TRIP_CHANGE: "Rule: trip cost change",
    pr.KIND_LOWEST_FARE: "Rule: lowest fare",
    pr.KIND_CARD_RECONCILIATION: "Rule: card reconciliation",
    # Follow-up (grades): AIR-07 / LOD-05 / NR-01 (SVPs and above).
    pr.KIND_GRADE_ENTITLEMENT: "Rule: grade entitlement",
}

# Flags stored before the QA round carry the storage type (e.g. a CARD-03 flag with
# ``rule_type=category_allow``).  They are mapped at READ time, never rewritten:
#   * a flag type only one process rule emits names that rule's kind;
#   * any other process-rule flag (``observed.process_rule``) stored under a process
#     storage type claims no rule type at all (its flag type still labels it).
LEGACY_PROCESS_FLAG_KIND: dict[str, str] = {
    pr.UNRECONCILED_CARD: pr.KIND_CARD_RECONCILIATION,
    pr.LATE_SUBMISSION: pr.KIND_SUBMISSION_DEADLINE,
}
PROCESS_STORAGE_RULE_TYPES: frozenset[str] = frozenset(
    rt for rt, _ in pr.PROCESS_KIND_SHAPES.values()
)

EXCEPTION_TYPES: dict[str, ExceptionType] = {
    **{v: ExceptionType(v, label, KIND_FLAG) for v, label in _FLAG_TYPES},
    **{
        rt.value: ExceptionType(rt.value, _RULE_LABELS.get(rt.value, rt.value), KIND_RULE)
        for rt in PolicyRuleType
    },
    **{k: ExceptionType(k, label, KIND_RULE) for k, label in _PROCESS_RULE_LABELS.items()},
    OUT_OF_POLICY_SEGMENT: ExceptionType(
        OUT_OF_POLICY_SEGMENT, "Out-of-policy trip segment", KIND_SEGMENT
    ),
}


def parse_exception_type(value: str) -> ExceptionType:
    """Resolve a filter value to a canonical exception type (deny-by-default → 400)."""
    key = value.strip()
    found = EXCEPTION_TYPES.get(key)
    if found is None:
        valid = ", ".join(EXCEPTION_TYPES)
        raise BadRequestError(f"Invalid exception_type '{value}'. Valid: {valid}.")
    return found


def _flags_of(document: object) -> list[Mapping[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    flags = document.get("flags")
    if not isinstance(flags, list):
        return []
    return [f for f in flags if isinstance(f, Mapping)]


def _is_process_flag(flag: Mapping[str, Any]) -> bool:
    observed = flag.get("observed")
    return isinstance(observed, Mapping) and observed.get("process_rule") is True


def effective_rule_type(flag: Mapping[str, Any]) -> str | None:
    """The rule type a stored flag is filtered and labelled under (legacy-aware).

    Python twin of ``_rule_predicate`` (the SQL match).
    """
    flag_type = flag.get("flag_type")
    if isinstance(flag_type, str) and flag_type in LEGACY_PROCESS_FLAG_KIND:
        return LEGACY_PROCESS_FLAG_KIND[flag_type]
    rule_type = flag.get("rule_type")
    if not isinstance(rule_type, str):
        return None
    if rule_type in PROCESS_STORAGE_RULE_TYPES and _is_process_flag(flag):
        return None
    return rule_type


def exception_types_of(*documents: object) -> list[str]:
    """Canonical exception-type values present in the given flag documents.

    Python twin of the SQL containment match (same three rules), returned in
    canonical order so rows render deterministically.
    """
    present: set[str] = set()
    for doc in documents:
        for flag in _flags_of(doc):
            flag_type = flag.get("flag_type")
            if isinstance(flag_type, str) and flag_type in EXCEPTION_TYPES:
                if EXCEPTION_TYPES[flag_type].kind == KIND_FLAG:
                    present.add(flag_type)
            rule_type = effective_rule_type(flag)
            if isinstance(rule_type, str) and rule_type in EXCEPTION_TYPES:
                if EXCEPTION_TYPES[rule_type].kind == KIND_RULE:
                    present.add(rule_type)
            if flag.get("in_policy") is False:
                present.add(OUT_OF_POLICY_SEGMENT)
    return [v for v in EXCEPTION_TYPES if v in present]


def exception_labels(values: Iterable[str]) -> list[str]:
    return [EXCEPTION_TYPES[v].label for v in values if v in EXCEPTION_TYPES]


def _containment(column: Any, etype: ExceptionType) -> sa.ColumnElement[bool]:
    # Parameter-bound JSONB fragment (the same pattern refresh_duplicate_peers uses).
    return column.op("@>")(sa.cast(etype.containment, JSONB))


def _path_exists(column: Any, path: str, variables: dict[str, Any]) -> sa.ColumnElement[bool]:
    """``jsonb_path_exists`` with a constant path and parameter-bound variables."""
    return sa.func.jsonb_path_exists(
        column, sa.cast(sa.literal(path), JSONPATH), sa.cast(variables, JSONB)
    )


# Constant paths (no value is ever spliced in: every value is a bound $variable).
_LEGACY_VARS = {f"lf{i}": ft for i, ft in enumerate(sorted(LEGACY_PROCESS_FLAG_KIND))}
_STORAGE_TYPE_PATH = (
    "$.flags[*] ? (@.rule_type == $rt && !(@.observed.process_rule == true)"
    + "".join(f" && !(@.flag_type == ${name})" for name in _LEGACY_VARS)
    + ")"
)
_PROCESS_KIND_PATH = "$.flags[*] ? (@.rule_type == $rt || @.flag_type == $lf)"


def _rule_predicate(column: Any, etype: ExceptionType) -> sa.ColumnElement[bool]:
    """SQL twin of ``effective_rule_type`` for one rule exception type."""
    if etype.value in PROCESS_STORAGE_RULE_TYPES:
        # Index-friendly containment, minus process-rule flags stored under the type.
        return sa.and_(
            _containment(column, etype),
            _path_exists(column, _STORAGE_TYPE_PATH, {"rt": etype.value, **_LEGACY_VARS}),
        )
    legacy = [ft for ft, kind in LEGACY_PROCESS_FLAG_KIND.items() if kind == etype.value]
    if legacy:
        (flag_type,) = legacy
        return _path_exists(column, _PROCESS_KIND_PATH, {"rt": etype.value, "lf": flag_type})
    return _containment(column, etype)


def _contains(column: Any, etype: ExceptionType) -> sa.ColumnElement[bool]:
    if etype.kind == KIND_RULE:
        return _rule_predicate(column, etype)
    return _containment(column, etype)


def approval_exception_predicate(etype: ExceptionType) -> sa.ColumnElement[bool]:
    """Approval carries *etype* in its risk_summary OR its report subject's pre-submit flags."""
    report_has = sa.exists().where(
        ExpenseReport.id == Approval.subject_id,
        ExpenseReport.organization_id == Approval.organization_id,
        _contains(ExpenseReport.pre_submit_flags, etype),
    )
    return sa.or_(
        _contains(Approval.risk_summary, etype),
        sa.and_(Approval.subject_type == ApprovalSubjectType.expense_report, report_has),
    )


def report_exception_predicate(etype: ExceptionType) -> sa.ColumnElement[bool]:
    return _contains(ExpenseReport.pre_submit_flags, etype)


# ---------------------------------------------------------------------------
# Value parsing (validated; every failure is a 400 in the standard envelope)
# ---------------------------------------------------------------------------

_MAX_DEPARTMENT_LEN = 120
_MONEY_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")


def parse_iso_date(field: str, value: str) -> date:
    """Strict ISO calendar date (YYYY-MM-DD)."""
    raw = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise BadRequestError(f"Invalid date for '{field}': '{value}' (expected YYYY-MM-DD).")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BadRequestError(
            f"Invalid date for '{field}': '{value}' (expected YYYY-MM-DD)."
        ) from exc


def day_start(d: date) -> datetime:
    """Inclusive lower bound: 00:00 UTC on *d*."""
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def day_end_exclusive(d: date) -> datetime:
    """Exclusive upper bound making a ``*_to`` date inclusive: 00:00 UTC the next day."""
    return day_start(d + timedelta(days=1))


def parse_money(field: str, value: str) -> Decimal:
    """Non-negative money amount with at most 2 decimals (Decimal, never float)."""
    raw = value.strip()
    if not _MONEY_RE.fullmatch(raw):
        raise BadRequestError(
            f"Invalid amount for '{field}': '{value}' (expected a non-negative number, "
            "at most 2 decimals)."
        )
    try:
        return Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise BadRequestError(f"Invalid amount for '{field}': '{value}'.") from exc


def parse_department(value: str) -> str:
    dept = value.strip()
    if not dept or len(dept) > _MAX_DEPARTMENT_LEN:
        raise BadRequestError(f"Invalid department (expected 1-{_MAX_DEPARTMENT_LEN} characters).")
    return dept


def parse_uuid(field: str, value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError) as exc:
        raise BadRequestError(f"Invalid UUID for field '{field}': '{value}'.") from exc


def _check_range(filters: Mapping[str, str], low: str, high: str, parse: Any) -> None:
    if low in filters and high in filters and parse(low, filters[low]) > parse(high, filters[high]):
        raise BadRequestError(f"'{low}' must not be after '{high}'.")


def split_filters(
    filters: Mapping[str, str], extended: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Split parsed filters into (pre-existing core filters, extended filters)."""
    core = {k: v for k, v in filters.items() if k not in extended}
    ext = {k: v for k, v in filters.items() if k in extended}
    return core, ext


def _department_owner_ids(organization_id: uuid.UUID, department: str) -> sa.Select[Any]:
    """Users of *organization_id* in *department* (case-insensitive exact match)."""
    return sa.select(User.id).where(
        User.organization_id == organization_id,
        sa.func.lower(User.department) == department.lower(),
    )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

APPROVAL_EXTENDED_FILTER_FIELDS: set[str] = {
    "exception_type",
    "department",
    "owner_id",
    "submitted_from",
    "submitted_to",
    "decided_from",
    "decided_to",
    "amount_min",
    "amount_max",
}


def _approval_amount_predicate(op: str, amount: Decimal) -> sa.ColumnElement[bool]:
    """Polymorphic subject amount (report total / trip estimated cost) compared to *amount*."""

    def cmp(col: Any) -> sa.ColumnElement[bool]:
        return col >= amount if op == ">=" else col <= amount

    report_ok = sa.exists().where(
        ExpenseReport.id == Approval.subject_id,
        ExpenseReport.organization_id == Approval.organization_id,
        cmp(ExpenseReport.total_amount),
    )
    trip_ok = sa.exists().where(
        Trip.id == Approval.subject_id,
        Trip.organization_id == Approval.organization_id,
        cmp(Trip.estimated_cost),
    )
    return sa.or_(
        sa.and_(Approval.subject_type == ApprovalSubjectType.expense_report, report_ok),
        sa.and_(Approval.subject_type == ApprovalSubjectType.trip, trip_ok),
    )


def apply_approval_extended_filters(
    stmt: sa.Select[Any], filters: Mapping[str, str], *, organization_id: uuid.UUID
) -> sa.Select[Any]:
    """AND the extended approval filters onto an already-scoped statement."""
    _check_range(filters, "submitted_from", "submitted_to", parse_iso_date)
    _check_range(filters, "decided_from", "decided_to", parse_iso_date)
    _check_range(filters, "amount_min", "amount_max", parse_money)

    # decided_from / decided_to bound ONE decided step (a single EXISTS): two
    # steps straddling the range must not satisfy it together.
    decided_bounds: list[sa.ColumnElement[bool]] = []
    if "decided_from" in filters:
        d = parse_iso_date("decided_from", filters["decided_from"])
        decided_bounds.append(ApprovalStep.decided_at >= day_start(d))
    if "decided_to" in filters:
        d = parse_iso_date("decided_to", filters["decided_to"])
        decided_bounds.append(ApprovalStep.decided_at < day_end_exclusive(d))
    if decided_bounds:
        stmt = stmt.where(
            sa.exists().where(
                ApprovalStep.approval_id == Approval.id,
                ApprovalStep.decided_at.isnot(None),
                *decided_bounds,
            )
        )

    for field, value in filters.items():
        if field == "exception_type":
            stmt = stmt.where(approval_exception_predicate(parse_exception_type(value)))
        elif field == "department":
            dept = parse_department(value)
            stmt = stmt.where(
                Approval.submitter_id.in_(_department_owner_ids(organization_id, dept))
            )
        elif field == "owner_id":
            stmt = stmt.where(Approval.submitter_id == parse_uuid(field, value))
        elif field == "submitted_from":
            stmt = stmt.where(Approval.created_at >= day_start(parse_iso_date(field, value)))
        elif field == "submitted_to":
            stmt = stmt.where(Approval.created_at < day_end_exclusive(parse_iso_date(field, value)))
        elif field == "amount_min":
            stmt = stmt.where(_approval_amount_predicate(">=", parse_money(field, value)))
        elif field == "amount_max":
            stmt = stmt.where(_approval_amount_predicate("<=", parse_money(field, value)))
    return stmt


# ---------------------------------------------------------------------------
# Expense reports
# ---------------------------------------------------------------------------

REPORT_EXTENDED_FILTER_FIELDS: set[str] = {
    "exception_type",
    "department",
    "submitted_from",
    "submitted_to",
    "amount_min",
    "amount_max",
}


def apply_report_extended_filters(
    stmt: sa.Select[Any], filters: Mapping[str, str], *, organization_id: uuid.UUID
) -> sa.Select[Any]:
    """AND the extended expense-report filters onto an already-scoped statement."""
    _check_range(filters, "submitted_from", "submitted_to", parse_iso_date)
    _check_range(filters, "amount_min", "amount_max", parse_money)

    for field, value in filters.items():
        if field == "exception_type":
            stmt = stmt.where(report_exception_predicate(parse_exception_type(value)))
        elif field == "department":
            dept = parse_department(value)
            stmt = stmt.where(
                ExpenseReport.owner_id.in_(_department_owner_ids(organization_id, dept))
            )
        elif field == "submitted_from":
            stmt = stmt.where(ExpenseReport.submitted_at >= day_start(parse_iso_date(field, value)))
        elif field == "submitted_to":
            stmt = stmt.where(
                ExpenseReport.submitted_at < day_end_exclusive(parse_iso_date(field, value))
            )
        elif field == "amount_min":
            stmt = stmt.where(ExpenseReport.total_amount >= parse_money(field, value))
        elif field == "amount_max":
            stmt = stmt.where(ExpenseReport.total_amount <= parse_money(field, value))
    return stmt
