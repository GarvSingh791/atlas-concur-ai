"""Analytics module Pydantic schemas — role-scoped dashboard overview."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Shared sub-schemas
# ---------------------------------------------------------------------------


class KpiMetric(BaseModel):
    label: str
    # None = no data to average yet (product gap 1: shown as "—", never as 0).
    value: float | None
    unit: Literal["count", "currency", "percent", "days"] | None = None
    # Plain-language definition of the metric (UI tooltip).
    description: str | None = None
    trend: float | None = None
    trend_direction: Literal["up", "down", "flat"] | None = None


class Recommendation(BaseModel):
    id: str
    title: str
    body: str
    citation: str | None = None
    type: str  # e.g. "action", "info"


class PolicyWarning(BaseModel):
    id: str
    severity: Literal["low", "medium", "high"]
    message: str
    source_clause_ref: str | None = None


class ApprovalQueueItem(BaseModel):
    id: str  # approval UUID as str
    subject_type: str
    submitter_name: str
    amount: float | None = None
    currency: str | None = None
    age_days: int
    status: str


class ActivityItem(BaseModel):
    id: str
    action: str
    actor_name: str | None = None
    entity_type: str
    occurred_at: datetime
    summary: str


# ---------------------------------------------------------------------------
# Role-specific overview schemas
# ---------------------------------------------------------------------------


class EmployeeOverview(BaseModel):
    kpis: list[KpiMetric]
    ai_recommendations: list[Recommendation]  # ≤3
    policy_warnings: list[PolicyWarning]  # ≤3


class ManagerOverview(BaseModel):
    approval_queue: list[ApprovalQueueItem]
    approval_queue_total: int
    team_spend: dict  # {total: float, currency: str, by_category: list[{category, amount}]}
    recent_activity: list[ActivityItem]


class PolicyComplianceBreakdown(BaseModel):
    compliant: int
    exceptions: int
    violations: int


class FinanceOverview(BaseModel):
    kpis: list[KpiMetric]
    spend_over_time: list[dict]  # [{period: str, amount: float}]
    spend_by_category: list[dict]  # [{category: str, amount: float}]
    spend_by_department: list[dict]  # [{department: str, amount: float, budget: float}]
    policy_compliance: PolicyComplianceBreakdown
    recent_activity: list[ActivityItem]


# ---------------------------------------------------------------------------
# Top-level response
# ---------------------------------------------------------------------------


class AnalyticsOverviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: str  # "employee" | "manager" | "finance_admin"
    generated_at: datetime
    employee: EmployeeOverview | None = None
    manager: ManagerOverview | None = None
    finance: FinanceOverview | None = None
