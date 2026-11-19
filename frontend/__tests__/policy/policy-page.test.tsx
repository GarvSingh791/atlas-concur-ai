import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
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
import PolicyPage from "@/app/(app)/policy/page";
import type { Policy, PolicyRule } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(cleanup);

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
    summary: ["Applies to every expense line."],
    rule_type: "receipt_required",
    applies_to: "all",
    definition: { threshold_usd: 25 },
    source_clause_ref: "RECEIPT-REQ",
    status: "active",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

const RULES: PolicyRule[] = [
  rule({ id: "r1" }),
  rule({
    id: "r2",
    title: "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day",
    rule_type: "per_diem",
    applies_to: "meals",
    definition: {
      per: "day",
      rates: [
        { tier: "tier3", label: "Tier 3", default: true, amount: 65 },
        {
          tier: "tier1",
          label: "Tier 1",
          cities: ["New York", "San Francisco", "London", "Paris"],
          amount: 85,
        },
      ],
    },
    summary: ["Applies to meal expense lines."],
  }),
  rule({
    id: "car",
    title: "Car rental cap — Standard $75/day",
    rule_type: "amount_cap",
    applies_to: "car",
    status: "draft",
    definition: { per: "day", tiers: [{ tier: "standard", cap: 75 }] },
    summary: ["Applies only to car-rental expense lines."],
  }),
  rule({
    id: "ground",
    title: "Ground transport cap — Standard $75/item",
    rule_type: "amount_cap",
    applies_to: "all",
    status: "draft",
    definition: {
      currency: "USD",
      per: "item",
      expense_categories: ["ground_transport"],
      tiers: [{ tier: "standard", cap: 75 }],
      exception_approver: "manager",
    },
    summary: ["Applies to every ground-transport expense line."],
  }),
];

beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockImplementation((path: string) =>
    Promise.resolve(path === "/policies" ? POLICY : RULES),
  );
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PolicyPage />
    </QueryClientProvider>,
  );
}

describe("Policy navigator — titles, summary and lifecycle controls", () => {
  it("lists rule titles (not rule_type slugs) under readable group headings", async () => {
    renderPage();
    expect(
      await screen.findByText("Receipts required — $25 and over"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("receipt_required")).toBeNull();
    expect(screen.queryByText("per_diem")).toBeNull();
    expect(screen.getByText("All expenses")).toBeTruthy();
    expect(screen.getByText("Car rentals")).toBeTruthy();
  });

  it("the CAR draft's detail shows its summary and offers Activate + Archive", async () => {
    renderPage();
    fireEvent.click(
      await screen.findByText("Car rental cap — Standard $75/day"),
    );
    expect(screen.getByTestId("rule-detail-title").textContent).toBe(
      "Car rental cap — Standard $75/day",
    );
    expect(screen.getByTestId("rule-detail-summary").textContent).toContain(
      "car-rental expense lines",
    );
    expect(screen.getByRole("button", { name: /Activate…/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Archive draft…/ })).toBeTruthy();
  });

  it("an active rule offers Archive only; tiers render readably", async () => {
    renderPage();
    fireEvent.click(
      await screen.findByText(
        "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day",
      ),
    );
    expect(screen.queryByRole("button", { name: /Activate…/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Archive…$/ })).toBeTruthy();
    const def = screen.getByTestId("rule-definition");
    expect(
      within(def).getByText(
        "Tier 1 (New York, San Francisco, London, …): $85/day",
      ),
    ).toBeTruthy();
    expect(
      within(def).getByText("Tier 3 (all other locations): $65/day"),
    ).toBeTruthy();
  });

  it("a category-scoped cap is filed under its category, not All expenses", async () => {
    renderPage();
    await screen.findByText("Receipts required — $25 and over");
    const groupOf = (title: string) =>
      screen.getByText(title).closest("nav > div")?.querySelector("h2")
        ?.textContent;
    expect(groupOf("Ground transport cap — Standard $75/item")).toBe(
      "Ground transport",
    );
    expect(groupOf("Receipts required — $25 and over")).toBe("All expenses");
    expect(groupOf("Car rental cap — Standard $75/day")).toBe("Car rentals");
  });

  it("a single standard tier renders without a tier name", async () => {
    renderPage();
    fireEvent.click(
      await screen.findByText("Car rental cap — Standard $75/day"),
    );
    const def = screen.getByTestId("rule-definition");
    expect(within(def).getByText("$75 per day")).toBeTruthy();
    expect(def.textContent).not.toContain("Standard:");
  });

  it("search matches titles", async () => {
    renderPage();
    await screen.findByText("Receipts required — $25 and over");
    fireEvent.change(screen.getByLabelText("Search rules"), {
      target: { value: "per diem" },
    });
    expect(screen.queryByText("Receipts required — $25 and over")).toBeNull();
    expect(
      screen.getByText(
        "Meals per diem — Tier 1 $85, Tier 2 $75, Tier 3 $65/day",
      ),
    ).toBeTruthy();
  });
});
