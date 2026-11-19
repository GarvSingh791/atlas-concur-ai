"""Grade-entitlement checks (AIR-07 / LOD-05 / NR-01) — wiring of ``grade_entitlements``.

Called once per expense line by ``presubmit.run_presubmit`` (so pre-submit, the
approval agent's fresh re-evaluation and the resubmission re-assessment all agree).
For every ACTIVE ``grade_entitlement`` rule of the organization (finance controls them
on the policy page) the line is checked for that rule's kind of fee using:

* TRUSTED words — the OCR vendor (or its frozen OCR snapshot) and line items of the
  entry's receipts that belong to the OWNER, and the merchant of the owner's linked
  card transaction (same organization);
* the entry's own vendor / description (may only widen scrutiny);
* the type of the entry's linked trip segment on the owner's trip (a hotel / flight
  segment only ADDS hotel / airport context to detection).

The decision uses the OWNER's grade of record (``traveller_grade.owner_grade`` — read
once per report, org-scoped, unknown -> fail closed).  Flags are ``PreSubmitFlag``
dicts (``process_checks.flag_dict``) carrying ``rule_type = grade_entitlement`` so the
finance filters / CSV label them "Rule: grade entitlement".

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent import entry_context
from ai_agents.agents.process_checks import ProcessRules, RuleRef, flag_dict
from ai_agents.context import AgentContext
from ai_agents.tools import grade_entitlements as ge
from ai_agents.tools import process_rules as pr
from database.models.card_transaction import CardTransaction
from database.models.receipt import Receipt
from database.models.trip import Trip
from database.models.trip_segment import TripSegment

logger = logging.getLogger(__name__)


def grade_rules(process: ProcessRules) -> tuple[RuleRef, ...]:
    """The active grade-entitlement rules (already loaded with the process rules)."""
    return process.of(pr.KIND_GRADE_ENTITLEMENT)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _owned_receipts(
    entry: Any, *, organization_id: uuid.UUID, owner_id: uuid.UUID | None, session: Session
) -> list[Receipt]:
    ids = list(getattr(entry, "receipt_ids", ()) or ())
    primary = getattr(entry, "primary_receipt_id", None)
    if primary and primary not in ids:
        ids.insert(0, primary)
    out: list[Receipt] = []
    for raw in ids:
        rid = _as_uuid(raw)
        receipt = session.get(Receipt, rid) if rid is not None else None
        if not isinstance(receipt, Receipt) or receipt.organization_id != organization_id:
            continue
        if owner_id is not None and receipt.uploaded_by != owner_id:
            continue
        out.append(receipt)
    return out


def _owned_merchant(
    entry: Any, *, organization_id: uuid.UUID, owner_id: uuid.UUID | None, session: Session
) -> str | None:
    tid = _as_uuid(getattr(entry, "source_card_txn_id", None))
    txn = session.get(CardTransaction, tid) if tid is not None else None
    if not isinstance(txn, CardTransaction) or txn.organization_id != organization_id:
        return None
    if owner_id is not None and txn.user_id != owner_id:
        return None
    merchant = txn.merchant
    return merchant.strip() if isinstance(merchant, str) and merchant.strip() else None


def owned_segment_type(
    entry: Any, *, organization_id: uuid.UUID, owner_id: uuid.UUID | None, session: Session
) -> str | None:
    """The type ("hotel", "flight", ...) of the entry's linked trip segment when it is on
    the OWNER's trip in this organization, else None.

    Used only to ADD hotel / airport context to detection (it can never clear a line), so
    any status of the owner's trip counts.
    """
    sid = _as_uuid(getattr(entry, "trip_segment_id", None))
    segment = session.get(TripSegment, sid) if sid is not None else None
    if not isinstance(segment, TripSegment):
        return None
    trip = session.get(Trip, segment.trip_id)
    if not isinstance(trip, Trip) or trip.organization_id != organization_id:
        return None
    if owner_id is not None and trip.traveler_id != owner_id:
        return None
    seg_type = getattr(segment.segment_type, "value", segment.segment_type)
    return str(seg_type) if seg_type else None


def entry_evidence(
    entry: Any, *, organization_id: uuid.UUID, owner_id: uuid.UUID | None, session: Session
) -> tuple[tuple[str, ...], tuple[tuple[str, float], ...]]:
    """(trusted words, trusted receipt line items) of one entry — never the claimant's."""
    words: list[str] = []
    items: list[tuple[str, float]] = []
    for receipt in _owned_receipts(
        entry, organization_id=organization_id, owner_id=owner_id, session=session
    ):
        fields = receipt.extracted_fields or {}
        prov = receipt.field_provenance or {}
        words.extend(
            entry_context.receipt_texts(fields, prov, grounding_context=receipt.grounding_context)
        )
        items.extend(entry_context.receipt_line_items(fields, prov) or ())
    merchant = _owned_merchant(
        entry, organization_id=organization_id, owner_id=owner_id, session=session
    )
    if merchant:
        words.append(merchant)
    return tuple(words), tuple(items)


def _justified(flag: dict[str, Any], justification: str | None) -> dict[str, Any]:
    """A junior grade's violation may be submitted with a written justification; it stays
    a policy_violation (the SVP-level approver still decides)."""
    if flag["flag_type"] != ge.POLICY_VIOLATION:
        return flag
    flag["justifiable"] = True
    if justification:
        flag["severity"] = "advisory"
        flag["justification"] = justification
    return flag


def entry_grade_flags(
    entry: Any,
    *,
    entry_ref: str,
    rules: Iterable[RuleRef],
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID | None,
    grade_of_owner: Callable[[], str | None],
) -> list[dict[str, Any]]:
    """The grade-entitlement flags of one expense line (``[]`` when none applies).

    *grade_of_owner* returns the OWNER's grade of record (called only when a rule
    detects something, so a report with no such line costs no lookup).
    """
    rules = tuple(rules)
    if not rules or context.organization_id is None:
        return []
    category = str(getattr(entry, "category", "") or "") or None
    typed = (getattr(entry, "vendor", None), getattr(entry, "description", None))
    trusted, items = entry_evidence(
        entry, organization_id=context.organization_id, owner_id=owner_id, session=session
    )
    segment_type = owned_segment_type(
        entry, organization_id=context.organization_id, owner_id=owner_id, session=session
    )
    flags: list[dict[str, Any]] = []
    for rule in rules:
        defn = rule.definition
        try:
            entitlement = ge.rule_entitlement(defn)
        except ValueError:
            # Nothing to detect with (the activation validator refuses such a rule).
            logger.warning("grade_checks: rule %s names no known entitlement", rule.rule_id)
            continue
        detection = ge.detect(
            entitlement,
            category=category,
            typed=typed,
            trusted=trusted,
            line_items=items,
            segment_type=segment_type,
        )
        if detection is None:
            continue
        try:
            finding = ge.evaluate(
                defn,
                detection=detection,
                traveller_grade=grade_of_owner(),
                claim_amount=getattr(entry, "amount", None),
                currency=getattr(entry, "currency", None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("grade_checks: rule %s could not be evaluated: %s", rule.rule_id, exc)
            finding = pr.Finding(
                clause_ref=ge.rule_clause(defn),
                flag_type=pr.NOT_EVALUATED,
                severity="advisory",
                reason=(
                    f"Rule {ge.rule_clause(defn)} could not be evaluated: its definition is "
                    "malformed."
                ),
                observed={"could_not_verify": "definition"},
            )
        flag = flag_dict(
            finding, rule=rule, entry_ref=entry_ref, entry_id=getattr(entry, "entry_id", None)
        )
        flags.append(_justified(flag, getattr(entry, "justification", None)))
    return flags
