/** Product gap 6 + 8 — the approval filter bar (controls, count, clear, export). */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import { ApprovalFilterBar } from "@/components/approvals/approval-filter-bar";
import type { ExceptionTypeOption } from "@/lib/types/approval-filters";

afterEach(cleanup);

const TYPES: ExceptionTypeOption[] = [
  {
    value: "missing_receipt",
    label: "Missing receipt",
    kind: "flag",
    count: 3,
  },
  { value: "amount_cap", label: "Rule: amount cap", kind: "rule", count: 0 },
];

function setup(
  overrides: Partial<React.ComponentProps<typeof ApprovalFilterBar>> = {},
) {
  const onChange = vi.fn();
  render(
    <ApprovalFilterBar
      value={{}}
      onChange={onChange}
      exceptionTypes={TYPES}
      departments={["Engineering", "Sales"]}
      total={12}
      loaded={12}
      isFetching={false}
      canExport={false}
      {...overrides}
    />,
  );
  return onChange;
}

describe("ApprovalFilterBar", () => {
  it("emits the next filter set for each control", () => {
    const onChange = setup({ value: { current_status: "pending" } });
    fireEvent.change(screen.getByLabelText("Exception type"), {
      target: { value: "missing_receipt" },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      current_status: "pending",
      exception_type: "missing_receipt",
    });
    fireEvent.change(screen.getByLabelText("Department"), {
      target: { value: "Sales" },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      current_status: "pending",
      department: "Sales",
    });
    fireEvent.change(screen.getByLabelText("Submitted from"), {
      target: { value: "2026-04-01" },
    });
    expect(onChange).toHaveBeenLastCalledWith({
      current_status: "pending",
      submitted_from: "2026-04-01",
    });
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "" },
    });
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("lists exception types with their scoped counts", () => {
    setup();
    expect(
      screen.getByRole("option", { name: "Missing receipt (3)" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("option", { name: "Rule: amount cap (0)" }),
    ).toBeTruthy();
  });

  it("shows the result count and a Clear all only when filtered", () => {
    setup();
    expect(screen.getByTestId("approval-result-count").textContent).toBe(
      "12 results",
    );
    expect(screen.queryByTestId("approval-filters-clear")).toBeNull();
    cleanup();

    const onChange = setup({
      value: { department: "Sales", exception_type: "missing_receipt" },
      total: 140,
      loaded: 100,
    });
    expect(screen.getByTestId("approval-result-count").textContent).toBe(
      "140 results · showing the first 100 · 2 filters applied",
    );
    fireEvent.click(screen.getByTestId("approval-filters-clear"));
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("keeps a linked department visible even without an option", () => {
    setup({ value: { department: "Legacy Dept" } });
    expect(
      (screen.getByLabelText("Department") as HTMLSelectElement).value,
    ).toBe("Legacy Dept");
  });

  it("offers Export CSV only to finance", () => {
    setup();
    expect(screen.queryByTestId("export-csv")).toBeNull();
    cleanup();
    setup({ canExport: true });
    expect(screen.getByTestId("export-csv").textContent).toContain(
      "Export CSV",
    );
  });
});
