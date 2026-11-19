"use client";

import {
  useAnalyticsOverview,
  useTrips,
  useExpenseReports,
} from "@/lib/query/analytics-hooks";
import { WidgetBoundary } from "@/components/dashboard/widget-boundary";
import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import { TimelineWidget } from "@/components/dashboard/widgets/timeline-widget";
import {
  RecommendationsWidget,
  PolicyWarningsWidget,
} from "@/components/dashboard/widgets/status-widget";
import {
  AtlasButton,
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/atlas";
import { ReportStatusBadge } from "@/components/expenses/report-status-badge";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import { useRouter } from "next/navigation";

// ---------------------------------------------------------------------------
// Inline expense list — expense reports don't have the trip-shaped fields
// (origin, primary_destination) that TimelineWidget requires, so we render
// a simple list instead.
// ---------------------------------------------------------------------------

interface ExpenseListItem {
  id: string;
  name?: string;
  title?: string;
  submitted_at?: string;
  created_at?: string;
  status: string;
  /** Decimal on the server → serialized as a JSON string. */
  total_amount?: number | string;
  currency?: string;
}

interface ExpensePage {
  items: ExpenseListItem[];
  total: number;
}

interface RecentExpensesWidgetProps {
  data: ExpensePage | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

function RecentExpensesWidget({
  data,
  isLoading,
  isError,
  onRetry,
}: RecentExpensesWidgetProps) {
  if (isLoading) return <LoadingState variant="table" rows={4} />;
  if (isError) {
    return (
      <ErrorState
        heading="Failed to load expenses"
        subtext="Could not fetch recent expense reports."
        onRetry={onRetry}
      />
    );
  }

  const expenses = data?.items ?? [];

  if (expenses.length === 0) {
    return (
      <EmptyState
        heading="No expense reports"
        subtext="Your recent expense reports will appear here."
      />
    );
  }

  return (
    <div className="divide-y divide-border rounded-atlas-md border border-border bg-card">
      {expenses.map((exp) => (
        <div
          key={exp.id}
          className="flex items-center justify-between gap-3 px-4 py-3"
          data-testid={`recent-expense-${exp.id}`}
        >
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium truncate">
              {exp.name ?? exp.title ?? "Expense Report"}
            </p>
            <p className="text-xs text-muted-foreground">
              {formatDate(exp.submitted_at ?? exp.created_at)}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0 sm:gap-3">
            {exp.total_amount != null && (
              <span className="whitespace-nowrap text-sm font-mono">
                {formatCurrency(exp.total_amount, exp.currency ?? "USD")}
              </span>
            )}
            <ReportStatusBadge status={exp.status} />
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EmployeeDashboard
// ---------------------------------------------------------------------------

export function EmployeeDashboard() {
  const router = useRouter();
  const {
    data: overview,
    isLoading,
    isError,
    refetch,
  } = useAnalyticsOverview();
  const {
    data: trips,
    isLoading: tripsLoading,
    isError: tripsError,
    refetch: refetchTrips,
  } = useTrips({ size: 5 });
  const {
    data: expenses,
    isLoading: expLoading,
    isError: expError,
    refetch: refetchExpenses,
  } = useExpenseReports({ size: 5 });

  const employee = overview?.employee;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-foreground">Dashboard</h1>

      {/* KPI Cards */}
      <WidgetBoundary widgetName="KPI metrics">
        <KpiWidget
          kpis={employee?.kpis}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => refetch()}
        />
      </WidgetBoundary>

      {/* Trip Timeline + Recent Expenses — QA round: grid-cols-1 + min-w-0 let
          the column shrink to a phone's width instead of growing to its
          longest (nowrap) title and clipping the badges off-screen. */}
      <div
        className="grid grid-cols-1 gap-4 md:grid-cols-2"
        data-testid="employee-widgets"
      >
        <div className="min-w-0">
          <h2 className="text-base font-medium text-foreground mb-3">
            Trip Timeline
          </h2>
          <WidgetBoundary widgetName="Trip Timeline">
            <TimelineWidget
              data={trips as Parameters<typeof TimelineWidget>[0]["data"]}
              isLoading={tripsLoading}
              isError={tripsError}
              onRetry={() => refetchTrips()}
            />
          </WidgetBoundary>
        </div>
        <div className="min-w-0">
          <h2 className="text-base font-medium text-foreground mb-3">
            Recent Expenses
          </h2>
          <WidgetBoundary widgetName="Recent Expenses">
            <RecentExpensesWidget
              data={expenses as ExpensePage | undefined}
              isLoading={expLoading}
              isError={expError}
              onRetry={() => refetchExpenses()}
            />
          </WidgetBoundary>
        </div>
      </div>

      {/* AI Recommendations + Policy Warnings */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="min-w-0">
          <h2 className="text-base font-medium text-foreground mb-3">
            AI Recommendations
          </h2>
          <WidgetBoundary widgetName="AI Recommendations">
            <RecommendationsWidget
              recommendations={employee?.ai_recommendations}
              isLoading={isLoading}
              isError={isError}
              onRetry={() => refetch()}
            />
          </WidgetBoundary>
        </div>
        <div>
          <h2 className="text-base font-medium text-foreground mb-3">
            Policy Warnings
          </h2>
          <WidgetBoundary widgetName="Policy Warnings">
            <PolicyWarningsWidget
              warnings={employee?.policy_warnings}
              isLoading={isLoading}
              isError={isError}
              onRetry={() => refetch()}
            />
          </WidgetBoundary>
        </div>
      </div>

      {/* CTA */}
      <div className="flex justify-center pt-2">
        <AtlasButton
          onClick={() => router.push("/planner")}
          variant="primary"
          size="lg"
        >
          Plan your next trip →
        </AtlasButton>
      </div>
    </div>
  );
}
