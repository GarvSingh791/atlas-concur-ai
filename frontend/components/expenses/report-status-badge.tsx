import * as React from "react";
import {
  FileEdit,
  Clock,
  CheckCircle2,
  XCircle,
  BadgeCheck,
  Circle,
  Archive,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ExpenseReportStatus } from "@/lib/types";
import { isKnownReportStatus, reportStatusLabel } from "@/lib/expenses/format";

/**
 * Report lifecycle status badge — P5-T7.
 *
 * The shared StatusBadge primitive only models policy/approval semantics
 * (in-policy/out-of-policy/pending/approved/rejected) and can't express the five
 * report-lifecycle states. This is a thin feature-local composition over the
 * generic `Badge` primitive (no new design-system primitive), token-only, giving
 * all five states a distinct colour + icon + correct label. Reimbursed is
 * distinguished from Approved by its icon (BadgeCheck vs CheckCircle2).
 *
 * C7R: a status the UI does not know yet (the lifecycle grows on the server)
 * renders as a neutral badge with a humanized label instead of crashing.
 */

const STATUS_STYLES: Record<
  ExpenseReportStatus,
  { className: string; Icon: LucideIcon }
> = {
  draft: {
    className: "border-border bg-muted text-muted-foreground",
    Icon: FileEdit,
  },
  submitted: {
    className: "border-border bg-muted text-[var(--color-info)]",
    Icon: Clock,
  },
  approved: {
    className: "border-border bg-muted text-[var(--color-success)]",
    Icon: CheckCircle2,
  },
  rejected: {
    className: "border-border bg-muted text-[var(--color-error)]",
    Icon: XCircle,
  },
  reimbursed: {
    className: "border-border bg-muted text-[var(--color-success)]",
    Icon: BadgeCheck,
  },
};

// Product gap 4: a returned report its owner withdrew (status stays "draft" on
// the server; displayReportStatus() maps it to "withdrawn" for the badge).
const WITHDRAWN_STYLE: { className: string; Icon: LucideIcon } = {
  className: "border-border bg-muted text-muted-foreground",
  Icon: Archive,
};

const NEUTRAL_STYLE: { className: string; Icon: LucideIcon } = {
  className: "border-border bg-muted text-muted-foreground",
  Icon: Circle,
};

export interface ReportStatusBadgeProps {
  /** A known lifecycle state — or any string the server may add later. */
  status: ExpenseReportStatus | (string & {});
  className?: string;
}

export function ReportStatusBadge({
  status,
  className,
}: ReportStatusBadgeProps) {
  const { className: styleClass, Icon } = isKnownReportStatus(status)
    ? STATUS_STYLES[status]
    : status === "withdrawn"
      ? WITHDRAWN_STYLE
      : NEUTRAL_STYLE;
  return (
    <Badge
      variant="outline"
      // QA round: never wraps or shrinks next to a long title (390px clip).
      className={cn(
        "shrink-0 gap-1 whitespace-nowrap font-medium",
        styleClass,
        className,
      )}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {reportStatusLabel(status)}
    </Badge>
  );
}
