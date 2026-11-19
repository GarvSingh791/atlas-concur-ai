import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { useUploadQueue } from "@/lib/receipts/use-upload-queue";
import type { Receipt } from "@/lib/types";

const postMock = api.post as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  // jsdom doesn't implement object URLs.
  globalThis.URL.createObjectURL = vi.fn(() => "blob:mock");
  globalThis.URL.revokeObjectURL = vi.fn();
});

afterEach(() => vi.clearAllMocks());

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  // eslint-disable-next-line react/display-name
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function pngFile(name = "r.png"): File {
  return new File(["x"], name, { type: "image/png" });
}

function processedReceipt(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rcpt-1",
    organization_id: "org-1",
    uploaded_by: "user-1",
    expense_id: null,
    image_url: "org-1/abc",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: { amount: 10, currency: "USD" },
    field_confidence: { amount: 0.95 },
    field_provenance: { amount: "deterministic" },
    created_at: "2026-06-11T10:00:00Z",
    ...over,
  };
}

describe("useUploadQueue", () => {
  it("uploads synchronously and makes a processed receipt reviewable + auto-selected", async () => {
    postMock.mockResolvedValue(processedReceipt());
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });

    act(() => result.current.enqueue([pngFile()]));

    await waitFor(() =>
      expect(result.current.items[0]?.status).toBe("processed"),
    );
    expect(result.current.items[0].receipt?.ocr_status).toBe("processed");
    expect(result.current.selectedId).toBe(result.current.items[0].id);
  });

  it("treats an ocr_status='failed' response as a failed item (no corrupt review)", async () => {
    postMock.mockResolvedValue(processedReceipt({ ocr_status: "failed" }));
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });

    act(() => result.current.enqueue([pngFile()]));

    await waitFor(() => expect(result.current.items[0]?.status).toBe("failed"));
    expect(result.current.selectedId).toBeNull();
  });

  it("surfaces an upload error and retries with the SAME idempotency key (no duplicate)", async () => {
    postMock.mockRejectedValueOnce(new Error("network"));
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });

    act(() => result.current.enqueue([pngFile()]));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("failed"));

    const key = result.current.items[0].idempotencyKey;

    postMock.mockResolvedValueOnce(processedReceipt());
    act(() => result.current.retry(result.current.items[0].id));
    await waitFor(() =>
      expect(result.current.items[0]?.status).toBe("processed"),
    );

    const lastCall = postMock.mock.calls.at(-1) as [
      string,
      { headers: Record<string, string> },
    ];
    expect(lastCall[0]).toBe("/receipts");
    expect(lastCall[1].headers["Idempotency-Key"]).toBe(key);
  });

  it("validates nothing itself — invalid files are filtered upstream by the zone", () => {
    // Sanity: enqueue accepts the files it's given (validation lives in UploadZone).
    postMock.mockResolvedValue(processedReceipt());
    const { result } = renderHook(() => useUploadQueue(), {
      wrapper: makeWrapper(),
    });
    act(() => result.current.enqueue([pngFile("a.png"), pngFile("b.png")]));
    expect(result.current.items).toHaveLength(2);
  });
});
