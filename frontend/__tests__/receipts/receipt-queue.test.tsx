/**
 * UAT round 2 (Issue 3 follow-up): a needs-review receipt showed a green
 * success check next to "Needs review". The icon now uses the SAME predicate as
 * the status label (`receipt.needs_review === true`), so they cannot disagree.
 */
import { describe, it, expect, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

import { ReceiptQueue } from "@/components/receipts/receipt-queue";
import type { QueueItem } from "@/lib/receipts/use-upload-queue";
import type { Receipt } from "@/lib/types";

afterEach(cleanup);

function receipt(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rc-1",
    organization_id: "org",
    uploaded_by: "u",
    expense_id: null,
    image_url: "org/abc.png",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: { amount: 12, currency: "USD", vendor: "Cafe" },
    field_confidence: { amount: 0.95, currency: 0.95, vendor: 0.95 },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      vendor: "deterministic",
    },
    created_at: "2026-06-11T10:00:00Z",
    ...over,
  };
}

function item(
  r: Receipt | undefined,
  over: Partial<QueueItem> = {},
): QueueItem {
  return {
    id: "q1",
    file: new File(["x"], "r.png"),
    fileName: "r.png",
    idempotencyKey: "k",
    status: "processed",
    previewUrl: "",
    receipt: r,
    ...over,
  };
}

function renderQueue(items: QueueItem[]) {
  return render(
    <ReceiptQueue
      items={items}
      selectedId={null}
      onSelect={() => {}}
      onRetry={() => {}}
      onRemove={() => {}}
    />,
  );
}

describe("ReceiptQueue status icon", () => {
  it("needs-review item shows a warning icon, not a success check", () => {
    const { container } = renderQueue([
      item(
        receipt({
          needs_review: true,
          review_reasons: ["Vendor is unclear."],
        }),
      ),
    ]);
    expect(screen.getByText("Needs review")).toBeTruthy();
    expect(
      container.querySelector('[data-status-icon="needs-review"]'),
    ).not.toBeNull();
    expect(container.querySelector('[data-status-icon="ready"]')).toBeNull();
  });

  it("a clean processed item keeps the success check", () => {
    const { container } = renderQueue([item(receipt({ needs_review: false }))]);
    expect(screen.getByText("Ready to review")).toBeTruthy();
    expect(
      container.querySelector('[data-status-icon="ready"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-status-icon="needs-review"]'),
    ).toBeNull();
  });

  it("uploading and failed items keep their icons", () => {
    const { container } = renderQueue([
      item(undefined, { id: "a", status: "uploading" }),
      item(undefined, { id: "b", status: "failed", error: "Boom" }),
    ]);
    expect(
      container.querySelector('[data-status-icon="uploading"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-status-icon="failed"]'),
    ).not.toBeNull();
  });
});

describe("ReceiptsScreen empty state", () => {
  it("does not claim the user has no receipts — only no uploads this session", async () => {
    const { ReceiptsScreen } =
      await import("@/components/receipts/receipts-screen");
    const { QueryClient, QueryClientProvider } =
      await import("@tanstack/react-query");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ReceiptsScreen />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("No receipts yet")).toBeNull();
    expect(screen.getByText("No uploads in this session")).toBeTruthy();
    expect(screen.getByText(/Receipt Inbox/)).toBeTruthy();
  });
});
