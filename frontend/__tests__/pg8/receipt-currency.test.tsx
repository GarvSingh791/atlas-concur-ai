import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), patch: vi.fn(), get: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import { initialReportId } from "@/components/expenses/expenses-screen";
import type { Receipt } from "@/lib/types";

const patchMock = api.patch as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function receipt(overrides: Partial<Receipt> = {}): Receipt {
  return {
    id: "rcpt-1",
    organization_id: "org-1",
    uploaded_by: "user-1",
    expense_id: "e-1",
    image_url: "org-1/abc",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: {
      amount: 220,
      currency: "EUR",
      date: "2026-06-11",
      vendor: "Hotel Paris",
      location: "Paris",
      expense_type: "meals",
    },
    field_confidence: {
      amount: 0.95,
      currency: 0.95,
      date: 0.95,
      vendor: 0.95,
      location: 0.95,
      expense_type: 0.95,
    },
    field_provenance: {},
    created_at: "2026-06-11T10:00:00Z",
    amount_conversion: {
      original_amount: "220.0",
      original_currency: "EUR",
      converted_amount: "238.70",
      currency: "USD",
      rate: "1.085",
      supported: true,
      rates_as_of: "2026-11-01",
    },
    ...overrides,
  };
}

function renderReview(r: Receipt) {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ExtractionReview
        receipt={r}
        previewUrl=""
        fileName="r.png"
        onReceiptUpdate={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe("receipt review currency (product gap 8)", () => {
  it("shows the original and the converted amount with the rate", () => {
    renderReview(receipt());
    const note = screen.getByTestId("receipt-amount-conversion");
    expect(note.textContent).toContain("EUR 220.00 ≈ USD 238.70 at 1.085");
    expect(note.textContent).toContain("not live market data");
  });

  it("shows no conversion for a base-currency receipt", () => {
    renderReview(receipt({ amount_conversion: null }));
    expect(screen.queryByTestId("receipt-amount-conversion")).toBeNull();
  });

  it("refuses an unsupported currency correction before any PATCH", async () => {
    renderReview(receipt());
    fireEvent.click(screen.getByLabelText("Edit Currency"));
    fireEvent.change(screen.getByLabelText("Currency value"), {
      target: { value: "BRL" },
    });
    fireEvent.click(screen.getByLabelText("Save Currency"));
    // The editor stays open (the save was rejected) and nothing was sent.
    await waitFor(() =>
      expect(screen.getByLabelText("Currency value")).toBeTruthy(),
    );
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("sends a supported currency upper-cased", async () => {
    const base = receipt();
    patchMock.mockResolvedValue({
      ...base,
      extracted_fields: { ...base.extracted_fields, currency: "GBP" },
    });
    renderReview(base);
    fireEvent.click(screen.getByLabelText("Edit Currency"));
    fireEvent.change(screen.getByLabelText("Currency value"), {
      target: { value: " gbp " },
    });
    fireEvent.click(screen.getByLabelText("Save Currency"));
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [path, opts] = patchMock.mock.calls[0] as [
      string,
      { body: Record<string, unknown> },
    ];
    expect(path).toBe("/receipts/rcpt-1");
    expect(opts.body).toEqual({ field: "currency", value: "GBP" });
  });

  it("flags an unsupported conversion instead of a figure", () => {
    renderReview(
      receipt({
        amount_conversion: {
          original_amount: "120.0",
          original_currency: "BRL",
          converted_amount: null,
          currency: "USD",
          rate: null,
          supported: false,
        },
      }),
    );
    const note = screen.getByTestId("receipt-amount-conversion");
    expect(note.getAttribute("data-supported")).toBe("false");
    expect(note.textContent).toContain("BRL is not a supported currency");
  });
});

describe("expenses deep link", () => {
  it("reads ?report= from the search string", () => {
    expect(initialReportId("?report=r1")).toBe("r1");
    expect(initialReportId("?report=")).toBeNull();
    expect(initialReportId("")).toBeNull();
    expect(initialReportId("?other=1")).toBeNull();
  });
});
