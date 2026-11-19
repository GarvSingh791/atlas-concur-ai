/**
 * Integration (policy tracks x approvals): the approver's exception list names
 * the policy flag types the new rules emit — a report-level EXP-01 / CARD-03
 * flag ("Report", "Corporate card") or a LOD-02 advisory must be told apart
 * from a policy violation on the same screen.
 */
import { describe, it, expect, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

import { ExceptionsList } from "@/components/approvals/exceptions-list";
import { FLAG_TYPE_LABEL, toDecisionView } from "@/lib/approvals/risk-summary";
import type { RiskSummary } from "@/lib/types/approvals";

afterEach(cleanup);

const RS: RiskSummary = {
  route: "escalate",
  subject_type: "expense_report",
  subject_in_policy: true,
  blocking_flag_count: 0,
  flags: [
    {
      entry_ref: "Report",
      flag_type: "late_submission",
      source_clause_ref: "EXP-01",
      reason: "Submitted 45 days after trip completion.",
      severity: "advisory",
    },
    {
      entry_ref: "Corporate card",
      flag_type: "unreconciled_card",
      source_clause_ref: "CARD-03",
      reason: "2 card transactions older than 30 days are not reconciled.",
      severity: "advisory",
    },
    {
      entry_ref: "Hilton",
      flag_type: "policy_advisory",
      source_clause_ref: "LOD-02",
      reason: "12.00% over the cap, auto-approved for a tool booking.",
      severity: "advisory",
    },
    {
      entry_ref: "Mystery",
      flag_type: "brand_new_type",
      reason: "r",
      severity: "advisory",
    },
  ],
} as RiskSummary;

describe("policy flag types in the approver's exception list", () => {
  it("labels every policy-track flag type", () => {
    for (const type of [
      "late_submission",
      "unreconciled_card",
      "policy_advisory",
      "currency_unverified",
      "missing_receipt",
      "approval_required",
      "not_evaluated",
      "policy_violation",
    ]) {
      expect(FLAG_TYPE_LABEL[type], type).toBeTruthy();
    }
  });

  it("maps the type label onto each exception (unknown types keep rendering)", () => {
    const view = toDecisionView(RS, "expense_report");
    expect(view.exceptions.map((e) => e.typeLabel)).toEqual([
      "Late submission",
      "Unreconciled card transactions",
      "Policy advisory",
      null,
    ]);
  });

  it("renders the type next to the entry", () => {
    const view = toDecisionView(RS, "expense_report");
    render(<ExceptionsList exceptions={view.exceptions} inPolicyNote={null} />);
    const types = screen
      .getAllByTestId("exception-type")
      .map((n) => n.textContent);
    expect(types).toEqual([
      "· Late submission",
      "· Unreconciled card transactions",
      "· Policy advisory",
    ]);
    expect(screen.getByText("Mystery")).toBeTruthy();
  });
});
