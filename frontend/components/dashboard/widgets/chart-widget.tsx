"use client";

import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { LoadingState, EmptyState, ErrorState } from "@/components/atlas";
import type { SpendOverTime, SpendByCategory } from "@/lib/types/analytics";

interface ChartWidgetProps {
  title: string;
  data: (SpendOverTime | SpendByCategory)[] | undefined;
  dataKey: string; // "period" or "category"
  valueKey?: string; // "amount" (default)
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

export function ChartWidget({
  title,
  data,
  dataKey,
  valueKey = "amount",
  isLoading,
  isError,
  onRetry,
}: ChartWidgetProps) {
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
        <BarChart
          data={data}
          margin={{ top: 4, right: 16, bottom: 4, left: 0 }}
        >
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
          <Bar
            dataKey={valueKey}
            fill="var(--color-primary-600)"
            radius={[3, 3, 0, 0]}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
