"""Entry policy fields (product gap 2): attendees, gift disclosure and mileage.

* ``current_mileage_rate`` — the MIL-01 rate of the caller's organisation (the
  active ``mileage`` / ``check: rate`` rule; the canonical figure when none).
* ``mileage_record`` — the JSON stored in ``expenses.mileage``: what the claimant
  entered + what the server derived (billable miles, rate, clause).
* ``apply_entry_policy_fields`` — the PATCH /expenses/{id} rules for these
  fields (category gating, mileage recomputation, client amount refused).
* ``add_mileage_entry`` — POST /expense-reports/{id}/mileage.

The amount of a mileage entry is ALWAYS ``billable miles x rate`` computed here
in Decimal (``ai_agents.tools.spend_policy.compute_mileage``); a client-sent
amount that disagrees is refused (422), never stored.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.tools.spend_policy import (
    ATTENDEE_CATEGORIES,
    DISCLOSURE_CATEGORIES,
    MILEAGE_CATEGORY,
    MileageComputation,
    compute_mileage,
    to_decimal,
)
from backend.app.core.audit import record_audit
from backend.app.core.errors import PreconditionRequiredError, UnprocessableEntityError
from backend.app.core.idempotency import idempotency_lookup, idempotency_record
from backend.app.modules.expense.schemas import (
    DisclosureInput,
    ExpenseEntryResponse,
    MileageEntryCreate,
    MileageInput,
    MileageRateResponse,
)
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.identity import User
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule, PolicyRuleStatus, PolicyRuleType
from database.seeds.corpus import figures as F

logger = logging.getLogger(__name__)

MILEAGE_RATE_CLAUSE = "MIL-01"
MILEAGE_CURRENCY = "USD"  # MIL-01: the IRS rate is US dollars per mile
MILEAGE_IDEMPOTENCY_SCOPE = "expense.add_mileage"


def current_mileage_rate(session: Session, organization_id: uuid.UUID) -> Decimal:
    """The MIL-01 rate for *organization_id* (Decimal USD per mile).

    The ACTIVE ``mileage`` rule with ``check: rate`` (org-scoped via its policy);
    several -> the LOWEST (fail closed); none / unreadable -> the canonical figure
    (``figures.MILEAGE_RATE_USD_PER_MILE``).
    """
    rows = session.scalars(
        sa.select(PolicyRule.definition)
        .join(Policy, PolicyRule.policy_id == Policy.id)
        .where(
            Policy.organization_id == organization_id,
            PolicyRule.status == PolicyRuleStatus.active,
            PolicyRule.rule_type == PolicyRuleType.mileage,
        )
    ).all()
    rates = []
    for definition in rows:
        if isinstance(definition, dict) and definition.get("check") == "rate":
            rate = to_decimal(definition.get("rate_per_mile"))
            if rate is not None and rate > 0:
                rates.append(rate)
    if rates:
        return min(rates)
    return Decimal(F.MILEAGE_RATE_USD_PER_MILE)


def mileage_rate_response(session: Session, current_user: User) -> MileageRateResponse:
    """GET /mileage-rate: the caller's organisation's MIL-01 rate (read-only)."""
    return MileageRateResponse(
        rate_per_mile=current_mileage_rate(session, current_user.organization_id),
        currency=MILEAGE_CURRENCY,
        rate_clause=MILEAGE_RATE_CLAUSE,
    )


def _compute(trip: MileageInput, rate: Decimal) -> MileageComputation:
    try:
        return compute_mileage(
            distance=trip.distance,
            unit=trip.unit,
            round_trip=trip.round_trip,
            commute_offset=trip.commute_offset,
            rate_per_mile=rate,
        )
    except ValueError as exc:
        raise UnprocessableEntityError(f"Mileage could not be computed: {exc}.") from exc


def mileage_record(trip: MileageInput, comp: MileageComputation) -> dict[str, Any]:
    """The JSON stored in ``expenses.mileage`` (Decimals as strings, never floats)."""
    return {
        "distance": str(trip.distance),
        "unit": trip.unit,
        "origin": trip.origin,
        "destination": trip.destination,
        "round_trip": trip.round_trip,
        "commute_offset": str(trip.commute_offset),
        "air_impractical": trip.air_impractical,
        "one_way_miles": str(comp.one_way_miles),
        "claimed_miles": str(comp.claimed_miles),
        "commute_offset_miles": str(comp.commute_offset_miles),
        "billable_miles": str(comp.billable_miles),
        "rate_per_mile": str(comp.rate_per_mile),
        "rate_clause": MILEAGE_RATE_CLAUSE,
    }


def _refuse_client_amount(sent: Any, computed: Decimal) -> None:
    amount = to_decimal(sent)
    if amount is not None and amount != computed:
        raise UnprocessableEntityError(
            f"The amount of a mileage entry is computed by Atlas ({computed} USD = business "
            "miles x the MIL-01 rate) and cannot be set."
        )


RECEIPT_ON_MILEAGE_MESSAGE = (
    "A mileage entry needs no receipt and cannot hold one: it is documented by its route "
    "and distance and its amount is computed by Atlas (REF-08, MIL-01)."
)
# Entry columns a receipt correction may never write on a mileage entry: the
# amount is computed (MIL-01), the currency is USD and the category is fixed.
MILEAGE_COMPUTED_COLUMNS = frozenset({"amount", "currency", "category"})


def is_mileage_entry(entry: Expense) -> bool:
    return getattr(entry.category, "value", entry.category) == MILEAGE_CATEGORY


def refuse_receipt_on_mileage(entry: Expense) -> None:
    """422 when a receipt would be attached to a mileage entry (review fix).

    REF-08: mileage is documented by its trip, and a linked receipt's corrections
    would otherwise flow onto the computed amount / currency / category.
    """
    if is_mileage_entry(entry):
        raise UnprocessableEntityError(RECEIPT_ON_MILEAGE_MESSAGE)


def clear_unfit_policy_fields(entry: Expense) -> None:
    """Clear the policy fields the entry's (changed) category no longer carries.

    Used by category changes that do not go through ``apply_entry_policy_fields``
    (a receipt ``expense_type`` correction), so a stale attendee count or gift
    disclosure never outlives its category.
    """
    category = getattr(entry.category, "value", entry.category)
    if category not in ATTENDEE_CATEGORIES:
        entry.attendee_count = None
    if category not in DISCLOSURE_CATEGORIES:
        entry.disclosure = None
    if category != MILEAGE_CATEGORY:
        entry.mileage = None


def _disclosure_record(value: DisclosureInput | None) -> dict[str, Any] | None:
    if value is None:
        return None
    record = value.model_dump()
    return record if any(v is not None for v in record.values()) else None


def route_description(origin: Any, destination: Any) -> str:
    """The description Atlas generates for a mileage entry: '<origin> → <destination>'."""
    return f"{origin} → {destination}"


def apply_entry_policy_fields(
    session: Session,
    entry: Expense,
    update_data: dict[str, Any],
    payload: Any,
) -> dict[str, Any]:
    """Apply the product-gap-2 fields of a PATCH and return the REMAINING generic fields.

    Raises 422 when a field does not fit the entry's (new) category, when a
    category change would turn an entry into / out of a mileage entry, or when a
    client amount disagrees with the computed mileage amount.  Fields that no
    longer fit a changed category are cleared (never left stale).
    """
    remaining = dict(update_data)
    old_category = getattr(entry.category, "value", entry.category)
    new_category = remaining.get("category", entry.category)
    new_category = getattr(new_category, "value", new_category)
    is_mileage = old_category == MILEAGE_CATEGORY

    if (new_category == MILEAGE_CATEGORY) != is_mileage:
        raise UnprocessableEntityError(
            "A mileage entry's category cannot be changed (delete it instead), and other "
            "entries cannot become mileage — use Add mileage."
            if is_mileage
            else "Use Add mileage to claim personal-vehicle miles: the amount is computed "
            "from the distance (MIL-01)."
        )

    if "attendee_count" in remaining:
        count = remaining.pop("attendee_count")
        if count is not None and new_category not in ATTENDEE_CATEGORIES:
            raise UnprocessableEntityError(
                "Attendees can only be recorded on meals or client entertainment entries."
            )
        entry.attendee_count = count
    if "disclosure" in remaining:
        remaining.pop("disclosure")
        record = _disclosure_record(getattr(payload, "disclosure", None))
        if record is not None and new_category not in DISCLOSURE_CATEGORIES:
            raise UnprocessableEntityError(
                "A gift disclosure can only be recorded on gift or entertainment entries."
            )
        entry.disclosure = record
    if new_category not in ATTENDEE_CATEGORIES:
        entry.attendee_count = None
    if new_category not in DISCLOSURE_CATEGORIES:
        entry.disclosure = None

    # Review fix: checked for EVERY mileage edit (with or without a trip) — the
    # amount is computed in USD (MIL-01), so it can never be relabelled.  Popped
    # so the generic setattr loop in update_entry can never overwrite it.
    if is_mileage and "currency" in remaining:
        if remaining.pop("currency") != MILEAGE_CURRENCY:
            raise UnprocessableEntityError("A mileage entry is always in USD (MIL-01).")

    if "mileage" in remaining:
        remaining.pop("mileage")
        trip = getattr(payload, "mileage", None)
        if not is_mileage:
            raise UnprocessableEntityError("Mileage details belong to mileage entries only.")
        if trip is None:
            raise UnprocessableEntityError("A mileage entry needs its trip details.")
        comp = _compute(trip, current_mileage_rate(session, entry.organization_id))
        _refuse_client_amount(remaining.pop("amount", None), comp.amount)
        # Review fix: the generated description is the route an approver reads —
        # it follows the trip while it is still Atlas's own text (a description
        # the claimant typed, or one sent in this PATCH, is never overwritten).
        old_trip = entry.mileage if isinstance(entry.mileage, dict) else {}
        if "description" not in remaining and entry.description == route_description(
            old_trip.get("origin"), old_trip.get("destination")
        ):
            entry.description = route_description(trip.origin, trip.destination)
        entry.mileage = mileage_record(trip, comp)
        entry.amount = comp.amount
        entry.currency = MILEAGE_CURRENCY
    elif is_mileage:
        _refuse_client_amount(remaining.pop("amount", None), Decimal(str(entry.amount)))
    return remaining


def add_mileage_entry(
    session: Session,
    current_user: User,
    report_id: uuid.UUID,
    payload: MileageEntryCreate,
    *,
    expected_version: int | None = None,
    idempotency_key: str | None = None,
) -> ExpenseEntryResponse:
    """POST /expense-reports/{id}/mileage — add a server-computed mileage entry.

    Owner-only, DRAFT report (404 unreadable / 403 not owner / 409 not draft or
    stale If-Match).  amount = business miles x the org's MIL-01 rate; a sent
    amount that differs -> 422.  Re-runs the pre-submit check, bumps the report
    version and writes an ``expense.created`` audit row.

    Integration hardening (double submit): the request must carry If-Match (the
    report version — a repeat is then a stale 409) or an Idempotency-Key (a repeat
    with the same key returns the entry the first request created, adds nothing);
    neither -> 428.
    """
    # Late import: service imports the presubmit machinery this module must not
    # load at import time of the schemas it shares.
    from backend.app.modules.expense import service  # noqa: PLC0415

    if expected_version is None and idempotency_key is None:
        raise PreconditionRequiredError(
            "Adding mileage needs the report version (If-Match) or an Idempotency-Key, "
            "so a repeated submit cannot add the trip twice."
        )
    report = service._load_report_for_write(session, current_user, report_id)
    scoped_key = f"{report.id}:{idempotency_key}" if idempotency_key is not None else None
    if scoped_key is not None:
        prior = idempotency_lookup(
            session,
            organization_id=current_user.organization_id,
            scope=MILEAGE_IDEMPOTENCY_SCOPE,
            key=scoped_key,
        )
        if prior is not None:
            existing = session.scalar(
                sa.select(Expense).where(
                    Expense.id == prior,
                    Expense.organization_id == current_user.organization_id,
                    Expense.report_id == report.id,
                )
            )
            if existing is not None:
                return service.entries_with_receipt_ids(
                    session, [existing], report=report, viewer=current_user
                )[0]
    service._check_version(report, expected_version)
    trip = MileageInput.model_validate(payload.model_dump(include=set(MileageInput.model_fields)))
    comp = _compute(trip, current_mileage_rate(session, current_user.organization_id))
    _refuse_client_amount(payload.amount, comp.amount)
    entry = Expense(
        organization_id=current_user.organization_id,
        report_id=report.id,
        trip_segment_id=None,
        category=ExpenseCategory.mileage,
        amount=comp.amount,
        currency=MILEAGE_CURRENCY,
        expense_date=payload.expense_date,
        vendor=None,
        description=payload.description or route_description(trip.origin, trip.destination),
        status=ExpenseStatus.draft,
        match_source=ExpenseMatchSource.manual,
        is_itemized=False,
        mileage=mileage_record(trip, comp),
        created_by=current_user.id,
        updated_by=current_user.id,
    )
    session.add(entry)
    session.flush()
    report.total_amount = service._compute_total(session, report.id)
    report.updated_by = current_user.id
    service.refresh_presubmit_flags(session, report)
    service._bump_report_version(
        session, report, expected_version=expected_version, actor_id=current_user.id
    )
    session.refresh(entry)
    record_audit(
        session,
        organization_id=current_user.organization_id,
        action="expense.created",
        entity_type="expense",
        entity_id=entry.id,
        actor_id=current_user.id,
        before_state=None,
        after_state=service._entry_snapshot(entry),
    )
    if scoped_key is not None:
        idempotency_record(
            session,
            organization_id=current_user.organization_id,
            scope=MILEAGE_IDEMPOTENCY_SCOPE,
            key=scoped_key,
            entity_id=entry.id,
        )
    session.commit()
    return service.entries_with_receipt_ids(session, [entry], report=report, viewer=current_user)[0]
