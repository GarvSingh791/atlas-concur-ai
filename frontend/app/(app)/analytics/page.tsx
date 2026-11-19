"use client";

import { useMemo, useState } from "react";
import { BarChart3, Download } from "lucide-react";

import { useAnalyticsOverview } from "@/lib/query/analytics-hooks";
import { useSession } from "@/lib/auth/session-provider";
import { WidgetBoundary } from "@/components/dashboard/widget-boundary";
import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import { ChartWidget } from "@/components/dashboard/widgets/chart-widget";
import { AreaChartWidget } from "@/components/dashboard/widgets/area-chart-widget";
import { ComplianceDonutWidget } from "@/components/dashboard/widgets/compliance-donut-widget";
import { DepartmentBudgetWidget } from "@/components/dashboard/widgets/department-budget-widget";
import { AtlasButton, EmptyState } from "@/components/atlas";

export default function AnalyticsPage() {
  const { role } = useSession();
  const {
    data: overview,
    isLoading,
    isError,
    refetch,
  } = useAnalyticsOverview();
  const finance = overview?.finance;

  // Date-range filter — DISPLAY ONLY. Filters the rendered spend-trends series
  // by month; it never recomputes a KPI or aggregate (the server owns those).
  const periods = useMemo(
    () => (finance?.spend_over_time ?? []).map((p) => p.period),
    [finance],
  );
  const [fromPeriod, setFromPeriod] = useState<string>("");
  const [toPeriod, setToPeriod] = useState<string>("");

  const filteredTrends = useMemo(() => {
    const series = finance?.spend_over_time ?? [];
    return series.filter((p) => {
      if (fromPeriod && p.period < fromPeriod) return false;
      if (toPeriod && p.period > toPeriod) return false;
      return true;
    });
  }, [finance, fromPeriod, toPeriod]);

  // Export — client-side download of the already-rendered data (no backend).
  function handleExport() {
    if (!finance) return;
    const payload = {
      generated_at: overview?.generated_at ?? null,
      kpis: finance.kpis,
      spend_over_time: filteredTrends,
      spend_by_category: finance.spend_by_category,
      spend_by_department: finance.spend_by_department,
      policy_compliance: finance.policy_compliance,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "atlas-finance-analytics.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  // The endpoint only populates `finance` for finance_admin. Other roles that
  // reach this route (defence in depth — nav already gates it) get an honest
  // notice rather than a blank screen.
  if (!isLoading && role !== "finance_admin") {
    return (
      <div className="flex h-[calc(100vh-100px)] items-center justify-center">
        <EmptyState
          icon={BarChart3}
          heading="Finance analytics"
          subtext="This dashboard is available to finance administrators."
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-foreground">
          Finance Analytics
        </h1>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            From
            <select
              value={fromPeriod}
              onChange={(e) => setFromPeriod(e.target.value)}
              className="rounded-atlas-sm border border-border bg-card px-2 py-1 text-xs text-foreground"
            >
              <option value="">Earliest</option>
              {periods.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            To
            <select
              value={toPeriod}
              onChange={(e) => setToPeriod(e.target.value)}
              className="rounded-atlas-sm border border-border bg-card px-2 py-1 text-xs text-foreground"
            >
              <option value="">Latest</option>
              {periods.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <AtlasButton
            variant="secondary"
            size="sm"
            onClick={handleExport}
            disabled={!finance}
          >
            <Download aria-hidden="true" />
            Export
          </AtlasButton>
        </div>
      </div>

      {/* 5 KPI cards */}
      <WidgetBoundary widgetName="Finance KPIs">
        <KpiWidget
          kpis={finance?.kpis}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>

      {/* Spend trends + Policy compliance */}
      <div className="grid gap-4 md:grid-cols-2">
        <WidgetBoundary widgetName="Spend Over Time">
          <AreaChartWidget
            title="Spend Over Time"
            data={filteredTrends}
            dataKey="period"
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
        <WidgetBoundary widgetName="Policy Compliance">
          <ComplianceDonutWidget
            title="Policy Compliance"
            data={finance?.policy_compliance}
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
      </div>

      {/* Department comparison */}
      <WidgetBoundary widgetName="Department Comparison">
        <DepartmentBudgetWidget
          title="Department Comparison (Budget vs Actual)"
          data={finance?.spend_by_department}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>

      {/* Top categories */}
      <WidgetBoundary widgetName="Spend by Category">
        <ChartWidget
          title="Top Categories"
          data={finance?.spend_by_category}
          dataKey="category"
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>
    </div>
  );
}
