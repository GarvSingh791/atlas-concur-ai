/**
 * QA round, defect 6 — the policy rule list shows each rule's clause code
 * (source_clause_ref) next to its title, so look-alike rules are told apart;
 * a title that already ends with its clause code ("… (CARD-03)") is not
 * repeated.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";
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
import PolicyPage from "@/app/(app)/policy/page";
import type { Policy, PolicyRule } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

const POLICY: Policy = {
  id: "policy-1",
  name: "TEP-FY26",
  version: 2,
  document_text: "…",
  effective_from: "2026-01-01",
  is_active: true,
};

function rule(over: Partial<PolicyRule>): PolicyRule {
  return {
    id: "r",
    policy_id: "policy-1",
    title: "Receipts required — $25 and over",
    summary: [],
    rule_type: "receipt_required",
    applies_to: "all",
    definition: {},
    source_clause_ref: "RECEIPT-REQ",
    status: "active",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

const RULES: PolicyRule[] = [
  rule({ id: "receipt" }),
  rule({
    id: "card",
    title: "Unreconciled card transactions — 30 days (CARD-03)",
    rule_type: "category_allow",
    source_clause_ref: "CARD-03",
  }),
  rule({ id: "noclause", title: "Hotel cap", source_clause_ref: null }),
];

beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockImplementation((path: string) =>
    Promise.resolve(path === "/policies" ? POLICY : RULES),
  );
});
afterEach(cleanup);

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PolicyPage />
    </QueryClientProvider>,
  );
}

describe("policy rule list clause codes", () => {
  it("shows the clause code beside a title that does not name it", async () => {
    renderPage();
    await screen.findByText("Receipts required — $25 and over");
    expect(screen.getByTestId("rule-clause-receipt").textContent).toBe(
      "RECEIPT-REQ",
    );
  });

  it("does not repeat a clause code the title already carries", async () => {
    renderPage();
    await screen.findByText(
      "Unreconciled card transactions — 30 days (CARD-03)",
    );
    expect(screen.queryByTestId("rule-clause-card")).toBeNull();
    expect(screen.queryByTestId("rule-clause-noclause")).toBeNull();
  });
});
