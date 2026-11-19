"""Approval module Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.app.modules.expense.schemas import ExpenseEntryResponse, ExpenseReportResponse
from backend.app.modules.travel.schemas import SegmentResponse, TripResponse
from database.models.approval import ApprovalSubjectType


class ApprovalTriggerRequest(BaseModel):
    subject_type: ApprovalSubjectType
    subject_id: uuid.UUID


class ApprovalResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    submitter_id: uuid.UUID
    current_status: str
    # QA round: same meaning as on ApprovalListItem — the detail header shows
    # "withdrawn" for a returned subject its owner withdrew.
    subject_withdrawn: bool = False
    display_status: str = ""
    is_auto_approved: bool
    risk_summary: dict[str, Any] | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ApprovalDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject" | "request_changes"
    reason: str | None = None


# ---------------------------------------------------------------------------
# Read surfaces (P5-T8) — list + detail
# ---------------------------------------------------------------------------


class ApprovalListItem(BaseModel):
    """Lean queue row for GET /approvals.

    submitter_name + subject_title/amount are resolved by the service (joins on
    users + the polymorphic subject); risk_level is derived server-side from
    risk_summary (the client never scores risk).
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    submitter_id: uuid.UUID
    submitter_name: str | None
    subject_title: str | None
    subject_amount: Decimal | None
    currency: str | None
    current_status: str
    # QA round: a returned subject its owner withdrew stays ``changes_requested`` on
    # the approval but is closed — ``display_status`` is then "withdrawn" (else
    # ``current_status``); the queue badge, status filter and CSV use it.
    subject_withdrawn: bool = False
    display_status: str = ""
    is_auto_approved: bool
    risk_level: str  # server-derived: "low" | "medium" | "high"
    # For auto-approved rows: the auto_approve step's rationale (so the collapsed
    # auto-approved lane shows it inline without a detail fetch). None otherwise.
    auto_approve_rationale: str | None = None
    created_at: datetime
    version: int
    # Product gap 6: the submitter's department and the canonical exception
    # types the row carries (approval/filters.py) — what the finance filters match.
    submitter_department: str | None = None
    exception_types: list[str] = []
    # Follow-up (grades): the approver grade a flag requires ("SVP" / "VP"), or None.
    required_approver_grade: str | None = None


class ApprovalStepResponse(BaseModel):
    """One decision step (read-only history). approver_id is NULL for system auto-approve."""

    id: uuid.UUID
    approver_id: uuid.UUID | None
    approver_name: str | None
    step_order: int
    decision: str  # approve | reject | request_changes | auto_approve
    reason: str | None
    decided_at: datetime | None
    created_at: datetime


class ExpenseReportContext(BaseModel):
    """The expense_report subject summary + its line-item entries."""

    report: ExpenseReportResponse
    entries: list[ExpenseEntryResponse]


class TripContext(BaseModel):
    """The trip subject summary + its segments."""

    trip: TripResponse
    segments: list[SegmentResponse]


class GradeRequirement(BaseModel):
    """Follow-up (grades): who may decide an approval whose flags name an approver grade.

    ``viewer_can_decide`` / ``message`` are computed for the requesting user; the
    server enforces the same rule on the decision (403 INSUFFICIENT_APPROVER_GRADE).
    """

    required_grade: str
    required_title: str
    required_short: str
    clauses: list[str] = []
    routed_to_approver_id: str | None = None
    routed_to_name: str | None = None
    routed_by: str | None = None
    rationale: str | None = None
    viewer_can_decide: bool
    message: str


class ApprovalDetailResponse(BaseModel):
    """Full approval detail for the decision panel (GET /approvals/{id}).

    Carries the raw risk_summary (rendered structurally, never re-derived on the
    client), the server-derived risk_level, the decision history, and the
    polymorphic subject context (expense_report OR trip — mutually exclusive).
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    submitter_id: uuid.UUID
    submitter_name: str | None
    current_status: str
    # QA round: same meaning as on ApprovalListItem — the detail header shows
    # "withdrawn" for a returned subject its owner withdrew.
    subject_withdrawn: bool = False
    display_status: str = ""
    is_auto_approved: bool
    risk_summary: dict[str, Any] | None
    risk_level: str  # server-derived: "low" | "medium" | "high"
    version: int
    created_at: datetime
    updated_at: datetime
    steps: list[ApprovalStepResponse]
    expense_report: ExpenseReportContext | None = None
    trip: TripContext | None = None
    grade_requirement: GradeRequirement | None = None
