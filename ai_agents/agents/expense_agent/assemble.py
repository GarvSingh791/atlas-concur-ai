"""Report assembly for the Expense Agent (P4-T10, FR-X4).

Builds a DraftReport from DraftEntry rows with a derived total_amount.
Handles mixed currencies (foreign entries converted into the report currency).
Deterministic title generation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ai_agents.agents.expense_agent.categorize import DraftEntry
from config.constants.fx_rates import UnknownCurrency, convert, is_supported, quantize_money

logger = logging.getLogger(__name__)


def _converted_total(entries: list[DraftEntry], currency: str) -> float:
    """Sum of *entries* in *currency*, each converted; unconvertible ones left out."""
    if not is_supported(currency):
        return round(sum(e.amount for e in entries if e.currency.upper() == currency), 2)
    total = Decimal("0")
    for e in entries:
        try:
            total += convert(Decimal(str(e.amount)), e.currency, currency)
        except UnknownCurrency:
            continue
    return float(quantize_money(total, currency))


@dataclass(frozen=True)
class DraftReport:
    """Assembled draft expense report."""

    entries: list[DraftEntry]
    total_amount: float  # derived — entries converted into the report currency
    currency: str
    trip_id: str | None
    title: str
    mixed_currency: bool = False


def assemble_report(
    entries: list[DraftEntry],
    *,
    trip_id: str | None,
    report_title: str | None = None,
    today: str | None = None,
) -> DraftReport:
    """Assemble a DraftReport from DraftEntry rows.

    Args:
        entries: All draft expense entries.
        trip_id: Trip ID if all matched segments share one trip; else None.
        report_title: Optional custom title from intent.
        today: Today's date string (YYYY-MM-DD); defaults to date.today().
    """
    if today is None:
        today = str(date.today())

    if not entries:
        title = report_title or f"Draft Expense Report – {today}"
        return DraftReport(
            entries=[],
            total_amount=0.0,
            currency="USD",
            trip_id=trip_id,
            title=title,
            mixed_currency=False,
        )

    # Determine primary currency
    currencies = {e.currency.upper() for e in entries}
    mixed_currency = len(currencies) > 1

    if mixed_currency:
        # Product gap 8: the report is in the most common SUPPORTED currency and
        # every other supported entry is converted into it (static reference
        # rates, per entry) — never dropped from the total, never added 1:1.  An
        # entry in an unsupported currency cannot be converted: it is left out
        # here and persistence refuses to write it (advisory note).
        from collections import Counter

        currency_counts = Counter(e.currency.upper() for e in entries)
        ranked = [c for c, _n in currency_counts.most_common() if is_supported(c)]
        primary_currency = ranked[0] if ranked else currency_counts.most_common(1)[0][0]
        logger.info("ExpenseAgent: mixed currencies %s; total in %s.", currencies, primary_currency)
        total_amount = _converted_total(entries, primary_currency)
    else:
        primary_currency = next(iter(currencies))
        total_amount = round(sum(e.amount for e in entries), 2)

    # Determine date for title
    most_recent_date = today
    for e in entries:
        if e.expense_date and e.expense_date > most_recent_date:
            most_recent_date = e.expense_date

    title = report_title or f"Draft Expense Report – {most_recent_date[:10]}"

    return DraftReport(
        entries=entries,
        total_amount=total_amount,
        currency=primary_currency,
        trip_id=trip_id,
        title=title,
        mixed_currency=mixed_currency,
    )
