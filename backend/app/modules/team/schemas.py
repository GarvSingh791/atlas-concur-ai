"""Team module Pydantic schemas (Product gap 5)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel

from backend.app.core.pagination import Page
from backend.app.modules.expense.schemas import ReportListItem
from backend.app.modules.travel.schemas import TripListItem


class TeamMemberSummary(BaseModel):
    """One team member's travel & expense posture (no email / internal flags).

    Spend = SUM(total_amount) of the member's approved + reimbursed expense
    reports dated (submitted_at, else created_at) inside the window, counted
    ONLY for reports in the organization's base currency (there is no FX
    conversion in Atlas); ``other_currency_reports`` counts the approved /
    reimbursed reports in the trailing-90-day window that were left out.
    """

    id: uuid.UUID
    name: str
    department: str | None
    role: str
    # QA round: "active" | "inactive" — a deactivated report stays listed (their
    # history is still the manager's to read) but is marked Inactive.
    status: str = "active"
    open_trips: int  # draft + pending_approval + approved
    reports_by_status: dict[str, int]  # every ExpenseReportStatus, zero-filled
    pending_approvals: int  # approvals on the member's submissions still pending
    spend_mtd: Decimal
    spend_90d: Decimal
    currency: str  # organization base currency
    other_currency_reports: int


class TeamMemberDetail(BaseModel):
    """GET /team/members/{id}: summary + the member's trips and reports (paginated)."""

    member: TeamMemberSummary
    trips: Page[TripListItem]
    reports: Page[ReportListItem]
