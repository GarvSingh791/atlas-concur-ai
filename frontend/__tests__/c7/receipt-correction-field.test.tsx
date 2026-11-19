/**
 * C7-16 follow-up: ReceiptCorrection.field is narrowed to the ReceiptFieldKey
 * union, mirroring the backend CorrectableReceiptField literal. The
 * `@ts-expect-error` lines are enforced by `tsc --noEmit` (they fail the type
 * check if `field` widens back to `string`); the runtime checks pin the
 * PATCH body shape.
 */
import { describe, it, expect, vi, expectTypeOf } from "vitest";
import * as React from "react";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { patch } = vi.hoisted(() => ({ patch: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch },
}));

import {
  useCorrectReceiptField,
  type CorrectReceiptArgs,
} from "@/lib/query/receipt-hooks";
import type { ReceiptCorrection, ReceiptFieldKey } from "@/lib/types";

describe("ReceiptCorrection.field", () => {
  it("is the ReceiptFieldKey union, not string", () => {
    expectTypeOf<ReceiptCorrection["field"]>().toEqualTypeOf<ReceiptFieldKey>();
    expectTypeOf<
      CorrectReceiptArgs["field"]
    >().toEqualTypeOf<ReceiptFieldKey>();

    const ok: ReceiptCorrection = { field: "expense_type", value: "meals" };
    // @ts-expect-error — only the six correctable keys are accepted.
    const bad: ReceiptCorrection = { field: "ocr_status", value: "done" };
    expect([ok.field, bad.field]).toEqual(["expense_type", "ocr_status"]);
  });

  it("the correction mutation PATCHes { field, value } for a valid key", async () => {
    patch.mockResolvedValue({ id: "rc-1" });
    const qc = new QueryClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useCorrectReceiptField(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        receiptId: "rc-1",
        field: "amount",
        value: "42.50",
      });
    });
    expect(patch).toHaveBeenCalledWith("/receipts/rc-1", {
      body: { field: "amount", value: "42.50" },
    });
  });
});
