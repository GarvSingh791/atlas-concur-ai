/**
 * Follow-up (grades): the approver-grade level in the approvals UI.
 *
 * - the queue badges an item that requires an SVP;
 * - the decision panel names the required grade, the clauses and the routed
 *   approver;
 * - a viewer below the grade sees the decision buttons DISABLED with the
 *   server's explanation; a qualifying viewer can decide;
 * - a 403 INSUFFICIENT_APPROVER_GRADE shows a specific toast;
 * - AIR-07 / LOD-05 / NR-01 flags render with their clause and type label.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

import { ApprovalQueue } from "@/components/approvals/approval-queue";
import { DecisionActions } from "@/components/approvals/decision-actions";
import { ExceptionsList } from "@/components/approvals/exceptions-list";
import { GradeRequirementPanel } from "@/components/approvals/grade-requirement";
import { requiredGradeLabel } from "@/lib/approvals/grade";
import { toDecisionView } from "@/lib/approvals/risk-summary";
import { ApiError } from "@/lib/types/api";
import type {
  ApprovalDetail,
  ApprovalListItem,
  GradeRequirement,
  RiskSummary,
} from "@/lib/types/approvals";

const { showToast, mutateAsync } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mutateAsync: vi.fn(),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/query/approval-hooks", () => ({
  useDecideApproval: () => ({ mutateAsync, isPending: false }),
}));

afterEach(cleanup);
beforeEach(() => {
  showToast.mockReset();
  mutateAsync.mockReset();
  mutateAsync.mockResolvedValue({});
});

const REQ: GradeRequirement = {
  required_grade: "SVP",
  required_title: "Senior Vice President",
  required_short: "SVP",
  clauses: ["AIR-07"],
  routed_to_approver_id: "u-svp",
  routed_to_name: "Sam SVP",
  routed_by: "manager_chain",
  rationale:
    "AIR-07 requires an approver of grade Senior Vice President (SVP) or above.",
  viewer_can_decide: false,
  message:
    "Only an approver of grade Senior Vice President (SVP) or above can decide this; your grade of record is Vice President. It is routed to Sam SVP.",
};

function detail(over: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u1",
    submitter_name: "Alex",
    current_status: "pending",
    is_auto_approved: false,
    risk_summary: null,
    risk_level: "medium",
    version: 4,
    created_at: "2026-11-14T00:00:00Z",
    updated_at: "2026-11-14T00:00:00Z",
    steps: [],
    expense_report: null,
    trip: null,
    ...over,
  };
}

function item(over: Partial<ApprovalListItem>): ApprovalListItem {
  return {
    id: "a1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "s1",
    submitter_id: "u1",
    submitter_name: "Alex Employee",
    subject_title: "Lounge pass",
    subject_amount: "20.00",
    currency: "USD",
    current_status: "pending",
    is_auto_approved: false,
    risk_level: "high",
    auto_approve_rationale: null,
    created_at: "2026-11-14T00:00:00Z",
    version: 1,
    ...over,
  };
}

describe("approver grade level", () => {
  it("labels the required grade", () => {
    expect(requiredGradeLabel("SVP")).toBe("Requires SVP");
    expect(requiredGradeLabel("CSUITE")).toBe("Requires C-suite");
    expect(requiredGradeLabel(null)).toBeNull();
  });

  it("badges a queue row that requires an SVP, and only that row", () => {
    render(
      <ApprovalQueue
        items={[
          item({ id: "svp", required_approver_grade: "SVP" }),
          item({ id: "plain", subject_title: "Taxi" }),
        ]}
        isLoading={false}
        isError={false}
        onRetry={() => {}}
        selectedId={null}
        viewedIds={new Set()}
        onSelect={() => {}}
      />,
    );
    const badges = screen.getAllByTestId("queue-grade-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("Requires SVP");
  });

  it("names the grade, the clause and the routed approver", () => {
    render(<GradeRequirementPanel requirement={REQ} />);
    expect(screen.getByTestId("grade-requirement-badge").textContent).toContain(
      "Requires SVP",
    );
    expect(screen.getByText(/Required by AIR-07/)).toBeTruthy();
    expect(screen.getByText(/Routed to Sam SVP/)).toBeTruthy();
  });

  it("disables the decision buttons with the explanation for a junior viewer", () => {
    render(
      <DecisionActions
        approval={detail({ grade_requirement: REQ })}
        canDecide
      />,
    );
    for (const name of [/Approve/, /Request changes/, /Reject/]) {
      expect(
        (screen.getByRole("button", { name }) as HTMLButtonElement).disabled,
      ).toBe(true);
    }
    expect(screen.getByTestId("grade-blocked-note").textContent).toContain(
      "your grade of record is Vice President",
    );
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("lets a qualifying viewer decide", async () => {
    render(
      <DecisionActions
        approval={detail({
          grade_requirement: { ...REQ, viewer_can_decide: true },
        })}
        canDecide
      />,
    );
    expect(screen.queryByTestId("grade-blocked-note")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
  });

  it("explains a server 403 INSUFFICIENT_APPROVER_GRADE", async () => {
    mutateAsync.mockRejectedValue(
      new ApiError(
        {
          code: "INSUFFICIENT_APPROVER_GRADE",
          message: "This approval requires an approver of grade SVP or above.",
          correlation_id: "c-1",
        },
        403,
      ),
    );
    render(<DecisionActions approval={detail()} canDecide />);
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(showToast).toHaveBeenCalledTimes(1));
    expect(showToast.mock.calls[0][0]).toMatchObject({
      title: "A more senior approver must decide this",
      description: "This approval requires an approver of grade SVP or above.",
    });
  });

  it("renders AIR-07 / LOD-05 / NR-01 flags with their clause and type", () => {
    const rs = {
      route: "escalate",
      subject_type: "expense_report",
      flags: [
        {
          entry_ref: "Airport",
          flag_type: "policy_violation",
          rule_type: "grade_entitlement",
          source_clause_ref: "AIR-07",
          reason:
            "Early-boarding / airport lounge fee: AIR-07 reimburses them only for SVPs and above.",
          requires_approver: "SVP",
          severity: "blocking",
        },
        {
          entry_ref: "Hilton",
          flag_type: "approval_required",
          rule_type: "grade_entitlement",
          source_clause_ref: "LOD-05",
          reason: "Hotel suite / room upgrade: no grade is recorded.",
          requires_approver: "SVP",
          severity: "advisory",
        },
        {
          entry_ref: "Priority Pass",
          flag_type: "policy_advisory",
          rule_type: "grade_entitlement",
          source_clause_ref: "NR-01",
          reason: "Airport lounge membership: in policy for an SVP.",
          severity: "advisory",
        },
      ],
    } as RiskSummary;
    const view = toDecisionView(rs, "expense_report");
    render(<ExceptionsList exceptions={view.exceptions} inPolicyNote={null} />);
    for (const clause of ["AIR-07", "LOD-05", "NR-01"]) {
      expect(screen.getAllByText(new RegExp(clause)).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText(/Policy violation/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Pre-approval required/).length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText(/Policy advisory/).length).toBeGreaterThan(0);
  });
});
