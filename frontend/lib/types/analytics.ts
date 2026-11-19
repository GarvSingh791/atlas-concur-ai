/**
 * Analytics DTO types — mirrors backend/app/modules/analytics/schemas.py
 * byte-for-byte. Update both when the schema changes.
 *
 * Note: RoleKey is defined in identity.ts and re-used here via import.
 * It is intentionally NOT re-exported to avoid ambiguity in the barrel.
 */

import type { RoleKey } from "./identity";

export interface KpiMetric {
  label: string;
  /** null = nothing to average yet (rendered "—", never 0). */
  value: number | null;
  /** Plain-language definition (tooltip); absent on older servers. */
  description?: string | null;
  unit: "count" | "currency" | "percent" | "days" | null;
  trend: number | null;
  trend_direction: "up" | "down" | "flat" | null;
}

export interface Recommendation {
  id: string;
  title: string;
  body: string;
  citation: string | null;
  type: string;
}

export interface PolicyWarning {
  id: string;
  severity: "low" | "medium" | "high";
  message: string;
  source_clause_ref: string | null;
}

export interface ApprovalQueueItem {
  id: string;
  subject_type: string;
  submitter_name: string;
  amount: number | null;
  currency: string | null;
  age_days: number;
  status: string;
}

export interface ActivityItem {
  id: string;
  action: string;
  actor_name: string | null;
  entity_type: string;
  occurred_at: string; // ISO datetime string
  summary: string;
}

export interface SpendByCategory {
  category: string;
  amount: number;
}

export interface SpendOverTime {
  period: string; // "YYYY-MM"
  amount: number;
}

export interface SpendByDepartment {
  department: string;
  amount: number; // actual spend (retained for dashboard back-compat)
  budget: number; // P5-T9: per-department budget for budget-vs-actual bars
}

export interface PolicyComplianceBreakdown {
  compliant: number;
  exceptions: number;
  violations: number;
}

export interface TeamSpend {
  total: number;
  currency: string;
  by_category: SpendByCategory[];
}

export interface EmployeeOverview {
  kpis: KpiMetric[];
  ai_recommendations: Recommendation[];
  policy_warnings: PolicyWarning[];
}

export interface ManagerOverview {
  approval_queue: ApprovalQueueItem[];
  approval_queue_total: number;
  team_spend: TeamSpend;
  recent_activity: ActivityItem[];
}

export interface FinanceOverview {
  kpis: KpiMetric[];
  spend_over_time: SpendOverTime[];
  spend_by_category: SpendByCategory[];
  spend_by_department: SpendByDepartment[];
  policy_compliance: PolicyComplianceBreakdown;
  recent_activity: ActivityItem[];
}

export interface AnalyticsOverviewResponse {
  role: RoleKey;
  generated_at: string; // ISO datetime string
  employee: EmployeeOverview | null;
  manager: ManagerOverview | null;
  finance: FinanceOverview | null;
}
