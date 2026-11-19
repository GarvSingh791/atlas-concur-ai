/**
 * QA round, defect 2 (other filter bars of this round) — the card feed's
 * From/To filter already refuses an inverted range client-side; a 400 the server
 * still returns now shows the server's validation message, not "Please try
 * again". A non-validation error keeps the generic copy.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/receipts" }));
import { api } from "@/lib/api/client";
import { CardFeed } from "@/components/receipts/card-feed";
import { ApiError, validationErrorMessage } from "@/lib/types/api";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const err = (status: number, message: string) =>
  new ApiError({ code: "X", message, correlation_id: "c" }, status);

describe("validationErrorMessage", () => {
  it("returns the server message for 400 / 422 only", () => {
    expect(validationErrorMessage(err(400, "Bad date."))).toBe("Bad date.");
    expect(validationErrorMessage(err(422, "Invalid."))).toBe("Invalid.");
    expect(validationErrorMessage(err(500, "boom"))).toBeNull();
    expect(validationErrorMessage(err(403, "nope"))).toBeNull();
    expect(validationErrorMessage(new Error("x"))).toBeNull();
    expect(validationErrorMessage(err(400, "  "))).toBeNull();
  });
});

describe("card feed errors", () => {
  it("shows the server's validation message for a 400", async () => {
    getMock.mockRejectedValue(
      err(400, "'date_from' must not be after 'date_to'."),
    );
    wrap(<CardFeed onUploadReceipt={() => {}} />);
    expect(
      await screen.findByText("'date_from' must not be after 'date_to'."),
    ).toBeTruthy();
  });

  it("keeps the generic copy for other failures", async () => {
    getMock.mockRejectedValue(err(500, "internal detail"));
    wrap(<CardFeed onUploadReceipt={() => {}} />);
    expect(await screen.findByText("Please try again.")).toBeTruthy();
    expect(screen.queryByText("internal detail")).toBeNull();
  });
});
