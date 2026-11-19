/**
 * Team DTO mirrors (Product gap 5) — backend/app/modules/team/schemas.py.
 *
 * Money is a Decimal on the server → a JSON STRING here (e.g. "1500.00").
 * Spend counts approved + reimbursed reports in the organization's base
 * currency only; `other_currency_reports` says how many were left out.
 */

import type { PaginatedResponse } from "./api";
import type { ExpenseReportStatus, ReportListItem } from "./expenses";
import type { TripListItem } from "./trips";

export interface TeamMemberSummary {
  id: string;
  name: string;
  department: string | null;
  role: string;
  /** QA round: account status — "inactive" once deactivated (older servers omit it). */
  status?: "active" | "inactive" | (string & {});
  /** draft + pending_approval + approved trips. */
  open_trips: number;
  reports_by_status: Record<ExpenseReportStatus, number>;
  pending_approvals: number;
  spend_mtd: string;
  spend_90d: string;
  currency: string;
  other_currency_reports: number;
}

export interface TeamMemberDetail {
  member: TeamMemberSummary;
  trips: PaginatedResponse<TripListItem>;
  reports: PaginatedResponse<ReportListItem>;
}

export type TeamSortKey =
  | "name"
  | "department"
  | "open_trips"
  | "pending_approvals"
  | "spend_mtd"
  | "spend_90d";

export interface TeamListParams {
  page?: number;
  size?: number;
  q?: string;
  /** e.g. "-spend_90d", "name". */
  sort?: string;
}
