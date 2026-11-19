"""Persistence helpers for the Expense Agent (P4-T10, FR-X7).

persist_expense_report — writes ExpenseReport + Expense entries via database.models.
    session.flush() only; the caller (P4-T13 endpoint) commits.

append_assistant_message — reused from travel_agent persistence pattern.

LAYERING LAW: never imports from backend/.
"""

from __future__ import annotations

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.intent import ExpenseIntent
from ai_agents.agents.expense_agent.presubmit import (
    PreSubmitFlag,
    build_flags_snapshot,
    draft_entries_for_expenses,
    run_presubmit,
)
from ai_agents.context import AgentContext
from ai_agents.guardrails.citations import Citation
from ai_agents.receipt_pipeline.evidence import (
    machine_total,
    set_shared_expense_ids,
    shared_expense_ids,
)
from ai_agents.receipt_pipeline.inbox import release_inbox_draft
from ai_agents.receipt_pipeline.retention import receipt_retained_for_audit
from ai_agents.tools.report_totals import compute_report_total
from config.constants.fx_rates import UnknownCurrency, normalize_currency
from database.models.card_transaction import CardTransaction
from database.models.conversation_message import ConversationMessage, MessageRole
from database.models.expense import Expense, ExpenseCategory, ExpenseMatchSource, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import Organization
from database.models.receipt import Receipt
from database.models.trip import Trip

logger = logging.getLogger(__name__)

_DECIMAL_PLACES = Decimal("0.01")

# C7-22 contract flag, read by ingest.persistence_releases_inbox_drafts().
# True means persist_expense_report calls release_inbox_draft() before
# re-pointing a receipt that is still linked to its own Receipt-Inbox draft
# entry, so ingest may treat such receipts as matchable.
RELEASES_INBOX_DRAFTS_ON_RELINK = True


def _quantize(amount: float) -> Decimal:
    return Decimal(str(amount)).quantize(_DECIMAL_PLACES, rounding=ROUND_HALF_UP)


def _supported_or_none(code: object) -> str | None:
    try:
        return normalize_currency(code)
    except UnknownCurrency:
        return None


def _org_base_currency(session: Session, org_id: uuid.UUID) -> str:
    """The organization's base currency (USD when it is missing or unsupported)."""
    base = session.scalar(sa.select(Organization.base_currency).where(Organization.id == org_id))
    return _supported_or_none(base) or "USD"


def persist_expense_report(
    *,
    intent: ExpenseIntent,
    report: DraftReport,
    flags_snapshot: dict[str, Any],
    reflection_outcome_value: str | None,
    context: AgentContext,
    session: Session,
) -> tuple[ExpenseReport, list[str]]:
    """Write ExpenseReport + Expense entries + link receipts/card_txns; no commit.

    Returns:
        (expense_report, advisory_notes) where advisory_notes contains any
        non-fatal warnings (e.g., ownership mismatches on receipt linking).
    """
    advisory_notes: list[str] = []
    user_id = context.user_id
    org_id = context.organization_id

    # ── 1. Create ExpenseReport ───────────────────────────────────────────────
    trip_id_uuid: uuid.UUID | None = None
    if report.trip_id:
        try:
            trip_id_uuid = uuid.UUID(report.trip_id)
        except (ValueError, AttributeError):
            advisory_notes.append(f"Invalid trip_id {report.trip_id!r}; stored as NULL.")
    if trip_id_uuid is not None:
        # C7-01: only link a trip the caller owns in their own organization.
        # A manager's direct report's trip, or another org's trip, is dropped.
        owned_trip_id = session.scalar(
            sa.select(Trip.id).where(
                Trip.id == trip_id_uuid,
                Trip.organization_id == org_id,
                Trip.traveler_id == user_id,
            )
        )
        if owned_trip_id is None:
            advisory_notes.append(
                f"trip_id {report.trip_id!r} not found or ownership mismatch; stored as NULL."
            )
            trip_id_uuid = None

    # Product gap 8: the report is in a supported currency (the org's base when
    # the draft's own is not convertible); the total is recomputed from the
    # entries actually written (step 5b), never trusted from the draft.
    report_currency = _supported_or_none(report.currency) or _org_base_currency(session, org_id)
    expense_report = ExpenseReport(
        organization_id=org_id,
        owner_id=user_id,
        trip_id=trip_id_uuid,
        title=report.title,
        status=ExpenseReportStatus.draft,
        total_amount=_quantize(report.total_amount),
        currency=report_currency,
        pre_submit_flags=flags_snapshot,
        created_by=user_id,
        updated_by=user_id,
    )
    session.add(expense_report)
    session.flush()

    # ── 2. Create Expense entries ──────────────────────────────────────────────
    expense_objects: list[Expense] = []
    skipped_entry_ids: set[int] = set()

    for entry in report.entries:
        # Validate expense_date
        from datetime import date as _date

        try:
            expense_date = _date.fromisoformat(entry.expense_date)
        except (ValueError, TypeError, AttributeError):
            advisory_notes.append(
                f"Entry for '{entry.vendor}' has invalid expense_date "
                f"{entry.expense_date!r}; skipped."
            )
            skipped_entry_ids.add(id(entry))
            continue

        # Product gap 8: an amount in a currency Atlas cannot convert is never
        # written (it could not be compared with a cap or added to the total).
        entry_currency = _supported_or_none(entry.currency)
        if entry_currency is None:
            advisory_notes.append(
                f"Entry for '{entry.vendor}' is in currency {str(entry.currency).strip().upper()!r}"
                ", which is not supported; skipped."
            )
            skipped_entry_ids.add(id(entry))
            continue

        trip_segment_id_uuid: uuid.UUID | None = None
        if entry.trip_segment_id:
            try:
                trip_segment_id_uuid = uuid.UUID(entry.trip_segment_id)
            except (ValueError, AttributeError):
                advisory_notes.append(
                    f"Invalid trip_segment_id {entry.trip_segment_id!r}; stored as NULL."
                )

        match_source: ExpenseMatchSource | None = None
        if entry.match_source:
            try:
                match_source = ExpenseMatchSource(entry.match_source)
            except ValueError:
                pass

        expense = Expense(
            organization_id=org_id,
            report_id=expense_report.id,
            trip_segment_id=trip_segment_id_uuid,
            category=ExpenseCategory(entry.category),
            amount=_quantize(entry.amount),
            currency=entry_currency,
            expense_date=expense_date,
            vendor=entry.vendor,
            description=entry.description,
            status=(
                ExpenseStatus(entry.status)
                if entry.status in ("matched", "flagged", "draft")
                else ExpenseStatus.draft
            ),
            match_source=match_source,
            is_itemized=entry.is_itemized,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(expense)
        expense_objects.append(expense)

    session.flush()

    # ── 3. Link receipts ──────────────────────────────────────────────────────
    # Receipt Inboxes a smart-matched receipt's auto-draft line left (UAT2 defect
    # D): each is re-checked ONCE after every link below is in place (step 7).
    released_inboxes: set[uuid.UUID] = set()
    for entry, expense in zip(
        [e for e in report.entries if id(e) not in skipped_entry_ids], expense_objects, strict=False
    ):
        if entry.primary_receipt_id:
            try:
                receipt_uuid = uuid.UUID(entry.primary_receipt_id)
                receipt = session.scalar(
                    sa.select(Receipt).where(
                        Receipt.id == receipt_uuid,
                        Receipt.organization_id == org_id,
                        Receipt.uploaded_by == user_id,
                    )
                )
                if receipt is not None and receipt_retained_for_audit(
                    session, organization_id=org_id, receipt_id=receipt.id
                ):
                    # UAT2: evidence detached from a reviewed report is kept for
                    # audit and never backs another claim.
                    advisory_notes.append(
                        f"Receipt {entry.primary_receipt_id!r} is kept for audit (it was "
                        "part of a submitted report); not linked."
                    )
                    continue
                if receipt is not None:
                    # C7-22: a receipt still linked elsewhere may only move when
                    # that link is its own editable Receipt-Inbox draft entry,
                    # which is released (deleted) first so the charge is not
                    # counted twice. Any other link (submitted/approved report,
                    # non-inbox entry) is left untouched.
                    if (
                        receipt.expense_id is not None
                        and receipt.expense_id != expense.id
                        and not release_inbox_draft(
                            session,
                            context=context,
                            receipt=receipt,
                            refresh_flags=False,
                            released_reports=released_inboxes,
                        )
                    ):
                        advisory_notes.append(
                            f"Receipt {entry.primary_receipt_id!r} is already linked to "
                            "another entry; not re-linked."
                        )
                        continue
                    receipt.expense_id = expense.id
                else:
                    advisory_notes.append(
                        f"Receipt {entry.primary_receipt_id!r} not found or ownership "
                        "mismatch; not linked."
                    )
            except (ValueError, AttributeError) as exc:
                advisory_notes.append(f"Could not link receipt: {exc}")

    # ── 3b. Itemized siblings share their folio (UAT2 hardening) ─────────────
    # categorize gives only the FIRST sibling of an itemized receipt its
    # primary_receipt_id; the explicit sibling marker (sibling_group_id) lets
    # the other siblings be backed by the SAME receipt row — while the siblings'
    # summed amounts are covered by the receipt's machine-read total (the
    # pre-submit check re-validates the split on every run).
    session.flush()  # the primary links above must be visible to the lookup
    persisted = list(
        zip(
            [e for e in report.entries if id(e) not in skipped_entry_ids],
            expense_objects,
            strict=False,
        )
    )
    shared_any = False
    groups: dict[str, list[tuple[Any, Expense]]] = {}
    for entry, expense in persisted:
        if entry.sibling_group_id:
            groups.setdefault(entry.sibling_group_id, []).append((entry, expense))
    for group_id, members in groups.items():
        primary = next((expense for entry, expense in members if entry.primary_receipt_id), None)
        if primary is None or len(members) < 2:
            continue
        folio = session.scalar(
            sa.select(Receipt).where(
                Receipt.expense_id == primary.id,
                Receipt.organization_id == org_id,
                Receipt.uploaded_by == user_id,
            )
        )
        if folio is None:
            continue
        total = machine_total(folio)
        claimed = sum((Decimal(str(expense.amount)) for _e, expense in members), Decimal("0"))
        if total is None or claimed > total:
            advisory_notes.append(
                f"Itemized receipt {folio.id} not shared with its sibling lines "
                f"(group {group_id}): its total does not cover them."
            )
            continue
        set_shared_expense_ids(
            folio,
            [*shared_expense_ids(folio), *(e.id for _d, e in members if e.id != primary.id)],
        )
        shared_any = True

    # ── 4. Link card transactions ──────────────────────────────────────────────
    for entry, expense in zip(
        [e for e in report.entries if id(e) not in skipped_entry_ids], expense_objects, strict=False
    ):
        if entry.source_card_txn_id:
            try:
                txn_uuid = uuid.UUID(entry.source_card_txn_id)
                txn = session.scalar(
                    sa.select(CardTransaction).where(
                        CardTransaction.id == txn_uuid,
                        CardTransaction.organization_id == org_id,
                        CardTransaction.user_id == user_id,
                    )
                )
                if txn is not None:
                    txn.expense_id = expense.id
                    txn.matched = True
                else:
                    advisory_notes.append(
                        f"CardTransaction {entry.source_card_txn_id!r} not found or "
                        "ownership mismatch; not linked."
                    )
            except (ValueError, AttributeError) as exc:
                advisory_notes.append(f"Could not link card transaction: {exc}")

    # ── 5. Final flush ────────────────────────────────────────────────────────
    session.flush()

    # ── 5b. Total from the persisted entries (product gap 8) ──────────────────
    # The SAME rule as the API and the Receipt-Inbox release: foreign entries
    # converted into the report currency, skipped entries not counted.  Every
    # written entry is in a supported currency (step 2), so this cannot raise.
    expense_report.total_amount = compute_report_total(session, expense_report.id)
    session.flush()

    # ── 6. Re-check a report whose folio was shared (UAT2 hardening, revision) ─
    # The caller's flags were computed on the DraftReport, when only the FIRST
    # sibling held the folio: the other siblings carried a BLOCKING
    # missing_receipt flag that the share above has made untrue.  Persisting
    # that snapshot left Submit disabled with no way to clear it, so the stored
    # flags are rebuilt from the persisted entries (the same evaluation every
    # later refresh and approval time use).
    if shared_any:
        refresh_persisted_flags(expense_report, context=context, session=session)

    # ── 7. Re-check each Receipt Inbox a line was released from (UAT2 defect D) ─
    # A twin left behind kept a possible_duplicate naming the released line.
    for inbox_id in sorted(released_inboxes, key=str):
        inbox = session.get(ExpenseReport, inbox_id)
        if inbox is not None:
            refresh_persisted_flags(inbox, context=context, session=session)

    logger.info(
        "persist_expense_report: report %s created for user %s (%d entries, %d advisory notes)",
        expense_report.id,
        user_id,
        len(expense_objects),
        len(advisory_notes),
    )
    return expense_report, advisory_notes


def refresh_persisted_flags(
    expense_report: ExpenseReport,
    *,
    context: AgentContext,
    session: Session,
) -> list[PreSubmitFlag] | None:
    """Rebuild *expense_report*'s stored pre-submit flags from its persisted entries.

    The ai_agents twin of the backend's ``refresh_presubmit_flags`` (which this
    package may not import): ``draft_entries_for_expenses`` → ``run_presubmit``
    (strict, evidence owned by the report owner) → ``build_flags_snapshot``, and
    each entry's ``flagged`` status follows its own blocking flags.  Runs inside
    a SAVEPOINT; on an evaluation failure the existing snapshot is kept (fail
    closed — the stored flags are never silently emptied) and None is returned.
    """
    entries = list(
        session.scalars(
            sa.select(Expense)
            .where(
                Expense.report_id == expense_report.id,
                Expense.organization_id == expense_report.organization_id,
            )
            .order_by(Expense.created_at, Expense.id)
        ).all()
    )
    try:
        with session.begin_nested():
            drafts = draft_entries_for_expenses(
                entries, session=session, organization_id=expense_report.organization_id
            )
            flags, _ = run_presubmit(
                DraftReport(
                    entries=drafts,
                    total_amount=float(str(expense_report.total_amount or 0)),
                    currency=expense_report.currency,
                    trip_id=str(expense_report.trip_id) if expense_report.trip_id else None,
                    title=expense_report.title,
                ),
                context=context,
                session=session,
                owner_id=expense_report.owner_id,
                strict=True,
            )
    except Exception as exc:  # noqa: BLE001 — keep the agent-time flags, never break persist
        logger.warning(
            "persist_expense_report: flag refresh failed for report %s (%s); "
            "agent-time flags kept",
            expense_report.id,
            type(exc).__name__,
        )
        return None
    blocking_ids = {f.entry_id for f in flags if f.severity == "blocking" and f.entry_id}
    for entry in entries:
        if str(entry.id) in blocking_ids:
            entry.status = ExpenseStatus.flagged
        elif entry.status == ExpenseStatus.flagged:
            entry.status = ExpenseStatus.draft
    expense_report.pre_submit_flags = build_flags_snapshot(flags, len(entries))
    session.flush()
    return flags


def append_assistant_message(
    *,
    conversation_id: str,
    content: str,
    tool_calls_trace: dict[str, Any],
    citations: tuple[Citation, ...],
    session: Session,
) -> ConversationMessage:
    """Append an assistant ConversationMessage at the next sequence; no commit.

    Reuses the exact pattern from travel_agent/persistence.py.
    """
    conv_uuid = uuid.UUID(conversation_id)

    max_seq: int | None = session.scalar(
        sa.select(sa.func.max(ConversationMessage.sequence)).where(
            ConversationMessage.conversation_id == conv_uuid
        )
    )
    next_seq = (max_seq or 0) + 1

    msg = ConversationMessage(
        conversation_id=conv_uuid,
        role=MessageRole.assistant,
        content=content,
        tool_calls=tool_calls_trace or None,
        citations=({"citations": [c.to_dict() for c in citations]} if citations else None),
        sequence=next_seq,
    )
    session.add(msg)
    session.flush()
    return msg
