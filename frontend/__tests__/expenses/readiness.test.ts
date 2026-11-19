import { describe, it, expect } from "vitest";

import {
  normalizePreSubmitFlags,
  suggestedFix,
  readinessLabel,
} from "@/lib/expenses/readiness";

describe("normalizePreSubmitFlags", () => {
  it("returns 'ready' for null / empty flags", () => {
    expect(normalizePreSubmitFlags(null).level).toBe("ready");
    expect(normalizePreSubmitFlags(undefined).level).toBe("ready");
    expect(normalizePreSubmitFlags({}).level).toBe("ready");
    expect(normalizePreSubmitFlags({ flags: [] }).level).toBe("ready");
    expect(normalizePreSubmitFlags({ warnings: [] }).level).toBe("ready");
  });

  it("normalizes the agent shape ({flags:[...]}) — blocking → issue, advisory → review", () => {
    const raw = {
      flags: [
        {
          entry_ref: "Hilton",
          flag_type: "policy_violation",
          source_clause_ref: "Travel Policy §3.2",
          reason: "Nightly rate exceeds cap",
          severity: "blocking",
        },
        {
          entry_ref: "Uber",
          flag_type: "approval_required",
          source_clause_ref: "Travel Policy §4.5",
          reason: "Needs manager approval",
          severity: "advisory",
        },
      ],
      blocking_count: 1,
      advisory_count: 1,
    };
    const r = normalizePreSubmitFlags(raw);
    expect(r.level).toBe("issue"); // any issue dominates
    expect(r.issueCount).toBe(1);
    expect(r.reviewCount).toBe(1);
    expect(r.flags[0].message).toBe("Nightly rate exceeds cap");
    expect(r.flags[0].clauseRef).toBe("Travel Policy §3.2");
    expect(r.flags[0].entryRef).toBe("Hilton");
    expect(r.flags[0].flagType).toBe("policy_violation");
  });

  it("normalizes the seeded shape ({warnings:[...]}) — high → issue, message/clause_ref mapped", () => {
    const raw = {
      warnings: [
        {
          severity: "high",
          message: "Lodging rate exceeds policy limit",
          clause_ref: "§4.2",
        },
      ],
    };
    const r = normalizePreSubmitFlags(raw);
    expect(r.level).toBe("issue");
    expect(r.issueCount).toBe(1);
    expect(r.flags[0].message).toBe("Lodging rate exceeds policy limit");
    expect(r.flags[0].clauseRef).toBe("§4.2");
    // The seed shape carries no entry_ref / flag_type.
    expect(r.flags[0].entryRef).toBeNull();
    expect(r.flags[0].flagType).toBeNull();
  });

  it("is 'review' (amber) when only non-blocking severities are present", () => {
    const r = normalizePreSubmitFlags({
      warnings: [{ severity: "medium", message: "Soft flag" }],
    });
    expect(r.level).toBe("review");
    expect(r.reviewCount).toBe(1);
    expect(r.issueCount).toBe(0);
  });

  it("treats an unknown severity conservatively as 'review', never crashing", () => {
    const r = normalizePreSubmitFlags({
      flags: [{ reason: "weird", severity: "purple" }],
    });
    expect(r.level).toBe("review");
    expect(r.flags[0].message).toBe("weird");
  });
});

describe("suggestedFix", () => {
  it("maps known flag types to actionable hints", () => {
    expect(
      suggestedFix({
        severity: "issue",
        message: "x",
        clauseRef: null,
        entryRef: null,
        flagType: "missing_receipt",
      }),
    ).toMatch(/receipt/i);
  });

  it("falls back by severity when flag type is absent (seed shape)", () => {
    expect(
      suggestedFix({
        severity: "issue",
        message: "x",
        clauseRef: null,
        entryRef: null,
        flagType: null,
      }),
    ).toMatch(/resolve/i);
  });
});

describe("pending verification (Issue 6)", () => {
  it("maps pending_verification and suggests approver confirmation", () => {
    const summary = normalizePreSubmitFlags({
      flags: [
        {
          entry_ref: "New York Marriott Marquis",
          flag_type: "policy_violation",
          rule_type: "amount_cap",
          source_clause_ref: "HOTEL-CAP",
          reason:
            "Hotel amount 750.00 USD ... within the Tier 1 New York cap of $300/night if the city is confirmed.",
          severity: "advisory",
          justifiable: true,
          pending_verification: "nights_and_city",
        },
        {
          entry_ref: "Meals",
          flag_type: "policy_violation",
          rule_type: "per_diem",
          reason: "Meal amount 210.00 USD ...",
          severity: "advisory",
          justifiable: true,
          pending_verification: "days",
        },
      ],
    });
    expect(summary.level).toBe("review");
    expect(summary.flags[0].pendingVerification).toBe("nights_and_city");
    expect(suggestedFix(summary.flags[0])).toBe(
      "Within policy for the nights and city you stated — your approver will confirm before approval.",
    );
    expect(suggestedFix(summary.flags[1])).toMatch(/days you stated/);
  });

  it("an unknown pending value falls back to the policy_violation hint", () => {
    const summary = normalizePreSubmitFlags({
      flags: [
        {
          flag_type: "policy_violation",
          severity: "blocking",
          justifiable: true,
          pending_verification: "something_else",
        },
      ],
    });
    expect(suggestedFix(summary.flags[0])).toBe(
      "Adjust the amount to within policy, or add a justification.",
    );
  });
});

describe("policy_advisory (product gap 2)", () => {
  it("is review-level and says no action is needed", () => {
    const summary = normalizePreSubmitFlags({
      flags: [
        {
          flag_type: "policy_advisory",
          severity: "advisory",
          source_clause_ref: "LOD-02",
          reason: "Nightly rate 330.00 USD is 10.00% over the Tier 1 cap…",
        },
      ],
    });
    expect(summary.level).toBe("review");
    expect(summary.flags[0].clauseRef).toBe("LOD-02");
    expect(suggestedFix(summary.flags[0])).toBe(
      "Within the policy's automatic allowance — no action needed.",
    );
  });
});

describe("readinessLabel", () => {
  it("maps levels to vocabulary labels", () => {
    expect(readinessLabel("ready")).toBe("Ready");
    expect(readinessLabel("review")).toBe("Needs review");
    expect(readinessLabel("issue")).toBe("Issue found");
  });
});
