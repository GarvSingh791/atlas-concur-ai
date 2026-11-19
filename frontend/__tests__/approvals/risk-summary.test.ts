import { describe, it, expect } from "vitest";

import { toDecisionView } from "@/lib/approvals/risk-summary";
import type { RiskSummary } from "@/lib/types/approvals";

describe("toDecisionView", () => {
  it("returns an empty view for null risk_summary", () => {
    const v = toDecisionView(null, "expense_report");
    expect(v.aiAnalysis).toEqual({
      route: null,
      inPolicy: null,
      blockingCount: 0,
      requiresApprover: false,
      totalUsd: null,
      notEvaluated: false,
      reassessedAt: null,
      reassessed: false,
      rationale: [],
    });
    expect(v.exceptions).toEqual([]);
    expect(v.inPolicyNote).toBeNull();
  });

  it("a justified policy_violation is still a blocking exception and carries the justification", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "expense_report",
      subject_in_policy: false,
      blocking_flag_count: 1,
      flags: [
        {
          entry_ref: "Grand Hotel",
          flag_type: "policy_violation",
          source_clause_ref: "HOTEL-CAP",
          reason: "Over cap.",
          severity: "advisory",
          justifiable: true,
          justification: "  Sold-out conference week.  ",
        },
        {
          entry_ref: "Lunch",
          flag_type: "approval_required",
          reason: "Group meal.",
          severity: "advisory",
        },
      ],
    };
    const v = toDecisionView(rs, "expense_report");
    expect(v.exceptions[0]).toMatchObject({
      severity: "blocking",
      justification: "Sold-out conference week.",
    });
    expect(v.exceptions[1]).toMatchObject({
      severity: "advisory",
      justification: null,
    });
  });

  it("maps an escalated expense_report's flags to exceptions with reason + citation", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "expense_report",
      subject_in_policy: false,
      blocking_flag_count: 1,
      requires_approver: false,
      subject_total_usd: 1234.5,
      flags: [
        {
          entry_ref: "Hotel — 3 nights",
          source_clause_ref: "HOTEL-CAP",
          reason: "Over cap.",
          severity: "blocking",
        },
        {
          entry_ref: "Meals",
          source_clause_ref: "MEALS-PD",
          reason: "Over per-diem.",
          severity: "advisory",
        },
      ],
    };
    const v = toDecisionView(rs, "expense_report");
    expect(v.aiAnalysis.route).toBe("escalate");
    expect(v.aiAnalysis.inPolicy).toBe(false);
    expect(v.aiAnalysis.blockingCount).toBe(1);
    expect(v.aiAnalysis.totalUsd).toBe(1234.5);
    expect(v.exceptions).toHaveLength(2);
    expect(v.exceptions[0]).toMatchObject({
      label: "Hotel — 3 nights",
      reason: "Over cap.",
      clauseRef: "HOTEL-CAP",
      severity: "blocking",
    });
    expect(v.exceptions[1].clauseRef).toBe("MEALS-PD");
    expect(v.exceptions[1].severity).toBe("advisory");
    // unique React keys
    expect(new Set(v.exceptions.map((e) => e.ref)).size).toBe(2);
  });

  it("treats only out-of-policy segments as trip exceptions (no clause ref)", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "trip",
      subject_in_policy: false,
      blocking_flag_count: 0,
      requires_approver: false,
      subject_total_usd: 1410,
      flags: [
        {
          segment_id: "s1",
          segment_type: "flight",
          in_policy: true,
          policy_notes: null,
        },
        {
          segment_id: "s2",
          segment_type: "hotel",
          in_policy: false,
          policy_notes: "Nightly rate over cap.",
        },
      ],
    };
    const v = toDecisionView(rs, "trip");
    expect(v.exceptions).toHaveLength(1);
    expect(v.exceptions[0]).toMatchObject({
      reason: "Nightly rate over cap.",
      clauseRef: null,
      severity: "advisory",
    });
    expect(v.exceptions[0].label.toLowerCase()).toContain("hotel");
    expect(v.inPolicyNote).toBe("1 of 2 segments in policy");
  });

  it("shows an open-returned-subject flag on a trip without counting it as a segment", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "trip",
      subject_in_policy: true,
      blocking_flag_count: 1,
      requires_approver: false,
      subject_total_usd: 298,
      flags: [
        {
          segment_id: "s1",
          segment_type: "flight",
          in_policy: true,
          policy_notes: null,
        },
        {
          flag_type: "open_returned_report",
          severity: "blocking",
          entry_ref: "Returned trip",
          reason:
            'The submitter has a trip a reviewer returned that is not yet resolved ("JFK").',
        },
      ],
    };
    const v = toDecisionView(rs, "trip");
    expect(v.exceptions).toHaveLength(1);
    expect(v.exceptions[0]).toMatchObject({
      label: "Returned trip",
      severity: "blocking",
    });
    expect(v.exceptions[0].reason).toContain("returned");
    expect(v.inPolicyNote).toBe("1 of 1 segments in policy");
  });

  it("shows a possible split trip (UAT-2 Issue 4) as a blocking trip exception", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "trip",
      subject_in_policy: true,
      blocking_flag_count: 1,
      requires_approver: false,
      subject_total_usd: 654,
      flags: [
        {
          segment_id: "s1",
          segment_type: "hotel",
          in_policy: true,
          policy_notes: null,
        },
        {
          flag_type: "possible_split_trip",
          severity: "blocking",
          entry_ref: "Overlapping trip",
          reason:
            "This trip overlaps another trip by the same traveler to the same destination (ORD → JFK, 2026-11-17 – 2026-11-20, approved).",
        },
        {
          flag_type: "refiled_rejected_claim",
          severity: "blocking",
          entry_ref: "Returned trip",
          reason: "A trip to JFK that a reviewer returned overlaps this one.",
        },
      ],
    };
    const v = toDecisionView(rs, "trip");
    expect(v.exceptions.map((e) => [e.label, e.severity])).toEqual([
      ["Overlapping trip", "blocking"],
      ["Returned trip", "blocking"],
    ]);
    expect(v.exceptions[0].reason).toContain("overlaps another trip");
    // Whole-trip flags are not segments.
    expect(v.inPolicyNote).toBe("1 of 1 segments in policy");
  });

  it("shows a re-filed withdrawn return as a whole-trip blocking exception", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "trip",
      subject_in_policy: true,
      blocking_flag_count: 1,
      requires_approver: true,
      subject_total_usd: 400,
      flags: [
        {
          flag_type: "refiled_returned_claim",
          severity: "blocking",
          entry_ref: "Withdrawn returned trip",
          reason:
            "A trip to JFK that a reviewer sent back for changes overlaps this one.",
        },
      ],
    };
    const v = toDecisionView(rs, "trip");
    expect(v.exceptions.map((e) => [e.label, e.severity])).toEqual([
      ["Withdrawn returned trip", "blocking"],
    ]);
    // A whole-trip flag, never counted as a segment.
    expect(v.inPolicyNote).toBeNull();
  });

  it("has no exceptions for an auto-approved (in-policy) subject", () => {
    const rs: RiskSummary = {
      route: "auto_approve",
      subject_type: "expense_report",
      subject_in_policy: true,
      blocking_flag_count: 0,
      requires_approver: false,
      subject_total_usd: 85,
      flags: [],
    };
    const v = toDecisionView(rs, "expense_report");
    expect(v.exceptions).toEqual([]);
    expect(v.aiAnalysis.route).toBe("auto_approve");
    expect(v.aiAnalysis.inPolicy).toBe(true);
  });

  it("labels a possible re-filed claim (phase 10) as an advisory exception", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "expense_report",
      subject_in_policy: true,
      blocking_flag_count: 0,
      flags: [
        {
          flag_type: "possible_refile",
          severity: "advisory",
          entry_ref: "Cafe Alpha",
          reason: "It may be the same claim typed again.",
        },
      ],
    };
    const v = toDecisionView(rs, "expense_report");
    expect(v.exceptions).toHaveLength(1);
    expect(v.exceptions[0].severity).toBe("advisory");
    expect(v.exceptions[0].label).toBe("Cafe Alpha");
    expect(v.exceptions[0].typeLabel).toBe("Possible re-filed claim");
  });

  it("normalizes an unknown severity to 'info'", () => {
    const rs: RiskSummary = {
      route: "escalate",
      subject_type: "expense_report",
      flags: [{ entry_ref: "X", reason: "r", severity: "weird" }],
    };
    const v = toDecisionView(rs, "expense_report");
    expect(v.exceptions[0].severity).toBe("info");
  });

  it("does not throw on missing/partial fields", () => {
    const v = toDecisionView({ flags: [{}] }, "expense_report");
    expect(v.exceptions).toHaveLength(1);
    expect(v.exceptions[0].label).toBeTruthy();
  });
});
