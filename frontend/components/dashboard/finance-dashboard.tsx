"use client";

import { useAnalyticsOverview } from "@/lib/query/analytics-hooks";
import { WidgetBoundary } from "@/components/dashboard/widget-boundary";
import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import { ChartWidget } from "@/components/dashboard/widgets/chart-widget";
import { ReimbursementQueueWidget } from "@/components/dashboard/widgets/reimbursement-queue-widget";
import type { SpendOverTime, SpendByCategory } from "@/lib/types/analytics";

export function FinanceDashboard() {
  const {
    data: overview,
    isLoading,
    isError,
    refetch,
  } = useAnalyticsOverview();
  const finance = overview?.finance;

  // SpendByDepartment has a `department` key, not `category` or `period`.
  // ChartWidget's data type is `(SpendOverTime | SpendByCategory)[]` which
  // covers the common runtime shape {[dataKey]: string; amount: number}.
  // We cast here since the chart only reads the dataKey and valueKey at runtime.
  const spendByDepartment = (finance?.spend_by_department ?? []) as unknown as (
    | SpendOverTime
    | SpendByCategory
  )[];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-foreground">Dashboard</h1>

      {/* Consolidated KPIs */}
      <WidgetBoundary widgetName="Finance KPIs">
        <KpiWidget
          kpis={finance?.kpis}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>

      {/* Product gap 1: approved reports awaiting (simulated) reimbursement */}
      <WidgetBoundary widgetName="Ready to reimburse">
        <ReimbursementQueueWidget />
      </WidgetBoundary>

      {/* Spend Charts */}
      <div className="grid gap-4 md:grid-cols-2">
        <WidgetBoundary widgetName="Spend Over Time">
          <ChartWidget
            title="Spend Over Time"
            data={finance?.spend_over_time}
            dataKey="period"
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
        <WidgetBoundary widgetName="Spend by Category">
          <ChartWidget
            title="Spend by Category"
            data={finance?.spend_by_category}
            dataKey="category"
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
      </div>

      {/* Spend by Department */}
      <WidgetBoundary widgetName="Spend by Department">
        <ChartWidget
          title="Spend by Department"
          data={spendByDepartment}
          dataKey="department"
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>
    </div>
  );
}
