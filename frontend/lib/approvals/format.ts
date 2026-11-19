/**
 * Approval display formatters + badge-variant mappers (P5-T8).
 *
 * Pure presentation helpers — money/date formatting and mapping the server's
 * status / risk_level enums onto StatusBadge variants. No business logic.
 */

import { parseDateForDisplay } from "@/lib/dates";
import type { StatusBadgeVariant } from "@/components/atlas";
import type { ApprovalDisplayStatus, RiskLevel } from "@/lib/types/approvals";

/** Format a Decimal-as-string money value (or null) for display. */
export function formatMoney(
  amount: string | null | undefined,
  currency: string | null | undefined,
): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const num = Number(amount);
  if (Number.isNaN(num)) return amount;
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency ?? "USD",
    }).format(num);
  } catch {
    return `${currency ?? ""} ${num.toFixed(2)}`.trim();
  }
}

/** Format an ISO timestamp/date (or null) as a short date. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseDateForDisplay(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

const STATUS_BADGE: Record<ApprovalDisplayStatus, StatusBadgeVariant> = {
  pending: "pending",
  approved: "approved",
  rejected: "rejected",
  auto_approved: "auto-approved",
  changes_requested: "changes-requested",
  // QA round: closed by its owner — neutral, never "awaiting changes".
  withdrawn: "neutral",
};

/** Badge for any approval/report status string. Unknown values map to the
 *  neutral badge (never silently to "pending", which would be a false claim). */
export function statusBadgeVariant(
  status: ApprovalDisplayStatus | (string & {}),
): StatusBadgeVariant {
  return Object.prototype.hasOwnProperty.call(STATUS_BADGE, status)
    ? STATUS_BADGE[status as ApprovalDisplayStatus]
    : "neutral";
}

const STATUS_LABEL: Record<ApprovalDisplayStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  auto_approved: "Auto-approved",
  changes_requested: "Changes requested",
  withdrawn: "Withdrawn",
};

export function statusLabel(
  status: ApprovalDisplayStatus | (string & {}),
): string {
  if (Object.prototype.hasOwnProperty.call(STATUS_LABEL, status)) {
    return STATUS_LABEL[status as ApprovalDisplayStatus];
  }
  const s = status.replace(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Unknown";
}

const RISK_BADGE: Record<RiskLevel, StatusBadgeVariant> = {
  low: "risk-low",
  medium: "risk-medium",
  high: "risk-high",
};

export function riskBadgeVariant(risk: RiskLevel): StatusBadgeVariant {
  return RISK_BADGE[risk] ?? "risk-low";
}
