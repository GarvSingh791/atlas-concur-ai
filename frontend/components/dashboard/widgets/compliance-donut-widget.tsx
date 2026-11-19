"use client";

import { ResponsiveContainer, PieChart, Pie, Cell, Tooltip } from "recharts";
import { LoadingState, EmptyState, ErrorState } from "@/components/atlas";
import type { PolicyComplianceBreakdown } from "@/lib/types/analytics";

interface ComplianceDonutWidgetProps {
  title: string;
  data: PolicyComplianceBreakdown | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

const SEGMENTS: {
  key: keyof PolicyComplianceBreakdown;
  label: string;
  color: string;
}[] = [
  { key: "compliant", label: "Compliant", color: "var(--color-success)" },
  { key: "exceptions", label: "Exceptions", color: "var(--color-warning)" },
  { key: "violations", label: "Violations", color: "var(--color-error)" },
];

/**
 * Policy-compliance donut (P5-T9) — Compliant / Exceptions / Violations with a
 * center compliance %. Renders the server-computed breakdown as-is; the center
 * % is a pure display of server counts, NOT a client metric computation.
 */
export function ComplianceDonutWidget({
  title,
  data,
  isLoading,
  isError,
  onRetry,
}: ComplianceDonutWidgetProps) {
  if (isLoading) return <LoadingState variant="card" />;

  if (isError) {
    return (
      <ErrorState
        heading={`Failed to load ${title}`}
        subtext="Could not fetch compliance data."
        onRetry={onRetry}
      />
    );
  }

  const total = data ? data.compliant + data.exceptions + data.violations : 0;

  if (!data || total === 0) {
    return (
      <EmptyState
        heading="No compliance data"
        subtext="Compliance breakdown will appear here once available."
      />
    );
  }

  const chartData = SEGMENTS.map((s) => ({
    name: s.label,
    value: data[s.key],
    color: s.color,
  }));
  const compliantPct = Math.round((data.compliant / total) * 100);

  return (
    <div className="rounded-atlas-md border border-border bg-card p-4">
      <h3 className="text-sm font-medium text-foreground mb-4">{title}</h3>
      <div className="relative">
        <ResponsiveContainer width="100%" height={220}>
          <PieChart>
            <Pie
              data={chartData}
              dataKey="value"
              nameKey="name"
              innerRadius={64}
              outerRadius={92}
              paddingAngle={2}
              startAngle={90}
              endAngle={-270}
            >
              {chartData.map((entry) => (
                <Cell
                  key={entry.name}
                  fill={entry.color}
                  stroke="var(--card)"
                />
              ))}
            </Pie>
            <Tooltip
              formatter={(value: number, name: string) => [value, name]}
              contentStyle={{ fontSize: 12 }}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-2xl font-semibold text-foreground">
            {compliantPct}%
          </span>
          <span className="text-xs text-muted-foreground">Compliant</span>
        </div>
      </div>
      <ul className="mt-3 flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs">
        {chartData.map((entry) => (
          <li key={entry.name} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: entry.color }}
              aria-hidden="true"
            />
            <span className="text-muted-foreground">
              {entry.name} ({entry.value})
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
