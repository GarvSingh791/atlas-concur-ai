/**
 * UAT round 2 — Issue 3: the receipts "Confidence" label covered the Edit/Save
 * buttons and the value column collapsed to 0px.
 *
 * jsdom cannot lay out a page or evaluate container queries, so these tests
 * pin the STRUCTURAL contract the CSS in styles/globals.css relies on (the
 * real overlap check is the p7t4 Playwright multi-width test):
 *   - the row uses the container-query grid (`.atlas-field-grid`), not a
 *     viewport-breakpoint `grid-cols-*` utility that would override it;
 *   - each cell carries its `data-cell` grid-area hook;
 *   - the ConfidenceIndicator can wrap/shrink and its label is a real font-size;
 *   - the inline editor wraps and its input can shrink.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { FieldRow, FIELD_ROW_GRID } from "@/components/receipts/field-row";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import { ConfidenceIndicator } from "@/components/atlas/confidence-indicator";
import type { FieldRow as FieldRowModel } from "@/lib/receipts/fields";
import type { Receipt } from "@/lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function row(over: Partial<FieldRowModel> = {}): FieldRowModel {
  return {
    key: "vendor",
    label: "Vendor",
    value: "Marriott Marquis Times Square Very Long Hotel Name",
    rawValue: "Marriott Marquis Times Square Very Long Hotel Name",
    hasValue: true,
    confidence: 97,
    belowThreshold: false,
    provenance: "deterministic",
    requiresUserInput: false,
    ...over,
  };
}

function classesOf(el: Element | null): string[] {
  expect(el).not.toBeNull();
  return (el as Element).getAttribute("class")?.split(/\s+/) ?? [];
}

describe("FieldRow layout contract", () => {
  it("renders label/value/confidence/source cells inside .atlas-field-grid", () => {
    const { container } = render(<FieldRow row={row()} onSave={() => {}} />);
    expect(FIELD_ROW_GRID).toBe("atlas-field-grid");
    const grid = container.querySelector(".atlas-field-grid");
    // No viewport-breakpoint grid utilities may override the container query.
    expect(classesOf(grid).some((c) => /grid-cols|^md:grid/.test(c))).toBe(
      false,
    );
    const cells = Array.from(grid!.children).map((c) =>
      c.getAttribute("data-cell"),
    );
    expect(cells).toEqual(["label", "value", "confidence", "source"]);
    for (const cell of ["label", "value", "confidence"]) {
      expect(classesOf(grid!.querySelector(`[data-cell="${cell}"]`))).toContain(
        "min-w-0",
      );
    }
    // The visible value span can itself shrink (truncate needs min-w-0).
    const value = screen.getByTitle(row().value);
    expect(classesOf(value)).toEqual(
      expect.arrayContaining(["min-w-0", "truncate"]),
    );
    // Edit button is present and does not shrink away.
    expect(
      classesOf(screen.getByRole("button", { name: "Edit Vendor" })),
    ).toContain("shrink-0");
  });

  it("confidence label uses the typed caption size and keeps the grey colour", () => {
    render(<FieldRow row={row()} onSave={() => {}} />);
    const label = screen.getByText("Confidence: 97%");
    const cls = classesOf(label);
    expect(cls).toContain("text-[length:var(--text-caption-size)]");
    expect(cls).toContain("text-[#64748B]");
    expect(cls).not.toContain("text-[var(--text-caption-size)]");
  });

  it("source badge cell carries no cell-level justify utility (CSS owns placement)", () => {
    const { container } = render(<FieldRow row={row()} onSave={() => {}} />);
    const source = container.querySelector('[data-cell="source"]');
    expect(classesOf(source).some((c) => /justify-self/.test(c))).toBe(false);
  });

  it("edit mode wraps and lets the input shrink", () => {
    render(<FieldRow row={row()} onSave={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit Vendor" }));
    const input = screen.getByLabelText("Vendor value");
    expect(classesOf(input)).toEqual(
      expect.arrayContaining(["min-w-[6rem]", "flex-1"]),
    );
    expect(classesOf(input.parentElement)).toContain("flex-wrap");
    expect(screen.getByRole("button", { name: "Save Vendor" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Cancel editing Vendor" }),
    ).toBeTruthy();
  });
});

describe("ConfidenceIndicator can shrink", () => {
  it("root wraps and shrinks; bar keeps a minimum; meter contract unchanged", () => {
    render(<ConfidenceIndicator value={83} />);
    const meter = screen.getByRole("meter");
    expect(classesOf(meter)).toEqual(
      expect.arrayContaining(["flex-wrap", "min-w-0", "max-w-full"]),
    );
    expect(meter.getAttribute("aria-valuenow")).toBe("83");
    expect(meter.getAttribute("aria-valuemin")).toBe("0");
    expect(meter.getAttribute("aria-valuemax")).toBe("100");
    expect(meter.getAttribute("aria-label")).toBe("Medium confidence");
    const bar = meter.lastElementChild;
    expect(classesOf(bar)).toEqual(
      expect.arrayContaining(["w-[120px]", "min-w-[48px]", "shrink"]),
    );
  });

  it("still omits the label when showLabel is false", () => {
    render(<ConfidenceIndicator value={95} showLabel={false} />);
    expect(screen.queryByText(/Confidence:/)).toBeNull();
  });
});

describe("ExtractionReview field table", () => {
  function receipt(): Receipt {
    return {
      id: "rcpt-9",
      organization_id: "org-1",
      uploaded_by: "user-1",
      expense_id: null,
      image_url: "org-1/abc",
      ocr_status: "processed",
      processing_path: "deterministic",
      extracted_fields: {
        amount: 42.5,
        currency: "USD",
        date: "2026-06-11",
        vendor: "Marriott",
      },
      field_confidence: {
        amount: 0.95,
        currency: 0.92,
        date: 0.9,
        vendor: 0.9,
      },
      field_provenance: {
        amount: "deterministic",
        currency: "deterministic",
        date: "deterministic",
        vendor: "deterministic",
      },
      grounding_context: null,
      created_at: "2026-06-11T10:00:00Z",
    } as Receipt;
  }

  it("table wrapper is a size container and the header is .atlas-field-header", () => {
    (api.get as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...receipt(),
      image_access_url: null,
    });
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { container } = render(
      <QueryClientProvider client={qc}>
        <ExtractionReview
          receipt={receipt()}
          previewUrl=""
          fileName="r.png"
          onReceiptUpdate={() => {}}
        />
      </QueryClientProvider>,
    );
    const table = container.querySelector(".atlas-field-table");
    expect(table).not.toBeNull();
    expect(classesOf(table)).toContain("min-w-0");
    const header = table!.querySelector(".atlas-field-header");
    const hcls = classesOf(header);
    expect(hcls).toContain("atlas-field-grid");
    expect(hcls).not.toContain("hidden");
    expect(hcls.some((c) => /^md:/.test(c))).toBe(false);
    expect(
      Array.from(header!.children).map((c) => c.getAttribute("data-cell")),
    ).toEqual(["label", "value", "confidence", "source"]);
    // Header + every field row are grids inside the same size container, and
    // each grid has exactly one value cell.
    const grids = table!.querySelectorAll(".atlas-field-grid");
    expect(grids.length).toBeGreaterThanOrEqual(2);
    expect(table!.querySelectorAll('[data-cell="value"]').length).toBe(
      grids.length,
    );
  });
});
