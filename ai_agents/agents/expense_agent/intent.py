"""Expense intent extraction (P4-T10, FR-X1)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpenseIntent:
    """Structured expense intent extracted from task.payload."""

    trip_id: str | None  # filter itinerary to specific trip
    owner_id: str | None  # if set and != ctx.user_id → role-blocked
    conversation_id: str | None
    explain_with_llm: bool = False
    report_title: str | None = None


def parse_intent(payload: dict) -> ExpenseIntent:
    """Extract ExpenseIntent from the raw Task.payload dict."""
    return ExpenseIntent(
        trip_id=payload.get("trip_id") or None,
        owner_id=payload.get("owner_id") or None,
        conversation_id=payload.get("conversation_id") or None,
        explain_with_llm=bool(payload.get("explain_with_llm", False)),
        report_title=payload.get("report_title") or None,
    )
