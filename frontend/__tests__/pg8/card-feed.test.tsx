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
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/receipts" }));
import { api } from "@/lib/api/client";
import { CardFeed, expenseHref } from "@/components/receipts/card-feed";
import { ReceiptsScreen } from "@/components/receipts/receipts-screen";
import type { CardTransaction } from "@/lib/types/cards";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const matched: CardTransaction = {
  id: "t1",
  merchant: "Joe's Pizza NYC",
  txn_date: "2026-06-11",
  amount: "38.50",
  currency: "USD",
  last_four: "4242",
  status: "matched",
  expense_id: "e1",
  report_id: "r1",
  receipt_id: "rc1",
  amount_conversion: null,
};
const unmatched: CardTransaction = {
  id: "t2",
  merchant: "Hotel Paris",
  txn_date: "2026-06-10",
  amount: "220.00",
  currency: "EUR",
  last_four: null,
  status: "unmatched",
  expense_id: null,
  report_id: null,
  receipt_id: null,
  amount_conversion: {
    original_amount: "220.00",
    original_currency: "EUR",
    converted_amount: "238.70",
    currency: "USD",
    rate: "1.085",
    supported: true,
  },
};

function page(items: CardTransaction[], extra: Record<string, number> = {}) {
  return { items, total: items.length, page: 1, size: 20, pages: 1, ...extra };
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("CardFeed (product gap 8)", () => {
  it("lists merchant, date, amount, status and the right call to action", async () => {
    getMock.mockResolvedValue(page([matched, unmatched]));
    const onUpload = vi.fn();
    wrap(<CardFeed onUploadReceipt={onUpload} />);

    const row1 = await screen.findByTestId("card-txn-t1");
    expect(row1.textContent).toContain("Joe's Pizza NYC");
    expect(row1.textContent).toContain("Jun 11, 2026");
    expect(row1.textContent).toContain("$38.50");
    expect(row1.textContent).toContain("Matched");
    expect(row1.textContent).toContain("Card ending 4242");
    const link = screen.getByTestId("card-txn-link-t1");
    expect(link.getAttribute("href")).toBe("/expenses?report=r1");
    expect(link.textContent).toContain("receipt");
    expect(screen.queryByTestId("card-txn-upload-t1")).toBeNull();

    const row2 = screen.getByTestId("card-txn-t2");
    expect(row2.textContent).toContain("Unmatched");
    expect(screen.getByTestId("card-txn-conversion-t2").textContent).toContain(
      "EUR 220.00 ≈ USD 238.70 at 1.085",
    );
    expect(screen.queryByTestId("card-txn-link-t2")).toBeNull();
    fireEvent.click(screen.getByTestId("card-txn-upload-t2"));
    expect(onUpload).toHaveBeenCalledTimes(1);

    expect(getMock).toHaveBeenCalledWith("/card-transactions", {
      params: { page: 1, size: 20 },
    });
  });

  it("sends the status and date filters", async () => {
    getMock.mockResolvedValue(page([unmatched]));
    wrap(<CardFeed onUploadReceipt={() => {}} />);
    await screen.findByTestId("card-txn-t2");

    fireEvent.change(screen.getByTestId("card-feed-status"), {
      target: { value: "unmatched" },
    });
    fireEvent.change(screen.getByTestId("card-feed-from"), {
      target: { value: "2026-06-01" },
    });
    await waitFor(() =>
      expect(getMock).toHaveBeenLastCalledWith("/card-transactions", {
        params: {
          status: "unmatched",
          date_from: "2026-06-01",
          page: 1,
          size: 20,
        },
      }),
    );
  });

  it("never sends an inverted date range", async () => {
    getMock.mockResolvedValue(page([]));
    wrap(<CardFeed onUploadReceipt={() => {}} />);
    await screen.findByText("No card transactions");
    const calls = getMock.mock.calls.length;
    fireEvent.change(screen.getByTestId("card-feed-from"), {
      target: { value: "2026-06-20" },
    });
    fireEvent.change(screen.getByTestId("card-feed-to"), {
      target: { value: "2026-06-01" },
    });
    expect(await screen.findByTestId("card-feed-range-error")).toBeTruthy();
    const sent = getMock.mock.calls
      .slice(calls)
      .map((c) => (c[1] as { params: Record<string, string> }).params);
    expect(
      sent.some(
        (p) => p.date_from === "2026-06-20" && p.date_to === "2026-06-01",
      ),
    ).toBe(false);
  });

  it("shows an error state with retry", async () => {
    getMock.mockRejectedValue(new Error("boom"));
    wrap(<CardFeed onUploadReceipt={() => {}} />);
    expect(
      await screen.findByText("Couldn’t load your card feed"),
    ).toBeTruthy();
  });

  it("links an entry by report id", () => {
    expect(expenseHref("a b")).toBe("/expenses?report=a%20b");
  });
});

describe("Receipts screen tabs", () => {
  it("opens the card feed tab and returns to uploads from Upload receipt", async () => {
    getMock.mockResolvedValue(page([unmatched]));
    wrap(<ReceiptsScreen />);
    expect(screen.queryByTestId("card-feed")).toBeNull();
    fireEvent.click(screen.getByTestId("receipts-tab-cards"));
    expect(
      screen.getByTestId("receipts-tab-cards").getAttribute("aria-selected"),
    ).toBe("true");
    fireEvent.click(await screen.findByTestId("card-txn-upload-t2"));
    expect(screen.queryByTestId("card-feed")).toBeNull();
    expect(
      screen.getByTestId("receipts-tab-uploads").getAttribute("aria-selected"),
    ).toBe("true");
  });
});
