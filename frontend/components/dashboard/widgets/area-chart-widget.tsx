"use client";

import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { LoadingState, EmptyState, ErrorState } from "@/components/atlas";
import type { SpendOverTime } from "@/lib/types/analytics";

interface AreaChartWidgetProps {
  title: string;
  data: SpendOverTime[] | undefined;
  dataKey: string; // e.g. "period"
  valueKey?: string; // "amount" (default)
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/**
 * Spend-trends area chart (P5-T9) — indigo gradient over a server time-series.
 * A sibling to ChartWidget (BarChart); the dashboard's BarChart is untouched.
 * Renders the server series as-is — no client aggregation.
 */
export function AreaChartWidget({
  title,
  data,
  dataKey,
  valueKey = "amount",
  isLoading,
  isError,
  onRetry,
}: AreaChartWidgetProps) {
  if (isLoading) return <LoadingState variant="card" />;

  if (isError) {
    return (
      <ErrorState
        heading={`Failed to load ${title}`}
        subtext="Could not fetch chart data."
        onRetry={onRetry}
      />
    );
  }

  if (!data || data.length === 0) {
    return (
      <EmptyState
        heading={`No ${title.toLowerCase()} data`}
        subtext="Chart data will appear here once available."
      />
    );
  }

  return (
    <div className="rounded-atlas-md border border-border bg-card p-4">
      <h3 className="text-sm font-medium text-foreground mb-4">{title}</h3>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart
          data={data}
          margin={{ top: 4, right: 16, bottom: 4, left: 0 }}
        >
          <defs>
            <linearGradient id="spendTrendGradient" x1="0" y1="0" x2="0" y2="1">
              <stop
                offset="5%"
                stopColor="var(--color-primary-600)"
                stopOpacity={0.35}
              />
              <stop
                offset="95%"
                stopColor="var(--color-primary-600)"
                stopOpacity={0.02}
              />
            </linearGradient>
          </defs>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--card-border-color)"
          />
          <XAxis dataKey={dataKey} tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip
            formatter={(value: number) => [
              `$${value.toLocaleString()}`,
              "Amount",
            ]}
            contentStyle={{ fontSize: 12 }}
          />
          <Area
            type="monotone"
            dataKey={valueKey}
            stroke="var(--color-primary-600)"
            strokeWidth={2}
            fill="url(#spendTrendGradient)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
