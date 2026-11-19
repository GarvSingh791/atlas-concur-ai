"use client";

import { KPICard, EmptyState, ErrorState } from "@/components/atlas";
import type { KpiMetric } from "@/lib/types/analytics";

interface KpiWidgetProps {
  kpis: KpiMetric[] | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

export function KpiWidget({
  kpis,
  isLoading,
  isError,
  onRetry,
}: KpiWidgetProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <KPICard key={i} label="" value="" loading />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <ErrorState
        heading="Failed to load KPIs"
        subtext="Could not fetch key metrics."
        onRetry={onRetry}
      />
    );
  }

  if (!kpis || kpis.length === 0) {
    return (
      <EmptyState
        heading="No metrics yet"
        subtext="KPI data will appear here once available."
      />
    );
  }

  // Map unit to KPICard value formatting
  function formatValue(metric: KpiMetric): string {
    // Product gap 1: an average with nothing to average yet is "—", never 0.
    if (metric.value === null || metric.value === undefined) return "—";
    if (metric.unit === "currency")
      return `$${metric.value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (metric.unit === "percent") return `${metric.value.toFixed(1)}%`;
    if (metric.unit === "days") return `${metric.value.toFixed(1)} days`;
    return metric.value.toLocaleString();
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {kpis.map((metric) => (
        <div
          key={metric.label}
          title={metric.description ?? undefined}
          data-testid="kpi-tile"
        >
          <KPICard
            label={metric.label}
            value={formatValue(metric)}
            trend={
              metric.trend != null
                ? `${Math.abs(metric.trend).toFixed(1)}% vs last period`
                : undefined
            }
            trendDirection={metric.trend_direction ?? "flat"}
          />
          {metric.description && (
            <p className="sr-only">{metric.description}</p>
          )}
        </div>
      ))}
    </div>
  );
}
