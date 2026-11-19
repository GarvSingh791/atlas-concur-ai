import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

afterEach(cleanup);
import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import type { KpiMetric } from "@/lib/types/analytics";

const mockKpis: KpiMetric[] = [
  {
    label: "Upcoming Trips",
    value: 2,
    unit: "count",
    trend: null,
    trend_direction: null,
  },
  {
    label: "Pending Approvals",
    value: 1,
    unit: "count",
    trend: null,
    trend_direction: null,
  },
];

describe("KpiWidget", () => {
  it("shows loading state when isLoading=true", () => {
    const { container } = render(
      <KpiWidget kpis={undefined} isLoading={true} isError={false} />,
    );
    // Should render shimmer skeletons (loading state)
    expect(container.querySelector(".atlas-shimmer")).toBeTruthy();
  });

  it("shows error state when isError=true", () => {
    render(<KpiWidget kpis={undefined} isLoading={false} isError={true} />);
    expect(screen.getByText(/Failed to load KPIs/i)).toBeTruthy();
  });

  it("shows empty state when kpis is empty array", () => {
    render(<KpiWidget kpis={[]} isLoading={false} isError={false} />);
    expect(screen.getByText(/No metrics yet/i)).toBeTruthy();
  });

  it("renders kpi cards with labels", () => {
    render(<KpiWidget kpis={mockKpis} isLoading={false} isError={false} />);
    expect(screen.getByText("Upcoming Trips")).toBeTruthy();
    expect(screen.getByText("Pending Approvals")).toBeTruthy();
  });
});
