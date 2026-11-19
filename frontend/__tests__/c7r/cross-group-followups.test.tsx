/**
 * C7R cross-group follow-ups (frontend side of backend changes made in the same
 * round):
 *   X1  the approval panel shows a failed resubmission re-evaluation as
 *       "Not evaluated", never "In policy: No" with no explanation, and marks a
 *       re-evaluated verdict "Re-evaluated after resubmission";
 *   X2  entering a withheld total that creates the deferred draft tells the user
 *       (toast + verdict notice) and refreshes the expense / analytics caches;
 *   X3  a low-confidence amount (now "enter the receipt total" server copy) is
 *       still withheld for the user to enter.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import * as React from "react";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));
import { api } from "@/lib/api/client";
import { toDecisionView } from "@/lib/approvals/risk-summary";
import { AiAnalysis } from "@/components/approvals/ai-analysis";
import {
  correctionToast,
  policyVerdictText,
} from "@/components/receipts/extraction-review";
import {
  receiptTouchedExpenses,
  useCorrectReceiptField,
} from "@/lib/query/receipt-hooks";
import { receiptAmountNeedsInput } from "@/lib/receipts/fields";
import type { Receipt } from "@/lib/types";
import type { RiskSummary } from "@/lib/types/approvals";

afterEach(cleanup);

/** The rendered value of the AI analysis "In policy" field. */
function inPolicyValue(): string | null | undefined {
  return screen.getByText("In policy").nextElementSibling?.textContent;
}

const FAILED_REASSESSMENT: RiskSummary = {
  route: "escalate",
  subject_type: "expense_report",
  subject_in_policy: null,
  is_policy_compliant: null,
  blocking_flag_count: 0,
  requires_approver: false,
  subject_total_usd: null,
  flags: [],
  policy_rationale: [
    "Risk re-assessment after resubmission failed; human review required.",
  ],
  evaluation_failed: true,
  reassessed_on_resubmission: true,
  resubmitted_at: "2026-11-09T10:15:00Z",
};

describe("X1 — resubmission re-evaluation on the approval panel", () => {
  it("a failed re-evaluation renders 'Not evaluated' with its reason", () => {
    const view = toDecisionView(FAILED_REASSESSMENT, "expense_report");
    expect(view.aiAnalysis.notEvaluated).toBe(true);
    expect(view.aiAnalysis.inPolicy).toBeNull();
    render(<AiAnalysis analysis={view.aiAnalysis} currency="USD" />);
    expect(
      screen.getByText("Not evaluated — human review required"),
    ).toBeTruthy();
    expect(inPolicyValue()).toBe("Not evaluated");
    expect(screen.getByTestId("ai-analysis-rationale").textContent).toMatch(
      /re-assessment after resubmission failed/,
    );
  });

  it("legacy fallback snapshot (in_policy false, compliance + total null) is also not evaluated", () => {
    const legacy: RiskSummary = {
      ...FAILED_REASSESSMENT,
      subject_in_policy: false,
      evaluation_failed: undefined,
    };
    expect(
      toDecisionView(legacy, "expense_report").aiAnalysis.notEvaluated,
    ).toBe(true);
  });

  it("a successful re-evaluation keeps its verdict and is marked re-evaluated", () => {
    const view = toDecisionView(
      {
        route: "escalate",
        subject_in_policy: false,
        is_policy_compliant: false,
        blocking_flag_count: 1,
        subject_total_usd: 98765,
        flags: [],
        reassessed_on_resubmission: true,
        resubmitted_at: "2026-11-09T10:15:00Z",
      },
      "expense_report",
    );
    expect(view.aiAnalysis.notEvaluated).toBe(false);
    expect(view.aiAnalysis.inPolicy).toBe(false);
    render(<AiAnalysis analysis={view.aiAnalysis} currency="USD" />);
    expect(screen.getByTestId("ai-analysis-reassessed").textContent).toMatch(
      /^Re-evaluated after resubmission on /,
    );
    expect(inPolicyValue()).toBe("No");
  });
});

function receipt(overrides: Partial<Receipt> = {}): Receipt {
  return {
    id: "r-1",
    organization_id: "o-1",
    uploaded_by: "u-1",
    expense_id: null,
    image_url: "k",
    ocr_status: "processed",
    processing_path: "agent_gap_fill",
    extracted_fields: { amount: 980, currency: "USD", vendor: "AA" },
    field_confidence: { amount: 0.6, currency: 0.99, vendor: 0.95 },
    field_provenance: {
      amount: "ai_inferred",
      currency: "deterministic",
      vendor: "deterministic",
    },
    created_at: "2026-11-09T10:00:00Z",
    needs_review: true,
    review_reasons: [
      "Amount was not read with confidence — enter the receipt total",
    ],
    ...overrides,
  };
}

describe("X2 — the deferred draft created by entering a withheld total", () => {
  const drafted = receipt({
    expense_id: "e-1",
    linked_expense_sync: "drafted",
    field_provenance: { amount: "user_edited" },
    needs_review: false,
    review_reasons: [],
    policy_verdict: {
      in_policy: false,
      blocking_count: 1,
      advisory_count: 0,
      flag_types: ["missing_receipt"],
    },
  });

  it("the toast names the draft and the server's verdict", () => {
    const toast = correctionToast(drafted);
    expect(toast.title).toBe("Expense drafted");
    expect(toast.description).toMatch(/Receipt Inbox/);
    expect(toast.description).toMatch(/1 blocking policy issue/);
    expect(toast.description).toMatch(/not receipt evidence/);
    expect(correctionToast(receipt()).title).toBe("Field updated");
    expect(
      correctionToast(
        receipt({ linked_expense_sync: "skipped_report_not_editable" }),
      ).variant,
    ).toBe("info");
  });

  it("verdict text never claims in-policy for a blocked entry", () => {
    expect(
      policyVerdictText({
        in_policy: true,
        blocking_count: 0,
        advisory_count: 0,
        flag_types: [],
      }),
    ).toBe("In policy.");
    expect(
      policyVerdictText({
        in_policy: true,
        blocking_count: 2,
        advisory_count: 0,
        flag_types: ["policy_violation"],
      }),
    ).toMatch(/^2 blocking policy issues\./);
  });

  it("a correction that drafted or updated an entry refreshes expense caches", async () => {
    expect(receiptTouchedExpenses(drafted)).toBe(true);
    expect(
      receiptTouchedExpenses(receipt({ linked_expense_sync: "updated" })),
    ).toBe(true);
    // UAT2: an upload attached to an entry re-ran that report's check.
    expect(
      receiptTouchedExpenses(receipt({ linked_expense_sync: "attached" })),
    ).toBe(true);
    expect(
      receiptTouchedExpenses(
        receipt({ linked_expense_sync: "not_applicable" }),
      ),
    ).toBe(false);

    const qc = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const spy = vi.spyOn(qc, "invalidateQueries");
    (api.patch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      drafted,
    );
    let mutate: ReturnType<typeof useCorrectReceiptField> | null = null;
    function Probe() {
      mutate = useCorrectReceiptField();
      return null;
    }
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    );
    await mutate!.mutateAsync({ receiptId: "r-1", field: "amount", value: 60 });
    await waitFor(() => {
      const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
      expect(keys).toContain(JSON.stringify(["expense-reports"]));
      expect(keys).toContain(JSON.stringify(["expense-report"]));
      expect(keys).toContain(JSON.stringify(["analytics"]));
    });
  });
});

describe("X3 — low-confidence amount", () => {
  it("is withheld for the user to enter (matches the server's 'enter' copy)", () => {
    expect(receiptAmountNeedsInput(receipt())).toBe(true);
  });
});
