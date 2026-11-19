import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { usePolicy, usePolicyRules } from "@/lib/query/policy-hooks";
import type { Policy, PolicyRule } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(() => vi.clearAllMocks());

function wrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // eslint-disable-next-line react/display-name
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const POLICY: Policy = {
  id: "policy-1",
  name: "Atlas Global Travel & Expense Policy",
  version: 1,
  document_text: "Section AIR-CABIN…",
  effective_from: "2026-01-01",
  is_active: true,
};

const RULES: PolicyRule[] = [
  {
    id: "r1",
    policy_id: "policy-1",
    title: "Flight cap — Standard $1,500/item",
    summary: ["Applies to airfare expense lines."],
    rule_type: "amount_cap",
    applies_to: "flight",
    definition: { currency: "USD", threshold: 1500 },
    source_clause_ref: "AIR-CAP",
    status: "active",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "r2",
    policy_id: "policy-1",
    title: "Car rental cap — Standard $75/day",
    summary: ["Applies only to car-rental expense lines."],
    rule_type: "amount_cap",
    applies_to: "car",
    definition: { currency: "USD", per: "day" },
    source_clause_ref: "CAR-CAP-DRAFT",
    status: "draft",
    version: 1,
    created_at: "2026-01-02T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
  },
];

describe("policy read hooks — map the server payload 1:1", () => {
  it("usePolicy returns the active policy document", async () => {
    getMock.mockResolvedValue(POLICY);
    const { result } = renderHook(() => usePolicy(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/policies");
    expect(result.current.data?.version).toBe(1);
    expect(result.current.data?.document_text).toContain("AIR-CABIN");
  });

  it("usePolicyRules returns the structured rule list incl. a draft", async () => {
    getMock.mockResolvedValue(RULES);
    const { result } = renderHook(() => usePolicyRules(), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/policy-rules", {
      params: undefined,
    });
    expect(result.current.data).toHaveLength(2);
    expect(result.current.data?.map((r) => r.status)).toEqual([
      "active",
      "draft",
    ]);
  });
});
