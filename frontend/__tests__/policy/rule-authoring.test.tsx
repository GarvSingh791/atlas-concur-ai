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
import { RuleAuthoring } from "@/components/policy/rule-authoring";
import { ApiError } from "@/lib/types/api";
import type { AuthorRuleResponse } from "@/lib/types/policy";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;
const postMock = api.post as unknown as ReturnType<typeof vi.fn>;
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

const RETEST = "Rideshare trips over $75 need a written business justification";

const SERVER_DRAFT: AuthorRuleResponse = {
  id: "rule-9",
  policy_id: "policy-1",
  title: "Ground transport cap — Standard $75/item",
  status: "draft",
  rule_type: "amount_cap",
  applies_to: "all",
  definition: {
    currency: "USD",
    per: "item",
    expense_categories: ["ground_transport"],
    tiers: [{ tier: "standard", cap: 75 }],
    exception_approver: "manager",
  },
  summary: [
    "Applies to every ground-transport expense line — taxi, rideshare, car rentals, train tickets, transit and parking alike (Atlas files all of these as ground transport and does not tell them apart).",
    "Each line's full amount is compared with a $75 cap.",
    "On expense reports, a line over the cap blocks submission until the employee adds a written justification; the report then always goes to their manager as a policy exception (never auto-approved).",
  ],
  source_clause_ref: null,
  version: 1,
};

function generate(text = RETEST) {
  fireEvent.change(screen.getByLabelText(/Describe the rule/i), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole("button", { name: /Generate/i }));
}

function apiError(code: string, message: string, status = 422): ApiError {
  return new ApiError({ code, message, correlation_id: "corr-123" }, status);
}

describe("RuleAuthoring — server is the source of the draft", () => {
  it("submits NL text and renders the SERVER's draft: title, intent, summary, readable definition", async () => {
    postMock.mockResolvedValue(SERVER_DRAFT);
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock).toHaveBeenCalledWith("/policies/policy-1/rules", {
      body: { title: RETEST },
    });

    expect(await screen.findByText("AI-Generated Draft")).toBeTruthy();
    expect(screen.getByTestId("draft-rule-title").textContent).toBe(
      "Ground transport cap — Standard $75/item",
    );
    expect(screen.getByTestId("draft-intent").textContent).toContain(RETEST);
    const summary = screen.getByTestId("draft-rule-summary");
    expect(within(summary).getAllByRole("listitem")).toHaveLength(3);
    expect(summary.textContent).toContain(
      "taxi, rideshare, car rentals, train tickets, transit and parking",
    );
    expect(screen.getByTestId("draft-rule-clause").textContent).toBe(
      "No matching clause cited",
    );
    // Readable definition rows; the raw JSON sits behind a disclosure.
    const def = screen.getByTestId("rule-definition");
    // A single standard tier carries no meaningful name (UAT2-G low item).
    expect(def.textContent).toContain("$75 per item");
    expect(def.textContent).not.toContain("Standard");
    expect(def.textContent).toContain("Ground transport");
    expect(screen.getByText("Raw definition")).toBeTruthy();
    expect(screen.getByText("Draft")).toBeTruthy();
    expect(screen.queryByText("amount_cap")).toBeNull();
  });

  it("activation sends no PATCH until the dialog is confirmed with the box ticked", async () => {
    postMock.mockResolvedValue(SERVER_DRAFT);
    patchMock.mockResolvedValue({ ...SERVER_DRAFT, status: "active" });
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");

    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    // The dialog restates what the rule does and what was asked.
    expect(within(dialog).getByText("Activate this rule?")).toBeTruthy();
    expect(
      within(dialog).getByTestId("lifecycle-rule-recap").textContent,
    ).toContain(
      "blocks submission until the employee adds a written justification",
    );
    expect(within(dialog).getByText(RETEST)).toBeTruthy();

    // The disabled button sits inside its "why disabled" tooltip trigger: take
    // the innermost (the real confirm button).
    const confirmButton = () =>
      within(dialog)
        .getAllByRole("button", { name: "Activate rule" })
        .at(-1) as HTMLButtonElement;
    expect(confirmButton().disabled).toBe(true);
    fireEvent.click(confirmButton());
    expect(patchMock).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("checkbox"));
    expect(confirmButton().disabled).toBe(false);
    fireEvent.click(confirmButton());
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    expect(patchMock).toHaveBeenCalledWith("/policy-rules/rule-9", {
      body: { status: "active" },
    });
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "success",
          title: "Rule activated",
        }),
      ),
    );
  });

  it("Archive draft goes through a confirmation dialog, then PATCHes archived", async () => {
    postMock.mockResolvedValue(SERVER_DRAFT);
    patchMock.mockResolvedValue({ ...SERVER_DRAFT, status: "archived" });
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");

    fireEvent.click(screen.getByRole("button", { name: /Archive draft…/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Discard this draft?")).toBeTruthy();
    expect(patchMock).not.toHaveBeenCalled();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Archive draft" }),
    );
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    expect(patchMock).toHaveBeenCalledWith("/policy-rules/rule-9", {
      body: { status: "archived" },
    });
  });

  it("cancelling the activation dialog sends nothing", async () => {
    postMock.mockResolvedValue(SERVER_DRAFT);
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(patchMock).not.toHaveBeenCalled();
  });
});

describe("RuleAuthoring — the draft carries the intent that produced it (UAT2-G)", () => {
  const WEEKEND = "No expenses may be claimed on weekends";

  it("generate draft A, type intent B: the card and the Activate dialog still show A", async () => {
    postMock.mockResolvedValueOnce({ ...SERVER_DRAFT, intent: RETEST });
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");

    // Typing a different intent (not yet generated) must not relabel draft A.
    fireEvent.change(screen.getByLabelText(/Describe the rule/i), {
      target: { value: WEEKEND },
    });
    expect(screen.getByTestId("draft-intent").textContent).toBe(
      `You asked: ${RETEST}`,
    );
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    const recap = within(dialog).getByTestId("lifecycle-rule-recap");
    expect(recap.textContent).toContain(RETEST);
    expect(recap.textContent).not.toContain(WEEKEND);
  });

  it("a refused intent B leaves draft A labelled with A, in the card and the dialog", async () => {
    postMock
      .mockResolvedValueOnce({ ...SERVER_DRAFT, intent: RETEST })
      .mockRejectedValueOnce(
        apiError("RULE_NOT_EXPRESSIBLE", "Atlas can't express this rule yet."),
      );
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");
    generate(WEEKEND);
    await screen.findByTestId("rule-authoring-error");

    expect(screen.getByText("AI-Generated Draft")).toBeTruthy();
    expect(screen.getByTestId("draft-intent").textContent).toBe(
      `You asked: ${RETEST}`,
    );
    fireEvent.click(screen.getByRole("button", { name: /Activate…/ }));
    const dialog = await screen.findByRole("dialog");
    const recap = within(dialog).getByTestId("lifecycle-rule-recap");
    expect(recap.textContent).toContain(RETEST);
    expect(recap.textContent).not.toContain(WEEKEND);
  });

  it("prefers the server-echoed intent over the textarea text", async () => {
    postMock.mockResolvedValueOnce({
      ...SERVER_DRAFT,
      intent: "Server-recorded intent",
    });
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");
    expect(screen.getByTestId("draft-intent").textContent).toBe(
      "You asked: Server-recorded intent",
    );
  });

  it("falls back to the submitted text when the server does not echo an intent", async () => {
    postMock.mockResolvedValueOnce(SERVER_DRAFT); // no `intent` key (older server)
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");
    fireEvent.change(screen.getByLabelText(/Describe the rule/i), {
      target: { value: WEEKEND },
    });
    expect(screen.getByTestId("draft-intent").textContent).toBe(
      `You asked: ${RETEST}`,
    );
  });

  it("a newly generated draft B replaces A together with B's intent", async () => {
    postMock
      .mockResolvedValueOnce({ ...SERVER_DRAFT, intent: RETEST })
      .mockResolvedValueOnce({
        ...SERVER_DRAFT,
        id: "rule-10",
        intent: "Taxi fares over $60 must be justified",
      });
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByText("AI-Generated Draft");
    generate("Taxi fares over $60 must be justified");
    await waitFor(() =>
      expect(screen.getByTestId("draft-intent").textContent).toBe(
        "You asked: Taxi fares over $60 must be justified",
      ),
    );
  });
});

describe("RuleAuthoring — a refused draft says why", () => {
  it.each([
    [
      "RULE_NOT_EXPRESSIBLE",
      "Atlas can’t express this rule yet",
      "Atlas can't express this rule yet: Atlas has no rail target.",
    ],
    [
      "RULE_CRITIC_REJECTED",
      "The AI draft didn’t match your description",
      "The AI's draft didn't match your description, so it was discarded: The rule only flags for review.",
    ],
    [
      "RULE_INVALID",
      "The AI produced an invalid rule",
      "The AI couldn't produce a valid rule definition: tiers must be a non-empty list.",
    ],
  ])(
    "%s shows a code-specific heading and the server reason",
    async (code, heading, message) => {
      postMock.mockRejectedValue(apiError(code, message));
      render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
      generate();

      const panel = await screen.findByTestId("rule-authoring-error");
      expect(panel.getAttribute("role")).toBe("alert");
      expect(within(panel).getByText(heading)).toBeTruthy();
      expect(
        screen.getByTestId("rule-authoring-error-message").textContent,
      ).toBe(message);
      expect(panel.textContent).toContain("Reference: corr-123");
      // The intent is kept so the user can refine it.
      expect(
        (screen.getByLabelText(/Describe the rule/i) as HTMLTextAreaElement)
          .value,
      ).toBe(RETEST);
      expect(panel.textContent?.includes("Supported today")).toBe(
        code === "RULE_NOT_EXPRESSIBLE",
      );
    },
  );

  it("PROVIDER_UNAVAILABLE shows retry copy with the correlation reference", async () => {
    postMock.mockRejectedValue(
      apiError(
        "PROVIDER_UNAVAILABLE",
        "The policy assistant is unavailable right now. Please try again shortly.",
        503,
      ),
    );
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    const panel = await screen.findByTestId("rule-authoring-error");
    expect(
      within(panel).getByText("The policy assistant is unavailable"),
    ).toBeTruthy();
    expect(panel.textContent).toContain("try again shortly");
    expect(panel.textContent).toContain("corr-123");
  });

  it("renders a hostile reason as text, never as markup", async () => {
    postMock.mockRejectedValue(
      apiError("RULE_CRITIC_REJECTED", "<img src=x onerror=alert(1)> nope"),
    );
    const { container } = render(<RuleAuthoring policyId="policy-1" />, {
      wrapper: wrapper(),
    });
    generate();
    await screen.findByTestId("rule-authoring-error");
    expect(container.querySelector("img")).toBeNull();
    expect(
      screen.getByTestId("rule-authoring-error-message").textContent,
    ).toContain("<img src=x");
  });

  it("editing the intent clears the error panel", async () => {
    postMock.mockRejectedValue(apiError("RULE_INVALID", "bad"));
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    await screen.findByTestId("rule-authoring-error");
    fireEvent.change(screen.getByLabelText(/Describe the rule/i), {
      target: { value: "Taxi fares over $60 must be justified" },
    });
    await waitFor(() =>
      expect(screen.queryByTestId("rule-authoring-error")).toBeNull(),
    );
  });

  it("a non-API failure falls back to the generic message", async () => {
    postMock.mockRejectedValue(new Error("boom"));
    render(<RuleAuthoring policyId="policy-1" />, { wrapper: wrapper() });
    generate();
    const panel = await screen.findByTestId("rule-authoring-error");
    expect(within(panel).getByText("Could not generate a draft")).toBeTruthy();
  });
});
