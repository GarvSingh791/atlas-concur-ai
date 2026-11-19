/**
 * Follow-up (UI polish) — the Policy page's "Policy document" pane shows the
 * CURRENT Helios policy (GET /policies `document`: sections + clause codes),
 * not the legacy stub's 8 sections; with no `document` it falls back to the
 * stored text.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
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
import { clauseBody } from "@/components/policy/policy-document";
import type { Policy } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

const STUB_TEXT = "[TRAVEL-CLASS] legacy stub section";
const BASE: Policy = {
  id: "policy-1",
  name: "Atlas Global Travel & Expense Policy",
  version: 1,
  document_text: STUB_TEXT,
  effective_from: "2026-01-01",
  is_active: true,
};
const DOCUMENT = {
  title: "Helios Global Travel & Expense Policy",
  heading: "GLOBAL TRAVEL & EXPENSE (T&E) POLICY — Helios Dynamics",
  sections: [
    {
      title: "Section 1 — General",
      clauses: [
        { clause_ref: "GEN-01", text: "GEN-01 Purpose: governs travel." },
      ],
    },
    {
      title: "Section 5 — Air Travel",
      clauses: [
        { clause_ref: "AIR-01", text: "AIR-01 Class: economy by default." },
        { clause_ref: "AIR-04", text: "AIR-04 Business class: SVP and above." },
      ],
    },
  ],
};

let policy: Policy = BASE;
beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockImplementation((path: string) =>
    Promise.resolve(path === "/policies" ? policy : []),
  );
});
afterEach(cleanup);

function renderPage() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <PolicyPage />
    </QueryClientProvider>,
  );
}

describe("Policy document pane", () => {
  it("renders the current corpus sections with clause codes", async () => {
    policy = { ...BASE, document: DOCUMENT };
    renderPage();
    const doc = await screen.findByTestId("policy-document");
    expect(within(doc).getByText(DOCUMENT.title)).toBeTruthy();
    const air = within(doc).getByRole("region", {
      name: "Section 5 — Air Travel",
    });
    expect(within(air).getByText("AIR-04")).toBeTruthy();
    expect(
      within(air).getByText("Business class: SVP and above."),
    ).toBeTruthy();
    expect(within(doc).getAllByRole("region")).toHaveLength(2);
    expect(screen.queryByText(STUB_TEXT)).toBeNull();
  });

  it("falls back to the stored document text without a corpus document", async () => {
    policy = { ...BASE, document: null };
    renderPage();
    expect(await screen.findByText(STUB_TEXT)).toBeTruthy();
    expect(screen.queryByTestId("policy-document")).toBeNull();
  });

  it("clauseBody drops only a leading clause code", () => {
    expect(clauseBody("AIR-04", "AIR-04 Cabin: x")).toBe("Cabin: x");
    expect(clauseBody("AIR-04", "See AIR-04 too")).toBe("See AIR-04 too");
  });
});
