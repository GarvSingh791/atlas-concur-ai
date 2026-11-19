"""Expense-side wiring of the process & timing rules (product gap 2, stage 2).

* ``apply_affidavit_change`` — EXP-04: PATCH ``receipt_affidavit`` true/false signs or
  withdraws the missing-receipt affidavit on an entry (the server stamps the time; the
  caller's ``expense.updated`` audit row records before -> after).  Signing is refused
  (422) where the editor does not offer it: a line with a receipt linked, a line that
  needs no receipt (RECEIPT-REQ threshold / category, REF-08), a line above the EXP-04
  ceiling (in USD) or an org without the EXP-04 setting.
* ``_clear_stale_affidavits`` (a ``before_flush`` listener) — integration security fix:
  a signed affidavit vouches for ONE claim.  When a draft line's expense date, amount,
  currency, vendor or description changes, or a receipt is linked to / unlinked from it
  (own or shared itemized), the signature is cleared: the owner must sign again, which
  stamps a fresh time (a re-signed affidavit queues behind the quarter's later ones).
  Lines of submitted / approved reports keep their stamp (a used affidavit stays used).
* ``read_time_process_flags`` — EXP-01 / CARD-03 for a DRAFT report as of today, merged
  into its stored ``pre_submit_flags`` for the response only (nothing is written): a
  draft nobody touched for weeks still shows it is late or stale.

The evaluation itself lives in ``ai_agents.agents.process_checks`` (shared with the
pre-submit check and the approval agent).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from ai_agents.agents import process_checks
from ai_agents.receipt_pipeline.evidence import SHARED_EXPENSES_KEY, receipt_ids_by_entry
from ai_agents.tools import process_rules as pr
from backend.app.core.errors import UnprocessableEntityError
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.receipt import Receipt

logger = logging.getLogger(__name__)


# The claim an affidavit vouches for: a change to any of these withdraws it.
AFFIDAVIT_EVIDENCE_FIELDS: tuple[str, ...] = (
    "expense_date",
    "amount",
    "currency",
    "vendor",
    "description",
)
AFFIDAVIT_NOT_OFFERED = (
    "A missing-receipt affidavit can only be signed on a line that needs a receipt and has "
    "none attached, within the EXP-04 amount ({ceiling})."
)


def _evidence_changed(entry: Expense) -> bool:
    state = sa.inspect(entry)
    return any(state.attrs[name].history.has_changes() for name in AFFIDAVIT_EVIDENCE_FIELDS)


def affidavit_offered(session: Session, entry: Expense) -> tuple[bool, str]:
    """(may *entry* be signed now, the EXP-04 ceiling as text) — the same condition the
    entry editor uses (a ``missing_receipt`` flag with ``affidavit_allowed``)."""
    rules = process_checks.load_process_rules(session, entry.organization_id)
    if not rules.receipt:
        return False, "not configured"
    defn = rules.receipt[0].definition
    ceiling = pr.affidavit_ceiling(defn)
    shown = pr.money(ceiling) if ceiling is not None else "the EXP-04 limit"
    linked = receipt_ids_by_entry(
        session, organization_id=entry.organization_id, entry_ids=[entry.id]
    )
    if linked.get(entry.id):
        return False, shown
    amount_usd, convertible = pr.amount_in_usd(entry.amount, entry.currency)
    if not convertible or amount_usd is None or ceiling is None or amount_usd > ceiling:
        return False, shown
    return pr.receipt_needed(defn, amount_usd=amount_usd, category=entry.category), shown


def apply_affidavit_change(session: Session, entry: Expense, signed: bool | None) -> None:
    """Sign (True) or withdraw (False) the affidavit; call AFTER the edit's other fields
    are set.  Signing keeps the first signing time unless this same edit changed the
    claim (then the signature is new: a fresh time).  Refused (422) where not offered."""
    if signed is True:
        offered, ceiling = affidavit_offered(session, entry)
        if not offered:
            raise UnprocessableEntityError(AFFIDAVIT_NOT_OFFERED.format(ceiling=ceiling))
        if entry.receipt_affidavit_at is None or _evidence_changed(entry):
            entry.receipt_affidavit_at = datetime.now(timezone.utc)
    elif signed is False:
        entry.receipt_affidavit_at = None


def _draft_line(session: Session, entry: Expense) -> bool:
    report = session.get(ExpenseReport, entry.report_id) if entry.report_id else None
    return report is not None and report.status == ExpenseReportStatus.draft


def _linked_ids(receipt: Receipt, *, which: str) -> set[uuid.UUID]:
    """Entry ids *receipt* backed before (``which="old"``) / backs now (``"new"``)."""
    state = sa.inspect(receipt)
    out: set[uuid.UUID] = set()
    exp_hist = state.attrs.expense_id.history
    gc_hist = state.attrs.grounding_context.history
    if which == "new":
        exp_values = [receipt.expense_id]
        gc_values = [receipt.grounding_context]
    else:
        exp_values = list(exp_hist.deleted or ()) or (
            [] if exp_hist.added else list(exp_hist.unchanged or ())
        )
        gc_values = list(gc_hist.deleted or ()) or (
            [] if gc_hist.added else list(gc_hist.unchanged or ())
        )
    for value in exp_values:
        if isinstance(value, uuid.UUID):
            out.add(value)
    for gc in gc_values:
        raw = gc.get(SHARED_EXPENSES_KEY) if isinstance(gc, dict) else None
        for item in raw if isinstance(raw, list) else ():
            try:
                out.add(uuid.UUID(str(item)))
            except (TypeError, ValueError):
                continue
    return out


def _clear_stale_affidavits(session: Session, _ctx: Any, _instances: Any) -> None:
    """``before_flush``: withdraw signatures whose claim or receipt linkage changed."""
    with session.no_autoflush:
        stale: set[uuid.UUID] = set()
        for obj in list(session.dirty):
            if (
                isinstance(obj, Expense)
                and obj.receipt_affidavit_at is not None
                and not sa.inspect(obj).attrs.receipt_affidavit_at.history.has_changes()
                and _evidence_changed(obj)
            ):
                stale.add(obj.id)
        for obj in list(session.new):
            if isinstance(obj, Receipt):
                stale |= _linked_ids(obj, which="new")
        for obj in list(session.deleted):
            if isinstance(obj, Receipt):
                stale |= _linked_ids(obj, which="old")
        for obj in list(session.dirty):
            if isinstance(obj, Receipt):
                state = sa.inspect(obj)
                if (
                    state.attrs.expense_id.history.has_changes()
                    or state.attrs.grounding_context.history.has_changes()
                ):
                    old, new = _linked_ids(obj, which="old"), _linked_ids(obj, which="new")
                    stale |= old ^ new
        for eid in stale:
            entry = session.get(Expense, eid)
            if (
                entry is not None
                and entry.receipt_affidavit_at is not None
                and _draft_line(session, entry)
            ):
                entry.receipt_affidavit_at = None


event.listen(Session, "before_flush", _clear_stale_affidavits)


def read_time_process_flags(session: Session, report: ExpenseReport) -> dict[str, Any] | None:
    """The report's ``pre_submit_flags`` with EXP-01 / CARD-03 recomputed for today.

    Drafts only (a submitted report keeps the flags its submission was judged on).
    Degrades to the stored snapshot if the check cannot run — never a 500.
    """
    stored = report.pre_submit_flags if isinstance(report.pre_submit_flags, dict) else None
    if report.status != ExpenseReportStatus.draft:
        return stored
    try:
        with session.begin_nested():
            dates = list(
                session.scalars(
                    sa.select(Expense.expense_date).where(
                        Expense.report_id == report.id,
                        Expense.organization_id == report.organization_id,
                    )
                ).all()
            )
            fresh = process_checks.report_flags(
                session,
                organization_id=report.organization_id,
                owner_id=report.owner_id,
                trip_id=report.trip_id,
                entry_dates=dates,
            )
    except Exception as exc:  # noqa: BLE001 — a read never fails on this
        logger.warning(
            "expense: read-time process flags failed for report=%s (%s)",
            report.id,
            type(exc).__name__,
        )
        return stored
    if stored is None and not fresh:
        return None
    return process_checks.merge_report_flags(stored, fresh)
