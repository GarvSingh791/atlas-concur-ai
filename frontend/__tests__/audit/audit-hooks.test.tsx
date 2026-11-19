import { describe, it, expect, beforeEach, vi } from "vitest";
import * as React from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn() },
}));

import { api } from "@/lib/api/client";
import {
  auditKeys,
  cleanAuditParams,
  useAuditLog,
} from "@/lib/query/audit-hooks";

const get = vi.mocked(api.get);

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const PAGE = { items: [], total: 0, page: 1, size: 20, pages: 0 };

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue(PAGE as never);
});

describe("cleanAuditParams", () => {
  it("drops undefined and blank filters and trims values", () => {
    expect(
      cleanAuditParams({
        page: 2,
        size: 20,
        sort: "created_at:desc",
        action: "  trip.created ",
        entity_type: "",
        actor_id: undefined,
      }),
    ).toEqual({
      page: 2,
      size: 20,
      sort: "created_at:desc",
      action: "trip.created",
    });
  });

  it("returns an empty object for no params", () => {
    expect(cleanAuditParams()).toEqual({});
  });

  it("keys equal for equivalent params (blank filter ignored)", () => {
    expect(auditKeys.list({ page: 1, action: "" })).toEqual(
      auditKeys.list({ page: 1 }),
    );
  });
});

describe("useAuditLog", () => {
  it("calls GET /audit with page, size, sort and the non-empty filters", async () => {
    const { result } = renderHook(
      () =>
        useAuditLog({
          page: 3,
          size: 20,
          sort: "created_at:asc",
          action: "trip.created",
          entity_type: "",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("/audit", {
      params: {
        page: 3,
        size: 20,
        sort: "created_at:asc",
        action: "trip.created",
      },
    });
    expect(result.current.data).toEqual(PAGE);
  });

  it("surfaces a failed request as an error state", async () => {
    get.mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useAuditLog({ page: 1 }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
