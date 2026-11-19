import { describe, it, expect, beforeAll, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import { ComplianceDonutWidget } from "@/components/dashboard/widgets/compliance-donut-widget";
import { DepartmentBudgetWidget } from "@/components/dashboard/widgets/department-budget-widget";
import type { KpiMetric, SpendByDepartment } from "@/lib/types/analytics";

afterEach(cleanup);

// jsdom lacks ResizeObserver, which recharts' ResponsiveContainer requires.
beforeAll(() => {
  globalThis.ResizeObserver =
    globalThis.ResizeObserver ??
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
});

const KPIS: KpiMetric[] = [
  {
    label: "Consolidated Spend",
    value: 12500,
    unit: "currency",
    trend: null,
    trend_direction: null,
  },
  {
    label: "Automation Rate",
    value: 62.5,
    unit: "percent",
    trend: null,
    trend_direction: null,
  },
  {
    label: "Avg Cycle Time",
    value: 2.4,
    unit: "days",
    trend: null,
    trend_direction: null,
  },
];

describe("KpiWidget — server-metric formatting (no client compute)", () => {
  it("formats currency / percent / days straight from the server values", () => {
    render(<KpiWidget kpis={KPIS} isLoading={false} isError={false} />);
    expect(screen.getByText("$12,500.00")).toBeTruthy();
    expect(screen.getByText("62.5%")).toBeTruthy();
    expect(screen.getByText("2.4 days")).toBeTruthy();
  });

  it("renders an empty state when there are no KPIs", () => {
    render(<KpiWidget kpis={[]} isLoading={false} isError={false} />);
    expect(screen.getByText(/No metrics yet/i)).toBeTruthy();
  });
});

describe("ComplianceDonutWidget — center % is a pure display of server counts", () => {
  it("renders the compliant percentage from the breakdown", () => {
    render(
      <ComplianceDonutWidget
        title="Policy Compliance"
        data={{ compliant: 6, exceptions: 2, violations: 2 }}
        isLoading={false}
        isError={false}
      />,
    );
    // 6 / (6+2+2) = 60%
    expect(screen.getByText("60%")).toBeTruthy();
    expect(screen.getByText(/Compliant \(6\)/)).toBeTruthy();
    expect(screen.getByText(/Violations \(2\)/)).toBeTruthy();
  });

  it("shows a gap state when the breakdown totals zero", () => {
    render(
      <ComplianceDonutWidget
        title="Policy Compliance"
        data={{ compliant: 0, exceptions: 0, violations: 0 }}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/No compliance data/i)).toBeTruthy();
  });
});

describe("DepartmentBudgetWidget — budget vs actual", () => {
  const DATA: SpendByDepartment[] = [
    { department: "engineering", amount: 900, budget: 1000 },
    { department: "operations", amount: 1200, budget: 800 },
  ];

  it("renders the comparison title for real data", () => {
    render(
      <DepartmentBudgetWidget
        title="Department Comparison"
        data={DATA}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText("Department Comparison")).toBeTruthy();
  });

  it("renders an honest gap state for empty data", () => {
    render(
      <DepartmentBudgetWidget
        title="Department Comparison"
        data={[]}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText(/No department data/i)).toBeTruthy();
  });
});
