import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ role: "finance_admin" }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import {
  RuleHistory,
  fieldLabel,
  formatValue,
} from "@/components/policy/rule-history";
import { normalizeRuleHistory } from "@/lib/query/policy-hooks";
import PolicyPage from "@/app/(app)/policy/page";
import type {
  Policy,
  PolicyRule,
  RuleHistoryResponse,
} from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

const HISTORY: RuleHistoryResponse = {
  rule_id: "r1",
  title: "Receipts required — $25 and over",
  created_at: "2026-01-01T00:00:00Z",
  entries: [
    {
      id: "a1",
      action: "policy_rule.drafted",
      label: "Drafted (AI rule authoring)",
      at: "2026-02-01T09:00:00Z",
      actor_name: "Fiona Finance",
      changes: [{ field: "status", before: null, after: "draft" }],
      note: null,
    },
    {
      id: "a2",
      action: "policy_rule.definition_updated",
      label: "Definition updated",
      at: "2026-03-01T09:00:00Z",
      actor_name: null,
      changes: [{ field: "definition.threshold_usd", before: 25, after: 30 }],
      note: "migration 0017",
    },
  ],
};

function renderWithClient(node: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

describe("RuleHistory", () => {
  it("fetches the rule's history and lists newest first with before → after", async () => {
    getMock.mockResolvedValue(HISTORY);
    renderWithClient(<RuleHistory ruleId="r1" />);

    const entries = await screen.findAllByTestId("rule-history-entry");
    expect(getMock).toHaveBeenCalledWith("/policy/rules/r1/history");
    expect(entries).toHaveLength(2);
    // Newest first.
    expect(within(entries[0]).getByText("Definition updated")).toBeTruthy();
    expect(
      within(entries[0]).getByText(/by System · migration 0017/),
    ).toBeTruthy();
    const change = within(entries[0]).getByTestId("rule-history-change");
    expect(change.textContent).toContain("Threshold usd:");
    expect(change.textContent).toContain("25");
    expect(change.textContent).toContain("30");
    expect(within(entries[1]).getByText(/by Fiona Finance/)).toBeTruthy();
  });

  it("shows an empty state when nothing was recorded", async () => {
    getMock.mockResolvedValue({ ...HISTORY, entries: [] });
    renderWithClient(<RuleHistory ruleId="r1" />);
    expect(await screen.findByTestId("rule-history-empty")).toBeTruthy();
  });

  it("offers a retry when the history cannot be loaded", async () => {
    getMock.mockRejectedValueOnce(new Error("boom"));
    getMock.mockResolvedValueOnce(HISTORY);
    renderWithClient(<RuleHistory ruleId="r1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findAllByTestId("rule-history-entry")).toHaveLength(2);
  });
});

describe("history helpers", () => {
  it("normalizes unexpected payloads to an empty list", () => {
    expect(normalizeRuleHistory(null)).toEqual([]);
    expect(normalizeRuleHistory([{ id: "x" }])).toEqual([]);
    expect(
      normalizeRuleHistory({ entries: [{ id: "x" }, HISTORY.entries[0]] }),
    ).toEqual([HISTORY.entries[0]]);
  });

  it("labels fields and formats values", () => {
    expect(fieldLabel("status")).toBe("Status");
    expect(fieldLabel("definition.max_tip_pct")).toBe("Max tip pct");
    expect(formatValue(null)).toBe("—");
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
  });
});

describe("Policy page — history replaces 'coming soon'", () => {
  const POLICY: Policy = {
    id: "policy-1",
    name: "TEP-FY26",
    version: 2,
    document_text: "…",
    effective_from: "2026-01-01",
    is_active: true,
  };
  const RULE: PolicyRule = {
    id: "r1",
    policy_id: "policy-1",
    title: "Receipts required — $25 and over",
    summary: ["Applies to every expense line."],
    rule_type: "receipt_required",
    applies_to: "all",
    definition: { threshold_usd: 25 },
    source_clause_ref: "RECEIPT-REQ",
    status: "active",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };

  it("renders the selected rule's history in the detail panel", async () => {
    getMock.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/policies"
          ? POLICY
          : path.endsWith("/history")
            ? HISTORY
            : [RULE],
      ),
    );
    renderWithClient(<PolicyPage />);
    expect(screen.queryByText(/coming soon/i)).toBeNull();
    fireEvent.click(
      await screen.findByRole("button", {
        name: /Receipts required — \$25 and over/,
      }),
    );
    expect(await screen.findAllByTestId("rule-history-entry")).toHaveLength(2);
    expect(getMock).toHaveBeenCalledWith("/policy/rules/r1/history");
  });
});
