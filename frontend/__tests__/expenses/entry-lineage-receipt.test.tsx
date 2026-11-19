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

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { EntryLineage } from "@/components/expenses/entry-lineage";
import { ApiError } from "@/lib/types";
import type { ExpenseEntry, Receipt } from "@/lib/types";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function entry(overrides: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-1",
    organization_id: "org-1",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "lodging",
    amount: "180.00",
    currency: "USD",
    expense_date: "2026-06-11",
    vendor: "Marriott",
    description: null,
    status: "draft",
    match_source: "receipt",
    is_itemized: false,
    created_by: "u-1",
    updated_by: "u-1",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    receipt_id: "rcpt-1",
    ...overrides,
  };
}

function receipt(url: string | null): Receipt {
  return {
    id: "rcpt-1",
    organization_id: "org-1",
    uploaded_by: "u-1",
    expense_id: "e-1",
    image_url: "org-1/abc",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: null,
    field_confidence: null,
    field_provenance: null,
    created_at: "2026-06-11T10:00:00Z",
    image_access_url: url,
    image_access_expires_at: url
      ? new Date(Date.now() + 300_000).toISOString()
      : null,
    image_access_expires_in: url ? 300 : null,
  };
}

function renderLineage(e: ExpenseEntry = entry()) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <EntryLineage entry={e} />
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

/** Wait until the GET /receipts/{id} query has actually SETTLED. */
async function settled(qc: QueryClient, status: "success" | "error") {
  await waitFor(() =>
    expect(qc.getQueryState(["receipt", "rcpt-1"])?.status).toBe(status),
  );
}

const IMG_ALT = "Receipt preview of Marriott";

describe("EntryLineage — scoped receipt image (C2)", () => {
  it("renders the signed link returned by GET /receipts/{id}", async () => {
    getMock.mockResolvedValue(receipt("https://signed.test/a?sig=1"));
    renderLineage();

    const img = (await screen.findByAltText(IMG_ALT)) as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("https://signed.test/a?sig=1");
    expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-1");
  });

  it("refetches once on an <img> error, then stops (no refetch loop)", async () => {
    getMock
      .mockResolvedValueOnce(receipt("https://signed.test/a?sig=1"))
      .mockResolvedValueOnce(receipt("https://signed.test/a?sig=2"));
    renderLineage();

    const first = await screen.findByAltText(IMG_ALT);
    fireEvent.error(first);
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByAltText(IMG_ALT).getAttribute("src")).toBe(
        "https://signed.test/a?sig=2",
      ),
    );

    // The fresh link also fails → give up gracefully, no further fetches.
    fireEvent.error(screen.getByAltText(IMG_ALT));
    expect(await screen.findByText("Preview unavailable")).toBeTruthy();
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("shows 'Preview unavailable' when the server has no servable link", async () => {
    getMock.mockResolvedValue(receipt(null));
    const { qc } = renderLineage();

    await settled(qc, "success");
    expect(
      qc.getQueryData<Receipt>(["receipt", "rcpt-1"])?.image_access_url,
    ).toBeNull();
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Preview unavailable")).toBeTruthy();
    expect(screen.queryByAltText(IMG_ALT)).toBeNull();
  });

  it("drops back to 'Preview unavailable' when a refetch returns no link", async () => {
    getMock
      .mockResolvedValueOnce(receipt("https://signed.test/a?sig=1"))
      .mockResolvedValueOnce(receipt(null));
    const { qc } = renderLineage();

    await screen.findByAltText(IMG_ALT);
    await qc.refetchQueries({ queryKey: ["receipt", "rcpt-1"] });

    await waitFor(() => expect(screen.queryByAltText(IMG_ALT)).toBeNull());
    expect(getMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Preview unavailable")).toBeTruthy();
  });

  it("shows 'Preview unavailable' when the receipt is out of scope (404)", async () => {
    getMock.mockRejectedValue(
      new ApiError(
        { code: "NOT_FOUND", message: "nope", correlation_id: "c-1" },
        404,
      ),
    );
    const { qc } = renderLineage();

    await settled(qc, "error");
    expect(getMock).toHaveBeenCalledTimes(1); // 4xx is never retried
    expect(screen.getByText("Preview unavailable")).toBeTruthy();
    expect(screen.queryByAltText(IMG_ALT)).toBeNull();
  });

  it("does not fetch anything for an entry without a linked receipt", () => {
    renderLineage(entry({ receipt_id: null }));
    expect(getMock).not.toHaveBeenCalled();
    expect(screen.getByText(/Linked from an uploaded receipt/i)).toBeTruthy();
  });
});
