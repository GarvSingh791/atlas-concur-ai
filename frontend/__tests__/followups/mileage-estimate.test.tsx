/**
 * Follow-up (UI polish) — the Add-mileage dialog shows a live, display-only
 * estimate (business miles × the current MIL-01 rate from GET /mileage-rate,
 * with the clause) before the claimant confirms. The POST still carries the
 * trip only — never an amount or a rate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const h = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api/client", () => ({ api: { get: h.get, post: h.post } }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import { AddMileageDialog } from "@/components/expenses/add-mileage-dialog";
import { estimateBillableMiles } from "@/lib/expenses/policy-fields";

beforeEach(() => {
  h.get.mockImplementation((path: string) =>
    path === "/mileage-rate"
      ? Promise.resolve({
          rate_per_mile: "0.725",
          currency: "USD",
          rate_clause: "MIL-01",
        })
      : Promise.reject(new Error(`unexpected GET ${path}`)),
  );
  h.post.mockResolvedValue({ id: "e1", amount: "0", mileage: null });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <AddMileageDialog
        report={{ id: "rep-1", version: 3 }}
        onOpenChange={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe("Add mileage live estimate", () => {
  it("shows the rate first, then distance × rate with the clause", async () => {
    renderDialog();
    const box = await screen.findByTestId("mileage-estimate");
    expect(box.textContent).toMatch(/\$0\.725\/mi \(MIL-01\)/);
    expect(h.get).toHaveBeenCalledWith("/mileage-rate");

    fireEvent.change(screen.getByTestId("mileage-distance"), {
      target: { value: "100" },
    });
    fireEvent.click(screen.getByLabelText("Round trip"));
    fireEvent.change(screen.getByTestId("mileage-commute"), {
      target: { value: "20" },
    });
    // 100 × 2 − 20 = 180 business mi × $0.725 = $130.50
    await waitFor(() =>
      expect(screen.getByTestId("mileage-estimate").textContent).toMatch(
        /\$130\.50.*180\.0 business mi × \$0\.725\/mi \(rate MIL-01\)/,
      ),
    );
  });

  it("the POST still sends no amount and no rate", async () => {
    renderDialog();
    await screen.findByTestId("mileage-estimate");
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2026-06-10" },
    });
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "HQ" },
    });
    fireEvent.change(screen.getByLabelText("To"), {
      target: { value: "Client" },
    });
    fireEvent.change(screen.getByTestId("mileage-distance"), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add mileage" }));
    await waitFor(() => expect(h.post).toHaveBeenCalledTimes(1));
    const [, opts] = h.post.mock.calls[0];
    expect(opts.body).not.toHaveProperty("amount");
    expect(opts.body).not.toHaveProperty("rate_per_mile");
  });

  it("no estimate box when the rate cannot be read", async () => {
    h.get.mockRejectedValue(new Error("down"));
    renderDialog();
    await waitFor(() => expect(h.get).toHaveBeenCalled());
    expect(screen.queryByTestId("mileage-estimate")).toBeNull();
  });

  it("billable miles mirror the server (km converted, offset deducted)", () => {
    const t = { unit: "mi" as const, round_trip: false, commute_offset: "0" };
    expect(estimateBillableMiles({ ...t, distance: "42.5" })).toBe("42.5");
    expect(
      estimateBillableMiles({ ...t, distance: "10", commute_offset: "10" }),
    ).toBeNull();
    expect(estimateBillableMiles({ ...t, unit: "km", distance: "16.1" })).toBe(
      "10.0",
    );
  });
});
