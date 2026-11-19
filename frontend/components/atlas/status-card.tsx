import * as React from "react";
import { StatusBadge, type StatusBadgeVariant } from "./status-badge";
import { cn } from "@/lib/utils";

/**
 * Atlas Status Card (Entity) — trip/report summary.
 *
 * Spec: UI_UX.md → Cards → Status card (entity)
 * 4px left border (status color), title, meta, status + policy badges.
 */

const STATUS_BORDER_COLORS: Record<string, string> = {
  success: "var(--color-success)",
  warning: "var(--color-warning)",
  error: "var(--color-error)",
  info: "var(--color-info)",
  primary: "var(--color-primary-600)",
};

export interface StatusCardProps {
  /** Card title (e.g. "Trip: NYC Tech Conference"). */
  title: string;
  /** Subtitle / meta text (e.g. "Jun 20–23 · $3,247"). */
  subtitle?: string;
  /** Status color determines left border. Default: "primary". */
  statusColor?: "success" | "warning" | "error" | "info" | "primary";
  /** Status badge variant. */
  status?: StatusBadgeVariant;
  /** Policy badge variant. */
  policyStatus?: StatusBadgeVariant;
  /** Click handler for interactive cards. */
  onClick?: () => void;
  className?: string;
}

export function StatusCard({
  title,
  subtitle,
  statusColor = "primary",
  status,
  policyStatus,
  onClick,
  className,
}: StatusCardProps) {
  const isInteractive = !!onClick;

  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
        "p-[var(--space-4)] shadow-atlas-1",
        "border-l-[4px]",
        "transition-all duration-atlas-fast",
        isInteractive &&
          "cursor-pointer hover:shadow-atlas-2 hover:-translate-y-[1px]",
        className,
      )}
      style={{ borderLeftColor: STATUS_BORDER_COLORS[statusColor] }}
      onClick={onClick}
      role={isInteractive ? "button" : undefined}
      tabIndex={isInteractive ? 0 : undefined}
      onKeyDown={
        isInteractive
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {/* Title */}
      <h3 className="atlas-text-subtitle text-[var(--color-primary-900)]">
        {title}
      </h3>

      {/* Subtitle */}
      {subtitle && (
        <p className="atlas-text-body text-[#64748B] mt-1">{subtitle}</p>
      )}

      {/* Badges */}
      {(status || policyStatus) && (
        <div className="flex flex-wrap items-center gap-2 mt-3">
          {status && <StatusBadge variant={status} />}
          {policyStatus && <StatusBadge variant={policyStatus} />}
        </div>
      )}
    </div>
  );
}
