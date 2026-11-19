/**
 * Expense presentation helpers — P5-T7.
 *
 * Pure formatting + label/icon maps only. NO client-side intelligence:
 * nothing here matches, categorizes, itemizes, evaluates policy, or sums
 * entries into a total. Totals are read straight from the report DTO.
 */

import {
  Plane,
  BedDouble,
  UtensilsCrossed,
  Car,
  Receipt as ReceiptIcon,
  Route,
  Martini,
  Gift,
  type LucideIcon,
} from "lucide-react";
import type {
  ExpenseCategory,
  ExpenseMatchSource,
  ExpenseReportStatus,
} from "@/lib/types";
import { parseDateForDisplay } from "@/lib/dates";

/** Format a Decimal-or-number money value. Coerces defensively (the backend may
 *  serialize Decimal as a string); a non-numeric value renders as an em dash. */
export function formatCurrency(
  amount: number | string | null | undefined,
  currency = "USD",
): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "number" ? amount : Number(amount);
  if (Number.isNaN(n)) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
    }).format(n);
  } catch {
    // Unknown/invalid currency code → fall back to a plain fixed-2 amount.
    return `${currency} ${n.toFixed(2)}`;
  }
}

/** Format an ISO date/datetime as "Jun 15, 2026"; null → em dash. */
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

export const CATEGORY_LABELS: Record<ExpenseCategory, string> = {
  airfare: "Airfare",
  lodging: "Lodging",
  meals: "Meals",
  ground_transport: "Ground transport",
  other: "Other",
  mileage: "Mileage",
  entertainment: "Client entertainment",
  gift: "Gift",
};

export const CATEGORY_ICONS: Record<ExpenseCategory, LucideIcon> = {
  airfare: Plane,
  lodging: BedDouble,
  meals: UtensilsCrossed,
  ground_transport: Car,
  other: ReceiptIcon,
  mileage: Route,
  entertainment: Martini,
  gift: Gift,
};

export const REPORT_STATUS_LABELS: Record<ExpenseReportStatus, string> = {
  draft: "Draft",
  submitted: "Submitted",
  approved: "Approved",
  rejected: "Rejected",
  reimbursed: "Reimbursed",
};

/** Every report lifecycle state the UI knows, in lifecycle order (filters). */
export const REPORT_STATUSES = Object.keys(
  REPORT_STATUS_LABELS,
) as ExpenseReportStatus[];

export function isKnownReportStatus(
  status: unknown,
): status is ExpenseReportStatus {
  return (
    typeof status === "string" &&
    Object.prototype.hasOwnProperty.call(REPORT_STATUS_LABELS, status)
  );
}

/** Label for any report status from the wire; unknown values are humanized
 *  ("changes_requested" → "Changes requested"), never crash. */
export function reportStatusLabel(status: string | null | undefined): string {
  if (isKnownReportStatus(status)) return REPORT_STATUS_LABELS[status];
  const s = (status ?? "").replace(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Unknown";
}

export interface MatchPresentation {
  label: string;
  /** Auto-matched (receipt/card_txn/trip) vs manual/unmatched — drives the
   *  visual distinction required by US-E4 AC1. */
  isAuto: boolean;
  isMatched: boolean;
}

/** Map an entry's match_source (DTO field, never inferred) to its presentation.
 *  Seeded entries have match_source = null → "Unmatched".
 *
 *  C7R: `receiptId` is the entry's linked receipt (ExpenseEntry.receipt_id). An
 *  entry the user created manually but then attached a receipt to is backed by
 *  evidence, so it must not read as a bare "Manual" / "Unmatched" line. */
export function matchPresentation(
  source: ExpenseMatchSource | null | undefined,
  receiptId?: string | null,
): MatchPresentation {
  const hasReceipt = Boolean(receiptId);
  switch (source) {
    case "receipt":
      return { label: "Auto · Receipt", isAuto: true, isMatched: true };
    case "card_txn":
      return { label: "Auto · Card", isAuto: true, isMatched: true };
    case "trip":
      return { label: "Auto · Trip", isAuto: true, isMatched: true };
    case "manual":
      return hasReceipt
        ? { label: "Manual · Receipt", isAuto: false, isMatched: true }
        : { label: "Manual", isAuto: false, isMatched: true };
    default:
      return hasReceipt
        ? { label: "Receipt linked", isAuto: false, isMatched: true }
        : { label: "Unmatched", isAuto: false, isMatched: false };
  }
}
