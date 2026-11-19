/**
 * C7R issue 4a — ReceiptResponse gains `needs_review` + `review_reasons`. When
 * the pipeline could not read the receipt reliably (e.g. total not legible) the
 * review UI shows the reasons prominently and does NOT prefill the amount from
 * anything; the user types the total.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get, patch, showToast } = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  showToast: vi.fn(),
}));
vi.mock("@/lib/api/client", () => ({ api: { get, patch } }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));

import { ExtractionReview } from "@/components/receipts/extraction-review";
import { ReceiptQueue } from "@/components/receipts/receipt-queue";
import { receiptReviewReasons, toFieldRows } from "@/lib/receipts/fields";
import type { Receipt } from "@/lib/types";

function receipt(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rc-1",
    organization_id: "org",
    uploaded_by: "u",
    expense_id: null,
    image_url: "org/abc.png",
    ocr_status: "processed",
    processing_path: "agent_gap_fill",
    // The pipeline's guess (e.g. a line-item subtotal) — must NOT be shown.
    extracted_fields: {
      amount: 13.37,
      currency: "USD",
      date: "2026-06-11",
      vendor: "Joe's Diner",
    },
    field_confidence: { amount: 0.41, currency: 0.9, vendor: 0.9 },
    field_provenance: {
      amount: "ai_inferred",
      currency: "deterministic",
      vendor: "deterministic",
    },
    created_at: "2026-06-11T10:00:00Z",
    needs_review: true,
    review_reasons: ["Total is not legible on the receipt."],
    ...over,
  };
}

function renderReview(r: Receipt) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
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

beforeEach(() => {
  get.mockResolvedValue({ image_access_url: null });
  patch.mockReset();
  showToast.mockReset();
});
afterEach(cleanup);

describe("C7R-4a needs-review receipts", () => {
  it("withholds the amount (value, confidence, provenance) until the user enters it", () => {
    const amount = toFieldRows(receipt()).find((r) => r.key === "amount")!;
    expect(amount.requiresUserInput).toBe(true);
    expect(amount.hasValue).toBe(false);
    expect(amount.rawValue).toBeNull();
    expect(amount.value).toBe("");
    expect(amount.confidence).toBeNull();
    expect(amount.provenance).toBeNull();
    // Other fields are untouched.
    const vendor = toFieldRows(receipt()).find((r) => r.key === "vendor")!;
    expect(vendor.value).toBe("Joe's Diner");
  });

  it("shows the user's own entered total once provenance is user_edited", () => {
    const r = receipt({
      extracted_fields: { amount: 48.2, currency: "USD" },
      field_provenance: { amount: "user_edited" },
    });
    const amount = toFieldRows(r).find((x) => x.key === "amount")!;
    expect(amount.requiresUserInput).toBe(false);
    expect(amount.value).toBe("$48.20");
  });

  it("a normal receipt (needs_review absent/false) is unchanged", () => {
    for (const r of [
      receipt({ needs_review: undefined, review_reasons: undefined }),
      receipt({ needs_review: false }),
    ]) {
      const amount = toFieldRows(r).find((x) => x.key === "amount")!;
      expect(amount.requiresUserInput).toBe(false);
      expect(amount.value).toBe("$13.37");
    }
  });

  it("renders the reasons prominently and never shows the guessed amount", () => {
    renderReview(receipt());
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/needs your review/i);
    expect(alert.textContent).toContain("Total is not legible on the receipt.");
    expect(screen.queryByText("$13.37")).toBeNull();
    expect(document.body.textContent).not.toContain("13.37");
  });

  it("the amount editor opens EMPTY (no prefill) and saves the typed total", async () => {
    patch.mockResolvedValue(
      receipt({
        extracted_fields: { amount: 48.2 },
        field_provenance: { amount: "user_edited" },
      }),
    );
    renderReview(receipt());
    fireEvent.click(screen.getByRole("button", { name: "Enter Amount" }));
    const input = screen.getByLabelText("Amount value") as HTMLInputElement;
    expect(input.value).toBe("");
    fireEvent.change(input, { target: { value: "48.20" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Amount" }));
    await vi.waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0][1]).toMatchObject({
      body: { field: "amount", value: 48.2 },
    });
  });

  it("rejects a non-numeric total without calling the API", async () => {
    renderReview(receipt());
    fireEvent.click(screen.getByRole("button", { name: "Enter Amount" }));
    const input = screen.getByLabelText("Amount value");
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Amount" }));
    await vi.waitFor(() => expect(showToast).toHaveBeenCalled());
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Amount value")).toBeTruthy(); // still editing
  });

  it("falls back to a generic message when no reasons are given; cleans reasons", () => {
    renderReview(receipt({ review_reasons: null }));
    expect(screen.getByRole("alert").textContent).toMatch(
      /could not be read reliably/i,
    );
    expect(
      receiptReviewReasons(
        receipt({ review_reasons: [" a ", "", 3 as unknown as string] }),
      ),
    ).toEqual(["a"]);
  });

  it("the upload queue labels a needs-review receipt", () => {
    render(
      <ReceiptQueue
        items={[
          {
            id: "q1",
            file: new File(["x"], "r.png"),
            fileName: "r.png",
            idempotencyKey: "k",
            status: "processed",
            previewUrl: "",
            receipt: receipt(),
          },
        ]}
        selectedId={null}
        onSelect={() => {}}
        onRetry={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText(/Needs review — enter the total/)).toBeTruthy();
  });
});
