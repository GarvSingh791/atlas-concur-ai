import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { showToast } = vi.hoisted(() => ({ showToast: vi.fn() }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import {
  RuleLifecycleActions,
  type LifecycleRule,
} from "@/components/policy/rule-lifecycle-actions";
import { ApiError } from "@/lib/types/api";
import type { PolicyRule } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;
const patchMock = api.patch as unknown as ReturnType<typeof vi.fn>;

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockResolvedValue([]);
});

function wrapper() {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  // eslint-disable-next-line react/display-name
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const DRAFT: LifecycleRule = {
  id: "car-draft",
  title: "Car rental cap — Standard $75/day",
  summary: [
    "Applies only to car-rental expense lines linked to a trip car segment that records a vehicle class. Atlas's trip planner and expense forms never record a vehicle class, so in practice no expense line is checked against it (car-rental receipts are filed as ground transport); taxi, rideshare, transit and parking expenses are not covered.",
    "On expense reports, a line over the cap is flagged for the approver as advisory only and never blocks submission.",
  ],
  status: "draft",
  rule_type: "amount_cap",
  applies_to: "car",
};

function activeRule(over: Partial<PolicyRule>): PolicyRule {
  return {
    id: "x",
    policy_id: "p",
    title: "Car rental cap — Standard $150/item",
    summary: [],
    rule_type: "amount_cap",
    applies_to: "car",
    definition: {},
    source_clause_ref: null,
    status: "active",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("RuleLifecycleActions", () => {
  it("draft shows Activate and Archive; active shows Archive only; archived shows none", () => {
    const { rerender } = render(<RuleLifecycleActions rule={DRAFT} />, {
      wrapper: wrapper(),
    });
    expect(screen.getByRole("button", { name: /Activate…/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Archive draft…/ })).toBeTruthy();

    rerender(<RuleLifecycleActions rule={{ ...DRAFT, status: "active" }} />);
    expect(screen.queryByRole("button", { name: /Activate/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Archive…$/ })).toBeTruthy();

    rerender(<RuleLifecycleActions rule={{ ...DRAFT, status: "archived" }} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/can’t be reactivated/)).toBeTruthy();
  });

  it("an unknown intent (navigator / older draft) shows no 'You asked' line", async () => {
    render(<RuleLifecycleActions rule={DRAFT} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByTestId("lifecycle-rule-recap").textContent,
    ).not.toContain("You asked");
  });

  it("a known intent is restated in the activation dialog", async () => {
    render(
      <RuleLifecycleActions rule={DRAFT} intent="Cap car rentals at $75" />,
      {
        wrapper: wrapper(),
      },
    );
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByTestId("lifecycle-rule-intent").textContent,
    ).toBe("You asked: Cap car rentals at $75");
  });

  it("the activation dialog restates the summary and lists conflicting active rules", async () => {
    getMock.mockResolvedValue([
      activeRule({ id: "a1", title: "Car rental cap — Standard $150/item" }),
      activeRule({ id: "a2", title: "Hotel cap", applies_to: "hotel" }),
      activeRule({ id: "a3", title: "Old archived", status: "archived" }),
    ]);
    render(<RuleLifecycleActions rule={DRAFT} />, { wrapper: wrapper() });
    await waitFor(() => expect(getMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(DRAFT.title)).toBeTruthy();
    expect(within(dialog).getByText(DRAFT.summary[1])).toBeTruthy();
    const conflicts = await within(dialog).findByTestId("lifecycle-conflicts");
    expect(conflicts.textContent).toContain(
      "Car rental cap — Standard $150/item",
    );
    expect(conflicts.textContent).not.toContain("Hotel cap");
    expect(conflicts.textContent).not.toContain("Old archived");
  });

  it("archiving an active rule warns it is irreversible and PATCHes only on confirm", async () => {
    patchMock.mockResolvedValue({ ...DRAFT, status: "archived" });
    const onChanged = vi.fn();
    render(
      <RuleLifecycleActions
        rule={{ ...DRAFT, status: "active" }}
        onChanged={onChanged}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.click(screen.getByRole("button", { name: /^Archive…$/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Archive this active rule?")).toBeTruthy();
    expect(dialog.textContent).toContain("can’t be reactivated");
    expect(patchMock).not.toHaveBeenCalled();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Archive rule" }),
    );
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/policy-rules/car-draft", {
        body: { status: "archived" },
      }),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith("archived"));
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success", title: "Rule archived" }),
    );
  });

  it("a refused activation shows the server's reason in an error toast", async () => {
    patchMock.mockRejectedValue(
      new ApiError(
        {
          code: "RULE_INVALID",
          message:
            "This rule can't be activated: tiers must be a non-empty list.",
          correlation_id: "c-1",
        },
        422,
      ),
    );
    const onChanged = vi.fn();
    render(<RuleLifecycleActions rule={DRAFT} onChanged={onChanged} />, {
      wrapper: wrapper(),
    });
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Activate rule" }),
    );
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "error",
          title: "Couldn’t update the rule",
          description:
            "This rule can't be activated: tiers must be a non-empty list.",
        }),
      ),
    );
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("never uses window.confirm", async () => {
    const spy = vi.spyOn(window, "confirm");
    render(<RuleLifecycleActions rule={DRAFT} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Archive draft…/ }));
    await screen.findByRole("dialog");
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});
