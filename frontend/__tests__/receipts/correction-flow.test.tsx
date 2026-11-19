import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Mock the API client at the boundary so we can assert exactly what the client sends.
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import type { Receipt } from "@/lib/types";

const patchMock = api.patch as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function baseReceipt(): Receipt {
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
      location: "New York, NY",
      expense_type: "hotel",
    },
    field_confidence: {
      amount: 0.95,
      currency: 0.92,
      date: 0.91,
      vendor: 0.9,
      location: 0.9,
      expense_type: 0.9,
    },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
      vendor: "deterministic",
      location: "deterministic",
      expense_type: "deterministic",
    },
    created_at: "2026-06-11T10:00:00Z",
  };
}

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function Harness({ initial }: { initial: Receipt }) {
  const [receipt, setReceipt] = React.useState(initial);
  return (
    <ExtractionReview
      receipt={receipt}
      previewUrl=""
      fileName="receipt.png"
      onReceiptUpdate={setReceipt}
    />
  );
}

describe("inline correction → PATCH → provenance flip", () => {
  it("sends a value-only PATCH (no provenance) and re-renders the flipped badge from the server response", async () => {
    const base = baseReceipt();
    const updated: Receipt = {
      ...base,
      extracted_fields: { ...base.extracted_fields, vendor: "Hilton" },
      field_provenance: { ...base.field_provenance, vendor: "user_edited" },
    };
    patchMock.mockResolvedValue(updated);

    render(
      <Wrapper>
        <Harness initial={base} />
      </Wrapper>,
    );

    // Before: vendor is server-extracted (no "User Edited" badge yet).
    expect(screen.queryByText("User Edited")).toBeNull();

    // Edit the vendor field inline.
    fireEvent.click(screen.getByLabelText("Edit Vendor"));
    const input = screen.getByLabelText("Vendor value") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Hilton" } });
    fireEvent.click(screen.getByLabelText("Save Vendor"));

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));

    // The client sent ONLY { field, value } — never provenance.
    const [path, opts] = patchMock.mock.calls[0] as [
      string,
      { body: Record<string, unknown> },
    ];
    expect(path).toBe("/receipts/rcpt-9");
    expect(opts.body).toEqual({ field: "vendor", value: "Hilton" });
    expect(Object.keys(opts.body).sort()).toEqual(["field", "value"]);
    expect(opts.body).not.toHaveProperty("provenance");

    // After: the badge flips to "User Edited" from the server-authoritative response.
    await waitFor(() => expect(screen.getByText("User Edited")).toBeTruthy());
  });

  it("coerces a numeric amount correction to a number", async () => {
    const base = baseReceipt();
    patchMock.mockResolvedValue({
      ...base,
      extracted_fields: { ...base.extracted_fields, amount: 99 },
      field_provenance: { ...base.field_provenance, amount: "user_edited" },
    });

    render(
      <Wrapper>
        <Harness initial={base} />
      </Wrapper>,
    );

    fireEvent.click(screen.getByLabelText("Edit Amount"));
    fireEvent.change(screen.getByLabelText("Amount value"), {
      target: { value: "99" },
    });
    fireEvent.click(screen.getByLabelText("Save Amount"));

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const opts = patchMock.mock.calls[0][1] as { body: { value: unknown } };
    expect(opts.body.value).toBe(99); // number, not the string "99"
  });
});
