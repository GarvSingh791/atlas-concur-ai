"use client";

import { useAnalyticsOverview } from "@/lib/query/analytics-hooks";
import { WidgetBoundary } from "@/components/dashboard/widget-boundary";
import { QueueWidget } from "@/components/dashboard/widgets/queue-widget";
import { ChartWidget } from "@/components/dashboard/widgets/chart-widget";
import { activityText, subjectTypeLabel } from "@/lib/dashboard/format";
import type { SpendByCategory } from "@/lib/types/analytics";

export function ManagerDashboard() {
  const {
    data: overview,
    isLoading,
    isError,
    refetch,
  } = useAnalyticsOverview();
  const manager = overview?.manager;

  // Build spend by category as chart data (already typed as SpendByCategory[])
  const spendByCategory: SpendByCategory[] =
    manager?.team_spend?.by_category ?? [];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-foreground">Dashboard</h1>

      {/* Approval Queue */}
      <div>
        <h2 className="text-base font-medium text-foreground mb-3">
          Pending Approvals
          {manager?.approval_queue_total != null && (
            <span className="ml-2 text-xs text-muted-foreground">
              ({manager.approval_queue_total} total)
            </span>
          )}
        </h2>
        <WidgetBoundary widgetName="Approval Queue">
          <QueueWidget
            items={manager?.approval_queue}
            total={manager?.approval_queue_total}
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
      </div>

      {/* Team Spend Chart */}
      <div>
        <WidgetBoundary widgetName="Team Spend">
          <ChartWidget
            title="Team Spend by Category"
            data={spendByCategory}
            dataKey="category"
            isLoading={isLoading}
            isError={isError}
            onRetry={() => refetch()}
          />
        </WidgetBoundary>
      </div>

      {/* Recent Activity */}
      <div>
        <h2 className="text-base font-medium text-foreground mb-3">
          Recent Activity
        </h2>
        <WidgetBoundary widgetName="Recent Activity">
          {isLoading ? (
            <div className="space-y-2">
              {[1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-12 rounded-atlas-md border border-border bg-card atlas-shimmer"
                />
              ))}
            </div>
          ) : isError ? (
            <div className="text-sm text-muted-foreground p-4 border border-border rounded-atlas-md">
              Failed to load recent activity.{" "}
              <button onClick={() => refetch()} className="underline">
                Retry
              </button>
            </div>
          ) : !manager?.recent_activity?.length ? (
            <div className="text-sm text-muted-foreground p-4 border border-border rounded-atlas-md">
              No recent activity.
            </div>
          ) : (
            <div className="divide-y divide-border rounded-atlas-md border border-border bg-card">
              {manager.recent_activity.map((item) => (
                <div key={item.id} className="px-4 py-3">
                  {/* QA round: readable action text — never the raw action
                      key or the entity UUID the server summary used to carry. */}
                  <p className="text-sm font-medium text-foreground">
                    {activityText(item.action)}
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {item.actor_name ?? "System"} ·{" "}
                    {subjectTypeLabel(item.entity_type)} ·{" "}
                    {new Date(item.occurred_at).toLocaleDateString()}
                  </p>
                </div>
              ))}
            </div>
          )}
        </WidgetBoundary>
      </div>
    </div>
  );
}
