import * as React from "react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Atlas KPI Card — overline label / display value / trend / optional sparkline.
 *
 * Spec: UI_UX.md → Cards → KPI card
 * 3px semantic top border, overline 11px uppercase, value --text-display, trend 12px semantic.
 */

export type TrendDirection = "up" | "down" | "flat";
export type AccentColor = "primary" | "success" | "warning" | "error" | "info";

const ACCENT_COLORS: Record<AccentColor, string> = {
  primary: "var(--color-primary-600)",
  success: "var(--color-success)",
  warning: "var(--color-warning)",
  error: "var(--color-error)",
  info: "var(--color-info)",
};

const TREND_COLORS: Record<TrendDirection, string> = {
  up: "text-[var(--color-success)]",
  down: "text-[var(--color-error)]",
  flat: "text-[#64748B]",
};

const TREND_ICONS: Record<TrendDirection, React.ElementType> = {
  up: TrendingUp,
  down: TrendingDown,
  flat: Minus,
};

export interface KPICardProps {
  /** Overline label (e.g. "Upcoming Trips"). */
  label: string;
  /** Display value (e.g. "$2,847" or "5 pending"). */
  value: string;
  /** Trend text (e.g. "12% vs last month"). */
  trend?: string;
  /** Direction determines color and icon. Default: "flat". */
  trendDirection?: TrendDirection;
  /** Accent color for the top border. Default: "primary". */
  accentColor?: AccentColor;
  /** Data points for an inline sparkline. */
  sparklineData?: number[];
  /** Loading state — shows skeleton. */
  loading?: boolean;
  className?: string;
}

function Sparkline({ data }: { data: number[] }) {
  if (data.length < 2) return null;

  const width = 100;
  const height = 40;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;

  const points = data
    .map((value, index) => {
      const x = (index / (data.length - 1)) * width;
      const y = height - ((value - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className="mt-2"
      aria-hidden="true"
    >
      <polyline
        fill="none"
        stroke="var(--color-primary-400)"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
    </svg>
  );
}

export function KPICard({
  label,
  value,
  trend,
  trendDirection = "flat",
  accentColor = "primary",
  sparklineData,
  loading = false,
  className,
}: KPICardProps) {
  const TrendIcon = TREND_ICONS[trendDirection];

  if (loading) {
    return (
      <div
        className={cn(
          "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
          "p-[var(--space-6)] shadow-atlas-1",
          "border-t-[3px]",
          className,
        )}
        style={{ borderTopColor: ACCENT_COLORS[accentColor] }}
      >
        <div className="atlas-shimmer h-3 w-24 rounded mb-3" />
        <div className="atlas-shimmer h-8 w-16 rounded mb-2" />
        <div className="atlas-shimmer h-3 w-32 rounded" />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
        "p-[var(--space-6)] shadow-atlas-1",
        "border-t-[3px]",
        "transition-shadow duration-atlas-fast",
        className,
      )}
      style={{ borderTopColor: ACCENT_COLORS[accentColor] }}
    >
      {/* Overline label */}
      <p className="atlas-text-overline text-[#64748B]">{label}</p>

      {/* Display value */}
      <p className="atlas-text-display text-[var(--color-primary-900)] mt-1">
        {value}
      </p>

      {/* Trend */}
      {trend && (
        <div
          className={cn(
            "flex items-center gap-1 mt-1",
            "text-[length:var(--text-caption-size)]",
            TREND_COLORS[trendDirection],
          )}
        >
          <TrendIcon className="h-3 w-3 shrink-0" aria-hidden="true" />
          <span>{trend}</span>
        </div>
      )}

      {/* Sparkline */}
      {sparklineData && <Sparkline data={sparklineData} />}
    </div>
  );
}
