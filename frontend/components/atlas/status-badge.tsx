import * as React from "react";
import {
  Check,
  X,
  Clock,
  Sparkles,
  RotateCcw,
  ShieldCheck,
  ShieldAlert,
  FileEdit,
  Archive,
  AlertTriangle,
  Circle,
} from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Atlas Status Badge — maps directly to schema enum values.
 *
 * Spec: UI_UX.md → Badges & chips
 * 22px height, 2px 8px padding, 11px/500 weight, radius-sm, inline-flex, 4px icon gap.
 *
 * P5-T8 extension: the full approval_status enum (auto-approved,
 * changes-requested) + Low/Med/High risk variants for the approval dashboard.
 * auto-approved uses the AI-plane tokens (it is a system/agent action).
 */

export type StatusBadgeVariant =
  | "in-policy"
  | "out-of-policy"
  | "pending"
  | "approved"
  | "rejected"
  | "auto-approved"
  | "changes-requested"
  | "risk-low"
  | "risk-medium"
  | "risk-high"
  // P5-T9: policy rule lifecycle status dots (policy_rules.status enum).
  | "rule-draft"
  | "rule-active"
  | "rule-archived"
  // C7R: advisory-only policy flag on a line (amber, not a violation).
  | "policy-advisory"
  // C7R: neutral fallback for any status value the UI does not know yet.
  | "neutral";

const STATUS_CONFIG: Record<
  StatusBadgeVariant,
  {
    label: string;
    icon: React.ElementType;
    bg: string;
    text: string;
    border: string;
  }
> = {
  "in-policy": {
    label: "In policy",
    icon: Check,
    bg: "bg-[#ECFDF5]",
    text: "text-[var(--color-success)]",
    border: "border-[#A7F3D0]",
  },
  "out-of-policy": {
    label: "Out of policy",
    icon: X,
    bg: "bg-[#FEF2F2]",
    text: "text-[var(--color-error)]",
    border: "border-[#FECACA]",
  },
  pending: {
    label: "Pending",
    icon: Clock,
    bg: "bg-[#FFFBEB]",
    text: "text-[var(--color-warning)]",
    border: "border-[#FDE68A]",
  },
  approved: {
    label: "Approved",
    icon: Check,
    bg: "bg-[#F0FDF4]",
    text: "text-[#16A34A]",
    border: "border-[#BBF7D0]",
  },
  rejected: {
    label: "Rejected",
    icon: X,
    bg: "bg-[#FEF2F2]",
    text: "text-[#EF4444]",
    border: "border-[#FECACA]",
  },
  // System auto-approval — rendered on the AI plane (tokens, not raw hex).
  "auto-approved": {
    label: "Auto-approved",
    icon: Sparkles,
    bg: "bg-[var(--color-ai-surface)]",
    text: "text-[var(--color-ai-primary)]",
    border: "border-[var(--color-primary-100)]",
  },
  "changes-requested": {
    label: "Changes requested",
    icon: RotateCcw,
    bg: "bg-[#FFFBEB]",
    text: "text-[var(--color-warning)]",
    border: "border-[#FDE68A]",
  },
  // Risk levels (server-derived) — green / amber / red, matching the policy palette.
  "risk-low": {
    label: "Low risk",
    icon: ShieldCheck,
    bg: "bg-[#ECFDF5]",
    text: "text-[var(--color-success)]",
    border: "border-[#A7F3D0]",
  },
  "risk-medium": {
    label: "Med risk",
    icon: ShieldAlert,
    bg: "bg-[#FFFBEB]",
    text: "text-[var(--color-warning)]",
    border: "border-[#FDE68A]",
  },
  "risk-high": {
    label: "High risk",
    icon: ShieldAlert,
    bg: "bg-[#FEF2F2]",
    text: "text-[var(--color-error)]",
    border: "border-[#FECACA]",
  },
  // Policy rule lifecycle — draft (amber) / active (green) / archived (muted).
  "rule-draft": {
    label: "Draft",
    icon: FileEdit,
    bg: "bg-[#FFFBEB]",
    text: "text-[var(--color-warning)]",
    border: "border-[#FDE68A]",
  },
  "rule-active": {
    label: "Active",
    icon: Check,
    bg: "bg-[#ECFDF5]",
    text: "text-[var(--color-success)]",
    border: "border-[#A7F3D0]",
  },
  "rule-archived": {
    label: "Archived",
    icon: Archive,
    bg: "bg-[var(--surface-table-stripe)]",
    text: "text-muted-foreground",
    border: "border-border",
  },
  "policy-advisory": {
    label: "Advisory",
    icon: AlertTriangle,
    bg: "bg-[#FFFBEB]",
    text: "text-[var(--color-warning)]",
    border: "border-[#FDE68A]",
  },
  neutral: {
    label: "Unknown",
    icon: Circle,
    bg: "bg-[var(--surface-table-stripe)]",
    text: "text-muted-foreground",
    border: "border-border",
  },
};

/** Type guard — true when `value` is a known StatusBadge variant. */
export function isStatusBadgeVariant(
  value: unknown,
): value is StatusBadgeVariant {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(STATUS_CONFIG, value)
  );
}

/** Human label for an arbitrary status string ("changes_requested" → "Changes requested"). */
export function humanizeStatus(value: string | null | undefined): string {
  const s = (value ?? "").replace(/[_-]+/g, " ").trim();
  if (!s) return "Unknown";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export interface StatusBadgeProps {
  variant: StatusBadgeVariant;
  /** Override the variant's default label (e.g. the raw status for `neutral`). */
  label?: string;
  className?: string;
}

export function StatusBadge({ variant, label, className }: StatusBadgeProps) {
  // Defensive: an unexpected value from the wire renders neutral, never crashes.
  const known = isStatusBadgeVariant(variant);
  const config = known ? STATUS_CONFIG[variant] : STATUS_CONFIG.neutral;
  const text =
    label ?? (known ? config.label : humanizeStatus(String(variant)));
  const Icon = config.icon;

  return (
    <span
      className={cn(
        // QA round: a fixed-height pill must never wrap its label onto a second
        // line or be squeezed by a long sibling — the TEXT next to it truncates.
        "inline-flex shrink-0 items-center gap-[var(--space-1)] whitespace-nowrap",
        "h-[22px] px-2 py-[2px]",
        "text-[11px] font-medium leading-none",
        "rounded-atlas-sm border",
        config.bg,
        config.text,
        config.border,
        className,
      )}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {text}
    </span>
  );
}
