"use client";

import {
  ResponsiveContainer,
  BarChart,
  Bar,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts";
import { LoadingState, EmptyState, ErrorState } from "@/components/atlas";
import type { SpendByDepartment } from "@/lib/types/analytics";

interface DepartmentBudgetWidgetProps {
  title: string;
  data: SpendByDepartment[] | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

/**
 * Department comparison (P5-T9) — grouped bars of budget vs actual. An actual
 * bar turns red when it exceeds its department budget. Renders server values
 * as-is; no client aggregation.
 */
export function DepartmentBudgetWidget({
  title,
  data,
  isLoading,
  isError,
  onRetry,
}: DepartmentBudgetWidgetProps) {
  if (isLoading) return <LoadingState variant="card" />;

  if (isError) {
    return (
      <ErrorState
        heading={`Failed to load ${title}`}
        subtext="Could not fetch department data."
        onRetry={onRetry}
      />
    );
  }

  if (!data || data.length === 0) {
    return (
      <EmptyState
        heading="No department data"
        subtext="Department comparison will appear here once available."
      />
    );
  }

  return (
    <div className="rounded-atlas-md border border-border bg-card p-4">
      <h3 className="text-sm font-medium text-foreground mb-4">{title}</h3>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart
          data={data}
          margin={{ top: 4, right: 16, bottom: 4, left: 0 }}
        >
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--card-border-color)"
          />
          <XAxis dataKey="department" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip
            formatter={(value: number, name: string) => [
              `$${value.toLocaleString()}`,
              name,
            ]}
            contentStyle={{ fontSize: 12 }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar
            dataKey="budget"
            name="Budget"
            fill="var(--color-primary-200)"
            radius={[3, 3, 0, 0]}
          />
          <Bar dataKey="amount" name="Actual" radius={[3, 3, 0, 0]}>
            {data.map((row) => (
              <Cell
                key={row.department}
                fill={
                  row.amount > row.budget
                    ? "var(--color-error)"
                    : "var(--color-primary-600)"
                }
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
